"""同分歧义拒绝 + 缺字段兜底 + 申请侧状态闸门。

这一组用例锁的是三条「**宁可挂起，也不写错**」的硬承诺：

1. **同分多候选一律挂起**，全部候选写进挂起项 —— 不取第一个，不猜；
2. **缺字段兜底**：缺蜡块号不落 L1、缺部位描述不落 L2、两个都缺直接 L4
   并标「字段缺失」；
3. **申请侧已取消 / 已终止的待查项，其结果不并入草稿** —— 报一项已经不做
   的检查，比少报一项严重得多。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patho_supplement.align.matcher import (
    MatchState,
    MatchThresholds,
    align_entries,
    align_entry,
)
from patho_supplement.align.order_guard import OrderStateGuard
from patho_supplement.align.query_derivation import PendingQuery
from patho_supplement.models import (
    Block,
    CheckItemState,
    MatchLevel,
    ReportBaseline,
    ResultEntry,
)
from patho_supplement.pending.reasons import PendingReason, reason_for_outcome
from patho_supplement.replay import run_replay

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CANCELLED_INBOX = FIXTURES / "cancelled"

ACCESSION = "ACC-EXAMPLE-0003"


# ---------------------------------------------------------------- 造数


def _entry(**kwargs) -> ResultEntry:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-CD20",
        value="阳性",
        region_seq=None,
        block_no=None,
        region_desc=None,
    )
    base.update(kwargs)
    return ResultEntry(**base)


def _block(**kwargs) -> Block:
    base = dict(
        region_seq=1,
        region_desc="子宫体",
        block_no="B1",
        order_no="ORD-EXAMPLE-0011",
        ordered_items=("IHC-CD20",),
    )
    base.update(kwargs)
    return Block(**base)


def _baseline(*blocks: Block) -> ReportBaseline:
    return ReportBaseline(
        accession_no=ACCESSION,
        blocks=blocks or (_block(),),
        base_version="base-v1",
    )


def _query(state: CheckItemState, **kwargs) -> PendingQuery:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-CD20",
        order_no="ORD-EXAMPLE-0011",
        region_seq=1,
        block_no="B1",
        region_desc="子宫体",
        state=state,
    )
    base.update(kwargs)
    return PendingQuery(**base)


def _guard(*queries: PendingQuery) -> OrderStateGuard:
    return OrderStateGuard.from_queries(queries)


# ---------------------------------------------------------------- 一、同分多候选


def test_l3_词法同分全部挂起且不取第一个():
    """两个部位词法同分 → 一律挂起，候选全量给出。"""
    outcome = align_entry(
        _entry(region_desc="组织"),
        _baseline(
            _block(region_seq=1, region_desc="黏膜组织", block_no="B1"),
            _block(region_seq=2, region_desc="浆膜组织", block_no="B2"),
        ),
        thresholds=MatchThresholds(bm25_threshold=0.05, lead_margin_threshold=0.05),
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.AMBIGUOUS
    assert outcome.match_level is MatchLevel.L4_PENDING
    assert outcome.pending_reason == "同分多候选"
    # 全部候选都在，一个都不少；没有「取第一个」的痕迹。
    assert len(outcome.candidates) == 2
    assert {block.block_no for block in outcome.candidates} == {"B1", "B2"}
    assert outcome.candidate is None


def test_同分多候选挂起率为百分之百():
    """一批结果全落在同分歧义上 → 挂起率 100%，没有一条被硬绑。"""
    entries = [
        _entry(region_desc="组织", region_seq=None, value=f"结果{index}")
        for index in range(5)
    ]
    baseline = _baseline(
        _block(region_seq=1, region_desc="黏膜组织", block_no="B1"),
        _block(region_seq=2, region_desc="浆膜组织", block_no="B2"),
    )
    matched, unmatched = align_entries(
        entries,
        {ACCESSION: baseline},
        thresholds=MatchThresholds(bm25_threshold=0.05, lead_margin_threshold=0.05),
    )
    assert matched == []
    assert len(unmatched) == 5
    assert all(item.state == MatchState.AMBIGUOUS for _, item in unmatched)
    assert all(len(item.candidates) == 2 for _, item in unmatched)


def test_同分候选全部写进挂起项():
    """挂起项里带全候选与缺失字段，技术人员照着就能去查登记。"""
    outcome = align_entry(
        _entry(region_desc="组织"),
        _baseline(
            _block(region_seq=1, region_desc="黏膜组织", block_no="B1"),
            _block(region_seq=2, region_desc="浆膜组织", block_no="B2"),
        ),
        thresholds=MatchThresholds(bm25_threshold=0.05, lead_margin_threshold=0.05),
    )
    reason = reason_for_outcome(outcome)
    assert reason is PendingReason.AMBIGUOUS
    assert set(outcome.missing_fields) == {"block_no", "region_desc"}


def test_l1_两块共用蜡块号且都申请过该项目时同样挂起():
    """同一个蜡块号登在两块上，收窄后仍是多候选 → 挂起。"""
    outcome = align_entry(
        _entry(block_no="B1", region_seq=None),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B1"),
        ),
    )
    assert outcome.state == MatchState.AMBIGUOUS
    assert len(outcome.candidates) == 2


# ---------------------------------------------------------------- 二、缺字段兜底


def test_缺蜡块号不落_l1():
    """没有蜡块号就进不了 L1 —— L1 只认蜡块号。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="子宫体", region_seq=None),
        _baseline(_block(region_seq=1, region_desc="子宫体", block_no="B1")),
    )
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED


