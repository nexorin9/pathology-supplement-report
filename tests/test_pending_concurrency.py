"""挂起项并发补齐的互斥与幂等。

两个人同时补同一条挂起项，是院内真会发生的：清单贴在网上，技术员甲乙各看
到一行，都在查蜡块登记本，回来都想把这一行补上。本模块保两件事：

1. **互斥** —— 同一行同一时刻只让一个人续跑。抢不到的人拿到一句可读的
   提示（不是失败，是「等一下」），等对方跑完重试即可；
2. **幂等** —— 同一组补齐值（挂起行 ID + 字段 + 值）重复提交，只生效一次：
   第二次返回上一次的结论，**不产出第二份草稿**。

补齐值登记在互斥锁**之外**（按字段 upsert，本身幂等）：抢锁失败的人补的值
不会丢 —— 这就是「互斥失败不丢补齐值」这一条的全部含义。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from patho_supplement.db import Database
from patho_supplement.models import (
    Block,
    ReportBaseline,
    ResultEntry,
    ResultStatus,
)
from patho_supplement.pending import resume as resume_module
from patho_supplement.pending.fills import parse_fill
from patho_supplement.pending.lock import PendingLock
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import PendingRecord
from patho_supplement.pending.resolve_keys import ResolveKeyLedger
from patho_supplement.pending.resume import (
    PendingLockedError,
    record_fills,
    resolve_key_for,
    resume_pending,
)
from patho_supplement.pending.store import PendingStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 基线：IHC-KI67 只在 B1（子宫体）上申请过；B2（宫颈部）上申请的是 CD20。
BASELINE = ReportBaseline(
    accession_no="ACC-EXAMPLE-0001",
    base_version="base-v3",
    blocks=(
        Block(
            region_seq=1,
            region_desc="子宫体",
            block_no="B1",
            order_no="ORD-EXAMPLE-0001",
            ordered_items=("IHC-KI67",),
        ),
        Block(
            region_seq=2,
            region_desc="宫颈部",
            block_no="B2",
            order_no="ORD-EXAMPLE-0002",
            ordered_items=("IHC-CD20",),
        ),
    ),
)


# --------------------------------------------------------------- 样例与夹具


def _fill(field_name: str, value: str):
    """构造一个补齐值（走词表校验，不手搓对象）。"""
    return parse_fill(f"{field_name}={value}")


def _entry(
    *,
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-KI67",
    value: str | None = "约15%",
    region_seq: int | None = None,
    block_no: str | None = None,
    region_desc: str | None = None,
    observed_at: str = "20260101132000",
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="Ki-67 免疫组化",
        value=value,
        status=ResultStatus.FINAL,
        observed_at=observed_at,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file="oru-ki67.hl7",
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """状态库路径（落文件，多个线程 / 进程各自开连接）。"""
    path = tmp_path / "state.db"
    database = Database(path)
    database.init_schema()
    database.close()
    return path


def _open(db_path: Path) -> Database:
    """开一个连接（调用方负责 close）。"""
    return Database(db_path)


def _pending_row(
    database: Database,
    *,
    entry: ResultEntry | None = None,
    reason: PendingReason = PendingReason.MISSING_BLOCK_NO,
    missing_fields: tuple[str, ...] = ("block_no",),
    candidates: tuple[Block, ...] = BASELINE.blocks,
    max_attempts: int = 3,
) -> int:
    """落一条挂起行，返回行 ID。"""
    source = entry if entry is not None else _entry()
    snapshot = PendingStore(database).save(
        PendingRecord.from_entry(
            source,
            reason,
            missing_fields=missing_fields,
            candidates=candidates,
        ),
        max_attempts=max_attempts,
    )
    return snapshot.snapshot_id


# --------------------------------------------------------------- 互斥


def test_two_threads_resolving_the_same_row_produce_one_draft(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """两个人同时补同一行：只有一个跑成功，草稿只有一份。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    entered = threading.Event()
    release = threading.Event()
    original_align = resume_module.align_entry

    def slow_align(*args, **kwargs):
        """卡在对齐这一步，让另一个线程确定能撞上锁。"""
        entered.set()
        assert release.wait(timeout=10), "持锁线程没有在 10 秒内被放行"
        return original_align(*args, **kwargs)

    monkeypatch.setattr(resume_module, "align_entry", slow_align)

    results: dict[str, tuple[str, object]] = {}

    def worker(name: str, value: str) -> None:
        connection = Database(db_path)
        try:
            record_fills(connection, pending_id=row_id, fills=[_fill("block_no", value)])
            results[name] = (
                "ok",
                resume_pending(
                    connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
                ),
            )
        except PendingLockedError as exc:
            results[name] = ("locked", exc)
        except Exception as exc:  # pragma: no cover - 失败要看得见
            results[name] = ("error", exc)
        finally:
            connection.close()

    first = threading.Thread(target=worker, args=("甲", "B1"), name="resolve-甲")
    first.start()
    assert entered.wait(timeout=10), "甲没有进入对齐环节（锁没抢到？）"

    second = threading.Thread(target=worker, args=("乙", "B1"), name="resolve-乙")
    second.start()
    second.join(timeout=10)
    assert not second.is_alive(), "乙没有在 10 秒内拿到结论"

    release.set()
    first.join(timeout=10)
    assert not first.is_alive(), "甲没有在 10 秒内跑完"

    # 甲跑成了，乙被互斥挡下
    assert results["甲"][0] == "ok", results["甲"]
    assert results["乙"][0] == "locked", results["乙"]

    database = _open(db_path)
    try:
        # 草稿只有一份，并入的也只有一条
        # （B2 的 CD20 还没回来，单独占一行「待回报」—— 占位不是结果）
        assert database.query_value("SELECT COUNT(*) FROM drafts") == 1
        assert (
            database.query_value(
                "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0"
            )
            == 1
        )
        # 挂起行了结，锁已释放
        snapshot = PendingStore(database).load_by_id(row_id)
        assert snapshot.resolved is True
        assert PendingLock(database).holder(row_id) is None
        # 互斥冲突留了一行审计
        conflicts = database.query_value(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'pending_lock_conflict'"
        )
        assert conflicts == 1
    finally:
        database.close()


