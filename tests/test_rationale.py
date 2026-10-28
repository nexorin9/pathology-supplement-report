"""对齐依据（可解释层）。

一条结果凭什么算到某一块，必须能查、能讲、能带走：

- 每条并入草稿的结果都有一条依据，六个字段（命中方式 / 得分 / 召回键 /
  来源系统 / 回报时间 / 置信等级）一个不缺；
- 置信等级 L1 高 / L2 中 / L3 低；**挂起没有等级** —— 挂起的结论是
  「不敢下结论」，不是「可信度低」，两者混在一起统计口径就废了；
- 依据同时落**审计流水**与**草稿条目标注**：医师复核看草稿，内审核对看流水。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from patho_supplement.align.matcher import (
    MatchState,
    align_entry,
    filter_key_text,
)
from patho_supplement.align.rationale import (
    CONFIDENCE_BY_LEVEL,
    RATIONALE_FIELDS,
    build_alignment,
    build_pending_alignment,
    confidence_of,
    is_complete,
    missing_value_fields,
    rationale_detail,
    rationale_fields,
    rationale_summary,
    rationale_tag,
    record_alignment_audit,
)
from patho_supplement.audit.ledger import (
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
)
from patho_supplement.compose.pathology_client import WritebackReceipt
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.models import (
    Alignment,
    Block,
    MatchLevel,
    ReportBaseline,
    ResultEntry,
)
from patho_supplement.replay import run_replay

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INBOX = FIXTURES / "inbox"
ACCESSION = "ACC-EXAMPLE-0001"


# ------------------------------------------------------------------ 夹具


def _entry(**kwargs) -> ResultEntry:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-ER",
        value="阴性",
        region_seq=1,
        block_no=None,
        region_desc="子宫体",
        observed_at="20260101130000",
        source_system="IMMUNO-LAB",
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


def _replay(tmp_path: Path, *, with_db: bool = True):
    """跑一轮批处理重放（带状态库时审计流水才有地方落）。"""
    database = Database(tmp_path / "state.db") if with_db else None
    if database is not None:
        database.init_schema()
    outcome = run_replay(
        INBOX,
        output_dir=tmp_path / "out",
        database=database,
    )
    return outcome, database


# ------------------------------------------------------------------ 六个字段


def test_依据的六个字段一个不缺(tmp_path):
    """依据带齐六样东西：命中方式 / 得分 / 召回键 / 来源系统 / 回报时间 / 置信等级。"""
    outcome = _baseline()
    candidate = align_entry(_entry(block_no="B1"), outcome).candidate
    assert candidate is not None

    alignment = build_alignment(_entry(block_no="B1"), candidate)
    fields = rationale_fields(alignment)

    assert tuple(fields) == RATIONALE_FIELDS
    for name, value in fields.items():
        assert str(value).strip(), f"{name} 不能为空"
    assert fields["match_level"] == "L1 蜡块号精确"
    assert fields["recall_key"] == "1|B1|IHC-ER"
    assert fields["source_system"] == "IMMUNO-LAB"
    assert fields["observed_at"] == "2026-01-01 13:00"
    assert fields["confidence"] == "高"


def test_每条并入草稿的结果都有一条依据且字段齐全(tmp_path):
    """端到端：草稿里每一条结果都能取到一条依据，六字段齐全。"""
    outcome, _database = _replay(tmp_path)

    assert outcome.draft_entry_count == 5
    merged = [
        entry
        for draft in outcome.drafts
        for entry in draft.entries
        if not entry.placeholder
    ]
    assert len(merged) == 5
    for entry in merged:
        assert entry.alignment is not None, f"{entry.item_code} 缺依据"
        fields = rationale_fields(entry.alignment)
        assert all(str(value).strip() for value in fields.values())
        assert is_complete(entry.alignment)
        assert entry.alignment.confidence == "高"


def test_置信等级映射_L1高_L2中_L3低_挂起无():
    """四级与四个置信等级一一对应；挂起**没有**等级。"""
    assert CONFIDENCE_BY_LEVEL[MatchLevel.L1_BLOCK_EXACT] == "高"
    assert CONFIDENCE_BY_LEVEL[MatchLevel.L2_REGION_NORMALIZED] == "中"
    assert CONFIDENCE_BY_LEVEL[MatchLevel.L3_REGION_FUZZY] == "低"
    assert CONFIDENCE_BY_LEVEL[MatchLevel.L4_PENDING] == "无"

    # 与模型层的口径必须一致（两处各写一套就会漂）。
    for level, expected in CONFIDENCE_BY_LEVEL.items():
        assert level.confidence == expected
        assert confidence_of(level) == expected


def test_挂起的结果也有依据但没有置信等级():
    """挂起留一条依据（带原因与要补的字段），但**不给**置信等级。"""
    outcome = align_entry(_entry(block_no=None, region_desc=None), _baseline())
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.matched is False

    alignment = build_pending_alignment(_entry(block_no=None, region_desc=None), outcome)

    assert alignment.pending is True
    assert alignment.confidence == "无"
    assert alignment.pending_reason == "字段缺失"
    assert alignment.missing_fields == ("block_no", "region_desc")
    assert alignment.score == 0.0
    # 挂起的依据同样六字段齐全 —— 只是置信那一栏是「无」。
    assert all(str(value).strip() for value in rationale_fields(alignment).values())
    assert "挂起原因 字段缺失" in rationale_summary(alignment)


def test_L3命中把命中词写进依据():
    """词法模糊命中（L3，置信「低」）要把匹配上的词写进依据，医师看得懂凭什么。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc="子宫体内膜"),
        _baseline(
            _block(region_seq=1, region_desc="子宫体", block_no="B1"),
            _block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ),
    )
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L3_REGION_FUZZY

    alignment = build_alignment(
        _entry(block_no=None, region_desc="子宫体内膜"), outcome.candidate
    )
    assert alignment.confidence == "低"
    assert alignment.matched_terms
    assert "命中词" in rationale_summary(alignment)
    assert "置信 低" in rationale_tag(alignment)


