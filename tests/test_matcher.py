"""四级匹配策略（L1 蜡块号精确 / L2 部位描述精确 / L3 词法模糊 / L4 挂起）。

这些用例锁住的是本产品最要紧的一条承诺：**错绑定数 = 0**。
凡是不唯一、不确定、互相矛盾的情形，一律挂起等人，不许猜。
"""

from __future__ import annotations

import pytest

from patho_supplement.align.matcher import (
    DEFAULT_BM25_THRESHOLD,
    DEFAULT_LEAD_MARGIN_THRESHOLD,
    MATCH_LEVEL_LABELS,
    MatchConfigError,
    MatchState,
    MatchThresholds,
    align_entries,
    align_entry,
    summarize_match_levels,
)
from patho_supplement.config import Config, ConfigError
from patho_supplement.models import Block, MatchLevel, ReportBaseline, ResultEntry

ACCESSION = "ACC-EXAMPLE-0001"


def _entry(**kwargs) -> ResultEntry:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-ER",
        value="阴性",
        region_seq=1,
        block_no=None,
        region_desc="子宫体",
    )
    base.update(kwargs)
    return ResultEntry(**base)


def _block(**kwargs) -> Block:
    base = dict(
        region_seq=1,
        region_desc="子宫体",
        block_no="B1",
        order_no="ORD-EXAMPLE-0001",
        ordered_items=("IHC-ER",),
    )
    base.update(kwargs)
    return Block(**base)


def _baseline(*blocks: Block) -> ReportBaseline:
    return ReportBaseline(
        accession_no=ACCESSION,
        blocks=blocks or (_block(),),
        base_version="base-v1",
    )


# ---------------------------------------------------------------- L1 蜡块号精确


def test_l1_block_no_exact_hit_matches_with_high_confidence():
    """基线写 B-1、回报写 b01 —— 同一块，L1 命中，置信「高」。"""
    outcome = align_entry(_entry(block_no="b01"), _baseline())
    assert outcome.state == MatchState.MATCHED
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L1_BLOCK_EXACT
    assert outcome.match_level is MatchLevel.L1_BLOCK_EXACT
    assert outcome.candidate.score == 1.0
    assert outcome.candidate.confidence == "高"
    assert outcome.candidate.block.block_no == "B1"


def test_l1_uses_the_resolved_block_as_recall_key():
    outcome = align_entry(_entry(block_no="b01"), _baseline())
    assert outcome.candidate is not None
    assert outcome.candidate.recall_key == "1|B1|IHC-ER"


def test_l1_does_not_bind_when_item_was_not_ordered_on_that_block():
    """块对上了但这个项目没在它上面申请 → 不绑，挂起「待查项未登记」。"""
    outcome = align_entry(
        _entry(block_no="B1", item_code="IHC-KI67"),
        _baseline(_block(ordered_items=("IHC-ER",))),
    )
    assert outcome.state == MatchState.ITEM_NOT_ORDERED
    assert outcome.matched is False
    assert outcome.missing_fields == ("item_code",)
    assert outcome.candidates[0].block_no == "B1"


def test_l1_does_not_rescue_an_unknown_block_no_by_region_desc():
    """蜡块号给了但基线里没有 → 对不上；不拿部位描述去救一个可能写错的蜡块号。"""
    outcome = align_entry(_entry(block_no="B9"), _baseline())
    assert outcome.state == MatchState.NO_BLOCK
    assert outcome.matched is False
    assert outcome.missing_fields == ("region_seq", "block_no")


