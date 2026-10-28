"""图式装配端到端回归：一条链走通，三类出口都不漏。

这是本产品上院内机前的最后一道护栏。前面每个模块各有各的单测，但
**模块之间接得上**这件事只有整链跑一遍才算数 —— 单测全绿而链在某个接缝上
断掉，是这类系统最常见的翻车方式。

**三类出口（一个都不能漏）**：

1. **正常成稿** —— 结果对得上 → 成稿 → 真回写 → 通知当班医师；
2. **挂起待人工** —— 对不上 → 落挂起行等人补齐，**不进草稿**；
3. **畸形报文拒收** —— 看不懂的报文 → 拒收并隔离，**绝不混进草稿**。

三类出口的共同底线是同一句话：**一条结果只有两个去向 —— 进草稿或进挂起，
没有第三个去向**。悄悄丢掉的结果，就是医师看不到的检查项。

**替身只出现在测试数据层**（标准库 ``http.server`` 起一个本地服务 +
一个记录用的通知通道），产品代码走的是与生产完全相同的一条路径：真实的
出站客户端、真实的协议、真实的凭据位。

**源产品主路径上的位置**：本文件验的是源实现
``github_ref/haystack/haystack/core/pipeline/component_checks.py::can_component_run``
L41-54 两道闸门（必需输入齐不齐 × 有没有触发源）与
``has_any_trigger`` L57-78「一个组件最多被触发一次」在**医院产品里**的可观察
结论 —— 链按顺序走一遍、同一批结果不二次推进、缺基线就停在原地。
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.notify.base import STATUS_SENT, ChannelResult
from patho_supplement.notify.dispatcher import NotifyDispatcher
from patho_supplement.pipeline import (
    NODE_ALIGN,
    NODE_COMPOSE,
    NODE_DERIVE_ORDERS,
    NODE_FETCH_BASELINE,
    NODE_FANOUT_RETRIEVE,
    NODE_INGEST,
    NODE_NOTIFY,
    NODE_WRITEBACK,
    REASON_ALREADY_VISITED,
    REASON_MISSING_INPUTS,
)

FIXTURES = Path(__file__).parent / "fixtures"
INBOX = FIXTURES / "inbox"
EDGE = FIXTURES / "edge"

#: 节点表顺序 = 一条报告的处理链顺序。
CHAIN = (
    NODE_INGEST,
    NODE_FETCH_BASELINE,
    NODE_DERIVE_ORDERS,
    NODE_FANOUT_RETRIEVE,
    NODE_ALIGN,
    NODE_COMPOSE,
    NODE_WRITEBACK,
    NODE_NOTIFY,
)

#: 替身认识的基线（与脱敏样例同一套字段词汇）。
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
}

#: 一份看懂的报文（缺 MSH-9，结构不可解析）。
MALFORMED = (
    "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101130500||"
    "ORU^R01|MSG-BROKEN|P|2.5.1\r\n"
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\r\n"
    "12|这段不是合法段名\r\n"
)


# --------------------------------------------------------------- 本地协议替身


class _FakePathology:
    """本地病理系统替身（只在本测试数据层出现）。

    起一个标准库 ``http.server``：GET 取基线、POST 收草稿。**产品代码走的是
    与生产完全相同的一条路径** —— 同一个 ``PathologyClient``、同一个协议、
    同一个凭据位。
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.server.req_log = self.requests
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _make_handler():
        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def _record(self, method: str, body: bytes) -> None:
                self.server.req_log.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                self._record("GET", b"")
                parts = self.path.strip("/").split("/")
                accession = parts[3] if len(parts) > 3 else ""
                baseline = BASELINES.get(accession)
                if baseline is None:
                    self._respond(404, {"message": "报告不存在"})
                    return
                self._respond(200, baseline)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._record("POST", body)
                parts = self.path.strip("/").split("/")
                accession = parts[3] if len(parts) > 3 else ""
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

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def of(self, method: str) -> list[dict]:
        return [item for item in self.requests if item["method"] == method]

    def client(self, *, token: str = "test-token", timeout: float = 2.0):
        from patho_supplement.compose.pathology_client import PathologyClient

        return PathologyClient(self.base_url, token, timeout=timeout)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _RecordingChannel:
    """记录发出的通知（测试数据层的通知通道替身）。

    按通道协议回一条 ``ChannelResult`` —— 发成功了就说成功，不让调度器把
    「返回了 None」当成发送失败，那会让通知这条路的断言变成假的。
    """

    def __init__(self) -> None:
        self.messages: list = []

    @property
    def name(self) -> str:
        return "recording"

    def is_configured(self) -> bool:
        return True

    def send(self, message) -> ChannelResult:  # noqa: ANN001 - 通道接口
        self.messages.append(message)
        return ChannelResult(
            channel=self.name,
            status=STATUS_SENT,
            detail="已记录",
            attempts=1,
            recipient_count=len(message.recipients) or 1,
        )


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


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


