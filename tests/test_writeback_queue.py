"""回写队列测试：退避重试 + 超次升级告警 + 重启续跑。

覆盖任务 id 38 的验收点：

- 回写失败进队列：payload / 尝试次数 / 下次重试时间落 SQLite；
- 指数退避：第 n 次失败后等 ``base × 2^(n-1)``，封顶 ``max_delay``；
- 重试用尽：升级告警（审计流水 + 通知回调），**草稿状态不推进**，队列行不删；
- 重启后继续重试：进程重启队列还在，已成好的草稿接着写回去；
- 守护集成：写回失败 → 草稿入队 → 下一轮接着写回 → 状态才推进。

替身只出现在**测试数据层**（标准库 ``http.server`` 起本地服务），
产品代码走的是与生产完全相同的一条路径。
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.compose.draft_store import persist_draft
from patho_supplement.compose.pathology_client import (
    PathologyClient,
    PathologyConflictError,
    PathologyHttpError,
    WritebackReceipt,
)
from patho_supplement.compose.writeback_queue import (
    ESCALATION_CONFLICT,
    ESCALATION_RETRY_EXHAUSTED,
    QUEUE_STATE_ESCALATED,
    QUEUE_STATE_PENDING,
    QUEUE_STATE_WRITTEN,
    WritebackRetryWorker,
    backoff_seconds,
    count_escalated_writebacks,
    count_open_writebacks,
    count_pending_writebacks,
    due_writebacks,
    enqueue_pending_writeback,
    list_escalated_writebacks,
    load_writeback_item,
    record_writeback_failure,
)
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.models import Alignment, Draft, DraftEntry, MatchLevel

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 基准时刻（所有时间断言都相对它，避免依赖真实时钟）。
BASE_MOMENT = datetime(2026, 3, 2, 9, 0, 0)


# ------------------------------------------------------------------ 测试替身


class _FakeClock:
    """可推进的时钟：退避重试的时间断言必须可复现。"""

    def __init__(self, start: datetime = BASE_MOMENT) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class _StubClient:
    """出站客户端替身：按预设序列抛错或返回回执。"""

    def __init__(self, *, failures: int = 0, conflict: bool = False) -> None:
        self.failures = failures
        self.conflict = conflict
        self.calls: list[dict] = []

    def post_supplemental_draft(self, accession_no, *, body, base_version=None, entries=()):
        self.calls.append(
            {
                "accession_no": accession_no,
                "body": body,
                "base_version": base_version,
                "entries": list(entries),
            }
        )
        if self.conflict:
            raise PathologyConflictError(
                "草稿版本冲突，病理系统当前版本与本地基础版本不一致",
                path="/api/v1/pathology-reports/x/supplemental-drafts",
                status_code=409,
            )
        if len(self.calls) <= self.failures:
            raise PathologyHttpError(
                "回写草稿失败（HTTP 500）",
                path="/api/v1/pathology-reports/x/supplemental-drafts",
                status_code=500,
            )
        return WritebackReceipt(
            accession_no=accession_no,
            status="accepted",
            version="draft-v2",
            draft_id="SD-EXAMPLE-0001",
        )


class _LocalPathology:
    """本地协议替身：POST 可切换 500 / 200 / 409。

    用真实 HTTP 走一遍，验证「注入 5xx」走的是与生产同一条出站路径。
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.posts: list[dict] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                self._respond(
                    200,
                    {
                        "accession_no": "ACC-EXAMPLE-0001",
                        "base_version": "base-v3",
                        "blocks": [
                            {
                                "region_seq": 1,
                                "region_desc": "子宫体",
                                "block_no": "B1",
                                "order_no": "ORD-EXAMPLE-0001",
                                "ordered_items": ["IHC-CD3"],
                            }
                        ],
                    },
                )

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.posts.append(
                    {"path": self.path, "body": body, "headers": dict(self.headers)}
                )
                if outer.mode == "fail":
                    self._respond(500, {"message": "病理系统内部错误"})
                    return
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
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

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
def local_pathology():
    fake = _LocalPathology()
    try:
        yield fake
    finally:
        fake.close()


# ------------------------------------------------------------------ 造数据


