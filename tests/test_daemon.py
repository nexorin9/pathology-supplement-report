"""守护主循环完整化：单实例锁 + 定时兜底扫描 + 中断恢复。

守护进程是本产品的**常驻形态**：盯住结果投递目录，回报到达即处理并回写
病理系统。院内机的现实约束有三条，这三条各对应一组用例：

1. **一个目录只跑一个实例** —— 两个实例会互相抢文件、抢回写。第二个实例
   启动即退出，并说清楚「谁占着、锁在哪、怎么办」。
2. **目录长时间没动静也要兜底扫一遍** —— 机器重启、进程被杀之后投进来的
   文件不能没人管；兜底扫描的间隔可配。
3. **重启不丢东西** —— 挂起项、幂等键、回写队列、目录里没归档的报文，
   重启后都接得回来；崩在续跑中间的挂起行锁也该被接管。

另有一条：**常驻进程的内存不能跟着处理的份数一起涨** —— 每轮结束就撒手，
历史产出对象只留最近几轮。

替身（本地病理系统）只出现在**测试数据层**（标准库 ``http.server``），
产品代码走的是与生产完全相同的一条路径。
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.config import Config, ConfigError
from patho_supplement.compose.pathology_client import PathologyClient
from patho_supplement.daemon import Daemon, SingleInstanceLock
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.logging_setup import LOGGER_NAME
from patho_supplement.recovery import expired_lock_rows, recover_state

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 替身认识的病理号基线（脱敏样例同一套字段词汇）。
BASELINES = {
    "ACC-EXAMPLE-0001": {
        "accession_no": "ACC-EXAMPLE-0001",
        "base_version": "base-v3",
        "blocks": [
            {
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "order_no": "ORD-EXAMPLE-0001",
                "ordered_items": ["IHC-CD3", "IHC-CD20"],
            },
            {
                "region_seq": 2,
                "region_desc": "宫颈",
                "block_no": "B2",
                "order_no": "ORD-EXAMPLE-0002",
                "ordered_items": ["IHC-CD20", "IHC-CK7"],
            },
        ],
    },
    "ACC-POST-FAIL": {
        "accession_no": "ACC-POST-FAIL",
        "base_version": "base-v1",
        "blocks": [
            {
                "region_seq": 1,
                "region_desc": "乳腺",
                "block_no": "B5",
                "order_no": "ORD-EXAMPLE-0003",
                "ordered_items": ["IHC-ER"],
            }
        ],
    },
    "ACC-FLAKY": {
        "accession_no": "ACC-FLAKY",
        "base_version": "base-v1",
        "blocks": [
            {
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "order_no": "ORD-EXAMPLE-0004",
                "ordered_items": ["IHC-CD3"],
            }
        ],
    },
}


class _FakePathology:
    """本地病理系统替身。

    ``fail_get`` 里的病理号取基线一律 500；``fail_post`` 里的病理号回写一律
    500（基线能取到、草稿写不回去 —— 用来看回写队列与中断恢复）。
    """

    def __init__(self) -> None:
        self.fail_get: set[str] = set()
        self.fail_post: set[str] = set()
        self.requests: list[tuple[str, str, bytes]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def client(self, *, token: str = "test-token", timeout: float = 2.0) -> PathologyClient:
        """出站客户端（产品代码本体，测试与生产同一条路径）。"""
        return PathologyClient(self.base_url, token, timeout=timeout)

    def _make_handler(self):
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                fake.requests.append(("GET", self.path, b""))
                accession = self.path.strip("/").split("/")[3]
                if accession in fake.fail_get:
                    self._respond(500, {"message": "病理系统内部错误"})
                    return
                baseline = BASELINES.get(accession)
                if baseline is None:
                    self._respond(404, {"message": "报告不存在"})
                    return
                self._respond(200, baseline)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append(("POST", self.path, body))
                accession = self.path.strip("/").split("/")[3]
                if accession in fake.fail_post:
                    self._respond(500, {"message": "病理系统内部错误"})
                    return
                self._respond(
                    200,
                    {
                        "status": "accepted",
                        "version": "draft-v2",
                        "draft_id": f"SD-EXAMPLE-{accession[-4:]}",
                    },
                )

            def _respond(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _restore_logging():
    """本模块会起守护进程（命令行会改装日志器）—— 用完还原，
    免得影响别的测试文件。"""
    logger = logging.getLogger(LOGGER_NAME)
    before_handlers = list(logger.handlers)
    before_propagate = logger.propagate
    before_level = logger.level
    yield
    for handler in list(logger.handlers):
        if handler not in before_handlers:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 清理不该让测试失败
                pass
    logger.handlers = list(before_handlers)
    logger.propagate = before_propagate
    logger.setLevel(before_level)


@pytest.fixture()
def fake_pathology():
    fake = _FakePathology()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    directory = tmp_path / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class _RecordingWatcher(InboxWatcher):
    """真实监听器 + 记录每次扫描是不是「兜底强制扫描」。

    兜底扫描与普通扫描的差别就在 ``scan(force=...)`` 这一个参数上；
    记录下来才能断言「间隔到了确实强制扫了」。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.force_flags: list[bool] = []

    def scan(self, *, force: bool = False, now: float | None = None) -> list[Path]:
        self.force_flags.append(force)
        return super().scan(force=force, now=now)


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


