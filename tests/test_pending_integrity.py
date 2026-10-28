"""续跑前的完整性校验。

对齐没对上的结果被拍成快照落库，人工补齐字段后再从挂起的那一步接着跑。
**续跑之前必须先验一遍快照**：缺了节点就硬报错，不静默跳过，也不当成
「已对齐」放过去 —— 否则报告里会少一项而没人知道，或者补齐流程没有落点
只能靠猜（猜错就是错绑定）。

这组用例守三件事：

1. 缺节点 / 内容不可解析 → 报错，报错信息带**挂起行 ID** 与**缺失项清单**；
2. 报错时挂起行**原样留在库里**（不删、不改状态、不改尝试次数），也不生成草稿；
3. 拒绝这件事本身留一行审计，让「为什么没续跑」可查。
"""

from __future__ import annotations

import json

import pytest

from patho_supplement.db import Database
from patho_supplement.models import Block, ResultEntry, ResultStatus
from patho_supplement.pending.integrity import (
    NODE_CANDIDATES,
    NODE_ENTRY,
    NODE_MISSING_FIELDS,
    NODE_PAYLOAD,
    IntegrityReport,
    SnapshotIntegrityError,
    assert_snapshot_resumable,
    assert_snapshots_resumable,
    check_snapshot,
    check_snapshots,
    load_resumable,
)
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import PendingRecord
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


@pytest.fixture()
def store(db):
    return PendingStore(db)


def _corrupt(db: Database, row_id: int, *, result_json: str | None = None, **columns) -> None:
    """把某一行挂起项改成坏数据（模拟库被改坏 / 快照半截）。"""
    assignments = []
    params: list[object] = []
    if result_json is not None:
        assignments.append("result_json = ?")
        params.append(result_json)
    for column, value in columns.items():
        assignments.append(f"{column} = ?")
        params.append(value)
    params.append(row_id)
    db.execute(
        f"UPDATE pending_items SET {', '.join(assignments)} WHERE id = ?", params
    )


def _pending_row(db: Database, row_id: int):
    return db.query_one(
        "SELECT accession_no, item_code, result_json, missing_fields, candidates_json,"
        " reason, attempts, state, snapshot_at FROM pending_items WHERE id = ?",
        (row_id,),
    )


# --------------------------------------------------------------- 齐备的快照


def test_complete_snapshot_passes(store):
    """三个节点齐备 → 校验通过，续跑放行。"""
    snapshot = store.save(
        _record(missing_fields=("block_no",), candidates=_candidates()), max_attempts=3
    )

    report = check_snapshot(snapshot)

    assert report.ok is True
    assert report.missing_nodes == ()
    assert report.snapshot_id == snapshot.snapshot_id
    assert report.accession_no == "ACC-EXAMPLE-0001"
    assert report.item_code == "IHC-CD20"
    assert_snapshot_resumable(snapshot)


def test_load_resumable_returns_snapshot_when_complete(store):
    """从状态库取：齐备的快照原样放行。"""
    store.save(_record(candidates=_candidates()), max_attempts=3)

    snapshots = load_resumable(store)

    assert len(snapshots) == 1
    assert snapshots[0].entry is not None
    assert snapshots[0].entry.value == "阳性"


# --------------------------------------------------------------- 缺节点


def test_missing_entry_is_rejected(store, db):
    """快照里读不出原始结果条目 → 报错，点名「未对齐结果」。"""
    snapshot = store.save(_record(), max_attempts=3)
    # 结果内容被改成认不出来的样子：entry 还原不出来
    _corrupt(db, snapshot.snapshot_id, result_json=json.dumps({"accession_no": "ACC-EXAMPLE-0001"}))

    broken = store.load_by_id(snapshot.snapshot_id)

    report = check_snapshot(broken)
    assert report.ok is False
    assert NODE_ENTRY in report.missing_nodes
    assert "未对齐结果" in report.describe()
    with pytest.raises(SnapshotIntegrityError) as excinfo:
        assert_snapshot_resumable(broken)
    assert str(snapshot.snapshot_id) in str(excinfo.value)


