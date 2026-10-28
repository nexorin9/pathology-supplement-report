"""图式装配：节点表 + 两道闸门 + 断点挂起点接入。

覆盖任务 id 12 的验收点：

- **节点表**：八个节点各自声明必需输入与产出，顺序即拓扑序；
- **闸门一**：基线没取回 → 停在 ``align`` 之前；没有结果到达 → 停在 ``align``
  之前；停住要看得见（跳过流水带缺哪几个输入）；
- **闸门二**：触发源只有图外输入与定时器（前驱喂入是图内推进，不算触发源）；
  **同一事件不二次推进** —— 同一事件再推一次，节点访问计数只增一次；
- **断点**：``align`` 节点的未命中出口接到挂起落库（快照持久化）；
- **装配**：``compose`` / ``writeback`` / ``notify`` 接到守护主循环 —— 真实
  出站回写 + 通知当班医师。

替身只出现在**测试数据层**（标准库 ``http.server`` 起一个本地服务），产品
代码走的是与生产完全相同的一条路径。
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
from patho_supplement.notify.dispatcher import NotifyDispatcher
from patho_supplement.pipeline import (
    NODE_ALIGN,
    NODE_COMPOSE,
    NODE_DERIVE_ORDERS,
    NODE_FETCH_BASELINE,
    NODE_FANOUT_RETRIEVE,
    NODE_INGEST,
    NODE_NOTIFY,
    NODE_SPECS,
    NODE_WRITEBACK,
    REASON_ALREADY_VISITED,
    REASON_MISSING_INPUTS,
    REASON_NOT_TRIGGERED,
    TRIGGER_EXTERNAL,
    TRIGGER_PREDECESSOR,
    TRIGGER_TIMER,
    NodeSpec,
    Pipeline,
    PipelineState,
    are_all_sockets_ready,
    has_any_trigger,
)

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

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


# ------------------------------------------------------------------ 节点表


def test_node_table_lists_eight_nodes_in_chain_order():
    """节点表就是一条报告的处理链，顺序不能乱。"""
    assert [spec.name for spec in NODE_SPECS] == [
        NODE_INGEST,
        NODE_FETCH_BASELINE,
        NODE_DERIVE_ORDERS,
        NODE_FANOUT_RETRIEVE,
        NODE_ALIGN,
        NODE_COMPOSE,
        NODE_WRITEBACK,
        NODE_NOTIFY,
    ]


def test_every_node_declares_required_inputs_and_outputs():
    """每个节点都要说清「我要什么」与「我产出什么」。"""
    for spec in NODE_SPECS:
        assert spec.required_inputs, f"{spec.name} 没声明必需输入"
        assert spec.outputs, f"{spec.name} 没声明产出"
        assert spec.label, f"{spec.name} 没声明中文标签"


def test_node_table_is_topologically_ordered():
    """每个节点的必需输入，要么是图外输入，要么由前面的节点产出。"""
    produced: set[str] = set()
    for spec in NODE_SPECS:
        for name in spec.required_inputs:
            if name in spec.external_inputs:
                continue
            assert name in produced, f"{spec.name} 的必需输入 {name} 没有前驱产出"
        produced.update(spec.outputs)


def test_align_requires_baseline_and_arrived_results():
    """对齐节点的两条硬前置：基线到手、有结果到达。"""
    spec = next(s for s in NODE_SPECS if s.name == NODE_ALIGN)
    assert "baseline" in spec.required_inputs
    assert "all_entries" in spec.required_inputs
    assert "all_entries" in spec.requires_nonempty


# ------------------------------------------------------------ 闸门一（输入）


def test_gate_one_reports_missing_required_input():
    """闸门一：必需输入没到齐 → 不齐，并点名缺哪几个。"""
    spec = NodeSpec(name="demo", label="演示", required_inputs=("a", "b"))
    state = PipelineState(event_id="e1")
    state.set("a", 1)

    ready, missing = are_all_sockets_ready(spec, state)

    assert ready is False
    assert missing == ("b",)


def test_gate_one_treats_empty_collection_as_absent():
    """键到了但内容是空的，等于没到（无结果到达 ≠ 有空结果）。"""
    spec = NodeSpec(
        name="demo", label="演示", required_inputs=("a",), requires_nonempty=("a",)
    )
    state = PipelineState(event_id="e1")
    state.set("a", ())

    ready, missing = are_all_sockets_ready(spec, state)

    assert ready is False
    assert missing == ("a",)


def test_missing_baseline_stops_before_align():
    """基线没取回 → 停在 align 之前，跳过流水点名缺 baseline。"""
    spec = next(s for s in NODE_SPECS if s.name == NODE_ALIGN)
    state = PipelineState(event_id="e1")
    state.set("result_entries", ("x",))
    state.set("all_entries", ("x",))
    # 没有 baseline。

    ready, missing = are_all_sockets_ready(spec, state)

    assert ready is False
    assert missing == ("baseline",)


def test_no_result_arrived_stops_before_align():
    """没有结果到达 → 停在 align 之前。"""
    spec = next(s for s in NODE_SPECS if s.name == NODE_ALIGN)
    state = PipelineState(event_id="e1")
    state.set("baseline", object())
    state.set("result_entries", ())
    state.set("all_entries", ())

    ready, missing = are_all_sockets_ready(spec, state)

    assert ready is False
    assert "all_entries" in missing


# ---------------------------------------------------------- 闸门二（触发源）


def test_trigger_from_predecessor():
    """前驱在本事件里喂了输入 → 触发。"""
    spec = NodeSpec(name="demo", label="演示", required_inputs=("a",))
    state = PipelineState(event_id="e1")
    state.set("a", 1)
    state.mark_produced("a", "upstream")

    triggered, source = has_any_trigger(spec, state)

    assert triggered is True
    assert source == TRIGGER_PREDECESSOR


def test_trigger_from_external_input_on_first_visit():
    """图外输入只在本事件首次访问时构成触发源。"""
    spec = NodeSpec(
        name="demo", label="演示", required_inputs=("a",), external_inputs=("a",)
    )
    state = PipelineState(event_id="e1")
    state.set("a", 1)

    triggered, source = has_any_trigger(spec, state)

    assert triggered is True
    assert source == TRIGGER_EXTERNAL


def test_trigger_from_timer_for_node_without_inputs():
    """没有输入的节点由定时器带起来（图自己的周期扫描）。"""
    spec = NodeSpec(name="demo", label="演示")
    state = PipelineState(event_id="e1")

    triggered, source = has_any_trigger(spec, state)

    assert triggered is True
    assert source == TRIGGER_TIMER


def test_stale_value_from_earlier_event_does_not_trigger():
    """跨事件留下的旧值不算前驱喂入 —— 不然上一轮的结果会反复触发下一轮。"""
    spec = NodeSpec(name="demo", label="演示", required_inputs=("a",))
    state = PipelineState(event_id="e2")
    state.set("a", 1)
    state.mark_produced("a", "upstream", event_id="e1")  # 上一轮留下的

    triggered, _ = has_any_trigger(spec, state)

    assert triggered is False


def test_node_without_trigger_is_skipped():
    """没有触发源的节点不跑，并记下原因。"""
    spec = NodeSpec(name="demo", label="演示", required_inputs=("a",))
    pipeline = Pipeline(specs=(spec,))
    state = PipelineState(event_id="e1")
    state.set("a", 1)

    run = pipeline.run(state)

    assert run.executed == []
    assert run.skipped[0].reason == REASON_NOT_TRIGGERED


# ------------------------------------------------- 同一事件不二次推进


def test_same_event_delivered_twice_does_not_advance_again():
    """同一事件再推一次：节点访问计数只增一次，第二次全部跳过。"""
    calls: list[str] = []

    def handler(state: PipelineState):
        calls.append("hit")
        return {"out": 1}

    spec = NodeSpec(name="demo", label="演示", external_inputs=("a",), outputs=("out",))
    pipeline = Pipeline(specs=(spec,))
    state = PipelineState(event_id="evt-1")
    state.set("a", 1)

    first = pipeline.run(state, handlers={"demo": handler})
    second = pipeline.run(state, handlers={"demo": handler})

    assert first.executed_nodes == ("demo",)
    assert second.executed_nodes == ()
    assert calls == ["hit"], "同一事件不该把节点再跑一遍"
    assert state.visit_count("demo") == 1
    assert {record.reason for record in second.skipped} == {REASON_ALREADY_VISITED}


def test_different_event_advances_again():
    """换一个事件号 → 节点可以再推进（不同事件是不同的一批结果）。"""
    spec = NodeSpec(name="demo", label="演示", external_inputs=("a",), outputs=("out",))
    pipeline = Pipeline(specs=(spec,))

    state_one = PipelineState(event_id="evt-1")
    state_one.set("a", 1)
    state_two = PipelineState(event_id="evt-2")
    state_two.set("a", 1)

    pipeline.run(state_one, handlers={"demo": lambda st: {"out": 1}})
    pipeline.run(state_two, handlers={"demo": lambda st: {"out": 1}})

    assert state_one.visit_count("demo") == 1
    assert state_two.visit_count("demo") == 1


def test_node_error_does_not_collapse_the_graph():
    """一个节点报错不掀翻整张图：后面的节点因拿不到产出而停住。"""

    def boom(state: PipelineState):
        raise ValueError("节点炸了")

    first = NodeSpec(name="a", label="甲", external_inputs=("x",), outputs=("y",))
    second = NodeSpec(name="b", label="乙", required_inputs=("y",), outputs=("z",))
    pipeline = Pipeline(specs=(first, second))
    state = PipelineState(event_id="evt-1")
    state.set("x", 1)

    run = pipeline.run(state, handlers={"a": boom, "b": lambda st: {"z": 1}})

    assert run.executed_nodes == ()
    assert any("节点炸了" in message for message in run.errors)
    reasons = {record.node: record.reason for record in run.skipped}
    assert reasons["a"] == "节点执行报错"
    assert reasons["b"] == REASON_MISSING_INPUTS


# ---------------------------------------------------------- 守护主循环装配


class _Recorder:
    """记录替身收到的每个请求，供断言逐条核对。"""

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
    """本地协议替身（只在本测试数据层出现）。"""

    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _make_handler():
        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库的访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                self.server.req_recorder.add("GET", self.path, self.headers, b"")
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
                self.server.req_recorder.add("POST", self.path, self.headers, body)
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

    def client(self, *, token: str = "test-token", timeout: float = 2.0):
        from patho_supplement.compose.pathology_client import PathologyClient

        return PathologyClient(self.base_url, token, timeout=timeout)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def fake_pathology():
    fake = _FakePathology()
    fake.server.req_recorder = fake.recorder
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    directory = tmp_path / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class _RecordingChannel:
    """记录发出的通知，供断言核对。"""

    def __init__(self) -> None:
        self.messages: list = []

    def send(self, message) -> None:  # noqa: ANN001 - 通道接口
        self.messages.append(message)

    @property
    def name(self) -> str:
        return "recording"


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


def _daemon(config: Config, fake, inbox: Path, tmp_path: Path, channel) -> Daemon:
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=fake.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=Database(tmp_path / "notify.db")),
    )


def test_daemon_round_runs_the_whole_node_chain(fake_pathology, inbox, tmp_path):
    """正常一轮：八个节点按链推进，回写与通知都真发生。"""
    shutil.copy(
        FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox / "acc-example-0001-01-cd3.hl7"
    )
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # 链上的每个节点都推进了一次。
    for node in (
        NODE_INGEST,
        NODE_FETCH_BASELINE,
        NODE_DERIVE_ORDERS,
        NODE_FANOUT_RETRIEVE,
        NODE_ALIGN,
        NODE_COMPOSE,
        NODE_WRITEBACK,
        NODE_NOTIFY,
    ):
        assert outcome.pipeline_visits.get(node) == 1, f"{node} 没推进"

    # compose / writeback / notify 接到守护主循环：真的回写了，真的通知了。
    assert outcome.drafts_written == 1
    posts = fake_pathology.recorder.of("POST")
    assert len(posts) == 1
    assert posts[0]["path"].endswith("/supplemental-drafts")
    assert channel.messages, "成稿后应当通知当班医师"

    # 召回层的路况在摘要里看得见。
    assert outcome.fanout["routes_total"] >= 1
    assert outcome.fanout["routes_failed"] == 0


def test_daemon_stops_before_align_when_baseline_unavailable(
    fake_pathology, inbox, tmp_path
):
    """取不到基线 → 停在 align 之前：不猜、不成稿、结果不丢。"""
    (inbox / "acc-missing-01.hl7").write_text(
        "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101130500||ORU^R01|MSG-MISSING|P|2.5.1\r\n"
        "OBR|1|ACC-MISSING|ORD-MISSING|IHC-CD3^CD3^L|||20260101120000\r\n"
        "ZBR|B1|子宫体|1\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n",
        encoding="utf-8",
    )
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # 没有成稿、没有回写、没有通知。
    assert outcome.drafts_written == 0
    assert fake_pathology.recorder.of("POST") == []
    assert channel.messages == []

    # align 根本没被推进 —— 停在它之前，跳过流水点名缺 baseline。
    assert outcome.pipeline_visits.get(NODE_ALIGN) is None
    skips = {item["node"]: item for item in outcome.pipeline_skips}
    assert skips[NODE_ALIGN]["reason"] == REASON_MISSING_INPUTS
    assert "baseline" in skips[NODE_ALIGN]["missing"]
    # 下游节点也跟着停住。
    assert NODE_COMPOSE in skips and NODE_WRITEBACK in skips

    # 结果没丢：报文留在原地，下一轮重来。
    assert (inbox / "acc-missing-01.hl7").exists()


def test_daemon_align_unmatched_exit_lands_in_pending_store(
    fake_pathology, inbox, tmp_path
):
    """align 节点的未命中出口 → 挂起落库（一条都不丢）。"""
    shutil.copy(
        FIXTURE_INBOX / "acc-example-0001-03-ki67-no-block.hl7",
        inbox / "acc-example-0001-03-ki67-no-block.hl7",
    )
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, channel)
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert outcome.pending_count == 1
    database = Database(tmp_path / "state.db")
    try:
        rows = database.query_all("SELECT * FROM pending_items")
    finally:
        database.close()
    assert len(rows) == 1
    assert rows[0]["accession_no"] == "ACC-EXAMPLE-0001"
    assert rows[0]["item_code"] == "IHC-KI67"
    # 没对上 → 不进草稿。
    assert outcome.drafts_written == 0


def test_daemon_records_pipeline_skips_in_round_outcome(
    fake_pathology, inbox, tmp_path
):
    """停在哪、缺什么，运维在运行摘要与状态库里都能查到。"""
    (inbox / "acc-missing-01.hl7").write_text(
        "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101130500||ORU^R01|MSG-MISSING|P|2.5.1\r\n"
        "OBR|1|ACC-MISSING|ORD-MISSING|IHC-CD3^CD3^L|||20260101120000\r\n"
        "ZBR|B1|子宫体|1\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n",
        encoding="utf-8",
    )
    channel = _RecordingChannel()
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, channel)
    try:
        outcome = daemon.run_once()
        summary = daemon.render_round(outcome)
    finally:
        daemon.close()

    assert outcome.pipeline_skips
    assert "节点停步" in summary
    assert NODE_ALIGN in summary