def test_缺部位描述不落_l2():
    """没有部位描述就进不了 L2 —— L2 只认部位描述。"""
    outcome = align_entry(
        _entry(block_no="B1", region_desc=None, region_seq=None),
        _baseline(_block(region_seq=1, region_desc="子宫体", block_no="B1")),
    )
    # 蜡块号还在，L1 成立；L2 不参与。
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L1_BLOCK_EXACT

    # 蜡块号也没有时，两级都落不了 → L4 挂起，绝不靠部位描述硬凑。
    no_block = align_entry(
        _entry(block_no=None, region_desc=None, region_seq=None),
        _baseline(_block(region_seq=1, region_desc="子宫体", block_no="B1")),
    )
    assert no_block.matched is False
    assert no_block.level is MatchLevel.L4_PENDING


def test_两个定位字段都缺直接落_l4_并标字段缺失():
    """蜡块号与部位描述皆缺 → L4，标「字段缺失」，缺哪两个字段写清楚。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc=None, region_seq=None),
        _baseline(_block(region_seq=1, region_desc="子宫体", block_no="B1")),
    )
    assert outcome.matched is False
    assert outcome.level is MatchLevel.L4_PENDING
    assert outcome.pending_reason == "字段缺失"
    assert set(outcome.missing_fields) == {"block_no", "region_desc"}
    assert reason_for_outcome(outcome) is PendingReason.FIELDS_MISSING


def test_字段缺失的挂起项标出两个要补的字段():
    """挂起项里写清要补哪两个字段，不是一句「对不上」。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc=None, region_seq=None),
        _baseline(_block(region_seq=1, region_desc="子宫体", block_no="B1")),
    )
    reason = reason_for_outcome(outcome)
    assert reason is PendingReason.FIELDS_MISSING
    assert reason.label == "字段缺失"


# ---------------------------------------------------------------- 三、申请侧状态闸门