def _daemon(
    config: Config,
    fake_pathology: _FakePathology,
    inbox: Path,
    tmp_path: Path,
    messages: list[str],
    *,
    watcher: InboxWatcher | None = None,
) -> Daemon:
    watch = watcher or InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=fake_pathology.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watch,
        output_dir=tmp_path / "out",
        echo=messages.append,
    )


def _write_result(
    inbox: Path,
    name: str,
    accession: str,
    *,
    item_code: str = "IHC-CD3",
    block_no: str = "B1",
    region_desc: str = "子宫体",
    region_seq: str = "1",
) -> Path:
    """写一份脱敏的 ORU^R01 结果回报（类目化占位，不含患者标识）。

    项目码决定这份回报能不能对上：基线里**没申请过**的项目码（``IHC-KI67``）
    才会落进挂起清单 —— 已申请过的项目码会正常并入草稿。
    """
    path = inbox / name
    path.write_text(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|M1|P|2.5.1\r\n"
        f"OBR|1|{accession}|ORD-EXAMPLE-0001|{item_code}^CD3^L|||20260101120000\r\n"
        f"ZBR|{block_no}|{region_desc}|{region_seq}\r\n"
        f"OBX|1|ST|{item_code}^CD3^L||阴性||||||F|||20260101130000\r\n",
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------ 单实例锁


def test_second_instance_is_refused_with_readable_hint(
    fake_pathology, inbox, tmp_path, capsys
):
    """第二个实例启动即退出，并说清楚「谁占着、锁在哪、怎么办」。"""
    from patho_supplement.cli import EXIT_ALREADY_RUNNING, main

    db_path = tmp_path / "state.db"
    lock_path = tmp_path / "patho_supplement.lock"
    env = {
        "PATHOLOGY_BASE_URL": fake_pathology.base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    import os

    for key, value in env.items():
        os.environ[key] = value

    holder = SingleInstanceLock(lock_path)
    assert holder.acquire() is True
    try:
        code = main(
            [
                "daemon",
                "--once",
                "--inbox",
                str(inbox),
                "--state-db",
                str(db_path),
                "--lock-file",
                str(lock_path),
            ]
        )
    finally:
        holder.release()

    captured = capsys.readouterr()
    assert code == EXIT_ALREADY_RUNNING
    assert "已有一个守护进程在运行" in captured.err
    assert str(lock_path) in captured.err
    assert "占用进程号" in captured.err
    assert "--lock-file" in captured.err

    # 冲突留痕：这个目录今天被启动过几次、每次都被谁占着，要能查。
    with Database(db_path) as db:
        rows = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'daemon_lock_conflict'"
        )
    assert len(rows) == 1
    assert str(lock_path) in rows[0]["detail_json"]


def test_lock_is_released_after_daemon_stops(fake_pathology, inbox, tmp_path):
    """进程退出后锁要放开 —— 否则重启自己都起不来。"""
    from patho_supplement.cli import main

    db_path = tmp_path / "state.db"
    lock_path = tmp_path / "patho_supplement.lock"
    import os

    for key, value in {
        "PATHOLOGY_BASE_URL": fake_pathology.base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }.items():
        os.environ[key] = value

    code = main(
        [
            "daemon",
            "--once",
            "--inbox",
            str(inbox),
            "--state-db",
            str(db_path),
            "--lock-file",
            str(lock_path),
        ]
    )
    assert code == 0

    again = SingleInstanceLock(lock_path)
    assert again.acquire() is True
    again.release()


def test_lock_file_defaults_to_state_db_directory(fake_pathology, inbox, tmp_path):
    """不给 --lock-file 时锁放在状态库同级 —— 同一状态库自然共用一把锁。"""
    from patho_supplement.cli import _lock_file_path

    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    args = type("Args", (), {"lock_file": None})()
    assert _lock_file_path(args, config) == tmp_path / "patho_supplement.lock"

    config2 = _config(
        inbox,
        fake_pathology.base_url,
        tmp_path / "state.db",
        PATHOLOGY_LOCK_FILE=str(tmp_path / "custom.lock"),
    )
    assert _lock_file_path(args, config2) == tmp_path / "custom.lock"


# ------------------------------------------------------------------ 兜底扫描


def test_fallback_interval_is_configurable():
    """兜底扫描间隔走配置面：缺项用缺省值，越界直接拒启。"""
    assert Config.from_env({}).watch_fallback_interval == 60.0
    assert (
        Config.from_env({"WATCH_FALLBACK_INTERVAL": "15"}).watch_fallback_interval
        == 15.0
    )
    for bad in ("0", "-1", "abc", "86401"):
        with pytest.raises(ConfigError) as excinfo:
            Config.from_env({"WATCH_FALLBACK_INTERVAL": bad})
        assert "WATCH_FALLBACK_INTERVAL" in str(excinfo.value)


def test_fallback_scan_forces_directory_scan_when_interval_elapsed(
    fake_pathology, inbox, tmp_path
):
    """间隔到了就强制扫一遍 —— 停机期间投进来的文件要有人接。"""
    watcher = _RecordingWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [], watcher=watcher)

    daemon.run_forever(max_cycles=3, fallback_interval=0.0)
    daemon.close()

    assert watcher.force_flags == [True, True, True]