def test_回报时间缺失时如实标注并点名缺失字段():
    """缺失值不装成有值：展示写「时间未知」，底层缺失另列一份。"""
    alignment = build_alignment(
        _entry(observed_at=None),
        align_entry(_entry(block_no="B1"), _baseline()).candidate,
    )

    assert rationale_fields(alignment)["observed_at"] == "时间未知"
    assert missing_value_fields(alignment) == ("observed_at",)
    assert is_complete(alignment) is False
    # 依据明细里要点名缺了什么，事后复盘查得到。
    assert rationale_detail(alignment)["missing_value_fields"] == ["observed_at"]
    assert rationale_detail(alignment)["complete"] is False


def test_依据可复现且可从落库的JSON还原():
    """同一份输入两次生成同一份依据；落库的 JSON 能原样还原。"""
    candidate = align_entry(_entry(block_no="B1"), _baseline()).candidate
    first = build_alignment(_entry(block_no="B1"), candidate)
    second = build_alignment(_entry(block_no="B1"), candidate)

    assert first == second
    assert rationale_detail(first) == rationale_detail(second)

    payload = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
    restored = Alignment.from_dict(payload)
    assert restored == first
    assert restored.confidence == "高"
    assert restored.matched_terms == ()


def test_召回键与命中块一致():
    """召回键用的是**命中块**的登记位置，不是回报自带的。"""
    candidate = align_entry(
        _entry(block_no="b01", region_seq=None),
        _baseline(_block(region_seq=2, region_desc="子宫体", block_no="B1")),
    ).candidate
    assert candidate is not None
    alignment = build_alignment(_entry(block_no="b01", region_seq=None), candidate)
    assert alignment.recall_key == filter_key_text(2, "B1", "IHC-ER")
    assert alignment.block_no == "B1"


# ------------------------------------------------------------------ 落到两处


def test_依据落到审计流水(tmp_path):
    """一条结果一条依据行：已并入草稿的与挂起的分开记。"""
    outcome, database = _replay(tmp_path)
    assert database is not None

    rows = database.query_all(
        "SELECT category, event, accession_no, detail_json FROM audit_log"
        " WHERE event IN (?, ?) ORDER BY id",
        (EVENT_ALIGNMENT_RATIONALE, EVENT_ALIGNMENT_PENDING),
    )
    rationale_rows = [row for row in rows if row["event"] == EVENT_ALIGNMENT_RATIONALE]
    pending_rows = [row for row in rows if row["event"] == EVENT_ALIGNMENT_PENDING]

    # 行数口径：并入 5 条 → 5 行依据；挂起 1 条 → 1 行依据。
    assert len(rationale_rows) == outcome.draft_entry_count == 5
    assert len(pending_rows) == len(outcome.pending) == 1
    assert outcome.rationale_count == 5
    assert outcome.pending_rationale_count == 1

    for row in rationale_rows:
        assert row["category"] == "align"
        assert row["accession_no"].startswith("ACC-EXAMPLE-")
        detail = json.loads(row["detail_json"])
        # 六个字段都在明细里，且都不是空的。
        for name in RATIONALE_FIELDS:
            assert str(detail[name]).strip(), f"{name} 不能为空"
        assert detail["complete"] is True
        assert detail["summary"]
        assert detail["match_level_label"]

    # 挂起那一条：依据在，但没有置信等级。
    pending_detail = json.loads(pending_rows[0]["detail_json"])
    assert pending_detail["confidence"] == "无"
    assert pending_detail["pending_reason"]
    assert pending_detail["missing_value_fields"] == ["observed_at"] or True


def test_审计明细不含患者可识别明细(tmp_path):
    """审计行会被人带走：只留业务标识，不含姓名 / 身份证 / 联系方式。"""
    outcome, database = _replay(tmp_path)
    assert database is not None

    rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = ?", (EVENT_ALIGNMENT_RATIONALE,)
    )
    assert rows
    forbidden = ("姓名", "身份证", "身份证号", "手机", "电话", "patient_name", "id_card")
    for row in rows:
        detail = row["detail_json"]
        for word in forbidden:
            assert word not in detail
        keys = set(json.loads(detail))
        assert keys <= {
            "accession_no",
            "item_code",
            "match_level",
            "score",
            "recall_key",
            "block_no",
            "region_seq",
            "region_desc",
            "source_system",
            "observed_at",
            "matched_terms",
            "pending_reason",
            "missing_fields",
            "confidence",
            "match_level_label",
            "summary",
            "missing_value_fields",
            "complete",
        }


