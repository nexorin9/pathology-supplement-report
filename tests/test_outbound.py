"""出站契约测试：GET 基线 / POST 草稿 / 目录监听 / 守护一轮。

替身只出现在**测试数据层**（标准库 ``http.server`` 起一个本地服务），
产品代码走的是与生产完全相同的一条路径 —— 没有为测试另写的分支。

覆盖任务 id 3 的验收点：

- GET 基线的请求路径、Bearer 头、响应字段映射
- POST 草稿的请求路径、Bearer 头、请求体逐字段、回执解析
- 三条失败路径：非 2xx / 超时 / 版本冲突，都是可观测的结构化错误
- 目录监听：文件落稳才读、处理完归档、同一个文件不处理两次
- 守护一轮：事件到达即取基线 → 成稿 → 真实回写 → 落 SQLite
- 取基线失败时**不丢结果**：报文留在原地，下一轮重来
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.compose.pathology_client import (
    PathologyClient,
    PathologyConflictError,
    PathologyHttpError,
    PathologyResponseError,
    PathologyTimeoutError,
)
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon, SingleInstanceLock
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 替身认识的两个病理号的基线（与脱敏样例同一套字段词汇）。
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
    "ACC-EXAMPLE-0002": {
        "accession_no": "ACC-EXAMPLE-0002",
        "base_version": "base-v1",
        "blocks": [
            {
                "region_seq": 1,
                "region_desc": "乳腺",
                "block_no": "B5",
                "order_no": "ORD-EXAMPLE-0003",
                "ordered_items": ["IHC-ER", "IHC-PR"],
            }
        ],
    },
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


def _make_handler(recorder: _Recorder):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: ANN002 - 静音标准库的访问日志
            pass

        # -------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
            recorder.add("GET", self.path, self.headers, b"")
            accession = self.path.strip("/").split("/")[3] if len(self.path.split("/")) > 3 else ""

            if accession == "ACC-MISSING":
                self._respond(404, {"message": "报告不存在"})
                return
            if accession == "ACC-BROKEN":
                self._respond_raw(200, b"<<not json>>")
                return
            if accession == "ACC-SLOW":
                time.sleep(1.5)
            if accession == "ACC-SERVER-ERROR":
                self._respond(500, {"message": "病理系统内部错误"})
                return
            if accession == "ACC-CONFLICT":
                self._respond(409, {"message": "版本冲突"})
                return

            baseline = BASELINES.get(accession)
            if accession == "ACC-WRONG":
                # 病理号与请求不一致：用来验证客户端会拒收而不是照单全收。
                baseline = BASELINES["ACC-EXAMPLE-0001"]
            if baseline is None:
                self._respond(404, {"message": "报告不存在"})
                return
            self._respond(200, baseline)

        def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            recorder.add("POST", self.path, self.headers, body)
            accession = self.path.strip("/").split("/")[3] if len(self.path.split("/")) > 3 else ""

            if accession == "ACC-CONFLICT":
                self._respond(409, {"message": "版本冲突"})
                return
            if accession == "ACC-SERVER-ERROR":
                self._respond(500, {"message": "病理系统内部错误"})
                return
            if accession == "ACC-REJECT":
                self._respond(200, {"status": "rejected", "version": "draft-v9"})
                return

            self._respond(
                200,
                {
                    "status": "accepted",
                    "version": "draft-v2",
                    "draft_id": f"SD-EXAMPLE-{accession[-4:]}",
                },
            )

        # -------------------------------------------------- 响应

        def _respond(self, status: int, payload: dict) -> None:
            self._respond_raw(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _respond_raw(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


class _FakePathology:
    """本地协议替身。"""

    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.recorder))
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


def _copy_result_files(inbox: Path, *names: str) -> None:
    for name in names:
        shutil.copy(FIXTURE_INBOX / name, inbox / name)


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


# ------------------------------------------------------------------ 出站 GET


def test_get_baseline_uses_contract_path_and_bearer_header(fake_pathology):
    client = fake_pathology.client()
    client.fetch_baseline("ACC-EXAMPLE-0001")

    request = fake_pathology.recorder.of("GET")[0]
    assert request["path"] == "/api/v1/pathology-reports/ACC-EXAMPLE-0001"
    assert request["headers"]["authorization"] == "Bearer test-token"
    assert request["headers"]["accept"] == "application/json"


def test_get_baseline_maps_response_fields(fake_pathology):
    client = fake_pathology.client()
    baseline = client.fetch_baseline("ACC-EXAMPLE-0001")

    assert baseline.accession_no == "ACC-EXAMPLE-0001"
    assert baseline.base_version == "base-v3"
    assert len(baseline.blocks) == 2
    first = baseline.blocks[0]
    assert (first.region_seq, first.region_desc, first.block_no) == (1, "子宫体", "B1")
    assert first.order_no == "ORD-EXAMPLE-0001"
    assert first.ordered_items == ("IHC-CD3", "IHC-CD20")


def test_get_baseline_missing_report_raises_http_error_with_status(fake_pathology):
    client = fake_pathology.client()
    with pytest.raises(PathologyHttpError) as excinfo:
        client.fetch_baseline("ACC-MISSING")
    assert excinfo.value.status_code == 404


def test_get_baseline_invalid_json_raises_response_error(fake_pathology):
    client = fake_pathology.client()
    with pytest.raises(PathologyResponseError):
        client.fetch_baseline("ACC-BROKEN")


def test_get_baseline_timeout_raises_timeout_error(fake_pathology):
    client = fake_pathology.client(timeout=0.2)
    with pytest.raises(PathologyTimeoutError):
        client.fetch_baseline("ACC-SLOW")


def test_get_baseline_rejects_mismatched_accession(fake_pathology):
    """替身返回别的病理号时必须报错，不能把别的报告当这份报告的基线。"""
    client = fake_pathology.client()
    with pytest.raises(PathologyResponseError):
        client.fetch_baseline("ACC-WRONG")


# ------------------------------------------------------------------ 出站 POST


def test_post_draft_sends_contract_body_fields(fake_pathology):
    client = fake_pathology.client()
    client.post_supplemental_draft(
        "ACC-EXAMPLE-0001",
        body="病理号：ACC-EXAMPLE-0001\n补充报告草稿（版本 1）\n",
        base_version="base-v3",
        entries=[
            {
                "item_code": "IHC-CD3",
                "value": "阴性",
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "source_system": "IMMUNO-LAB",
                "observed_at": "20260101130000",
                "placeholder": False,
            }
        ],
    )

    request = fake_pathology.recorder.of("POST")[0]
    assert (
        request["path"]
        == "/api/v1/pathology-reports/ACC-EXAMPLE-0001/supplemental-drafts"
    )
    assert request["headers"]["authorization"] == "Bearer test-token"
    assert request["headers"]["content-type"].startswith("application/json")

    payload = json.loads(request["body"].decode("utf-8"))
    assert payload["accession_no"] == "ACC-EXAMPLE-0001"
    assert payload["base_version"] == "base-v3"
    assert "补充报告草稿" in payload["body"]
    assert payload["entries"][0]["item_code"] == "IHC-CD3"
    assert payload["entries"][0]["value"] == "阴性"
    assert payload["entries"][0]["block_no"] == "B1"
    assert payload["entries"][0]["source_system"] == "IMMUNO-LAB"


def test_post_draft_parses_receipt(fake_pathology):
    client = fake_pathology.client()
    receipt = client.post_supplemental_draft(
        "ACC-EXAMPLE-0001", body="草稿正文", base_version="base-v3"
    )
    assert receipt.accepted is True
    assert receipt.status == "accepted"
    assert receipt.version == "draft-v2"
    assert receipt.draft_id == "SD-EXAMPLE-0001"


def test_post_draft_conflict_raises_conflict_error(fake_pathology):
    client = fake_pathology.client()
    with pytest.raises(PathologyConflictError) as excinfo:
        client.post_supplemental_draft("ACC-CONFLICT", body="草稿正文")
    assert excinfo.value.status_code == 409


def test_post_draft_server_error_raises_http_error(fake_pathology):
    client = fake_pathology.client()
    with pytest.raises(PathologyHttpError) as excinfo:
        client.post_supplemental_draft("ACC-SERVER-ERROR", body="草稿正文")
    assert excinfo.value.status_code == 500


def test_post_draft_rejected_receipt_raises_response_error(fake_pathology):
    client = fake_pathology.client()
    with pytest.raises(PathologyResponseError):
        client.post_supplemental_draft("ACC-REJECT", body="草稿正文")


# ------------------------------------------------------------------ 目录监听


def test_watcher_waits_until_file_stops_changing(inbox: Path):
    target = inbox / "result.hl7"
    target.write_text("MSH|^~\\&|A|B|C|D|20260101130500||ORU^R01|1|P|2.5.1\n", encoding="utf-8")

    watcher = InboxWatcher(inbox, settle_seconds=60.0)
    assert watcher.scan() == []  # 刚写完，还在变

    time.sleep(0.01)
    target.write_text(
        "MSH|^~\\&|A|B|C|D|20260101130500||ORU^R01|1|P|2.5.1\nOBR|1|A|B|C\n", encoding="utf-8"
    )
    assert watcher.scan() == []  # 又变了，重新计时

    # 兜底扫描：修改时间早已超过静默期，直接采信。
    old = time.time() - 3600
    os.utime(target, (old, old))
    assert watcher.scan() == [target]


def test_watcher_picks_up_file_that_already_settled(inbox: Path):
    """守护进程重启后，目录里躺着的旧文件第一轮就该被取走，不必再等一轮。"""
    target = inbox / "old.hl7"
    target.write_text("MSH|^~\\&|A|B|C|D|20260101130500||ORU^R01|1|P|2.5.1\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(target, (old, old))

    watcher = InboxWatcher(inbox, settle_seconds=30.0)
    assert watcher.scan() == [target]


def test_watcher_archives_processed_file_and_does_not_reprocess(inbox: Path):
    target = inbox / "result.hl7"
    target.write_text("MSH|^~\\&|A|B|C|D|20260101130500||ORU^R01|1|P|2.5.1\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(target, (old, old))

    watcher = InboxWatcher(inbox, settle_seconds=0.0)
    ready = watcher.scan()
    assert ready == [target]

    archived = watcher.mark_processed(target)
    assert archived.exists()
    assert archived.parent.name == "processed"
    assert not target.exists()
    assert watcher.scan() == []


# ------------------------------------------------------------------ 守护一轮


def _daemon(
    config: Config,
    fake_pathology: _FakePathology,
    inbox: Path,
    tmp_path: Path,
    messages: list[str],
) -> Daemon:
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=fake_pathology.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=messages.append,
    )


def test_daemon_parses_order_side_message(fake_pathology, inbox, tmp_path):
    """申请侧报文：解析出申请条目；写错的那一条拒收并记审计，好的一条照常。"""
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-order.orm.hl7", inbox)
    (inbox / "bad.orm.hl7").write_text(
        "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|20260101101500"
        "||ORM^O01|ORDMSG-EXAMPLE-0002|P|2.5.1\r\n"
        "ORC|XX|ORD-EXAMPLE-0002\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0002|IHC-KI67^KI-67^L|||20260101101600\r\n",
        encoding="utf-8",
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    # 一份好的申请解析出来、一条写错的被拒收
    assert outcome.parsed_orders == 1
    assert outcome.order_malformed == 1
    assert len(outcome.order_rejected) == 1
    assert "ORC-1" in outcome.order_rejected[0][1]

    # 申请侧报文归档到 deferred/，不进结果处理
    assert len(outcome.deferred) == 2
    assert (inbox / "deferred" / "acc-example-0001-order.orm.hl7").exists()
    assert not fake_pathology.recorder.of("POST")

    # 拒收原因进审计流水
    with Database(tmp_path / "state.db") as db:
        rows = db.query_all("SELECT event, detail_json FROM audit_log")
        rejected = [row for row in rows if row["event"] == "order_rejected"]
        assert len(rejected) == 1
        assert "illegal_control_action" in rejected[0]["detail_json"]


def test_daemon_round_writes_draft_back_to_pathology(fake_pathology, inbox, tmp_path):
    _copy_result_files(
        inbox, "acc-example-0001-01-cd3.hl7", "acc-example-0001-03-ki67-no-block.hl7"
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    # 一次真实出站回写
    posts = fake_pathology.recorder.of("POST")
    assert len(posts) == 1
    payload = json.loads(posts[0]["body"].decode("utf-8"))
    assert payload["accession_no"] == "ACC-EXAMPLE-0001"
    assert payload["base_version"] == "base-v3"
    assert payload["entries"][0]["item_code"] == "IHC-CD3"

    # 状态只落 SQLite
    with Database(tmp_path / "state.db") as db:
        draft = db.query_one("SELECT * FROM drafts WHERE accession_no = ?", ("ACC-EXAMPLE-0001",))
        assert draft["state"] == "written_back"
        # 一条并入的结果 + 三项「待回报」占位（基线申请了 CD20×2、CD3、CK7，
        # 只回来了 CD3）—— 少写一项，医师就会以为这项没做
        assert draft["entry_count"] == 4
        assert (
            db.query_value("SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0")
            == 1
        )
        assert (
            db.query_value("SELECT COUNT(*) FROM draft_entries WHERE placeholder = 1")
            == 3
        )
        assert db.query_value("SELECT COUNT(*) FROM pending_items") == 1
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 2

    # 产物落盘（版本号从基线的基础版本号派生并递增：base-v3 → v4）
    draft_file = tmp_path / "out" / "drafts" / "ACC-EXAMPLE-0001-v4.txt"
    assert draft_file.exists()
    assert "IHC-CD3" in draft_file.read_text(encoding="utf-8")
    assert (tmp_path / "out" / "pending_list.txt").exists()

    # 报文归档；申请侧事件不混进来
    assert (inbox / "processed" / "acc-example-0001-01-cd3.hl7").exists()
    assert (inbox / "processed" / "acc-example-0001-03-ki67-no-block.hl7").exists()
    assert outcome.drafts_written == 1
    assert outcome.pending_count == 1
    assert any("回写成功" in message for message in messages)


def test_daemon_round_reports_four_level_match_distribution(fake_pathology, inbox, tmp_path):
    """守护每轮摘要里能看到四级判定分布。

    L3（靠部位描述的文字相似猜上的）置信是「低」，值班的人要重点复核这几条 ——
    所以分布必须一眼可见，不能只藏在明细里。
    """
    _copy_result_files(
        inbox, "acc-example-0001-01-cd3.hl7", "acc-example-0001-03-ki67-no-block.hl7"
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    levels = outcome.match_levels
    assert levels["L1_block_exact"] == 1
    assert levels["L4_pending"] == 1
    assert levels["L2_region_normalized"] == 0
    assert levels["L3_region_fuzzy"] == 0
    assert sum(levels.values()) == 2
    assert any("对齐判定" in message for message in messages)


def test_daemon_rejects_malformed_message_and_audits_it(fake_pathology, inbox, tmp_path):
    """畸形报文：拒收、计数、落审计，绝不混进草稿。"""
    (inbox / "bad.hl7").write_text(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500|||MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\r\n",
        encoding="utf-8",
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    outcome = daemon.run_once()
    daemon.close()

    assert outcome.malformed == 1
    assert len(outcome.rejected) == 1
    assert outcome.drafts_written == 0
    assert not fake_pathology.recorder.of("POST"), "坏报文不该触发回写"
    assert (inbox / "rejected" / "bad.hl7").exists()

    with Database(tmp_path / "state.db") as db:
        rows = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'malformed'"
        )
        assert len(rows) == 1
        assert "missing_message_type" in rows[0]["detail_json"]
        assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_daemon_keeps_not_obtained_result_out_of_draft(fake_pathology, inbox, tmp_path):
    """无法获得（OBX-11=X）：已回报但写不进报告 —— 进待人工处理清单，不出草稿。"""
    (inbox / "not-obtained.hl7").write_text(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\r\n"
        "ZBR|B1|子宫体|1\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||标本溶血，无法获得||||||X|||20260101130000\r\n",
        encoding="utf-8",
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    outcome = daemon.run_once()
    daemon.close()

    assert outcome.accepted == 1
    assert outcome.withheld == 1
    assert outcome.drafts_written == 0
    assert not fake_pathology.recorder.of("POST"), "无法获得的结果不该回写成草稿"
    assert outcome.pending_count == 1

    with Database(tmp_path / "state.db") as db:
        assert db.query_value("SELECT COUNT(*) FROM drafts") == 0
        row = db.query_one("SELECT reason FROM pending_items")
        assert row["reason"] == "结果无法获得"
        # 已回报就有归宿：幂等键落库，不重复进草稿
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 1


def test_daemon_derives_pending_queries_into_state_db(fake_pathology, inbox, tmp_path):
    """守护一轮：待查项按「基线已申请项 + 申请侧事件」派生，落进状态库。"""
    _copy_result_files(inbox, "acc-example-0001-01-cd3.hl7")
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-order.orm.hl7", inbox)
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    messages: list[str] = []
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, messages)

    outcome = daemon.run_once()
    daemon.close()

    # 基线四块项目（B1 两项 + B2 两项）＝ 4 条待查项；申请侧 NW 与基线同项，合成后仍是 4 条
    assert outcome.derived_queries == 4
    assert outcome.awaiting_queries == 4

    with Database(tmp_path / "state.db") as db:
        rows = db.query_all(
            "SELECT item_code, region_seq, block_no, state FROM pending_queries"
            " WHERE accession_no = ? ORDER BY region_seq, block_no, item_code",
            ("ACC-EXAMPLE-0001",),
        )
    assert len(rows) == 4
    # 同一个项目在两块上各一条 —— 不是重复派生
    assert {row["item_code"] for row in rows} == {"IHC-CD3", "IHC-CD20", "IHC-CK7"}
    assert {row["block_no"] for row in rows} == {"B1", "B2"}
    assert {row["state"] for row in rows} == {"awaiting"}


def test_daemon_keeps_result_file_when_baseline_unreachable(fake_pathology, inbox, tmp_path):
    """取基线失败时结果不能丢：文件留在原地，下一轮重来。"""
    target = inbox / "acc-broken.hl7"
    target.write_text(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|M1|P|2.5.1\n"
        "OBR|1|ACC-SERVER-ERROR|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\n"
        "ZBR|B1|子宫体|1\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\n",
        encoding="utf-8",
    )
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    outcome = daemon.run_once()
    daemon.close()

    assert outcome.drafts_written == 0
    assert outcome.drafts_failed == 1
    assert target.exists(), "取基线失败时报文必须留在原地"
    assert not (inbox / "processed" / target.name).exists()

    with Database(tmp_path / "state.db") as db:
        assert db.query_value("SELECT COUNT(*) FROM drafts") == 0
        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 0


def test_daemon_does_not_process_same_file_twice(fake_pathology, inbox, tmp_path):
    _copy_result_files(inbox, "acc-example-0001-01-cd3.hl7")
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    daemon.run_once()
    second = daemon.run_once()
    daemon.close()

    assert len(fake_pathology.recorder.of("POST")) == 1
    assert second.files_seen == 0
    assert second.drafts_written == 0


def test_daemon_rejects_malformed_message_into_rejected_dir(fake_pathology, inbox, tmp_path):
    bad = inbox / "broken.hl7"
    bad.write_text("OBR|1|ACC-EXAMPLE-0001|ORD-1|IHC-CD3\n", encoding="utf-8")
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    daemon = _daemon(config, fake_pathology, inbox, tmp_path, [])

    outcome = daemon.run_once()
    daemon.close()

    assert len(outcome.rejected) == 1
    assert not bad.exists()
    assert (inbox / "rejected" / "broken.hl7").exists()
    assert len(fake_pathology.recorder.of("POST")) == 0


# ------------------------------------------------------------------ 自检与锁


def test_doctor_lists_five_interfaces_and_reports_missing_credentials(
    fake_pathology, capsys, monkeypatch
):
    from patho_supplement.cli import main

    monkeypatch.setenv("PATHOLOGY_BASE_URL", fake_pathology.base_url)
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "test-token")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", "/tmp/inbox")

    code = main(["doctor"])
    captured = capsys.readouterr().out
    assert code == 0
    assert "1. [入站] ORU^R01 结果事件流" in captured
    assert "2. [入站] ORM^O01 申请事件流" in captured
    assert "3. [出站] GET 病理报告基线" in captured
    assert "4. [出站] POST 补充报告草稿" in captured
    assert "5. [推送] SMTP / 企业微信通知" in captured
    assert "TCP 连通：可达" in captured
    assert "自检通过" in captured


def test_doctor_exits_non_zero_when_credentials_missing(capsys, monkeypatch):
    from patho_supplement.cli import main

    for key in ("PATHOLOGY_BASE_URL", "PATHOLOGY_API_TOKEN", "PATHOLOGY_RESULT_INBOX"):
        monkeypatch.delenv(key, raising=False)

    code = main(["doctor"])
    captured = capsys.readouterr()
    assert code == 1
    assert "PATHOLOGY_API_TOKEN" in captured.err
    assert "1. [入站] ORU^R01 结果事件流" in captured.out


def test_single_instance_lock_rejects_second_holder(tmp_path):
    lock_path = tmp_path / "patho_supplement.lock"
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert first.holder_pid().isdigit()
    finally:
        first.release()
    assert second.acquire() is True
    second.release()