def test_no_forced_scan_before_fallback_interval(
    fake_pathology, inbox, tmp_path
):
    """间隔没到就不强制扫 —— 平时按落稳判定读就够了。"""
    watcher = _RecordingWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [], watcher=watcher)

    daemon.run_forever(max_cycles=3, fallback_interval=3600.0)
    daemon.close()

    assert watcher.force_flags == [False, False, False]


def test_run_forever_uses_config_fallback_interval(
    fake_pathology, inbox, tmp_path
):
    """不给参数时兜底间隔取配置面 —— 配置面是唯一的口径来源。"""
    watcher = _RecordingWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    config = _config(
        inbox,
        fake_pathology.base_url,
        tmp_path / "state.db",
        WATCH_FALLBACK_INTERVAL="1",
    )
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [], watcher=watcher)

    daemon.run_forever(max_cycles=2)
    daemon.close()

    # 间隔 1 秒、两轮跑得比 1 秒快 —— 这一轮不会强制扫。
    assert watcher.force_flags == [False, False]


def test_doctor_prints_fallback_interval(fake_pathology, capsys, monkeypatch):
    from patho_supplement.cli import main

    monkeypatch.setenv("PATHOLOGY_BASE_URL", fake_pathology.base_url)
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "test-token")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", "/tmp/inbox")
    monkeypatch.setenv("WATCH_FALLBACK_INTERVAL", "45")

    code = main(["doctor"])
    captured = capsys.readouterr().out
    assert code == 0
    assert "WATCH_FALLBACK_INTERVAL" in captured
    assert "45" in captured


def test_cli_rejects_non_positive_fallback_interval(fake_pathology, inbox, tmp_path):
    from patho_supplement.cli import main

    import os

    for key, value in {
        "PATHOLOGY_BASE_URL": fake_pathology.base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(tmp_path / "state.db"),
    }.items():
        os.environ[key] = value

    code = main(
        [
            "daemon",
            "--once",
            "--inbox",
            str(inbox),
            "--state-db",
            str(tmp_path / "state.db"),
            "--fallback-interval",
            "0",
        ]
    )
    assert code == 1


# ------------------------------------------------------------------ 中断恢复


