"""挂起原因分类、缺失字段映射与原因历史。

这条线要保的是**技术员拿到清单就知道去哪儿补什么**：每一类原因都写清
「缺哪个字段、候选是谁、去哪儿补」；原因变了要留痕（带时间戳），
当前原因取最新一条，历史不被覆盖。
"""

from __future__ import annotations

import pytest

from patho_supplement.db import Database
from patho_supplement.models import Block, ResultEntry, ResultStatus
from patho_supplement.pending.reasons import (
    SEVEN_REASON_FAMILIES,
    PendingReason,
    ReasonGuidance,
    ReasonHistory,
    classify_reason,
    guidance_for,
    guidance_for_outcome,
    missing_fields_for_entry,
    missing_fields_for_reason,
    missing_fields_for_state,
    parse_reason,
    reason_for_entry,
    reason_for_outcome,
    reason_for_state,
)
from patho_supplement.pending.records import PendingRecord, render_pending_text
from patho_supplement.pending.store import PendingStore


# --------------------------------------------------------------- 样例


def _entry(
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-CD20",
    *,
    value: str | None = "阳性",
    block_no: str | None = "B1",
    region_seq: int | None = 1,
    region_desc: str | None = "子宫体内侧",
    status: ResultStatus = ResultStatus.FINAL,
    observed_at: str | None = "20260101103000",
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="CD20 免疫组化",
        value=value,
        status=status,
        observed_at=observed_at,
        order_no="ORD-EXAMPLE-001",
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file="oru-001.hl7",
    )


def _candidates() -> tuple[Block, ...]:
    return (
        Block(region_seq=1, region_desc="子宫体内侧", block_no="B1", order_no="ORD-EXAMPLE-001"),
        Block(region_seq=2, region_desc="宫颈部", block_no="B2", order_no="ORD-EXAMPLE-002"),
    )


class _FakeOutcome:
    """对齐结论的最小替身（只带原因层要看的三个字段）。"""

    def __init__(self, state: str, pending_reason: str | None = None, **kwargs):
        self.state = state
        self.pending_reason = pending_reason
        self.missing_fields = kwargs.get("missing_fields", ())
        self.candidates = kwargs.get("candidates", ())


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


# --------------------------------------------------------------- 七类主因


def test_七类主因各有正确标签():
    """七类主因是人工补齐的入口，标签要和技术员说的一样。"""
    assert [reason.value for reason in SEVEN_REASON_FAMILIES] == [
        "缺蜡块号",
        "部位描述缺失",
        "同分多候选",
        "召回命中 0 条",
        "待查项已取消",
        "待查项未登记",
        "回报时间倒挂",
    ]
    assert len(SEVEN_REASON_FAMILIES) == 7


@pytest.mark.parametrize(
    ("reason", "expected_fields"),
    [
        (PendingReason.MISSING_BLOCK_NO, ("block_no",)),
        (PendingReason.REGION_DESC_MISSING, ("region_desc",)),
        (PendingReason.AMBIGUOUS, ("block_no", "region_desc")),
        (PendingReason.NO_RECALL_HITS, ("region_seq", "block_no")),
        (PendingReason.ITEM_CANCELLED, ()),
        (PendingReason.ITEM_NOT_ORDERED, ("item_code",)),
        (PendingReason.TIME_INVERSION, ("observed_at",)),
    ],
)
def test_七类主因各有明确的缺失字段(reason, expected_fields):
    """每类原因都写清要补哪个字段 —— 技术员照着字段去查，不用反推。"""
    assert missing_fields_for_reason(reason) == expected_fields


def test_每类原因都有补齐指引():
    """每一类原因都要有一句「去哪儿补什么」。"""
    for reason in PendingReason:
        guidance = guidance_for(reason)
        assert guidance.hint, f"{reason.value} 缺补齐指引"
        assert guidance.reason is reason
        assert guidance.missing_fields == missing_fields_for_reason(reason)