def test_lock_conflict_message_is_readable_and_says_the_values_are_kept(
    db_path: Path,
):
    """没抢到锁的人拿到一句能照做的提示：等一下重试，值不会丢。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    # 模拟另一个进程正拿着这把锁
    assert PendingLock(database, owner="pid:99999").acquire(row_id).acquired
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        with pytest.raises(PendingLockedError) as excinfo:
            resume_pending(
                connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
            )
    finally:
        connection.close()

    message = str(excinfo.value)
    assert "正在被处理中" in message
    assert "pid:99999" in message
    assert "重试" in message
    assert "不会丢" in message
    # 行号也带上了，便于直接定位到清单上的那一行
    assert str(row_id) in message


def test_lock_conflict_keeps_the_filled_values(db_path: Path):
    """互斥失败不丢补齐值：补的值已经在库里，重试即可。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    assert PendingLock(database, owner="pid:99999").acquire(row_id).acquired
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        with pytest.raises(PendingLockedError):
            resume_pending(
                connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
            )
        # 补齐值原样留在库里
        assert PendingStore(connection).load_fills(row_id) == {"block_no": "B1"}
        # 这一行仍然是挂起（没被当成已对齐）
        snapshot = PendingStore(connection).load_by_id(row_id)
        assert snapshot.resolved is False
        assert snapshot.state == "pending"
    finally:
        connection.close()


