"""版本冲突处置：拒绝写回 + 保留本地草稿 + 转人工处置。

替身只出现在**测试数据层**（标准库 ``http.server`` 起一个本地服务），
产品代码走的是与生产完全相同的一条路径 —— 没有为测试另写的分支。

覆盖任务 id 39 的验收点：

- **拒绝写回**：对面回 409 之后草稿区没有新版本，本地草稿状态停在「已生成」，
  回执与回写时间都是空的 —— 没写回去就是没写回去。
- **保留本地草稿**：正文与条目原样留在库里，另落一份文本文件给医师看。
- **记审计行**：``writeback_conflict`` 行带本地草稿版本号与基础版本号。
- **转人工处置**：冲突进待人工处理清单（运行清单 + 导出的 CSV / 文本）。
- **不触发无限重试**：不进退避队列的常规重试路径；下一轮不再发起 POST，
  也不产生新版本 —— 反复撞同一面墙只会把版本链越堆越乱。
- **冲突可了结**：这份报告的后续版本写回成功后，冲突转「已了结」并留审计。
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.compose.conflict import (
    CONFLICT_SOURCE_DIRECT,
    CONFLICT_SOURCE_RETRY,
    CONFLICT_STATE_OPEN,
    CONFLICT_STATE_RESOLVED,
    list_open_conflicts,
)
from patho_supplement.compose.pathology_client import (
    PathologyClient,
    PathologyConflictError,
)
from patho_supplement.compose.writeback_queue import (
    ESCALATION_CONFLICT,
    QUEUE_STATE_ESCALATED,
    WritebackRetryWorker,
    enqueue_pending_writeback,
)
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 替身认识的病理号基线（与脱敏样例同一套字段词汇）。
BASELINE = {
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
}


class _Recorder:
    """记录替身收到的每个请求，供断言逐字段核对。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def add(self, method: str, path: str, headers, body: bytes) -> None:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": {key.lower(): value for key, value in headers.items()},
                "body": body,
            }
        )

    def of(self, method: str) -> list[dict]:
        return [item for item in self.requests if item["method"] == method]


class _FakePathology:
    """本地协议替身：POST 的回执可以切「冲突 / 接受」两种。

    ``mode="conflict"`` 时 POST 回 409（对面版本已经变了）；``mode="ok"`` 时
    回 200 接受。GET 一直给基线 —— 冲突是**回写**时才发现的，取基线正常。
    """

    def __init__(self, mode: str = "conflict") -> None:
        self.mode = mode
        self.recorder = _Recorder()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库的访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                outer.recorder.add("GET", self.path, self.headers, b"")
                self._respond(200, BASELINE)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.recorder.add("POST", self.path, self.headers, body)
                if outer.mode == "conflict":
                    self._respond(409, {"message": "版本冲突"})
                    return
                self._respond(
                    200,
                    {
                        "status": "accepted",
                        "version": "draft-v2",
                        "draft_id": "SD-EXAMPLE-0001",
                    },
                )

            def _respond(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return _Handler

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def client(self, *, token: str = "test-token", timeout: float = 2.0) -> PathologyClient:
        return PathologyClient(self.base_url, token, timeout=timeout)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def fake_pathology():
    fake = _FakePathology(mode="conflict")
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    directory = tmp_path / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _config(inbox: Path, base_url: str, db_path: Path) -> Config:
    return Config.from_env(
        {
            "PATHOLOGY_BASE_URL": base_url,
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(db_path),
        }
    )


def _daemon(
    config: Config,
    fake: _FakePathology,
    inbox: Path,
    tmp_path: Path,
    messages: list[str],
) -> Daemon:
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=fake.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=messages.append,
    )


def _conflict_row(db_path: Path):
    with Database(db_path) as db:
        return db.query_one("SELECT * FROM writeback_conflicts")


# ------------------------------------------------------------------ 拒绝写回