def test_restart_restores_pending_items_and_writeback_queue(
    fake_pathology, inbox, tmp_path
):
    """重启后挂起项条数与回写队列条数不变 —— 丢一条就是丢一份报告。"""
    db_path = tmp_path / "state.db"
    # 一份对不上部位归属的（挂起）+ 一份回写失败的（进回写队列）
    _write_result(inbox, "no-block.hl7", "ACC-EXAMPLE-0001", item_code="IHC-KI67", block_no="")
    _write_result(inbox, "post-fail.hl7", "ACC-POST-FAIL", item_code="IHC-ER", block_no="B5", region_desc="乳腺")
    fake_pathology.fail_post.add("ACC-POST-FAIL")

    config = _config(inbox, fake_pathology.base_url, db_path)
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)
    first = daemon.run_once()
    daemon.close()

    assert first.pending_count == 1
    with Database(db_path) as db:
        pending_before = db.query_value("SELECT COUNT(*) FROM pending_items")
        queue_before = db.query_value("SELECT COUNT(*) FROM writeback_queue")
        keys_before = db.query_value("SELECT COUNT(*) FROM idempotency_keys")
    assert pending_before == 1
    assert queue_before == 1

    # 「重启」：新进程、同一个状态库
    messages2: list[str] = []
    daemon2 = _daemon(config, fake_pathology, inbox, tmp_path, messages2)
    report = daemon2.recover()
    daemon2.close()

    assert report.pending_items == 1
    assert report.writeback_pending == 1
    assert report.idempotency_keys == keys_before
    assert any("启动恢复" in message for message in messages2)

    with Database(db_path) as db:
        assert db.query_value("SELECT COUNT(*) FROM pending_items") == pending_before
        assert db.query_value("SELECT COUNT(*) FROM writeback_queue") == queue_before
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == keys_before


def test_recovery_writes_audit_row(fake_pathology, inbox, tmp_path):
    """恢复这一动作本身留痕 —— 事后能答「重启之后手上还剩多少活」。"""
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.recover()
    daemon.close()

    with Database(db_path) as db:
        rows = db.query_all(
            "SELECT category, detail_json FROM audit_log WHERE event = 'daemon_recovered'"
        )
    assert len(rows) == 1
    assert rows[0]["category"] == "runtime"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["pending_items"] == 0
    assert detail["leftover_files"] == 0


def test_restart_keeps_unprocessed_report_file(fake_pathology, inbox, tmp_path):
    """取基线失败时报文留在原地 —— 重启后还在，下一轮接着处理。"""
    db_path = tmp_path / "state.db"
    fake_pathology.fail_get.add("ACC-FLAKY")
    _write_result(inbox, "flaky.hl7", "ACC-FLAKY")

    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    target = inbox / "flaky.hl7"
    assert target.exists(), "取基线失败时报文必须留在原地"

    # 「重启」：新进程接手，病理系统这会儿恢复了
    messages: list[str] = []
    fake_pathology.fail_get.discard("ACC-FLAKY")
    daemon2 = _daemon(config, fake_pathology, inbox, tmp_path, messages)
    report = daemon2.recover()
    second = daemon2.run_once()
    daemon2.close()

    assert report.leftover_files == 1, "重启时目录里没归档的报文要报出来"
    assert second.drafts_written == 1
    assert not target.exists()
    assert (inbox / "processed" / "flaky.hl7").exists()


def test_recovery_releases_expired_pending_lock(fake_pathology, inbox, tmp_path):
    """崩掉的进程留下的过期锁，重启后要接管 —— 那一行不能干等一个 TTL。"""
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        # 造一行挂起项，并给它挂一把过期锁（占用时间在 TTL 之前）
        db.execute(
            "INSERT INTO pending_items"
            "(accession_no, item_code, result_json, missing_fields, candidates_json,"
            " reason, attempts, state, snapshot_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ACC-EXAMPLE-0001",
                "IHC-KI67",
                "{}",
                "block_no",
                "[]",
                "缺蜡块号",
                1,
                "pending",
                "2026-01-15 10:00:00",
            ),
        )
        row_id = int(db.query_value("SELECT MAX(id) FROM pending_items"))
        db.execute(
            "UPDATE pending_items SET lock_token = ?, locked_at = ?, lock_owner = ?"
            " WHERE id = ?",
            ("dead-token", "2020-01-01 00:00:00", "pid:999999", row_id),
        )

    with Database(db_path) as db:
        expired = expired_lock_rows(db, ttl=config.pending_lock_ttl)
    assert expired == [row_id]

    daemon2 = _daemon(config, fake_pathology, inbox, tmp_path, [])
    report = daemon2.recover()
    daemon2.close()

    assert report.stale_locks_released == 1
    with Database(db_path) as db:
        holder = db.query_one(
            "SELECT lock_token FROM pending_items WHERE id = ?", (row_id,)
        )
    assert holder["lock_token"] is None