def test_missing_fields_is_rejected(store):
    """原因说要补字段，快照却没记要补哪些 → 报错，点名「缺失字段」。"""
    snapshot = store.save(
        _record(reason=PendingReason.MISSING_BLOCK_NO, missing_fields=()), max_attempts=3
    )

    report = check_snapshot(snapshot)

    assert report.ok is False
    assert NODE_MISSING_FIELDS in report.missing_nodes
    assert "缺失字段" in report.describe()


def test_missing_fields_allowed_when_nothing_to_fill(store):
    """已取消 / 已终止本来就没有字段可补 → 不算缺。"""
    snapshot = store.save(
        _record(reason=PendingReason.ITEM_CANCELLED, missing_fields=()), max_attempts=3
    )

    assert check_snapshot(snapshot).ok is True


def test_missing_candidates_rejected_for_ambiguous(store):
    """同分多候选却一个候选都没记 → 报错，点名「候选列表」。"""
    snapshot = store.save(
        _record(
            reason=PendingReason.AMBIGUOUS,
            missing_fields=("block_no", "region_desc"),
            candidates=(),
        ),
        max_attempts=3,
    )

    report = check_snapshot(snapshot)

    assert report.ok is False
    assert NODE_CANDIDATES in report.missing_nodes
    assert "候选列表" in report.describe()


def test_missing_candidates_allowed_for_other_reasons(store):
    """别的原因没有候选是正常的（连基线都没取到时本来就没有）。"""
    snapshot = store.save(
        _record(reason=PendingReason.MISSING_BLOCK_NO, missing_fields=("block_no",), candidates=()),
        max_attempts=3,
    )

    assert check_snapshot(snapshot).ok is True


def test_report_lists_every_issue(store):
    """一条快照同时缺多项 → 一次全报出来，不逐个来回试。"""
    snapshot = store.save(
        _record(reason=PendingReason.AMBIGUOUS, missing_fields=(), candidates=()), max_attempts=3
    )
    _corrupt(
        store.database,
        snapshot.snapshot_id,
        result_json=json.dumps({"accession_no": "ACC-EXAMPLE-0001"}),
    )
    broken = store.load_by_id(snapshot.snapshot_id)

    report = check_snapshot(broken)

    assert report.ok is False
    assert set(report.missing_nodes) == {NODE_ENTRY, NODE_MISSING_FIELDS, NODE_CANDIDATES}
    assert len(report.issues) == 3


# --------------------------------------------------------------- 内容不可解析


def test_unparsable_payload_is_rejected(store, db):
    """快照内容坏成非法 JSON → 报错，点名「快照内容」并给出解析原因。"""
    snapshot = store.save(_record(), max_attempts=3)
    _corrupt(db, snapshot.snapshot_id, result_json="{不是 JSON")

    with pytest.raises(SnapshotIntegrityError) as excinfo:
        load_resumable(store)

    error = excinfo.value
    assert error.row_ids == (snapshot.snapshot_id,)
    assert NODE_PAYLOAD in error.missing_nodes
    assert "快照内容" in str(error)
    assert "JSON" in str(error)


def test_unknown_reason_is_rejected(store, db):
    """挂起原因不认识 → 一样拒，不糊弄成别的原因。"""
    snapshot = store.save(_record(), max_attempts=3)
    _corrupt(db, snapshot.snapshot_id, reason="没见过的原因")

    with pytest.raises(SnapshotIntegrityError) as excinfo:
        load_resumable(store)

    assert NODE_PAYLOAD in excinfo.value.missing_nodes


def test_missing_row_is_rejected(store):
    """要续跑的行在库里根本没有 → 报错，不当成「没有待办」。"""
    with pytest.raises(SnapshotIntegrityError) as excinfo:
        load_resumable(store, snapshot_ids=[999])

    assert excinfo.value.row_ids == (999,)
    assert "没有这一行挂起项" in str(excinfo.value)


# --------------------------------------------------------------- 报错时的库状态


def test_pending_row_survives_rejection(store, db):
    """校验没过：挂起行原样留在库里（不删、不改状态、不改尝试次数）。"""
    snapshot = store.save(_record(), max_attempts=3)
    before = _pending_row(db, snapshot.snapshot_id)
    _corrupt(db, snapshot.snapshot_id, result_json=json.dumps({"accession_no": "ACC-EXAMPLE-0001"}))

    with pytest.raises(SnapshotIntegrityError):
        load_resumable(store)

    after = _pending_row(db, snapshot.snapshot_id)
    assert after is not None
    assert after["attempts"] == before["attempts"]
    assert after["state"] == before["state"]
    assert after["snapshot_at"] == before["snapshot_at"]
    assert after["reason"] == before["reason"]
    assert after["result_json"] == db.query_value(
        "SELECT result_json FROM pending_items WHERE id = ?", (snapshot.snapshot_id,)
    )