def test_conflict_refuses_writeback_and_keeps_draft_in_generated(
    fake_pathology, inbox, tmp_path
):
    """对面回 409：草稿区没有新版本，本地草稿状态停在「已生成」。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    assert outcome.conflicts == 1
    assert outcome.drafts_written == 0

    # 对面只收到一次 POST，且本地**没有**猜一个新版本号硬塞过去
    posts = fake_pathology.recorder.of("POST")
    assert len(posts) == 1
    payload = json.loads(posts[0]["body"].decode("utf-8"))
    assert payload["base_version"] == "base-v3"

    with Database(db_path) as db:
        draft = db.query_one("SELECT * FROM drafts")
        assert draft["state"] == "generated"  # 仍是「已生成」，没推进到「已回写」
        assert draft["receipt"] is None
        assert draft["written_back_at"] is None
        assert draft["entry_count"] >= 1
        assert (
            db.query_value("SELECT COUNT(*) FROM draft_entries") == draft["entry_count"]
        )


def test_conflict_keeps_local_draft_body_and_writes_it_to_disk(
    fake_pathology, inbox, tmp_path
):
    """本地草稿留底：正文在库里、另落一份文本文件，医师那份报告没丢。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        draft = db.query_one("SELECT * FROM drafts")
        assert "IHC-CD3" in draft["body"]
        assert draft["version"] == 4  # 基线 base-v3 → 首版 v4
        assert draft["base_version"] == "base-v3"

    draft_file = tmp_path / "out" / "drafts" / "ACC-EXAMPLE-0001-v4.txt"
    assert draft_file.exists()
    assert "IHC-CD3" in draft_file.read_text(encoding="utf-8")


def test_conflict_writes_audit_row_with_local_version(fake_pathology, inbox, tmp_path):
    """审计行带本地草稿版本号与基础版本号 —— 事后答得出「当时为什么没写回去」。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        rows = db.query_all(
            "SELECT event, detail_json FROM audit_log WHERE event = 'writeback_conflict'"
        )
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["draft_version"] == 4
        assert detail["base_version"] == "base-v3"
        assert detail["accession_no" if "accession_no" in detail else "conflict_id"]
        assert detail["conflict_id"] >= 1
        assert "版本冲突" in detail["reason"]
        assert detail["source"] == CONFLICT_SOURCE_DIRECT


def test_conflict_audit_row_holds_no_patient_identifiers(fake_pathology, inbox, tmp_path):
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        rows = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'writeback_conflict'"
        )
    assert len(rows) == 1
    text = rows[0]["detail_json"]
    for forbidden in ("姓名", "身份证", "手机号", "患者"):
        assert forbidden not in text


# ------------------------------------------------------------------ 转人工处置


def test_conflict_goes_into_pending_list_with_reason_and_local_version(
    fake_pathology, inbox, tmp_path
):
    """冲突进待人工处理清单：写清冲突原因与本地版本号。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    text = (tmp_path / "out" / "pending_list.txt").read_text(encoding="utf-8")
    assert "版本冲突待处置" in text
    assert "ACC-EXAMPLE-0001" in text
    assert "v4" in text
    assert "base-v3" in text
    assert "怎么处置" in text


def test_conflict_is_exported_with_the_pending_list(fake_pathology, inbox, tmp_path):
    """导出给技术员的那份清单里也有冲突 —— 一份文件带走所有要人处理的事。"""
    from patho_supplement.cli import main

    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    out_dir = tmp_path / "export"
    code = main(
        [
            "pending",
            "export",
            "--state-db",
            str(db_path),
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0

    csv_text = (out_dir / "pending_list.csv").read_text(encoding="utf-8-sig")
    assert "版本冲突" in csv_text
    assert "冲突#1" in csv_text
    assert "ACC-EXAMPLE-0001" in csv_text

    text = (out_dir / "pending_list.txt").read_text(encoding="utf-8")
    assert "版本冲突待处置" in text
    assert "v4" in text


def test_pending_list_cli_shows_the_conflict(fake_pathology, inbox, tmp_path, capsys):
    from patho_supplement.cli import main

    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])
    daemon.run_once()
    daemon.close()

    code = main(["pending", "list", "--state-db", str(db_path)])
    captured = capsys.readouterr().out
    assert code == 0
    assert "版本冲突待处置" in captured
    assert "ACC-EXAMPLE-0001" in captured
    assert "本地草稿 v4" in captured


# ------------------------------------------------------------------ 不无限重试


def test_conflict_does_not_enter_the_retry_queue(fake_pathology, inbox, tmp_path):
    """冲突不进退避队列的常规重试路径 —— 队列里不该有这一行。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        assert db.query_value("SELECT COUNT(*) FROM writeback_queue") == 0


def test_conflict_is_not_retried_in_the_next_round(fake_pathology, inbox, tmp_path):
    """下一轮不再撞同一面墙：不再发 POST，也不再产生新版本。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    first = daemon.run_once()
    second = daemon.run_once()
    daemon.close()

    assert first.conflicts == 1
    assert second.conflicts == 0
    assert len(fake_pathology.recorder.of("POST")) == 1

    with Database(db_path) as db:
        # 草稿仍是一份（没有 v5 冒出来），冲突仍是那一条
        assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
        assert db.query_value("SELECT COUNT(*) FROM writeback_conflicts") == 1
        assert db.query_value("SELECT COUNT(*) FROM writeback_queue") == 0