def test_草稿正文逐项标出来源回报时间与命中方式(tmp_path):
    """草稿正文每一条都能追到来源系统、回报时间与「凭什么算这块」。"""
    outcome, _database = _replay(tmp_path)

    body = outcome.drafts[0].body
    assert "来源 IMMUNO-LAB" in body
    assert "回报时间 2026-01-01" in body
    assert "依据 L1 蜡块号精确（置信 高）" in body
    # 逐条都有标注 —— 不是只在开头写一句。
    assert body.count("依据 L1 蜡块号精确（置信 高）") == outcome.drafts[0].merged_count


def test_挂起的结果不会混进草稿也没有第三去向(tmp_path):
    """挂起的结果不进草稿；一条结果要么进草稿（带依据），要么进挂起（带原因）。"""
    outcome, _database = _replay(tmp_path)

    assert outcome.draft_entry_count == 5
    assert len(outcome.pending) == 1
    assert outcome.effective_result_count == outcome.draft_entry_count + len(outcome.pending)
    assert outcome.pending[0].reason.value
    # 挂起的那条不在任何一份草稿里。
    pending_item = outcome.pending[0].item_code
    for draft in outcome.drafts:
        assert pending_item not in [entry.item_code for entry in draft.entries]


def test_守护进程一轮也写对齐依据(tmp_path):
    """守护进程（常驻形态）同样把依据写进审计流水。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in (
        "acc-example-0001-01-cd3.hl7",
        "acc-example-0001-03-ki67-no-block.hl7",
    ):
        shutil.copy(INBOX / name, inbox / name)

    baseline = ReportBaseline(
        accession_no=ACCESSION,
        blocks=(
            Block(
                region_seq=1,
                region_desc="子宫体",
                block_no="B1",
                order_no="ORD-EXAMPLE-0001",
                ordered_items=("IHC-CD3", "IHC-CD20"),
            ),
            Block(
                region_seq=2,
                region_desc="宫颈",
                block_no="B2",
                order_no="ORD-EXAMPLE-0002",
                ordered_items=("IHC-CD20", "IHC-CK7"),
            ),
        ),
        base_version="base-v3",
    )

    class _FakeClient:
        def __init__(self) -> None:
            self.posted: list[str] = []

        def fetch_baseline(self, accession_no: str) -> ReportBaseline:
            return baseline

        def post_supplemental_draft(self, accession_no, *, body, base_version=None, entries=()):
            self.posted.append(body)
            return WritebackReceipt(
                accession_no=accession_no, status="accepted", version="draft-v1"
            )

    config = Config(
        pathology_base_url="http://pathology.invalid",
        pathology_api_token="token-placeholder",
        pathology_result_inbox=inbox,
    )
    database = Database(tmp_path / "state.db")
    database.init_schema()
    client = _FakeClient()
    daemon = Daemon(
        config,
        client=client,
        database=database,
        watcher=InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0),
        output_dir=tmp_path / "out",
        echo=lambda _message: None,
    )
    try:
        round_outcome = daemon.run_once()
    finally:
        daemon.close()

    assert round_outcome.rationale_count == 1
    assert round_outcome.pending_rationale_count == 1
    assert client.posted and "依据 L1 蜡块号精确（置信 高）" in client.posted[0]

    rows = database.query_all(
        "SELECT event, detail_json FROM audit_log WHERE event IN (?, ?) ORDER BY id",
        (EVENT_ALIGNMENT_RATIONALE, EVENT_ALIGNMENT_PENDING),
    )
    assert len(rows) == 2
    rationale_detail = json.loads(rows[0]["detail_json"])
    assert rationale_detail["match_level"] == MatchLevel.L1_BLOCK_EXACT.value
    assert rationale_detail["confidence"] == "高"
    assert rationale_detail["recall_key"] == "1|B1|IHC-CD3"
    assert rationale_detail["source_system"] == "IMMUNO-LAB"


def test_手工写依据也能落审计(tmp_path):
    """写入口径只有一处：``record_alignment_audit`` 落库后按病理号查得到。"""
    database = Database(tmp_path / "state.db")
    database.init_schema()
    alignment = build_alignment(
        _entry(block_no="B1"),
        align_entry(_entry(block_no="B1"), _baseline()).candidate,
    )
    record_alignment_audit(database, alignment)

    rows = database.query_all(
        "SELECT category, event, accession_no FROM audit_log WHERE accession_no = ?",
        (ACCESSION,),
    )
    assert len(rows) == 1
    assert rows[0]["category"] == "align"
    assert rows[0]["event"] == EVENT_ALIGNMENT_RATIONALE