def test_已取消的待查项结果不并入草稿():
    """加做申请被取消后送来的结果 → 挂起，原因「待查项已取消」。"""
    guard = _guard(_query(CheckItemState.CANCELLED))
    outcome = align_entry(
        _entry(block_no="B1", region_seq=1, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.ITEM_CANCELLED
    assert outcome.pending_reason == "待查项已取消"
    assert reason_for_outcome(outcome) is PendingReason.ITEM_CANCELLED
    assert outcome.level is MatchLevel.L4_PENDING


def test_已终止的待查项结果不并入草稿():
    """加做申请被终止后送来的结果 → 挂起，原因「待查项已终止」。"""
    guard = _guard(_query(CheckItemState.TERMINATED))
    outcome = align_entry(
        _entry(block_no="B1", region_seq=1, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.ITEM_TERMINATED
    assert outcome.pending_reason == "待查项已终止"
    assert reason_for_outcome(outcome) is PendingReason.ITEM_TERMINATED


def test_已取消的项没有可补字段():
    """已取消 / 已终止不是缺字段 —— 没有可补的字段，要人决定这条结果怎么办。"""
    guard = _guard(_query(CheckItemState.CANCELLED))
    outcome = align_entry(
        _entry(block_no="B1", region_seq=1, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    assert outcome.missing_fields == ()


def test_闸门按命中块的位置复核():
    """回报没带蜡块号，但这一条落在被取消的块上 → 照样拦下。

    这是闸门的第二遍：对齐之前那遍用回报自带的定位，定位不到；命中块的位置
    才说得清这条结果属于哪一块。
    """
    guard = _guard(_query(CheckItemState.CANCELLED))
    outcome = align_entry(
        _entry(block_no=None, region_seq=None, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.ITEM_CANCELLED
    # 挂起项里给出「本来会归到哪一块」。
    assert [block.block_no for block in outcome.candidates] == ["B1"]


def test_暂停的待查项不拦():
    """暂停是「暂时不做」，申请还在账上 —— 不拦，避免制造无谓挂起。"""
    guard = _guard(_query(CheckItemState.HELD))
    outcome = align_entry(
        _entry(block_no="B1", region_seq=1, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    assert outcome.matched is True
    assert outcome.candidate is not None


def test_闸门不拦未取消的待查项():
    """同一份报告里，另一块没被取消 —— 那块的结果照常并入草稿。"""
    guard = _guard(
        _query(CheckItemState.CANCELLED),
        _query(
            CheckItemState.AWAITING,
            order_no="ORD-EXAMPLE-0012",
            region_seq=2,
            block_no="B2",
            region_desc="宫颈",
        ),
    )
    outcome = align_entry(
        _entry(block_no="B2", region_seq=2, region_desc="宫颈"),
        _baseline(
            _block(),
            _block(region_seq=2, region_desc="宫颈", block_no="B2", order_no="ORD-EXAMPLE-0012"),
        ),
        guard=guard,
    )
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.block.block_no == "B2"


def test_闸门按归一化后的蜡块号定位():
    """基线写 B-1、回报写 b01 —— 归一化后是同一块，照样拦得住。"""
    guard = _guard(_query(CheckItemState.CANCELLED, block_no="B-1"))
    outcome = align_entry(
        _entry(block_no="b01", region_seq=1, region_desc="子宫体"),
        _baseline(_block(block_no="B-1")),
        guard=guard,
    )
    assert outcome.matched is False
    assert outcome.state == MatchState.ITEM_CANCELLED


def test_闸门查不到对应待查项时不拦():
    """项目码对不上 → 不是这一项，闸门不拦（对齐层自己会判挂起）。"""
    guard = _guard(_query(CheckItemState.CANCELLED))
    outcome = align_entry(
        _entry(item_code="IHC-KI67", block_no="B1", region_seq=1, region_desc="子宫体"),
        _baseline(_block()),
        guard=guard,
    )
    # KI67 没在这块申请过 → 对齐层判「待查项未登记」，不是「已取消」。
    assert outcome.state == MatchState.ITEM_NOT_ORDERED


# ---------------------------------------------------------------- 四、端到端


def test_重放时已取消项的结果不进草稿(tmp_path):
    """端到端：一份报告里，被取消那块的结果进挂起清单，另一块照常成稿。"""
    outcome = run_replay(CANCELLED_INBOX, output_dir=tmp_path / "out")

    # 两条结果：一条属于已取消的 B1，一条属于未取消的 B2。
    assert outcome.parsed_results == 2
    assert outcome.derived_queries == 2
    assert outcome.cancelled_queries == 1

    # 草稿里只有未取消那条。
    assert outcome.draft_entry_count == 1
    assert len(outcome.drafts) == 1
    draft = outcome.drafts[0]
    assert draft.accession_no == ACCESSION
    assert draft.entries[0].block_no == "B2"

    # 被取消的那条进了挂起清单，原因是「待查项已取消」。
    assert len(outcome.pending) == 1
    record = outcome.pending[0]
    assert record.item_code == "IHC-CD20"
    assert record.reason is PendingReason.ITEM_CANCELLED
    assert record.reason.label == "待查项已取消"
    assert record.missing_fields == ()

    # 没有第三个去向：草稿 + 挂起 = 生效结果数。
    assert outcome.effective_result_count == 2


def test_重放清单里写明被取消的块(tmp_path):
    """待人工处理清单给出「哪一块上的哪一项被取消了」。"""
    outcome = run_replay(CANCELLED_INBOX, output_dir=tmp_path / "out")
    text = outcome.pending_path.read_text(encoding="utf-8")
    assert "待查项已取消" in text
    assert "B1" in text
    # 清单里不含患者可识别明细。
    for token in ("姓名", "身份证", "联系电话"):
        assert token not in text


def test_挂起清单与草稿互斥(tmp_path):
    """同一条结果不会既在草稿里又在挂起清单里。"""
    outcome = run_replay(CANCELLED_INBOX, output_dir=tmp_path / "out")
    draft_blocks = {
        (draft.accession_no, entry.item_code, entry.block_no)
        for draft in outcome.drafts
        for entry in draft.entries
        if not entry.placeholder
    }
    pending_blocks = {
        (record.accession_no, record.item_code, block.block_no)
        for record in outcome.pending
        for block in record.candidates
    }
    assert draft_blocks & pending_blocks == set()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