def test_conflict_archives_the_message_so_the_result_is_not_reprocessed(
    fake_pathology, inbox, tmp_path
):
    """报文归档：结果已有归宿（本地草稿 + 冲突待处置），不留在原地反复重来。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    assert (inbox / "processed" / "acc-example-0001-01-cd3.hl7").exists()
    with Database(db_path) as db:
        # 幂等键落库：这条结果不会再进第二版草稿
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 1


def test_retry_queue_conflict_is_registered_and_not_retried(tmp_path):
    """队列重试时撞上冲突：登记进冲突清单，且不再重试（直接升级转人工）。"""

    class _StubClient:
        def post_supplemental_draft(self, *args, **kwargs):
            raise PathologyConflictError(
                "草稿版本冲突，病理系统当前版本与本地基础版本不一致",
                path="/api/v1/pathology-reports/ACC-EXAMPLE-0001/supplemental-drafts",
                status_code=409,
            )

    db_path = tmp_path / "state.db"
    with Database(db_path) as db:
        db.execute(
            "INSERT INTO drafts(accession_no, version, base_version, state, entry_count,"
            " content_hash, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ACC-EXAMPLE-0001",
                4,
                "base-v3",
                "generated",
                2,
                "hash",
                "草稿正文",
                "2026-10-28 10:00:00",
            ),
        )
        draft_id = int(db.query_value("SELECT id FROM drafts"))
        queue_id = enqueue_pending_writeback(
            db,
            accession_no="ACC-EXAMPLE-0001",
            draft_id=draft_id,
            payload={
                "accession_no": "ACC-EXAMPLE-0001",
                "base_version": "base-v3",
                "body": "草稿正文",
                "version": 4,
            },
            error="回写失败",
            delay_seconds=0.0,
        )
        worker = WritebackRetryWorker(db, _StubClient(), max_retries=5)
        outcome = worker.run_once()

        assert outcome.attempted == 1
        assert outcome.failed == 1
        assert len(outcome.escalated) == 1
        assert outcome.escalated[0].reason == ESCALATION_CONFLICT

        # 队列行升级告警、不再重试；草稿状态不推进
        queue_row = db.query_one("SELECT * FROM writeback_queue WHERE id = ?", (queue_id,))
        assert queue_row["state"] == QUEUE_STATE_ESCALATED
        assert queue_row["escalation_reason"] == ESCALATION_CONFLICT
        assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "generated"

        # 冲突同时登记进冲突清单（来源标成 retry）
        conflict = db.query_one("SELECT * FROM writeback_conflicts")
        assert conflict is not None
        assert conflict["source"] == CONFLICT_SOURCE_RETRY
        assert conflict["draft_version"] == 4
        assert conflict["base_version"] == "base-v3"
        assert conflict["state"] == CONFLICT_STATE_OPEN

        # 再跑一轮：已升级的行不再被取出来重试
        again = worker.run_once()
        assert again.attempted == 0


# ------------------------------------------------------------------ 冲突了结


def test_conflict_is_closed_when_a_later_version_is_written_back(
    fake_pathology, inbox, tmp_path
):
    """这份报告后续版本写回成功后，先前的冲突随之了结（冲突行不删）。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    first = daemon.run_once()
    assert first.conflicts == 1

    # 病理系统那边版本变了（医师改过），这一轮接受
    fake_pathology.mode = "ok"
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-02-cd20-alias.hl7", inbox)
    second = daemon.run_once()
    daemon.close()

    assert second.drafts_written == 1
    assert second.conflicts_closed == 1

    with Database(db_path) as db:
        rows = db.query_all("SELECT * FROM writeback_conflicts")
        assert len(rows) == 1
        assert rows[0]["state"] == CONFLICT_STATE_RESOLVED
        assert rows[0]["resolved_at"]
        assert "回写成功" in rows[0]["resolution"]

        resolved = db.query_all(
            "SELECT detail_json FROM audit_log"
            " WHERE event = 'writeback_conflict_resolved'"
        )
        assert len(resolved) == 1


def test_open_conflicts_are_listed_per_accession(fake_pathology, inbox, tmp_path):
    """冲突清单能按病理号筛 —— 一次只看一份报告的冲突。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    db_path = tmp_path / "state.db"
    config = _config(inbox, fake_pathology.base_url, db_path)
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    daemon.close()

    with Database(db_path) as db:
        assert len(list_open_conflicts(db)) == 1
        assert len(list_open_conflicts(db, accession_no="ACC-EXAMPLE-0001")) == 1
        assert len(list_open_conflicts(db, accession_no="ACC-OTHER-9999")) == 0