def _daemon(config: Config, fake, tmp_path: Path, channel) -> Daemon:
    watcher = InboxWatcher(
        config.pathology_result_inbox, poll_interval=0.01, settle_seconds=0.0
    )
    # 通知调度器的审计流水落自己的库 —— 建好表，否则通知这条路会因为
    # 「no such table」把「发出去了」记成「发送失败」。
    notify_db = Database(tmp_path / "notify.db")
    notify_db.init_schema()
    return Daemon(
        config,
        client=fake.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=notify_db),
    )


def _rows(database: Database, sql: str, params=()) -> list:
    return database.query_all(sql, params)


# ------------------------------------------------------------- 出口一：正常成稿


def test_normal_exit_runs_the_whole_chain_end_to_end(
    fake_pathology, inbox, tmp_path
):
    """正常成稿：八个节点按链走一遍，真出站、真回写、真通知。

    这条用例是整条主干的**主路径断言**：入站解析 → 取基线 → 派生待查项 →
    多路召回 → 部位-蜡块对齐 → 成稿 → 回写 → 通知，一环都不能少。
    """
    shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # --- 节点推进：顺序与节点表一致，每个节点只走一次 -------------------
    assert outcome.pipeline_executed == list(CHAIN)
    assert outcome.pipeline_visits == {node: 1 for node in CHAIN}

    # --- 出站：GET 取基线、POST 回写草稿区，路径与凭据位都对 -------------
    gets = fake_pathology.of("GET")
    posts = fake_pathology.of("POST")
    assert len(gets) == 1
    assert gets[0]["path"] == "/api/v1/pathology-reports/ACC-EXAMPLE-0001"
    assert gets[0]["headers"]["authorization"] == "Bearer test-token"
    assert len(posts) == 1
    assert (
        posts[0]["path"]
        == "/api/v1/pathology-reports/ACC-EXAMPLE-0001/supplemental-drafts"
    )
    assert posts[0]["headers"]["authorization"] == "Bearer test-token"

    # --- 请求体：逐字段（草稿正文 + 基础版本号 + 结构化条目）------------
    payload = json.loads(posts[0]["body"].decode("utf-8"))
    assert set(payload) == {"accession_no", "base_version", "body", "entries"}
    assert payload["accession_no"] == "ACC-EXAMPLE-0001"
    assert payload["base_version"] == "base-v3"
    assert payload["entries"], "回写请求体必须带结构化条目"

    # --- 草稿正文：按取材部位分组，逐项标来源系统与回报时间 -------------
    draft_path = tmp_path / "out" / "drafts" / "ACC-EXAMPLE-0001-v4.txt"
    assert draft_path.exists(), "草稿正文应当落盘"
    body = draft_path.read_text(encoding="utf-8")
    assert "【子宫体 · 蜡块 B1】" in body
    assert "IHC-CD3" in body and "阴性" in body
    # 来源系统取回报报文的发送方（MSH-3），回报时间取观察时间。
    assert "来源 IMMUNO-LAB" in body
    assert "回报时间 2026-01-01 13:00" in body
    # 未到项以占位出现，少写一项医师就会以为这项没做。
    assert "待回报" in body

    # --- 状态落库：草稿、条目、幂等键 -------------------------------------
    database = Database(tmp_path / "state.db")
    try:
        drafts = _rows(database, "SELECT * FROM drafts")
        entries = _rows(database, "SELECT * FROM draft_entries")
        keys = _rows(database, "SELECT * FROM idempotency_keys")
    finally:
        database.close()
    assert len(drafts) == 1
    assert drafts[0]["state"] == "written_back"
    assert drafts[0]["base_version"] == "base-v3"
    # 条目数守恒：并入的 + 占位的 = 草稿条目行数。
    assert len(entries) == outcome.rationale_count + outcome.placeholder_count
    assert len(keys) == 1

    # --- 审计流水：一条结果一条对齐依据 + 一条回写回执 ------------------
    database = Database(tmp_path / "state.db")
    try:
        rationale = _rows(
            database, "SELECT * FROM audit_log WHERE event = 'alignment_rationale'"
        )
        accepted = _rows(
            database, "SELECT * FROM audit_log WHERE event = 'accepted'"
        )
    finally:
        database.close()
    assert len(rationale) == outcome.rationale_count
    assert len(accepted) == 1

    # --- 通知当班医师：回写成功才喊人签发 ------------------------------
    assert outcome.notify_sent == 1
    assert outcome.notify_failed == 0
    assert len(channel.messages) == 1
    assert "ACC-EXAMPLE-0001" in channel.messages[0].subject

    # --- 报文归档：落定之后才移走 ---------------------------------------
    assert not (inbox / "cd3.hl7").exists()
    assert (inbox / "processed" / "cd3.hl7").exists()