def test_lock_is_released_after_a_successful_resume(db_path: Path):
    """跑完就放锁：这一行后面还能再被处理。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        outcome = resume_pending(
            connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
        )
        assert outcome.merged is True
        assert PendingLock(connection).holder(row_id) is None
    finally:
        connection.close()


def test_lock_is_released_when_the_resume_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """续跑中途出错，锁也要放掉 —— 否则这一行就没人能再动了。"""

    def boom(*args, **kwargs):
        raise RuntimeError("对齐环节炸了")

    monkeypatch.setattr(resume_module, "align_entry", boom)

    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        with pytest.raises(RuntimeError):
            resume_pending(
                connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
            )
        assert PendingLock(connection).holder(row_id) is None
    finally:
        connection.close()


def test_a_fresh_lock_is_respected(db_path: Path):
    """锁没过期：别人抢不到，只能等。"""
    database = _open(db_path)
    row_id = _pending_row(database)

    first = PendingLock(database, owner="pid:111")
    acquired = first.acquire(row_id)
    assert acquired.acquired
    token = acquired.token or ""

    second = PendingLock(database, owner="pid:222")
    result = second.acquire(row_id)
    assert result.locked is True
    assert result.holder is not None
    assert result.holder.owner == "pid:111"
    # 释放要凭令牌：别人的令牌放不掉这把锁
    assert second.release(row_id, "别人的令牌") is False
    assert PendingLock(database).holder(row_id) is not None
    # 持有者本人可以放掉
    assert first.release(row_id, token) is True
    assert PendingLock(database).holder(row_id) is None
    database.close()


def test_a_stale_lock_is_taken_over_after_the_ttl(db_path: Path):
    """持锁的进程崩了：锁过期后可被接管，并留一行审计。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.execute(
        "UPDATE pending_items SET lock_token = ?, locked_at = ?, lock_owner = ?"
        " WHERE id = ?",
        ("old-token", "2020-01-01 00:00:00", "pid:1", row_id),
    )
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        outcome = resume_pending(
            connection,
            pending_id=row_id,
            baseline=BASELINE,
            max_attempts=3,
            lock_ttl=300.0,
        )
        assert outcome.merged is True
        taken_over = database_count = connection.query_value(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'pending_lock_taken_over'"
        )
        assert database_count == 1
        assert taken_over == 1
    finally:
        connection.close()


# --------------------------------------------------------------- 幂等


def test_resolve_idempotency_key_repeat_has_no_side_effect(db_path: Path):
    """同一组补齐值重复提交：不产出第二份草稿。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        first = resume_pending(
            connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
        )
        assert first.merged is True
        assert first.idempotent is False

        # 技术员点两下「补齐并续跑」
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        second = resume_pending(
            connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
        )

        assert second.merged is True
        assert second.idempotent is True
        assert second.draft_id == first.draft_id
        assert second.draft_version == first.draft_version
        assert second.draft_entry_count == first.draft_entry_count
        # 草稿仍然只有一份，并入的也只有一条（另一条是「待回报」占位）
        assert connection.query_value("SELECT COUNT(*) FROM drafts") == 1
        assert (
            connection.query_value(
                "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0"
            )
            == 1
        )
        # 幂等台账记着：同一把键提交了两次、生效到第 1 版
        key = resolve_key_for(row_id, [_fill("block_no", "B1")])
        ledger = ResolveKeyLedger(connection)
        assert ledger.submissions(key) == 2
        record = ledger.find(key)
        assert record is not None
        assert record.status == "merged"
        assert record.draft_id == first.draft_id
    finally:
        connection.close()


def test_resolve_idempotency_key_ignores_field_order(db_path: Path):
    """幂等键 = 行 ID + 字段 + 值；字段顺序不影响它。"""
    one = resolve_key_for(1, [_fill("block_no", "B1"), _fill("region_desc", "宫颈")])
    two = resolve_key_for(1, [_fill("region_desc", "宫颈"), _fill("block_no", "B1")])
    assert one == two
    # 值不同、行不同 → 键不同
    assert resolve_key_for(1, [_fill("block_no", "B1")]) != resolve_key_for(
        1, [_fill("block_no", "B2")]
    )
    assert resolve_key_for(1, [_fill("block_no", "B1")]) != resolve_key_for(
        2, [_fill("block_no", "B1")]
    )


def test_a_bare_rerun_on_a_resolved_row_is_still_refused(db_path: Path):
    """不重新提交补齐值就重跑：仍报「已了结」—— 那是手工重跑，不是重复提交。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    connection = _open(db_path)
    try:
        record_fills(connection, pending_id=row_id, fills=[_fill("block_no", "B1")])
        resume_pending(
            connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
        )
        with pytest.raises(Exception) as excinfo:
            resume_pending(
                connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
            )
        assert "已了结" in str(excinfo.value)
        assert connection.query_value("SELECT COUNT(*) FROM drafts") == 1
    finally:
        connection.close()


