"""结构化硬过滤与过滤策略降级。

覆盖三件事：硬过滤键怎么构造与怎么比、``REPLACE`` / ``MERGE`` 两档策略在
硬过滤命中 0 条时各自怎么处置、以及「命中 0 条必须显式挂起，绝不静默丢弃」
这条硬规矩有没有被守住。
"""

from __future__ import annotations

import pytest

from patho_supplement.align.filtering import (
    REASON_FUZZY_DEGRADED,
    REASON_INCOMPLETE_KEY,
    REASON_NO_HIT,
    FilterCandidate,
    FilterOutcome,
    FilterPolicy,
    apply_filter,
    build_filter_key,
    fuzzy_filter,
    hard_filter,
)
from patho_supplement.align.scorer import LexicalScorer

#: 一份报告里常见的几个部位（脱敏类目化占位，不含真实住院号与患者化名）。
_BLOCKS = (
    FilterCandidate(region_seq=1, block_no="B1", item_code="IHC-CD20", region_desc="子宫体内侧"),
    FilterCandidate(region_seq=2, block_no="B2", item_code="IHC-CD20", region_desc="宫颈部"),
    FilterCandidate(region_seq=3, block_no="B3", item_code="IHC-CD20", region_desc="乳腺组织"),
)


# ---------------------------------------------------------------- 过滤键构造


def test_filter_key_normalizes_block_no_and_item_code() -> None:
    key = build_filter_key(2, "b-03", "ihc-cd20")
    assert key.normalized_block_no == "B3"
    assert key.normalized_item_code == "IHC-CD20"
    assert key.text == "2|B3|IHC-CD20"


def test_filter_key_usable_requires_item_code_and_one_locator() -> None:
    # 三段齐全 → 可用
    assert build_filter_key(2, "B3", "IHC-CD20").usable
    # 只有部位序号 + 项目码 → 可用
    assert build_filter_key(2, None, "IHC-CD20").usable
    # 只有蜡块号 + 项目码 → 可用
    assert build_filter_key(None, "B3", "IHC-CD20").usable
    # 缺项目码 → 不可用（不知道在找哪一项检查）
    assert not build_filter_key(2, "B3", "").usable
    # 既无部位序号又无蜡块号 → 不可用（没有可比字段，等于万能钥匙）
    assert not build_filter_key(None, None, "IHC-CD20").usable


def test_filter_key_text_marks_missing_fields() -> None:
    assert build_filter_key(None, None, "IHC-CD20").text == "-|-|IHC-CD20"


# ---------------------------------------------------------------- 硬过滤


def test_hard_filter_hits_on_all_three_fields() -> None:
    key = build_filter_key(2, "B2", "IHC-CD20")
    hits = hard_filter(key, _BLOCKS)
    assert len(hits) == 1
    assert hits[0].candidate.block_no == "B2"
    assert hits[0].outcome is FilterOutcome.HARD
    assert hits[0].score == 1.0
    assert hits[0].pending is False


def test_hard_filter_accepts_different_writings_of_same_block() -> None:
    """基线写 B-3、回报写 b03 —— 归一化后是同一块，必须命中。"""
    key = build_filter_key(3, "B-3", "IHC-CD20")
    hits = hard_filter(key, _BLOCKS)
    assert len(hits) == 1
    assert hits[0].candidate.block_no == "B3"


def test_hard_filter_misses_on_conflicting_block_no() -> None:
    key = build_filter_key(3, "B9", "IHC-CD20")
    assert hard_filter(key, _BLOCKS) == ()


def test_hard_filter_misses_when_candidate_lacks_block_no() -> None:
    """候选没带蜡块号 → 不能确认就是这块，不算命中。"""
    candidates = (
        FilterCandidate(region_seq=2, block_no=None, item_code="IHC-CD20", region_desc="宫颈部"),
    )
    assert hard_filter(build_filter_key(2, "B2", "IHC-CD20"), candidates) == ()


def test_hard_filter_misses_on_conflicting_region_seq() -> None:
    key = build_filter_key(9, "B2", "IHC-CD20")
    assert hard_filter(key, _BLOCKS) == ()