# ------------------------------------------------------------- 出口二：挂起待人工


def test_pending_exit_lands_in_pending_store_and_never_enters_the_draft(
    fake_pathology, inbox, tmp_path
):
    """挂起待人工：回报没带蜡块号 → 落挂起行等人补齐，不进草稿、不回写。"""
    shutil.copy(
        INBOX / "acc-example-0001-03-ki67-no-block.hl7", inbox / "ki67.hl7"
    )
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # 对齐节点走到了，挂起行落了库。
    assert outcome.pending_count == 1
    database = Database(tmp_path / "state.db")
    try:
        pending = _rows(database, "SELECT * FROM pending_items")
        drafts = _rows(database, "SELECT * FROM drafts")
    finally:
        database.close()
    assert len(pending) == 1
    assert pending[0]["accession_no"] == "ACC-EXAMPLE-0001"
    assert pending[0]["item_code"] == "IHC-KI67"
    assert pending[0]["reason"] == "缺蜡块号"
    assert "block_no" in pending[0]["missing_fields"]

    # 没成稿 → 没回写、没通知。
    assert outcome.drafts_written == 0
    assert drafts == []
    assert fake_pathology.of("POST") == []
    assert channel.messages == []

    # 成稿节点因「没有命中」被闸门一挡住 —— 停住要看得见。
    skips = {item["node"]: item for item in outcome.pipeline_skips}
    assert skips[NODE_COMPOSE]["reason"] == REASON_MISSING_INPUTS
    assert "matched" in skips[NODE_COMPOSE]["missing"]
    # 对齐之前的四个节点都走过了：图确实推到了对齐这一步。
    assert outcome.pipeline_executed == [
        NODE_INGEST,
        NODE_FETCH_BASELINE,
        NODE_DERIVE_ORDERS,
        NODE_FANOUT_RETRIEVE,
        NODE_ALIGN,
    ]

    # 报文有归宿（进了挂起清单）→ 归档，不占着目录。
    assert (inbox / "processed" / "ki67.hl7").exists()


# ------------------------------------------------------------ 出口三：畸形拒收


def test_malformed_exit_is_quarantined_and_never_enters_the_draft(
    fake_pathology, inbox, tmp_path
):
    """畸形报文：拒收 + 隔离 + 记审计，绝不混进草稿。"""
    (inbox / "broken.hl7").write_text(MALFORMED, encoding="utf-8")
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert outcome.malformed == 1
    assert [name for name, _ in outcome.rejected] == ["broken.hl7"]

    # 图根本没被推进：报文在入站就被拒了，没有结果到达。
    assert outcome.pipeline_executed == []
    assert outcome.drafts_written == 0
    assert fake_pathology.of("GET") == []
    assert fake_pathology.of("POST") == []
    assert channel.messages == []

    database = Database(tmp_path / "state.db")
    try:
        drafts = _rows(database, "SELECT * FROM drafts")
        malformed = _rows(
            database, "SELECT * FROM audit_log WHERE event = 'malformed'"
        )
    finally:
        database.close()
    assert drafts == []
    assert len(malformed) == 1
    assert "broken.hl7" in malformed[0]["detail_json"]

    # 坏报文不丢：移进 rejected/ 隔离目录，人还能翻出来看。
    assert not (inbox / "broken.hl7").exists()
    assert (inbox / "rejected" / "broken.hl7").exists()


# --------------------------------------------- 三类出口同批共存（不串味）