def _seed_draft(database: Database) -> int:
    """落一份草稿（一条并入结果 + 一条待回报占位），返回草稿行 ID。"""
    alignment = Alignment(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD3",
        match_level=MatchLevel.L1_BLOCK_EXACT,
        score=1.0,
        recall_key="B1|IHC-CD3",
        block_no="B1",
        region_seq=1,
        region_desc="子宫体",
        source_system="IMMUNO-LAB",
        observed_at="20260101130000",
    )
    draft = Draft(
        accession_no="ACC-EXAMPLE-0001",
        version=4,
        entries=(
            DraftEntry(
                item_code="IHC-CD3",
                item_name="CD3",
                value="阴性",
                region_seq=1,
                region_desc="子宫体",
                block_no="B1",
                source_system="IMMUNO-LAB",
                observed_at="20260101130000",
                alignment=alignment,
            ),
            DraftEntry(
                item_code="IHC-CD20",
                item_name="CD20",
                region_seq=1,
                region_desc="子宫体",
                block_no="B1",
                placeholder=True,
            ),
        ),
        body="病理号：ACC-EXAMPLE-0001\n补充报告草稿（版本 4）\n",
        base_version="base-v3",
    )
    return persist_draft(database, draft)


def _enqueue(
    database: Database,
    draft_id: int,
    *,
    error: str = "回写草稿失败（HTTP 500）",
    delay_seconds: float = 0.0,
    now: datetime = BASE_MOMENT,
):
    return enqueue_pending_writeback(
        database,
        accession_no="ACC-EXAMPLE-0001",
        draft_id=draft_id,
        payload={
            "accession_no": "ACC-EXAMPLE-0001",
            "base_version": "base-v3",
            "body": "病理号：ACC-EXAMPLE-0001\n补充报告草稿（版本 4）\n",
            "version": 4,
        },
        error=error,
        delay_seconds=delay_seconds,
        now=now,
    )


@pytest.fixture()
def database(tmp_path: Path):
    db = Database(tmp_path / "state.db")
    db.init_schema()
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------ 入队与持久化


def test_enqueue_persists_item_with_payload(database: Database):
    """入队即持久化：payload 原样落库，尝试次数从 0 起。"""
    draft_id = _seed_draft(database)

    queue_id = _enqueue(database, draft_id)

    item = load_writeback_item(database, queue_id)
    assert item is not None
    assert item.accession_no == "ACC-EXAMPLE-0001"
    assert item.draft_id == draft_id
    assert item.state == QUEUE_STATE_PENDING
    assert item.attempts == 0
    assert item.payload["version"] == 4
    assert item.payload["base_version"] == "base-v3"
    assert "补充报告草稿" in item.payload["body"]
    assert "HTTP 500" in (item.last_error or "")
    assert item.draft_version == 4


def test_queue_survives_process_restart(tmp_path: Path):
    """进程重启后队列还在 —— 已经成好的草稿不能因为重启丢掉。"""
    db = Database(tmp_path / "state.db")
    db.init_schema()
    draft_id = _seed_draft(db)
    queue_id = _enqueue(db, draft_id)
    db.close()

    reopened = Database(tmp_path / "state.db")
    try:
        item = load_writeback_item(reopened, queue_id)
        assert item is not None
        assert item.payload["version"] == 4
        assert count_pending_writebacks(reopened) == 1
    finally:
        reopened.close()


def test_enqueue_respects_first_retry_delay(database: Database):
    """刚失败就立刻再打一次接口，对正在恢复中的病理系统是压力 —— 要等一等。"""
    draft_id = _seed_draft(database)

    queue_id = enqueue_pending_writeback(
        database,
        accession_no="ACC-EXAMPLE-0001",
        draft_id=draft_id,
        payload={"accession_no": "ACC-EXAMPLE-0001", "version": 4},
        error="回写草稿失败",
        delay_seconds=30.0,
        now=BASE_MOMENT,
    )

    item = load_writeback_item(database, queue_id)
    assert item is not None
    assert item.next_attempt_at == "2026-03-02 09:00:30"
    # 没到期就不取
    assert due_writebacks(database, now=BASE_MOMENT) == []
    assert len(due_writebacks(database, now=BASE_MOMENT + timedelta(seconds=30))) == 1


# ------------------------------------------------------------------ 退避计算


def test_backoff_is_exponential_and_capped():
    """退避是指数的：第 n 次失败后等 base × 2^(n-1)，并有封顶。"""
    assert backoff_seconds(1, base_delay=5.0) == 5.0
    assert backoff_seconds(2, base_delay=5.0) == 10.0
    assert backoff_seconds(3, base_delay=5.0) == 20.0
    assert backoff_seconds(4, base_delay=5.0) == 40.0
    assert backoff_seconds(5, base_delay=5.0) == 80.0
    # 封顶：再往上等也没意义
    assert backoff_seconds(20, base_delay=5.0, max_delay=60.0) == 60.0
    # 非法尝试次数不炸，退回基数
    assert backoff_seconds(0, base_delay=5.0) == 5.0
    assert backoff_seconds(-3, base_delay=5.0) == 5.0