def test_指引带候选部位():
    """原因 → 候选列表的映射：候选是谁，一并给出。"""
    guidance = guidance_for(PendingReason.AMBIGUOUS, _candidates())
    assert isinstance(guidance, ReasonGuidance)
    assert len(guidance.candidates) == 2
    assert "子宫体内侧" in guidance.candidate_text
    assert "B2" in guidance.candidate_text
    assert guidance.as_dict()["candidates"] == 2
    assert guidance.as_dict()["reason"] == "同分多候选"


def test_无候选时说清楚没有候选():
    guidance = guidance_for(PendingReason.ITEM_CANCELLED)
    assert guidance.candidates == ()
    assert guidance.candidate_text == "无"


def test_按对齐结论给指引():
    """结论 → 指引：候选一并带上。"""
    outcome = _FakeOutcome(
        "ambiguous", "同分多候选", candidates=_candidates()
    )
    guidance = guidance_for_outcome(outcome)
    assert guidance.reason is PendingReason.AMBIGUOUS
    assert guidance.missing_fields == ("block_no", "region_desc")
    assert len(guidance.candidates) == 2


def test_候选列表与缺失字段在摘要里可查():
    guidance = guidance_for(PendingReason.NO_RECALL_HITS, _candidates())
    summary = guidance.as_dict()
    assert summary["missing_fields"] == ["region_seq", "block_no"]
    assert summary["candidates"] == 2
    assert summary["hint"]


# --------------------------------------------------------------- 原因判定


def test_结果本身有问题优先于对齐状态():
    """无法获得 / 值为空：与对齐无关，先定这一条。"""
    entry = _entry(value="", status=ResultStatus.NOT_OBTAINED)
    outcome = _FakeOutcome("missing_block_no", "缺蜡块号")
    assert classify_reason(entry, outcome) is PendingReason.RESULT_NOT_OBTAINED


def test_未命中部位且回报没带部位描述时改判部位描述缺失():
    """要补的其实是部位描述 —— 别让人去查一个根本没填的字段。"""
    entry = _entry(block_no="B9", region_desc=None)
    outcome = _FakeOutcome("no_block", "未命中部位")
    assert classify_reason(entry, outcome) is PendingReason.REGION_DESC_MISSING


def test_未命中部位但回报带了部位描述时保持原判():
    entry = _entry(block_no="B9", region_desc="宫颈部")
    outcome = _FakeOutcome("no_block", "未命中部位")
    assert classify_reason(entry, outcome) is PendingReason.NO_MATCH


def test_召回命中零条被识别为挂起原因():
    """过滤层的措辞也要落进同一套分类。"""
    outcome = _FakeOutcome("missing_block_no", "召回命中 0 条")
    assert reason_for_outcome(outcome) is PendingReason.NO_RECALL_HITS
    assert classify_reason(_entry(), outcome) is PendingReason.NO_RECALL_HITS


def test_降级措辞归到召回命中零条():
    outcome = _FakeOutcome("missing_block_no", "硬过滤命中 0 条，降级模糊匹配")
    assert reason_for_outcome(outcome) is PendingReason.NO_RECALL_HITS


def test_按对齐状态兜底():
    assert reason_for_state("ambiguous") is PendingReason.AMBIGUOUS
    assert reason_for_state("item_cancelled") is PendingReason.ITEM_CANCELLED
    assert reason_for_state("不存在的状态") is PendingReason.NO_MATCH


def test_结果条目层面的原因():
    assert reason_for_entry(_entry(value="阳性")) is None
    assert reason_for_entry(_entry(value="")) is PendingReason.EMPTY_VALUE
    assert (
        reason_for_entry(_entry(value="", status=ResultStatus.NOT_OBTAINED))
        is PendingReason.RESULT_NOT_OBTAINED
    )
    assert missing_fields_for_entry(_entry(value="阳性")) == ()
    assert missing_fields_for_entry(_entry(value="")) == ("result_value",)