def test_l1_with_two_blocks_sharing_the_block_no_is_ambiguous():
    """同一个蜡块号出现在两块上 → 同分歧义，不猜。"""
    outcome = align_entry(
        _entry(block_no="B1", region_seq=None),
        _baseline(
            _block(region_seq=1, block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B1"),
        ),
    )
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.matched is False
    assert len(outcome.candidates) == 2
    assert outcome.pending_reason == "同分多候选"


def test_l1_region_desc_conflicting_with_block_is_not_bound():
    """回报写「宫颈」、基线那块登记的是「子宫体」→ 两个字段互相矛盾，挂起。"""
    outcome = align_entry(
        _entry(block_no="B1", region_desc="宫颈"),
        _baseline(_block(region_desc="子宫体", block_no="B1")),
    )
    assert outcome.state == MatchState.CONFLICT
    assert outcome.matched is False
    assert outcome.pending_reason == "部位与蜡块号冲突"
    assert "region_desc" in outcome.missing_fields


def test_l1_region_desc_alias_of_the_same_region_is_not_a_conflict():
    """「宫体」与「子宫体」是同一部位的两种写法 → 不算矛盾，照常命中。"""
    outcome = align_entry(
        _entry(block_no="B1", region_desc="宫体"),
        _baseline(_block(region_desc="子宫体", block_no="B1")),
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L1_BLOCK_EXACT


def test_l1_region_seq_conflict_does_not_bind():
    """部位序号对不上 → 不绑（蜡块号一样也不能越序）。"""
    outcome = align_entry(
        _entry(block_no="B1", region_seq=2),
        _baseline(_block(region_seq=1, block_no="B1")),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.NO_BLOCK


# ---------------------------------------------------------------- L2 部位描述精确


def test_l2_block_no_missing_but_region_desc_unique_match():
    """缺蜡块号，但部位描述唯一命中 → L2 成立，置信「中」。"""
    outcome = align_entry(_entry(block_no=None), _baseline())
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.confidence == "中"
    assert outcome.candidate.block.block_no == "B1"


def test_l2_pure_punctuation_block_no_counts_as_missing():
    """蜡块号写成「---」（纯符号）等于没填 → 走部位描述层。"""
    outcome = align_entry(_entry(block_no="---"), _baseline())
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED


def test_l2_ignores_blocks_that_never_ordered_the_item():
    """结果只可能落在申请过它的块上；没申请过就不作候选。"""
    outcome = align_entry(
        _entry(block_no=None, item_code="IHC-KI67"),
        _baseline(_block(ordered_items=("IHC-ER",))),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.missing_fields == ("block_no",)
    assert len(outcome.candidates) == 1


def test_l2_with_two_blocks_same_region_desc_is_ambiguous():
    """两个块的部位描述一样 → 无法唯一确定，挂起。"""
    outcome = align_entry(
        _entry(block_no=None, region_seq=None, region_desc="肿瘤"),
        _baseline(
            _block(region_seq=1, region_desc="肿瘤", block_no="B1"),
            _block(region_seq=2, region_desc="肿瘤", block_no="B2"),
        ),
    )
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.matched is False
    assert len(outcome.candidates) == 2


def test_l2_region_seq_disambiguates_two_blocks_with_same_desc():
    """两个块部位描述相同，但回报给了部位序号 → 序号把歧义消掉，不挂起。"""
    outcome = align_entry(
        _entry(block_no=None, region_seq=2, region_desc="肿瘤"),
        _baseline(
            _block(region_seq=1, region_desc="肿瘤", block_no="B1"),
            _block(region_seq=2, region_desc="肿瘤", block_no="B2"),
        ),
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.block.block_no == "B2"


def test_l2_region_seq_conflict_is_not_bound():
    """部位序号指向另一块 → 部位描述再像也不绑。"""
    outcome = align_entry(
        _entry(block_no=None, region_seq=2),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
    )
    assert outcome.matched is False


# ---------------------------------------------------------------- L3 词法模糊


def test_l2_region_desc_alias_is_deterministic_equivalence():
    """「宫体」是「子宫体」的别名：病理科自己维护的等价关系 → L2，不是 L3。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="宫体"),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.block.block_no == "B1"


def test_l3_free_text_region_desc_matches_with_low_confidence():
    """自由文本部位描述（「子宫体内膜」）词法命中 → L3，置信「低」。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="子宫体内膜"),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L3_REGION_FUZZY
    assert outcome.candidate.confidence == "低"
    assert outcome.candidate.score >= DEFAULT_BM25_THRESHOLD
    assert outcome.candidate.block.block_no == "B1"
    assert outcome.candidate.matched_terms


def test_l3_tied_candidates_never_fall_into_l3():
    """两个部位词法同分 → 不得落到 L3，一律挂起（并列处置见后续任务）。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="组织", region_seq=None),
        _baseline(
            _block(region_seq=1, region_desc="黏膜组织", block_no="B1"),
            _block(region_seq=2, region_desc="浆膜组织", block_no="B2"),
        ),
        thresholds=MatchThresholds(bm25_threshold=0.05, lead_margin_threshold=0.05),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.match_level is MatchLevel.L4_PENDING
    assert len(outcome.candidates) == 2
    assert outcome.pending_reason == "同分多候选"


def test_l3_lead_margin_too_small_is_ambiguous():
    """首名虽然领先，但幅度不够「唯一」 → 挂起，不硬绑。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="子宫体内膜", region_seq=None),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
        thresholds=MatchThresholds(bm25_threshold=0.32, lead_margin_threshold=0.9),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.AMBIGUOUS
    assert len(outcome.candidates) == 2


def test_l3_below_threshold_falls_back_to_pending():
    """词法分没到阈值 → 不落 L3，挂起等人。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="乳腺"),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.pending_reason == "词法匹配未达阈值"


# ---------------------------------------------------------------- L4 挂起


def test_l4_missing_block_no_and_missing_region_desc():
    """蜡块号与部位描述都没填 → L4 挂起，两个字段都要补。"""
    outcome = align_entry(_entry(block_no=None, region_desc=None), _baseline())
    assert outcome.matched is False
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.match_level is MatchLevel.L4_PENDING
    assert outcome.missing_fields == ("block_no", "region_desc")
    assert len(outcome.candidates) == 1


def test_l4_without_baseline():
    outcome = align_entry(_entry(), None)
    assert outcome.state == MatchState.NO_BASELINE
    assert outcome.missing_fields == ("baseline",)
    assert outcome.match_level is MatchLevel.L4_PENDING


def test_l4_never_returns_a_candidate():
    """挂起时不得给出候选块（否则上层可能误当成「已对上」）。"""
    outcome = align_entry(_entry(block_no=None, region_desc=None), _baseline())
    assert outcome.candidate is None
    assert outcome.matched is False


# ---------------------------------------------------------------- 阈值与配置面


def test_thresholds_default_values():
    thresholds = MatchThresholds()
    assert thresholds.bm25_threshold == DEFAULT_BM25_THRESHOLD
    assert thresholds.lead_margin_threshold == DEFAULT_LEAD_MARGIN_THRESHOLD


def test_thresholds_read_from_config_surface():
    """阈值从配置面读取（环境变量 BM25_THRESHOLD / LEAD_MARGIN_THRESHOLD）。"""
    config = Config.from_env(
        {"BM25_THRESHOLD": "0.51", "LEAD_MARGIN_THRESHOLD": "0.11"}
    )
    thresholds = MatchThresholds.from_config(config)
    assert thresholds.bm25_threshold == 0.51
    assert thresholds.lead_margin_threshold == 0.11


def test_thresholds_missing_in_config_fall_back_to_defaults():
    config = Config.from_env({})
    thresholds = MatchThresholds.from_config(config)
    assert thresholds.bm25_threshold == DEFAULT_BM25_THRESHOLD
    assert thresholds.lead_margin_threshold == DEFAULT_LEAD_MARGIN_THRESHOLD


@pytest.mark.parametrize(
    ("kwargs", "hint"),
    [
        ({"bm25_threshold": 1.5}, "BM25 阈值"),
        ({"bm25_threshold": -0.1}, "BM25 阈值"),
        ({"lead_margin_threshold": 2.0}, "领先幅度阈值"),
        ({"lead_margin_threshold": -1.0}, "领先幅度阈值"),
    ],
)
def test_out_of_range_thresholds_are_rejected_with_readable_reason(kwargs, hint):
    """阈值越界 → 报错并说明是哪一项、合法范围是多少。"""
    with pytest.raises(MatchConfigError) as excinfo:
        MatchThresholds(**kwargs)
    message = str(excinfo.value)
    assert hint in message
    assert "0" in message and "1" in message


def test_non_numeric_threshold_is_rejected():
    with pytest.raises(MatchConfigError):
        MatchThresholds(bm25_threshold="高")  # type: ignore[arg-type]


def test_config_surface_rejects_out_of_range_threshold():
    """环境变量越界同样拒绝启动，不静默改用默认值。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"BM25_THRESHOLD": "1.5"})
    assert "BM25_THRESHOLD" in str(excinfo.value)


def test_raising_bm25_threshold_reduces_l3_hits():
    """阈值调高 → L3 命中减少（这是「阈值可配」的实际效果，不是摆设）。"""
    baseline = _baseline(
        _block(region_seq=1, region_desc="子宫体", block_no="B1"),
        _block(region_seq=2, region_desc="宫颈", block_no="B2"),
    )
    entry = _entry(block_no=None, region_desc="子宫体内膜")
    loose = align_entry(entry, baseline)
    tight = align_entry(
        entry, baseline, thresholds=MatchThresholds(bm25_threshold=0.99)
    )
    assert loose.matched is True
    assert tight.matched is False
    assert tight.state == MatchState.MISSING_BLOCK_NO


# ---------------------------------------------------------------- 稳定性与批量


def test_same_input_gives_same_result():
    """同输入两次判定结果逐项一致（可复现，评审能复算）。"""
    baseline = _baseline(
        _block(region_seq=1, region_desc="子宫体", block_no="B1"),
        _block(region_seq=2, region_desc="宫颈", block_no="B2"),
    )
    entry = _entry(block_no=None, region_desc="子宫体内膜")
    first = align_entry(entry, baseline)
    second = align_entry(entry, baseline)
    assert first.as_dict() == second.as_dict()
    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.score == second.candidate.score


def test_candidate_order_does_not_change_the_outcome():
    """候选块传入顺序不同 → 结果相同（不因调度顺序改变报告内容）。"""
    blocks = [
        _block(region_seq=1, region_desc="子宫体", block_no="B1"),
        _block(region_seq=2, region_desc="宫颈", block_no="B2"),
    ]
    entry = _entry(block_no=None, region_desc="子宫体内膜", region_seq=None)
    forward = align_entry(entry, _baseline(*blocks))
    backward = align_entry(entry, _baseline(*reversed(blocks)))
    assert forward.state == backward.state
    assert forward.candidate is not None and backward.candidate is not None
    assert forward.candidate.block.block_no == backward.candidate.block.block_no
    assert forward.candidate.score == backward.candidate.score


def test_align_entries_splits_matched_and_unmatched_without_loss():
    """批量对齐：已对上与未对上加起来一条不少。"""
    baseline = _baseline()
    entries = [
        _entry(block_no="B1"),
        _entry(block_no=None, region_desc=None),
    ]
    matched, unmatched = align_entries(entries, {ACCESSION: baseline})
    assert len(matched) + len(unmatched) == len(entries)
    assert matched[0][0].item_code == "IHC-ER"
    assert unmatched[0][1].state == MatchState.MISSING_BLOCK_NO


def test_align_entries_reports_entries_without_baseline():
    matched, unmatched = align_entries([_entry(accession_no="ACC-NOT-EXIST")], {})
    assert not matched
    assert unmatched[0][1].state == MatchState.NO_BASELINE


# ------------------------------------------------- L1 收窄：按「申请过该项目的块」


def test_l1_duplicate_block_no_narrows_to_the_block_that_ordered_the_item():
    """同一个蜡块号登记在两块上（登记笔误），但这个项目只在其中一块申请过。

    「这个项目只在其中一块上申请过」是硬事实 —— 据此收窄候选集得到唯一命中，
    而不是把这条结果挂起等人。收窄只会少绑，不会错绑。
    """
    outcome = align_entry(
        _entry(block_no="B1", item_code="IHC-KI67", region_seq=None, region_desc=None),
        _baseline(
            _block(
                region_seq=1,
                region_desc="子宫体",
                block_no="B1",
                ordered_items=("IHC-ER",),
            ),
            _block(
                region_seq=2,
                region_desc="宫颈",
                block_no="B1",
                ordered_items=("IHC-KI67",),
            ),
        ),
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L1_BLOCK_EXACT
    assert outcome.candidate.block.region_seq == 2
    assert outcome.candidate.block.region_desc == "宫颈"


def test_l1_duplicate_block_no_stays_ambiguous_when_both_blocks_ordered_the_item():
    """两块都申请过这个项目 → 收窄后仍是多候选，不猜。"""
    outcome = align_entry(
        _entry(block_no="B1", region_seq=None, region_desc=None),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B1"),
        ),
    )
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.matched is False
    assert len(outcome.candidates) == 2


def test_l1_duplicate_block_no_with_item_nowhere_ordered_stays_pending():
    """两块共用蜡块号、且都没申请过这个项目 → 挂起，不硬绑。"""
    outcome = align_entry(
        _entry(block_no="B1", item_code="IHC-KI67", region_seq=None, region_desc=None),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1", ordered_items=("IHC-ER",)),
            _block(region_seq=2, region_desc="宫颈", block_no="B1", ordered_items=("IHC-ER",)),
        ),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.pending_reason == "同分多候选"


# ------------------------------------------------- 四级判定分布


def test_summarize_match_levels_counts_all_four_levels():
    """四级分布：每条结果落一个级别，四档恒定出现，合计等于结果条数。"""
    baseline = _baseline(
        _block(region_seq=1, region_desc="子宫体", block_no="B1"),
        _block(region_seq=2, region_desc="宫颈", block_no="B2"),
    )
    entries = [
        _entry(block_no="B1"),  # L1 蜡块号精确
        _entry(block_no=None, region_desc="宫颈", region_seq=2),  # L2 部位描述精确
        _entry(block_no=None, region_desc="子宫体内膜", region_seq=None),  # L3 词法模糊
        _entry(block_no=None, region_desc=None),  # L4 挂起
    ]
    matched, unmatched = align_entries(entries, {ACCESSION: baseline})
    counts = summarize_match_levels(matched, unmatched)
    assert counts[MatchLevel.L1_BLOCK_EXACT.value] == 1
    assert counts[MatchLevel.L2_REGION_NORMALIZED.value] == 1
    assert counts[MatchLevel.L3_REGION_FUZZY.value] == 1
    assert counts[MatchLevel.L4_PENDING.value] == 1
    assert sum(counts.values()) == len(entries)


def test_summarize_match_levels_keeps_all_four_keys_when_empty():
    """没有结果时四个级别仍在（计数为 0），摘要能一眼对上账。"""
    counts = summarize_match_levels([], [])
    assert set(counts) == {
        MatchLevel.L1_BLOCK_EXACT.value,
        MatchLevel.L2_REGION_NORMALIZED.value,
        MatchLevel.L3_REGION_FUZZY.value,
        MatchLevel.L4_PENDING.value,
    }
    assert sum(counts.values()) == 0


def test_match_level_labels_cover_every_level():
    """中文标签覆盖四级（运行摘要里运维要看的是中文，不是枚举名）。"""
    for level in MatchLevel:
        assert level.value in MATCH_LEVEL_LABELS