def test_due_writebacks_only_returns_due_items(database: Database):
    """没到期的项不取 —— 这正是退避的意义：接口不可达时别把病理系统打爆。"""
    draft_id = _seed_draft(database)
    queue_id = enqueue_pending_writeback(
        database,
        accession_no="ACC-EXAMPLE-0001",
        draft_id=draft_id,
        payload={"accession_no": "ACC-EXAMPLE-0001", "version": 4},
        error="回写草稿失败",
        delay_seconds=60.0,
        now=BASE_MOMENT,
    )

    assert due_writebacks(database, now=BASE_MOMENT) == []
    assert len(due_writebacks(database, now=BASE_MOMENT + timedelta(seconds=60))) == 1
    assert load_writeback_item(database, queue_id).state == QUEUE_STATE_PENDING


# ------------------------------------------------------------------ 失败与重试


def test_retry_after_5xx_records_attempt_and_pushes_next_attempt(database: Database):
    """注入 5xx：队列按退避重试，尝试次数 +1，下次重试时间按指数往后推。

    **草稿状态不推进** —— 回写没成功就绝不假装已回写。
    """
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)
    clock = _FakeClock()
    client = _StubClient(failures=1)
    worker = WritebackRetryWorker(
        database, client, max_retries=5, base_delay=5.0, clock=clock
    )

    outcome = worker.run_once()

    assert outcome.attempted == 1
    assert outcome.failed == 1
    assert outcome.succeeded == 0
    assert outcome.escalated == []

    item = load_writeback_item(database, queue_id)
    assert item.state == QUEUE_STATE_PENDING
    assert item.attempts == 1
    assert item.next_attempt_at == "2026-03-02 09:00:05"
    assert "HTTP 500" in (item.last_error or "")

    # 草稿状态没动：还停在「已生成」
    row = database.query_one("SELECT state FROM drafts WHERE id = ?", (draft_id,))
    assert row["state"] == "generated"

    # 没到点不重试
    again = worker.run_once()
    assert again.attempted == 0


def test_retry_succeeds_after_backoff_and_advances_draft(database: Database):
    """退避到点后重试成功：队列项转 written，草稿状态才推进到「已回写」。"""
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)
    clock = _FakeClock()
    client = _StubClient(failures=1)
    worker = WritebackRetryWorker(
        database, client, max_retries=5, base_delay=5.0, clock=clock
    )

    worker.run_once()
    clock.advance(5)
    outcome = worker.run_once()

    assert outcome.succeeded == 1
    assert outcome.failed == 0
    assert outcome.escalated == []

    item = load_writeback_item(database, queue_id)
    assert item.state == QUEUE_STATE_WRITTEN
    assert item.attempts == 2
    assert item.last_error is None

    row = database.query_one("SELECT state FROM drafts WHERE id = ?", (draft_id,))
    assert row["state"] == "written_back"
    assert count_open_writebacks(database) == 0


def test_retry_sends_the_stored_draft_entries(database: Database):
    """重试发出去的必须是库里存的那一份草稿（条目从 draft_entries 重读）。"""
    draft_id = _seed_draft(database)
    _enqueue(database, draft_id)
    client = _StubClient()
    worker = WritebackRetryWorker(database, client, max_retries=5, base_delay=5.0)

    worker.run_once()

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["accession_no"] == "ACC-EXAMPLE-0001"
    assert call["base_version"] == "base-v3"
    assert "补充报告草稿" in call["body"]
    codes = [entry.item_code for entry in call["entries"]]
    assert codes == ["IHC-CD3", "IHC-CD20"]
    assert call["entries"][0].value == "阴性"