def test_three_exits_coexist_in_one_batch_without_cross_contamination(
    fake_pathology, inbox, tmp_path
):
    """一批回报里三类出口同时发生：各自归位，互不串味。

    这是本文件最有价值的一条：把正常结果、对不上的结果、看不懂的报文放进同一
    个目录跑一轮，然后逐条核对 —— 对上的进了草稿、对不上的进了挂起、看不懂的
    进了隔离目录，**三条路互不污染**。
    """
    shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
    shutil.copy(
        INBOX / "acc-example-0001-03-ki67-no-block.hl7", inbox / "ki67.hl7"
    )
    (inbox / "broken.hl7").write_text(MALFORMED, encoding="utf-8")

    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # --- 三类出口各自到位 ----------------------------------------------
    assert outcome.drafts_written == 1
    assert outcome.pending_count == 1
    assert outcome.malformed == 1

    # --- 草稿里只有对上的那一条 ------------------------------------------
    body = (tmp_path / "out" / "drafts" / "ACC-EXAMPLE-0001-v4.txt").read_text(
        encoding="utf-8"
    )
    assert "IHC-CD3" in body and "阴性" in body
    # 挂起的那条**不许**出现在草稿正文里。
    assert "IHC-KI67" not in body
    assert "约15%" not in body
    # 畸形报文的内容更不许出现。
    assert "这段不是合法段名" not in body

    # --- 状态库里三条路各归各位 ----------------------------------------
    database = Database(tmp_path / "state.db")
    try:
        drafts = _rows(database, "SELECT * FROM drafts")
        entries = _rows(database, "SELECT * FROM draft_entries")
        pending = _rows(database, "SELECT * FROM pending_items")
        merged_entries = _rows(
            database, "SELECT * FROM draft_entries WHERE placeholder = 0"
        )
        rationale = _rows(
            database, "SELECT * FROM audit_log WHERE event = 'alignment_rationale'"
        )
        pending_rationale = _rows(
            database, "SELECT * FROM audit_log WHERE event = 'alignment_pending'"
        )
    finally:
        database.close()

    assert len(drafts) == 1
    assert len(merged_entries) == 1
    assert merged_entries[0]["item_code"] == "IHC-CD3"
    # 挂起的那条**连草稿条目表都不许进**（占位也不行）—— 它根本没进报告。
    assert "IHC-KI67" not in {row["item_code"] for row in entries}
    assert len(pending) == 1
    assert pending[0]["item_code"] == "IHC-KI67"
    assert len(rationale) == 1
    assert len(pending_rationale) == 1

    # --- 条数守恒：一条结果只有两个去向，没有第三个 --------------------
    #   本批到了 2 条结果（CD3 + KI67），1 条进草稿、1 条进挂起。
    assert outcome.accepted == 2
    assert len(merged_entries) + len(pending) == outcome.accepted

    # --- 出站只发生一次：只有那份成稿被写回去 ---------------------------
    assert len(fake_pathology.of("POST")) == 1
    assert len(channel.messages) == 1


# ------------------------------------------------- 图推进顺序与访问计数


def test_node_execution_order_follows_the_node_table(fake_pathology, inbox, tmp_path):
    """链的推进顺序 = 节点表顺序，不是碰巧。"""
    shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert tuple(outcome.pipeline_executed) == CHAIN
    # 每个节点的访问计数恰好一次。
    assert outcome.pipeline_visits == {node: 1 for node in CHAIN}


def test_two_files_for_one_accession_advance_the_chain_only_once(
    fake_pathology, inbox, tmp_path
):
    """同一份报告的两个回报文件 = 一个事件 = 链只走一遍。

    「一个组件最多被触发一次」这条规矩在产品里的可观察结论：同一批结果分几个
    文件投进来，图只推进一次，不产出两份草稿。
    """
    shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
    shutil.copy(INBOX / "acc-example-0001-02-cd20-alias.hl7", inbox / "cd20.hl7")
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # 节点访问计数按**病理号**算，不按文件数算。
    assert outcome.pipeline_visits == {node: 1 for node in CHAIN}
    # 只回写了一份草稿。
    assert outcome.drafts_written == 1
    assert len(fake_pathology.of("POST")) == 1

    # 两条结果都进了同一份草稿的同一个部位分组。
    database = Database(tmp_path / "state.db")
    try:
        drafts = _rows(database, "SELECT * FROM drafts")
        merged = _rows(
            database, "SELECT * FROM draft_entries WHERE placeholder = 0"
        )
    finally:
        database.close()
    assert len(drafts) == 1
    assert {row["item_code"] for row in merged} == {"IHC-CD3", "IHC-CD20"}