def test_状态层面的缺失字段仍然可用():
    assert missing_fields_for_state("ambiguous") == ("block_no", "region_desc")
    assert missing_fields_for_state("item_cancelled") == ()


def test_不认识的原因直接报错():
    """原因不认识就不能进清单 —— 宁可报错，也不糊弄成「未命中部位」。"""
    with pytest.raises(ValueError) as excinfo:
        parse_reason("看不懂的原因")
    assert "不认识" in str(excinfo.value)


def test_早期口径的标签仍然认得():
    """老库里存着「项目未登记」，照样读得出来。"""
    assert parse_reason("项目未登记") is PendingReason.ITEM_NOT_ORDERED
    assert parse_reason("待查项未登记") is PendingReason.ITEM_NOT_ORDERED


# --------------------------------------------------------------- 原因历史


def test_原因历史保留变化且当前取最新():
    """原因会变：历史留痕，当前原因取最新一条。"""
    history = ReasonHistory()
    history.append(PendingReason.MISSING_BLOCK_NO, at="2026-10-28 09:00:00")
    history.append(PendingReason.AMBIGUOUS, at="2026-10-28 10:30:00")

    assert len(history.entries) == 2
    assert history.current is not None
    assert history.current.reason is PendingReason.AMBIGUOUS
    assert history.current_reason is PendingReason.AMBIGUOUS
    assert history.current.recorded_at == "2026-10-28 10:30:00"
    assert history.previous_reasons == (PendingReason.MISSING_BLOCK_NO,)
    assert history.changed is True


def test_原因没变就不算变过():
    history = ReasonHistory()
    history.append(PendingReason.MISSING_BLOCK_NO, at="2026-10-28 09:00:00")
    history.append(PendingReason.MISSING_BLOCK_NO, at="2026-10-28 10:30:00")
    assert history.changed is False
    assert history.current_reason is PendingReason.MISSING_BLOCK_NO


def test_空历史没有当前原因():
    history = ReasonHistory()
    assert history.current is None
    assert history.current_reason is None
    assert history.changed is False


def test_原因历史落库后仍能查回(db):
    """原因历史落 SQLite：进程重启后仍查得到。"""
    store = PendingStore(db)
    store.record_reason(
        "ACC-EXAMPLE-0001", "IHC-CD20", PendingReason.MISSING_BLOCK_NO,
        recorded_at="2026-10-28 09:00:00",
    )
    store.record_reason(
        "ACC-EXAMPLE-0001", "IHC-CD20", PendingReason.AMBIGUOUS,
        recorded_at="2026-10-28 10:30:00",
    )

    history = store.reason_history("ACC-EXAMPLE-0001", "IHC-CD20")
    assert len(history) == 2
    assert history[0].reason is PendingReason.MISSING_BLOCK_NO
    assert history[1].reason is PendingReason.AMBIGUOUS
    assert history[1].recorded_at == "2026-10-28 10:30:00"
    assert store.current_reason("ACC-EXAMPLE-0001", "IHC-CD20") is PendingReason.AMBIGUOUS


def test_挂起落库时自动记原因(db):
    """每次挂起都记一条原因 —— 原因变了历史自然留痕。"""
    store = PendingStore(db)
    record = PendingRecord.from_entry(
        _entry(), PendingReason.MISSING_BLOCK_NO, missing_fields=("block_no",)
    )
    store.save(record, max_attempts=3)

    history = store.reason_history("ACC-EXAMPLE-0001", "IHC-CD20")
    assert len(history) == 1
    assert history[0].reason is PendingReason.MISSING_BLOCK_NO
    assert history[0].recorded_at