def test_retry_exhausts_and_escalates_without_advancing_draft(database: Database):
    """重试用尽：升级告警转人工，草稿状态不推进，队列行不删（绝不静默丢弃）。"""
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)
    clock = _FakeClock()
    client = _StubClient(failures=99)
    notices = []
    worker = WritebackRetryWorker(
        database,
        client,
        max_retries=5,
        base_delay=5.0,
        clock=clock,
        on_escalate=notices.append,
    )

    for _ in range(6):
        worker.run_once()
        clock.advance(120)

    # 只升级一次
    assert len(notices) == 1
    notice = notices[0]
    assert notice.accession_no == "ACC-EXAMPLE-0001"
    assert notice.draft_version == 4
    assert notice.attempts == 5
    assert notice.max_retries == 5
    assert notice.reason == ESCALATION_RETRY_EXHAUSTED
    assert notice.level == "critical"
    assert "病理号 ACC-EXAMPLE-0001" in notice.summary()

    # 队列项还在（行不删），状态是「升级告警等人」
    item = load_writeback_item(database, queue_id)
    assert item.state == QUEUE_STATE_ESCALATED
    assert item.attempts == 5
    assert item.escalated_at is not None
    assert item.escalation_reason == ESCALATION_RETRY_EXHAUSTED
    assert item.payload["version"] == 4
    assert count_escalated_writebacks(database) == 1
    assert count_open_writebacks(database) == 1

    # 草稿状态没推进：还停在「已生成」
    row = database.query_one("SELECT state FROM drafts WHERE id = ?", (draft_id,))
    assert row["state"] == "generated"

    # 升级之后不再自动重试（超次了就等人）
    client.calls.clear()
    worker.run_once()
    assert client.calls == []


def test_escalation_is_written_to_audit_log(database: Database):
    """升级告警落审计流水：谁、哪份草稿、试了几次、错在哪，一行说得清。"""
    draft_id = _seed_draft(database)
    _enqueue(database, draft_id)
    clock = _FakeClock()
    worker = WritebackRetryWorker(
        database,
        _StubClient(failures=99),
        max_retries=2,
        base_delay=5.0,
        clock=clock,
    )

    worker.run_once()
    clock.advance(60)
    worker.run_once()

    rows = database.query_all(
        "SELECT event, accession_no, detail_json FROM audit_log"
        " WHERE event = 'writeback_escalated'"
    )
    assert len(rows) == 1
    assert rows[0]["accession_no"] == "ACC-EXAMPLE-0001"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["attempts"] == 2
    assert detail["max_retries"] == 2
    assert detail["reason"] == ESCALATION_RETRY_EXHAUSTED
    assert detail["level"] == "critical"
    assert "HTTP 500" in detail["last_error"]
    assert detail["draft_version"] == 4


def test_version_conflict_escalates_without_retry(database: Database):
    """版本冲突不重试（重试也没用，对面版本已经变了）—— 直接转人工。"""
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)
    clock = _FakeClock()
    notices = []
    client = _StubClient(conflict=True)
    worker = WritebackRetryWorker(
        database, client, max_retries=5, base_delay=5.0, clock=clock, on_escalate=notices.append
    )

    outcome = worker.run_once()

    assert outcome.attempted == 1
    assert outcome.failed == 1
    assert len(notices) == 1
    assert notices[0].reason == ESCALATION_CONFLICT
    item = load_writeback_item(database, queue_id)
    assert item.state == QUEUE_STATE_ESCALATED
    assert item.attempts == 1
    # 草稿状态不推进
    row = database.query_one("SELECT state FROM drafts WHERE id = ?", (draft_id,))
    assert row["state"] == "generated"


def test_record_writeback_failure_keeps_item_in_queue(database: Database):
    """还没用完次数：留在队列里等下一轮，payload 与状态都不丢。"""
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)

    notice = record_writeback_failure(
        database,
        queue_id,
        error="回写草稿失败",
        max_retries=5,
        base_delay=5.0,
        now=BASE_MOMENT,
    )

    assert notice is None
    item = load_writeback_item(database, queue_id)
    assert item.state == QUEUE_STATE_PENDING
    assert item.attempts == 1
    assert item.next_attempt_at == "2026-03-02 09:00:05"
    assert count_pending_writebacks(database) == 1


def test_escalated_item_is_listed_for_manual_handling(database: Database):
    """升级告警的项能被单独列出来（运维/医师要看到「哪份草稿等人处置」）。"""
    draft_id = _seed_draft(database)
    queue_id = _enqueue(database, draft_id)
    record_writeback_failure(
        database, queue_id, error="回写草稿失败", max_retries=1, now=BASE_MOMENT
    )

    rows = list_escalated_writebacks(database)
    assert len(rows) == 1
    assert rows[0]["accession_no"] == "ACC-EXAMPLE-0001"
    assert rows[0]["escalation_reason"] == ESCALATION_RETRY_EXHAUSTED
    assert rows[0]["attempts"] == 1