def test_same_result_delivered_twice_does_not_advance_the_chain_again(
    fake_pathology, inbox, tmp_path
):
    """同一批结果再投一遍：图不再推进，不再产出第二份草稿。

    幂等在**两层**各拦一道：入站层按幂等键认出重复（不再入账），图层按事件
    记账（同一事件不二次推进）。两道都拦，才不会出现「重投一次就多一版草稿」。
    """
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
        first = daemon.run_once()

        # 同一批结果再投一遍（文件名相同、内容相同）。
        shutil.copy(INBOX / "acc-example-0001-01-cd3.hl7", inbox / "cd3.hl7")
        second = daemon.run_once()
    finally:
        daemon.close()

    assert first.drafts_written == 1
    assert first.pipeline_visits == {node: 1 for node in CHAIN}

    # 第二轮：认出重复，图一个节点都没推进。
    assert second.duplicates == 1
    assert second.pipeline_executed == []
    assert second.pipeline_visits == {}
    assert second.drafts_written == 0

    # 出站仍然只发生一次。
    assert len(fake_pathology.of("POST")) == 1
    database = Database(tmp_path / "state.db")
    try:
        drafts = _rows(database, "SELECT * FROM drafts")
        keys = _rows(database, "SELECT * FROM idempotency_keys")
    finally:
        database.close()
    assert len(drafts) == 1
    assert len(keys) == 1


def test_same_event_id_does_not_advance_the_chain_twice(
    fake_pathology, inbox, tmp_path
):
    """同一事件号再推一次：图层自己认得出来（一个组件最多被触发一次）。

    上一条用例证的是**幂等层**把重复挡在图外（图根本没被再推）。这条证的是
    **图层**的兜底：万一重复真的进了图，同一个事件号也不会让任何节点再跑一遍。

    这里直接用守护进程自己那张节点表与它自己的节点执行体（不是另搭一套），
    否则验的就不是产品真正在跑的那张图了。
    """
    from patho_supplement.daemon import DaemonRound
    from patho_supplement.ingest.er7 import parse_message
    from patho_supplement.ingest.oru_parser import parse_oru
    from patho_supplement.pipeline import PipelineState

    raw = (INBOX / "acc-example-0001-01-cd3.hl7").read_text(encoding="utf-8")
    message = parse_message(raw, source="cd3.hl7", encoding="utf-8")
    entries = parse_oru(message, source_file="cd3.hl7")

    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, tmp_path, channel)
    try:
        outcome = DaemonRound(started_at="2026-01-01 00:00:00")
        state = PipelineState(event_id="accession:ACC-EXAMPLE-0001")
        state.set("raw_entries", tuple(entries))
        state.set("order_requests", ())

        first = daemon._pipeline.run(state, handlers=daemon._node_handlers(outcome))
        second = daemon._pipeline.run(state, handlers=daemon._node_handlers(outcome))
    finally:
        daemon.close()

    # 第一遍：整条链走通。
    assert first.executed_nodes == CHAIN
    # 第二遍：同一个事件号，一个节点都不再推进，访问计数不增。
    assert second.executed_nodes == ()
    assert {record.reason for record in second.skipped} == {
        REASON_ALREADY_VISITED
    }
    for node in CHAIN:
        assert state.visit_count(node) == 1, f"{node} 被推进了不止一次"


# ------------------------------------------------------------ 替身只在数据层


def test_fake_pathology_server_lives_only_in_the_test_data_layer():
    """替身只在测试数据层：出站客户端里不许出现本地替身的痕迹。

    只查**出站客户端**这一个模块 —— 它是替身顶替的那个位置。别的模块出现
    ``localhost`` 是正常的（默认 SMTP 主机就是本机）。
    """
    client = (
        Path(__file__).parent.parent
        / "src"
        / "patho_supplement"
        / "compose"
        / "pathology_client.py"
    )
    source = client.read_text(encoding="utf-8")
    for token in (
        "pytest",
        "unittest.mock",
        "127.0.0.1",
        "localhost",
        "fake",
        "Fake",
        "stub",
        "Stub",
    ):
        assert token not in source, f"产品代码里出现了测试专用标记：{token}"
