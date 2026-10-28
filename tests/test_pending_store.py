"""挂起项快照持久化与重启恢复。

快照这条线要保的是**不丢结果**：一条对不上的结果被拍成快照落库，进程重启后
还要能原样恢复 —— 缺哪个字段、候选是谁、试了几次、什么时候拍的、哪个系统
回报的，一样都不能少。少一样，技术人员补齐时就得回头翻原始报文。
"""

from __future__ import annotations

import json

import pytest

from patho_supplement.db import Database
from patho_supplement.models import Block, ResultEntry, ResultStatus
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    PendingRecord,
)
from patho_supplement.pending.store import (
    PENDING_STATES,
    SNAPSHOT_FORMAT_VERSION,
    PendingSnapshotError,
    PendingStore,
    entry_from_payload,
    snapshot_payload,
)


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
    source_system: str = "ihc-lab",
    source_file: str | None = "oru-001.hl7",
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
        source_system=source_system,
        source_file=source_file,
    )


def _candidates() -> tuple[Block, ...]:
    return (
        Block(region_seq=1, region_desc="子宫体内侧", block_no="B1", order_no="ORD-EXAMPLE-001"),
        Block(region_seq=2, region_desc="宫颈部", block_no="B2", order_no="ORD-EXAMPLE-002"),
    )