def test_hard_filter_misses_on_different_item_code() -> None:
    key = build_filter_key(2, "B2", "IHC-CK20")
    assert hard_filter(key, _BLOCKS) == ()


def test_hard_filter_skips_unusable_key_instead_of_matching_everything() -> None:
    """键不可用 → 0 命中，绝不退化成「全都算命中」。"""
    assert hard_filter(build_filter_key(None, None, "IHC-CD20"), _BLOCKS) == ()
    assert hard_filter(build_filter_key(2, "B2", ""), _BLOCKS) == ()


def test_hard_filter_ignores_fields_absent_from_key() -> None:
    """键里没提部位序号 → 不比部位序号。"""
    key = build_filter_key(None, "B3", "IHC-CD20")
    hits = hard_filter(key, _BLOCKS)
    assert len(hits) == 1
    assert hits[0].candidate.region_seq == 3


# ---------------------------------------------------------------- REPLACE 策略


def test_replace_policy_returns_empty_when_hard_filter_misses() -> None:
    key = build_filter_key(9, "B9", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.REPLACE)
    assert result.count == 0
    assert result.hard_hits == 0
    assert result.fuzzy_hits == 0
    assert result.degraded is False
    assert result.pending is True
    assert result.pending_reason == REASON_NO_HIT


def test_replace_policy_excludes_fuzzy_candidates() -> None:
    """REPLACE 下模糊候选不进入结果集。"""
    key = build_filter_key(9, "B9", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy="REPLACE")
    assert result.count == 0
    assert result.fuzzy_items == ()
    assert result.fuzzy_hits == 0


def test_replace_policy_returns_hard_hits_when_matched() -> None:
    key = build_filter_key(2, "B2", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.REPLACE)
    assert result.count == 1
    assert result.hard_hits == 1
    assert result.pending is False
    assert result.degraded is False


# ---------------------------------------------------------------- MERGE 策略