def test_同一原因反复挂起不重复记历史(db):
    """试了几次由 attempts 记；原因历史只记「为什么变了」。"""
    store = PendingStore(db)
    for _ in range(3):
        store.save(
            PendingRecord.from_entry(
                _entry(), PendingReason.MISSING_BLOCK_NO, missing_fields=("block_no",)
            ),
            max_attempts=5,
        )
    assert len(store.reason_history("ACC-EXAMPLE-0001", "IHC-CD20")) == 1
    assert store.previous_attempts("ACC-EXAMPLE-0001", "IHC-CD20") == 3


def test_原因变了历史保留旧原因(db):
    store = PendingStore(db)
    store.save(
        PendingRecord.from_entry(
            _entry(), PendingReason.MISSING_BLOCK_NO, missing_fields=("block_no",)
        ),
        max_attempts=5,
    )
    store.save(
        PendingRecord.from_entry(
            _entry(), PendingReason.AMBIGUOUS, missing_fields=("block_no", "region_desc")
        ),
        max_attempts=5,
    )

    history = store.reason_history("ACC-EXAMPLE-0001", "IHC-CD20")
    assert [entry.reason for entry in history] == [
        PendingReason.MISSING_BLOCK_NO,
        PendingReason.AMBIGUOUS,
    ]
    assert store.current_reason("ACC-EXAMPLE-0001", "IHC-CD20") is PendingReason.AMBIGUOUS


def test_快照读回时带原因历史(db):
    """恢复出来的挂起项带着历史原因 —— 清单上看得见「为什么变了」。"""
    store = PendingStore(db)
    store.save(
        PendingRecord.from_entry(
            _entry(), PendingReason.MISSING_BLOCK_NO, missing_fields=("block_no",)
        ),
        max_attempts=5,
    )
    store.save(
        PendingRecord.from_entry(
            _entry(), PendingReason.AMBIGUOUS, missing_fields=("block_no", "region_desc")
        ),
        max_attempts=5,
    )

    loaded = store.load_all()
    latest = loaded.snapshots[-1]
    assert latest.reason is PendingReason.AMBIGUOUS
    assert len(latest.reason_history) == 2
    assert latest.to_record().previous_reasons == ("缺蜡块号",)


def test_没有历史时当前原因取挂起行上的(db):
    """老库里没有历史行，也不该返回空 —— 退回挂起行上的原因。"""
    store = PendingStore(db)
    store.save(
        PendingRecord.from_entry(
            _entry(), PendingReason.ITEM_CANCELLED, missing_fields=()
        ),
        max_attempts=5,
    )
    db.execute("DELETE FROM pending_reason_history")
    assert store.current_reason("ACC-EXAMPLE-0001", "IHC-CD20") is PendingReason.ITEM_CANCELLED


# --------------------------------------------------------------- 清单呈现


def test_清单写清缺什么补什么候选是谁():
    """清单是技术员直接带走的：要补的字段、候选部位、怎么补，一样不少。"""
    record = PendingRecord.from_entry(
        _entry(),
        PendingReason.AMBIGUOUS,
        missing_fields=missing_fields_for_reason(PendingReason.AMBIGUOUS),
        candidates=_candidates(),
    )
    text = render_pending_text([record])

    assert "原因 同分多候选" in text
    # 字段名对人说中文标签（与 cli pending list、导出清单同一套说法）；
    # 英文名只出现在命令语法里（pending resolve --set block_no=B1）。
    assert "需补齐字段：蜡块号、取材部位描述" in text
    assert "候选部位：子宫体内侧/蜡块 B1" in text
    assert "怎么补：" in text


def test_清单写出历史原因():
    record = PendingRecord.from_entry(
        _entry(),
        PendingReason.AMBIGUOUS,
        missing_fields=("block_no", "region_desc"),
        candidates=_candidates(),
    )
    record = PendingRecord(
        **{
            **{f: getattr(record, f) for f in record.__dataclass_fields__},
            "previous_reasons": ("缺蜡块号",),
        }
    )
    text = render_pending_text([record])
    assert "历史原因：缺蜡块号 → 同分多候选" in text