def _record(
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-CD20",
    *,
    reason: PendingReason = PendingReason.MISSING_BLOCK_NO,
    missing_fields: tuple[str, ...] = ("block_no",),
    candidates: tuple[Block, ...] = (),
    entry: ResultEntry | None = None,
) -> PendingRecord:
    source = entry if entry is not None else _entry(accession_no, item_code)
    return PendingRecord.from_entry(
        source,
        reason,
        missing_fields=missing_fields,
        candidates=candidates,
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


# --------------------------------------------------------------- 写入


def test_save_writes_snapshot_fields(db):
    """快照落库：六个必备内容都在。"""
    store = PendingStore(db)
    snapshot = store.save(
        _record(candidates=_candidates()), max_attempts=3
    )

    assert snapshot.snapshot_id > 0
    row = db.query_one(
        "SELECT accession_no, item_code, result_json, missing_fields, candidates_json,"
        " reason, attempts, state, snapshot_at, block_no, region_seq FROM pending_items"
    )
    assert row["accession_no"] == "ACC-EXAMPLE-0001"
    assert row["item_code"] == "IHC-CD20"
    assert row["reason"] == PendingReason.MISSING_BLOCK_NO.value
    assert row["attempts"] == 1
    assert row["state"] == PENDING_STATE_PENDING
    assert row["snapshot_at"]
    # 缺失字段与候选列表
    assert row["missing_fields"] == "block_no"
    assert [item["block_no"] for item in json.loads(row["candidates_json"])] == ["B1", "B2"]
    # 未对齐结果（含来源系统与回报时间）
    payload = json.loads(row["result_json"])
    assert payload["value"] == "阳性"
    assert payload["source_system"] == "ihc-lab"
    assert payload["observed_at"] == "20260101103000"
    assert payload["block_no"] == "B1"
    # 定位字段单独成列，便于按「病理号 + 蜡块号 + 项目码」回找
    assert row["block_no"] == "B1"
    assert row["region_seq"] == 1


def test_save_keeps_result_as_it_was_before_alignment(db):
    """快照拍的是**进对齐之前**的样子：原始回报的字段全留。"""
    store = PendingStore(db)
    entry = _entry(status=ResultStatus.CORRECTION, value="弱阳性")
    store.save(_record(entry=entry), max_attempts=3)

    payload = json.loads(
        db.query_value("SELECT result_json FROM pending_items")
    )
    assert payload["status"] == ResultStatus.CORRECTION.value
    assert payload["value"] == "弱阳性"
    assert payload["region_seq"] == 1
    assert payload["region_desc"] == "子宫体内侧"
    assert payload["order_no"] == "ORD-EXAMPLE-001"
    assert payload["item_name"] == "CD20 免疫组化"
    assert payload["snapshot_version"] == SNAPSHOT_FORMAT_VERSION


def test_save_counts_attempts_per_accession_and_item(db):
    """尝试次数按「病理号 + 项目码」累计。"""
    store = PendingStore(db)
    first = store.save(_record(), max_attempts=3)
    second = store.save(_record(), max_attempts=3)
    other_item = store.save(_record(item_code="IHC-CK20"), max_attempts=3)

    assert first.attempts == 1
    assert second.attempts == 2
    assert other_item.attempts == 1


def test_save_escalates_after_attempt_limit(db):
    """超过尝试上限转人工处置，并把说明写进快照。"""
    store = PendingStore(db)
    store.save(_record(), max_attempts=1)
    escalated = store.save(_record(), max_attempts=1)

    assert escalated.attempts == 2
    assert escalated.escalated is True
    assert escalated.state == PENDING_STATE_ESCALATED
    assert escalated.note and "转人工处置" in escalated.note
    assert store.count(states=(PENDING_STATE_ESCALATED,)) == 1


# --------------------------------------------------------------- 恢复


def test_restart_recovers_all_pending_items(db, tmp_path):
    """进程重启：全部挂起项恢复，字段与写入时一致。"""
    store = PendingStore(db)
    written = [
        store.save(_record(item_code="IHC-CD20"), max_attempts=3),
        store.save(_record(item_code="IHC-CK20"), max_attempts=3),
        store.save(_record(item_code="IHC-KI67"), max_attempts=3),
    ]
    db.close()

    # 重新开一个连接 —— 相当于进程重启
    reborn = Database(tmp_path / "state.db")
    reborn.init_schema()
    recovered = PendingStore(reborn).recover()

    assert recovered.count == len(written)
    assert recovered.broken_rows == ()
    by_code = {snapshot.item_code: snapshot for snapshot in recovered.snapshots}
    for original in written:
        snapshot = by_code[original.item_code]
        assert snapshot.accession_no == original.accession_no
        assert snapshot.reason is original.reason
        assert snapshot.missing_fields == original.missing_fields
        assert snapshot.candidates == original.candidates
        assert snapshot.attempts == original.attempts
        assert snapshot.state == original.state
        assert snapshot.snapshot_at == original.snapshot_at
        assert snapshot.entry == original.entry
        assert snapshot.block_no == original.block_no
        assert snapshot.source_system == original.source_system
    reborn.close()


def test_recovered_snapshot_keeps_enough_to_resume(db, tmp_path):
    """恢复出来的快照仍带原始结果条目 —— 续跑不必回头翻原始报文。"""
    store = PendingStore(db)
    store.save(_record(candidates=_candidates()), max_attempts=3)
    db.close()

    reborn = Database(tmp_path / "state.db")
    reborn.init_schema()
    snapshot = PendingStore(reborn).recover().snapshots[0]

    assert snapshot.entry is not None
    assert snapshot.entry.block_no == "B1"
    assert snapshot.entry.region_seq == 1
    assert snapshot.entry.region_desc == "子宫体内侧"
    assert snapshot.entry.value == "阳性"
    assert snapshot.entry.status is ResultStatus.FINAL
    assert snapshot.entry.source_system == "ihc-lab"
    assert snapshot.entry.source_file == "oru-001.hl7"
    reborn.close()


def test_recover_returns_records_for_pending_list(db):
    """恢复出来的快照能转成给人看的挂起记录。"""
    store = PendingStore(db)
    store.save(_record(candidates=_candidates()), max_attempts=3)
    snapshot = store.recover().snapshots[0]
    record = snapshot.to_record()

    assert record.accession_no == "ACC-EXAMPLE-0001"
    assert record.item_code == "IHC-CD20"
    assert record.reason is PendingReason.MISSING_BLOCK_NO
    assert record.missing_fields == ("block_no",)
    assert record.candidate_text() == "子宫体内侧/蜡块 B1；宫颈部/蜡块 B2"
    assert record.attempt == 1
    assert record.value == "阳性"
    assert record.source_system == "ihc-lab"


def test_load_by_id_returns_single_snapshot(db):
    """按行号读一条挂起项；没有这一行给 None。"""
    store = PendingStore(db)
    snapshot = store.save(_record(), max_attempts=3)

    loaded = store.load_by_id(snapshot.snapshot_id)
    assert loaded is not None
    assert loaded.accession_no == snapshot.accession_no
    assert store.load_by_id(999999) is None


def test_load_all_filters_by_state(db):
    """按状态过滤：已转人工的行不混在等人补齐的行里。"""
    store = PendingStore(db)
    store.save(_record(item_code="IHC-CD20"), max_attempts=3)
    store.save(_record(item_code="IHC-CK20"), max_attempts=1)
    store.save(_record(item_code="IHC-CK20"), max_attempts=1)

    pending_only = store.load_all(states=(PENDING_STATE_PENDING,))
    assert pending_only.count == 2
    assert store.load_all(states=(PENDING_STATE_ESCALATED,)).count == 1
    assert store.load_all(states=PENDING_STATES).count == 3


# --------------------------------------------------------------- 坏行


def test_broken_row_raises_in_strict_mode(db):
    """缺项即报错：读不出来的行不静默跳过。"""
    db.execute(
        "INSERT INTO pending_items(accession_no, item_code, result_json, missing_fields,"
        " candidates_json, reason, attempts, state, snapshot_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ACC-EXAMPLE-0002",
            "IHC-CD20",
            "{ 这不是合法 JSON",
            "block_no",
            "[]",
            PendingReason.MISSING_BLOCK_NO.value,
            1,
            PENDING_STATE_PENDING,
            "2026-01-01 10:00:00",
        ),
    )

    with pytest.raises(PendingSnapshotError) as excinfo:
        PendingStore(db).load_all()
    assert excinfo.value.row_id == 1
    assert "JSON" in excinfo.value.reason