def test_recovery_keeps_fresh_lock(fake_pathology, inbox, tmp_path):
    """没过期的锁不动 —— 别的进程可能正在补齐这一行。"""
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        db.execute(
            "INSERT INTO pending_items"
            "(accession_no, item_code, result_json, missing_fields, candidates_json,"
            " reason, attempts, state, snapshot_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ACC-EXAMPLE-0001",
                "IHC-KI67",
                "{}",
                "block_no",
                "[]",
                "缺蜡块号",
                1,
                "pending",
                "2026-01-15 10:00:00",
            ),
        )
        row_id = int(db.query_value("SELECT MAX(id) FROM pending_items"))
        db.execute(
            "UPDATE pending_items SET lock_token = ?, locked_at = ?, lock_owner = ?"
            " WHERE id = ?",
            ("live-token", "2999-01-01 00:00:00", "pid:1", row_id),
        )

    daemon2 = _daemon(config, fake_pathology, inbox, tmp_path, [])
    report = daemon2.recover()
    daemon2.close()

    assert report.stale_locks_released == 0
    with Database(db_path) as db:
        holder = db.query_one(
            "SELECT lock_token FROM pending_items WHERE id = ?", (row_id,)
        )
    assert holder["lock_token"] == "live-token"


def test_recovery_does_not_call_outbound(fake_pathology, inbox, tmp_path):
    """恢复只是把状态接回来：不取基线、不回写、不发通知。"""
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.recover()
    daemon.close()

    assert fake_pathology.requests == []


def test_recover_state_reports_broken_rows_without_raising(
    fake_pathology, inbox, tmp_path
):
    """状态库里有一行坏了，守护进程照样起得来 —— 那一行报出来就行。"""
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        db.execute(
            "INSERT INTO pending_items"
            "(accession_no, item_code, result_json, missing_fields, candidates_json,"
            " reason, attempts, state, snapshot_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ACC-EXAMPLE-0001",
                "IHC-KI67",
                "{不是合法 JSON",
                "block_no",
                "[]",
                "缺蜡块号",
                1,
                "pending",
                "2026-01-15 10:00:00",
            ),
        )
        broken_id = int(db.query_value("SELECT MAX(id) FROM pending_items"))

    with Database(db_path) as db:
        report = recover_state(db, max_attempts=config.pending_max_attempts)
    assert report.broken_rows == (broken_id,)
    assert report.pending_items == 0


# ------------------------------------------------------------------ 内存不涨


def test_run_forever_keeps_only_recent_rounds(fake_pathology, inbox, tmp_path):
    """常驻进程不攒历史产出对象 —— 跑几天下来内存不能跟着涨。"""
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    rounds = daemon.run_forever(max_cycles=5)
    daemon.close()

    assert len(rounds) == 1, "默认只保留最近一轮的产出"


def test_keep_rounds_is_configurable(fake_pathology, inbox, tmp_path):
    """要多留几轮可以，但留多少是显式说了算。"""
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    rounds = daemon.run_forever(max_cycles=5, keep_rounds=3)
    daemon.close()

    assert len(rounds) == 3


def test_run_once_releases_round_state(fake_pathology, inbox, tmp_path):
    """一轮结束就撒手：中间对象不留在守护进程里，状态只落 SQLite。"""
    _write_result(inbox, "one.hl7", "ACC-EXAMPLE-0001")
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()

    assert daemon._round_orders == {}
    assert daemon._round_outcome is None
    daemon.close()


def test_state_survives_across_daemon_instances(fake_pathology, inbox, tmp_path):
    """状态只落 SQLite —— 换个进程实例接上，手里还是那些活。"""
    db_path = tmp_path / "state.db"
    _write_result(inbox, "no-block.hl7", "ACC-EXAMPLE-0001", item_code="IHC-KI67", block_no="")
    config = _config(inbox, fake_pathology.base_url, db_path)

    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    daemon2 = _daemon(config, fake_pathology, inbox, tmp_path, [])
    report = daemon2.recover()
    daemon2.close()

    assert report.pending_items == 1
    with Database(db_path) as db:
        assert db.query_value("SELECT COUNT(*) FROM pending_items") == 1


def test_replay_path_unaffected_by_daemon_changes(tmp_path):
    """守护侧的改动不能连累批处理重放（两条路各走各的）。"""
    from patho_supplement.cli import main

    out_dir = tmp_path / "replay-out"
    code = main(
        [
            "replay",
            "--inbox",
            str(FIXTURE_INBOX),
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    drafts = list((out_dir / "drafts").glob("*.txt"))
    assert drafts, "重放路径仍应产出草稿"


def test_fixture_inbox_is_untouched_by_daemon_tests(inbox: Path):
    """测试不能污染仓库里的脱敏样例目录。"""
    before = sorted(p.name for p in FIXTURE_INBOX.iterdir())
    assert before  # 目录里有样例
    shutil.copytree(FIXTURE_INBOX, inbox, dirs_exist_ok=True)
    after = sorted(p.name for p in FIXTURE_INBOX.iterdir())
    assert before == after