def test_two_threads_with_different_fills_keep_one_draft(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """两个人补不同的值：仍然只有一份草稿，落败者的补齐值不丢。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    database.close()

    entered = threading.Event()
    release = threading.Event()
    original_align = resume_module.align_entry

    def slow_align(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "持锁线程没有在 10 秒内被放行"
        return original_align(*args, **kwargs)

    monkeypatch.setattr(resume_module, "align_entry", slow_align)

    results: dict[str, tuple[str, object]] = {}

    def worker(name: str, value: str) -> None:
        connection = Database(db_path)
        try:
            record_fills(connection, pending_id=row_id, fills=[_fill("block_no", value)])
            results[name] = (
                "ok",
                resume_pending(
                    connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
                ),
            )
        except PendingLockedError as exc:
            results[name] = ("locked", exc)
        except Exception as exc:  # pragma: no cover - 失败要看得见
            results[name] = ("error", exc)
        finally:
            connection.close()

    first = threading.Thread(target=worker, args=("甲", "B1"), name="resolve-甲")
    first.start()
    assert entered.wait(timeout=10)

    second = threading.Thread(target=worker, args=("乙", "B2"), name="resolve-乙")
    second.start()
    second.join(timeout=10)
    release.set()
    first.join(timeout=10)

    assert results["甲"][0] == "ok", results["甲"]
    assert results["乙"][0] == "locked", results["乙"]

    connection = _open(db_path)
    try:
        # 只有一份草稿、一条并入的条目（另一条是「待回报」占位）
        assert connection.query_value("SELECT COUNT(*) FROM drafts") == 1
        assert (
            connection.query_value(
                "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0"
            )
            == 1
        )
        # 落败者补的值也在库里（后补的覆盖先补的 —— 值本身按字段 upsert）
        assert PendingStore(connection).load_fills(row_id)["block_no"] in ("B1", "B2")
    finally:
        connection.close()


# --------------------------------------------------------------- 命令行


def _run_cli(args: list[str], env_overrides: dict[str, str] | None = None):
    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(PROJECT_ROOT / "tests" / "fixtures" / "inbox"),
        }
    )
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_reports_a_lock_conflict_with_its_own_exit_code(db_path: Path):
    """行正被别人处理：命令行给独立退出码，别把「等一下」当成「失败」。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    assert PendingLock(database, owner="pid:99999").acquire(row_id).acquired
    database.close()

    result = _run_cli(
        [
            "pending",
            "resolve",
            "--id",
            str(row_id),
            "--set",
            "block_no=B1",
            "--state-db",
            str(db_path),
            "--baseline",
            str(PROJECT_ROOT / "tests" / "fixtures" / "inbox" / "baseline.json"),
        ]
    )

    assert result.returncode == 4, result.stderr
    assert "行已被处理" in result.stderr
    assert "重试" in result.stderr

    # 补齐值没丢
    connection = _open(db_path)
    try:
        assert PendingStore(connection).load_fills(row_id) == {"block_no": "B1"}
    finally:
        connection.close()


def test_pending_list_marks_a_locked_row_as_in_progress(db_path: Path):
    """清单上看得出「这一行正有人在处理」，技术员就不会重复去补。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    assert PendingLock(database, owner="pid:99999").acquire(row_id).acquired
    database.close()

    result = _run_cli(["pending", "list", "--state-db", str(db_path)])
    assert result.returncode == 0, result.stderr
    assert "处理中" in result.stdout


def test_pending_list_shows_the_row_as_waiting_when_nobody_holds_the_lock(
    db_path: Path,
):
    """没人占锁时，行还是「等人补齐」—— 互斥标记不污染清单。"""
    database = _open(db_path)
    _pending_row(database)
    database.close()

    result = _run_cli(["pending", "list", "--state-db", str(db_path)])
    assert result.returncode == 0, result.stderr
    assert "等人补齐" in result.stdout
    assert "处理中" not in result.stdout


# --------------------------------------------------------------- 护栏


def test_the_lock_conflict_audit_row_holds_no_patient_identifiers(db_path: Path):
    """互斥冲突的审计行不带患者可识别明细。"""
    database = _open(db_path)
    row_id = _pending_row(database)
    assert PendingLock(database, owner="pid:99999").acquire(row_id).acquired
    database.close()

    connection = _open(db_path)
    try:
        with pytest.raises(PendingLockedError):
            resume_pending(
                connection, pending_id=row_id, baseline=BASELINE, max_attempts=3
            )
        rows = connection.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'pending_lock_conflict'"
        )
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["pending_id"] == row_id
        assert detail["holder"]["owner"] == "pid:99999"
        blob = json.dumps(detail, ensure_ascii=False)
        for word in ("姓名", "身份证", "手机号", "患者"):
            assert word not in blob
    finally:
        connection.close()