def test_broken_row_is_reported_not_dropped_in_tolerant_mode(db):
    """宽松模式：坏行进 broken_rows，不悄悄扔掉。"""
    db.execute(
        "INSERT INTO pending_items(accession_no, item_code, result_json, missing_fields,"
        " candidates_json, reason, attempts, state, snapshot_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ACC-EXAMPLE-0002",
            "IHC-CD20",
            "{ 坏 JSON",
            "block_no",
            "[]",
            PendingReason.MISSING_BLOCK_NO.value,
            1,
            PENDING_STATE_PENDING,
            "2026-01-01 10:00:00",
        ),
    )
    store = PendingStore(db)
    store.save(_record(), max_attempts=3)

    loaded = store.load_all(strict=False)
    assert loaded.count == 1
    assert loaded.broken_rows == (1,)


def test_unknown_reason_is_a_broken_row(db):
    """不认识的原因算坏行，不当成「未命中部位」糊弄过去。"""
    db.execute(
        "INSERT INTO pending_items(accession_no, item_code, result_json, missing_fields,"
        " candidates_json, reason, attempts, state, snapshot_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ACC-EXAMPLE-0002",
            "IHC-CD20",
            json.dumps({"accession_no": "ACC-EXAMPLE-0002", "item_code": "IHC-CD20"}),
            "",
            "[]",
            "说不清的原因",
            1,
            PENDING_STATE_PENDING,
            "2026-01-01 10:00:00",
        ),
    )
    with pytest.raises(PendingSnapshotError) as excinfo:
        PendingStore(db).load_all()
    assert "挂起原因不认识" in excinfo.value.reason


# --------------------------------------------------------------- 快照内容


def test_snapshot_payload_round_trip():
    """快照内容 JSON 往返一次，原始结果条目还原得出来。"""
    record = _record(candidates=_candidates())
    payload = json.loads(snapshot_payload(record))
    restored = entry_from_payload(payload)

    assert restored == record.entry


def test_entry_from_payload_gives_none_when_unrecognizable():
    """认不出来就明说认不出来，不拼一个半截的出来。"""
    assert entry_from_payload({}) is None
    assert entry_from_payload({"accession_no": "ACC-EXAMPLE-0001"}) is None
    assert entry_from_payload(
        {"accession_no": "ACC-EXAMPLE-0001", "item_code": "IHC-CD20", "status": "Z"}
    ) is None


def test_snapshot_without_entry_still_round_trips(db):
    """没有原始条目的手工记录也能落库、读回（字段齐全即可）。"""
    store = PendingStore(db)
    record = PendingRecord(
        accession_no="ACC-EXAMPLE-0003",
        item_code="IHC-CD20",
        reason=PendingReason.NO_MATCH,
        missing_fields=("region_seq", "block_no"),
        candidates=(),
        value="阳性",
        source_system="ihc-lab",
        observed_at="20260101103000",
        snapshot_at="2026-01-01 10:00:00",
    )
    store.save(record, max_attempts=3)

    snapshot = store.recover().snapshots[0]
    # 快照里字段齐全就能还原出原始条目（没有的字段留空，不编）
    assert snapshot.entry is not None
    assert snapshot.entry.accession_no == "ACC-EXAMPLE-0003"
    assert snapshot.entry.value == "阳性"
    assert snapshot.entry.source_system == "ihc-lab"
    assert snapshot.entry.block_no is None
    assert snapshot.reason is PendingReason.NO_MATCH
    assert snapshot.missing_fields == ("region_seq", "block_no")
    assert snapshot.snapshot_at == "2026-01-01 10:00:00"


def test_snapshot_has_no_patient_identifiers(db):
    """快照不含姓名 / 身份证 / 联系方式等患者可识别明细。"""
    store = PendingStore(db)
    store.save(_record(candidates=_candidates()), max_attempts=3)
    payload = db.query_value("SELECT result_json FROM pending_items")
    for forbidden in ("patient_name", "id_card", "id_no", "phone", "tel", "姓名", "身份证"):
        assert forbidden not in payload
    # 快照里只有业务标识：病理号、项目码、部位与蜡块号
    assert "ACC-EXAMPLE-0001" in payload
    assert "IHC-CD20" in payload