# ------------------------------------------------------------------ 守护集成


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


def _daemon(config: Config, client, inbox: Path, tmp_path: Path, messages: list[str]) -> Daemon:
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=client,
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=messages.append,
    )


def test_daemon_queues_failed_writeback_and_keeps_draft_unwritten(
    local_pathology, tmp_path: Path
):
    """守护一轮写回失败：草稿进队列，草稿状态不推进，结果不丢。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    local_pathology.mode = "fail"
    config = _config(inbox, local_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, local_pathology.client(), inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    assert outcome.drafts_failed == 1
    assert outcome.drafts_written == 0

    with Database(tmp_path / "state.db") as db:
        assert count_pending_writebacks(db) == 1
        draft = db.query_one("SELECT state FROM drafts WHERE accession_no = ?", ("ACC-EXAMPLE-0001",))
        assert draft["state"] == "generated"
        rows = db.query_all("SELECT event FROM audit_log WHERE event = 'queued'")
        assert len(rows) == 1
    assert any("回写失败" in message for message in messages)


def test_daemon_retries_queued_draft_on_next_round(local_pathology, tmp_path: Path):
    """病理系统恢复后，下一轮守护把队列里的草稿写回去，状态才推进。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    local_pathology.mode = "fail"
    config = _config(
        inbox,
        local_pathology.base_url,
        tmp_path / "state.db",
        WRITEBACK_RETRY_BASE_SECONDS="1",
    )
    messages: list[str] = []
    daemon = _daemon(config, local_pathology.client(), inbox, tmp_path, messages)

    first = daemon.run_once()
    assert first.drafts_failed == 1
    assert first.writeback_retried == 0

    # 病理系统恢复；队列项到期后由下一轮接着写
    local_pathology.mode = "ok"
    with Database(tmp_path / "state.db") as db:
        db.execute("UPDATE writeback_queue SET next_attempt_at = ?", ("2020-01-01 00:00:00",))

    second = daemon.run_once()
    daemon.close()

    assert second.writeback_retried == 1
    assert second.writeback_retry_succeeded == 1
    assert second.writeback_escalated == 0

    with Database(tmp_path / "state.db") as db:
        assert count_open_writebacks(db) == 0
        draft = db.query_one("SELECT state FROM drafts WHERE accession_no = ?", ("ACC-EXAMPLE-0001",))
        assert draft["state"] == "written_back"
        rows = db.query_all("SELECT event FROM audit_log WHERE event = 'writeback_retry_succeeded'")
        assert len(rows) == 1

    # 重试发出去的草稿与第一次是同一份（病理号 + 项目码 + 条目数）
    posts = local_pathology.posts
    assert len(posts) == 2
    first_body = json.loads(posts[0]["body"].decode("utf-8"))
    second_body = json.loads(posts[1]["body"].decode("utf-8"))
    assert first_body["accession_no"] == second_body["accession_no"]
    assert len(first_body["entries"]) == len(second_body["entries"])


def test_daemon_does_not_redraft_the_same_result_after_failure(
    local_pathology, tmp_path: Path
):
    """写回失败后不能每一轮都起草一版 —— 结果已经在队列里等着了。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    local_pathology.mode = "fail"
    config = _config(inbox, local_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, local_pathology.client(), inbox, tmp_path, messages)

    daemon.run_once()
    daemon.run_once()
    daemon.close()

    with Database(tmp_path / "state.db") as db:
        # 只起草了一份草稿、一条队列行
        assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
        assert db.query_value("SELECT COUNT(*) FROM writeback_queue") == 1
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 1


def test_daemon_archives_file_whose_results_are_already_recorded(
    local_pathology, tmp_path: Path
):
    """结果此前已落定（进程在归档前被杀）的报文：读完就归档，不在目录里空转。

    这份报文里的结果已经有归宿（进过草稿），留着它只会让守护每一轮都重读
    同一份文件。
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    fixture = FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7"
    shutil.copy(fixture, inbox)
    config = _config(inbox, local_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, local_pathology.client(), inbox, tmp_path, messages)

    daemon.run_once()
    # 模拟「归档之前进程被杀」：同一份报文又回到目录里
    shutil.copy(fixture, inbox)
    second = daemon.run_once()
    daemon.close()

    assert second.duplicates >= 1
    assert second.drafts_written == 0
    assert not (inbox / fixture.name).exists()
    assert (inbox / "processed" / fixture.name).exists()