def test_merge_policy_degrades_to_fuzzy_when_hard_filter_misses() -> None:
    """硬过滤命中 0 条 → 降级模糊：输出条数不为 0，且每条都带 pending。"""
    key = build_filter_key(9, "B9", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert result.count > 0
    assert result.hard_hits == 0
    assert result.fuzzy_hits == result.count
    assert result.degraded is True
    assert result.pending is True
    assert result.pending_reason == REASON_FUZZY_DEGRADED
    assert all(item.pending for item in result.items)
    assert all(item.pending_reason == REASON_FUZZY_DEGRADED for item in result.items)
    assert all(item.outcome is FilterOutcome.FUZZY for item in result.items)


def test_merge_policy_does_not_mix_fuzzy_candidates_into_hard_hits() -> None:
    """硬过滤命中时，模糊候选不混入（否则同一条结果在两路各出现一次）。"""
    key = build_filter_key(2, "B2", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert result.count == 1
    assert result.hard_hits == 1
    assert result.fuzzy_hits == 0
    assert result.degraded is False


def test_merge_policy_keeps_item_pending_when_no_candidate_is_compatible() -> None:
    """连项目码都不兼容 → 结果集为空，但本条仍显式挂起，不静默丢弃。"""
    key = build_filter_key(9, "B9", "IHC-CK20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert result.count == 0
    assert result.pending is True
    assert result.pending_reason == REASON_NO_HIT
    assert result.degraded is True


def test_merge_policy_marks_incomplete_key_with_its_own_reason() -> None:
    key = build_filter_key(None, None, "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert result.pending is True
    assert result.pending_reason == REASON_INCOMPLETE_KEY


def test_replace_policy_marks_incomplete_key_with_its_own_reason() -> None:
    key = build_filter_key(None, None, "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.REPLACE)
    assert result.count == 0
    assert result.pending is True
    assert result.pending_reason == REASON_INCOMPLETE_KEY


# ---------------------------------------------------------------- 降级语义


def test_fuzzy_degradation_keeps_item_code_constraint() -> None:
    """降级只放宽位置，不放宽项目：别的检查项不混进来。"""
    candidates = (
        FilterCandidate(region_seq=9, block_no="B9", item_code="IHC-CK20", region_desc="宫颈部"),
    )
    key = build_filter_key(9, "B9", "IHC-CD20")
    result = apply_filter(key, candidates, policy=FilterPolicy.MERGE)
    assert result.count == 0
    assert result.pending is True


def test_fuzzy_degradation_sorts_by_region_similarity() -> None:
    """模糊降级按部位描述的词法相似度排序，分高的在前。"""
    key = build_filter_key(9, "B9", "IHC-CD20", region_desc="宫颈部")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    scores = [item.score for item in result.items]
    assert scores == sorted(scores, reverse=True)
    assert result.items[0].candidate.region_desc == "宫颈部"


def test_fuzzy_degradation_respects_limit() -> None:
    key = build_filter_key(9, "B9", "IHC-CD20", region_desc="宫颈部")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE, limit=1)
    assert result.count == 1


def test_fuzzy_degradation_accepts_injected_scorer() -> None:
    scorer = LexicalScorer(["子宫体内侧", "宫颈部", "乳腺组织"])
    key = build_filter_key(9, "B9", "IHC-CD20", region_desc="宫颈部")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE, scorer=scorer)
    assert result.count == 3
    assert result.items[0].candidate.region_desc == "宫颈部"


def test_fuzzy_filter_keeps_candidate_and_score_paired() -> None:
    """同分的候选不能张冠李戴 —— 分与候选必须一一对应。"""
    candidates = (
        FilterCandidate(region_seq=1, block_no="B1", item_code="IHC-CD20", region_desc="宫颈部"),
        FilterCandidate(region_seq=2, block_no="B2", item_code="IHC-CD20", region_desc="宫颈部"),
    )
    key = build_filter_key(9, "B9", "IHC-CD20", region_desc="宫颈部")
    hits = fuzzy_filter(key, candidates)
    assert len(hits) == 2
    assert [hit.candidate.block_no for hit in hits] == ["B1", "B2"]


# ---------------------------------------------------------------- 其他


def test_unknown_policy_is_rejected() -> None:
    key = build_filter_key(2, "B2", "IHC-CD20")
    with pytest.raises(ValueError):
        apply_filter(key, _BLOCKS, policy="SOMETHING_ELSE")


def test_policy_accepts_string_and_enum() -> None:
    key = build_filter_key(2, "B2", "IHC-CD20")
    by_str = apply_filter(key, _BLOCKS, policy="MERGE")
    by_enum = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert by_str.as_dict() == by_enum.as_dict()


def test_filter_result_summary_is_serializable() -> None:
    key = build_filter_key(2, "B2", "IHC-CD20")
    result = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    summary = result.as_dict()
    assert summary["policy"] == "MERGE"
    assert summary["count"] == 1
    assert summary["hard_hits"] == 1
    assert summary["pending"] is False
    assert summary["key"]["text"] == "2|B2|IHC-CD20"


def test_same_input_gives_same_result() -> None:
    """确定性：同一输入两次结果逐项一致（可复现是本产品的承诺之一）。"""
    key = build_filter_key(9, "B9", "IHC-CD20", region_desc="宫颈部")
    first = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    second = apply_filter(key, _BLOCKS, policy=FilterPolicy.MERGE)
    assert [item.to_dict() for item in first.items] == [
        item.to_dict() for item in second.items
    ]


def test_candidate_payload_is_carried_through() -> None:
    """过滤层只搬运不解析：原对象原样传下去。"""
    payload = object()
    candidates = (
        FilterCandidate(region_seq=2, block_no="B2", item_code="IHC-CD20", payload=payload),
    )
    key = build_filter_key(2, "B2", "IHC-CD20")
    result = apply_filter(key, candidates, policy=FilterPolicy.MERGE)
    assert result.items[0].payload is payload


def test_empty_candidate_pool_still_yields_pending() -> None:
    """候选集为空 → 本条仍显式挂起，不静默丢弃。"""
    key = build_filter_key(2, "B2", "IHC-CD20")
    for policy in (FilterPolicy.REPLACE, FilterPolicy.MERGE):
        result = apply_filter(key, (), policy=policy)
        assert result.count == 0
        assert result.pending is True
        assert result.pending_reason == REASON_NO_HIT