def test_rejection_does_not_create_draft_or_aligned_item(store, db):
    """校验没过：不得当成已对齐、不得凭空生成草稿。"""
    snapshot = store.save(_record(), max_attempts=3)
    _corrupt(db, snapshot.snapshot_id, result_json="{坏掉")

    with pytest.raises(SnapshotIntegrityError):
        load_resumable(store)

    assert db.query_value("SELECT COUNT(*) FROM drafts", default=0) == 0
    assert db.query_value("SELECT COUNT(*) FROM draft_entries", default=0) == 0
    aligned = db.query_value(
        "SELECT COUNT(*) FROM check_items WHERE state IN ('aligned', 'merged')", default=0
    )
    assert aligned == 0


def test_rejection_writes_audit_row(store, db):
    """拒绝本身留一行审计：谁没续跑、缺了哪些节点，都查得到。"""
    snapshot = store.save(_record(), max_attempts=3)
    _corrupt(db, snapshot.snapshot_id, result_json="{坏掉")

    with pytest.raises(SnapshotIntegrityError):
        load_resumable(store)

    row = db.query_one(
        "SELECT category, event, detail_json FROM audit_log"
        " WHERE event = 'pending_integrity_rejected'"
    )
    assert row is not None
    assert row["category"] == "state"
    detail = json.loads(row["detail_json"])
    assert detail["rejected_rows"] == [snapshot.snapshot_id]
    assert NODE_PAYLOAD in detail["missing_nodes"]


# --------------------------------------------------------------- 批量与报错信息


def test_batch_reports_only_broken_rows(store, db):
    """一批挂起里有一条坏 → 整批不放行，但缺失清单只列坏的那条。"""
    good = store.save(_record(candidates=_candidates()), max_attempts=3)
    bad = store.save(
        _record(item_code="IHC-CK20", reason=PendingReason.AMBIGUOUS, missing_fields=("block_no",), candidates=()),
        max_attempts=3,
    )
    _corrupt(db, bad.snapshot_id, result_json="{坏掉")

    with pytest.raises(SnapshotIntegrityError) as excinfo:
        load_resumable(store)

    error = excinfo.value
    assert error.row_ids == (bad.snapshot_id,)
    assert good.snapshot_id not in error.row_ids
    # 好行没被静默跳过：整批不放行，调用方不会误以为这批都续跑了
    assert "不能续跑" in str(error)


def test_error_message_carries_row_id_and_missing_nodes(store):
    """报错信息带挂起行 ID 与缺失项清单，便于直接定位到哪一行。"""
    snapshot = store.save(
        _record(reason=PendingReason.AMBIGUOUS, missing_fields=(), candidates=()), max_attempts=3
    )

    with pytest.raises(SnapshotIntegrityError) as excinfo:
        assert_snapshots_resumable([snapshot])

    error = excinfo.value
    message = str(error)
    assert f"第 {snapshot.snapshot_id} 行" in message
    assert "ACC-EXAMPLE-0001" in message
    assert "IHC-CD20" in message
    assert set(error.missing_nodes) == {NODE_MISSING_FIELDS, NODE_CANDIDATES}


def test_check_snapshots_keeps_order(store):
    """逐条校验的顺序与传入顺序一致，便于对着清单看。"""
    first = store.save(_record(item_code="IHC-CD20"), max_attempts=3)
    second = store.save(_record(item_code="IHC-CK20"), max_attempts=3)

    reports = check_snapshots([first, second])

    assert [report.snapshot_id for report in reports] == [first.snapshot_id, second.snapshot_id]
    assert all(isinstance(report, IntegrityReport) for report in reports)


def test_report_as_dict_is_serializable(store):
    """校验结论可打印进摘要与审计。"""
    snapshot = store.save(_record(), max_attempts=3)

    payload = check_snapshot(snapshot).as_dict()

    assert payload["ok"] is True
    assert payload["missing_nodes"] == []
    assert json.dumps(payload, ensure_ascii=False)
