"""结构化行日志 + 每轮运行摘要（任务 id 42）。

四个验收点：

- **行日志格式**：一行一件事 —— 时间 / 级别 / 事件 / 字段，字段是 ``键=值``；
- **每轮运行摘要**：处理条数 / 对齐条数 / 挂起条数 / 写回成功 / 写回失败 / 耗时；
- **敏感字段不入日志**：结果值默认遮蔽，患者可识别字段整条丢掉；
- **跑完一轮有摘要**：守护跑完一轮，日志里有摘要行，且日志里没有结果值。

替身（本地病理系统）只出现在本测试文件里 —— 标准库 ``http.server`` 起一个
本地服务，产品代码走的是与生产完全相同的一条路径，没有为测试另写的分支。
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from patho_supplement.compose.pathology_client import PathologyClient
from patho_supplement.config import Config, ConfigError
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.logging_setup import (
    LOGGER_NAME,
    PLACEHOLDER_RESULT_VALUE,
    RoundSummary,
    StructuredLogger,
    build_round_summary,
    format_line,
    redact_fields,
    setup_logging,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_INBOX = PROJECT_ROOT / "tests" / "fixtures" / "inbox"

#: 替身认识的基线（与脱敏样例同一套字段词汇）。
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


# ------------------------------------------------------------------ 替身


class _FakePathology:
    """本地协议替身（只在测试数据层）。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def client(self) -> PathologyClient:
        return PathologyClient(self.base_url, "test-token", timeout=2.0)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _make_handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                outer.requests.append(("GET", self.path, b""))
                self._respond(200, BASELINE)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(("POST", self.path, body))
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


@pytest.fixture(autouse=True)
def _restore_logging():
    """本模块会改装日志器（挂 handler、改 propagate）—— 用完还原，
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


# ------------------------------------------------------------------ 行格式


def test_line_has_timestamp_level_event_and_key_value_fields():
    stream = io.StringIO()
    logger = setup_logging(level="INFO", stream=stream)

    logger.event("draft_written", accession_no="ACC-EXAMPLE-0001", entries=3)

    line = stream.getvalue().strip()
    parts = line.split(" | ")
    assert len(parts) == 3, line
    assert parts[0][:4].isdigit() and " " in parts[0]  # 2026-10-28 12:34:56
    assert parts[1].strip() == "INFO"
    assert parts[2] == "event=draft_written accession_no=ACC-EXAMPLE-0001 entries=3"


def test_field_order_is_stable_across_calls():
    stream = io.StringIO()
    logger = setup_logging(level="INFO", stream=stream)

    logger.event("round_summary", processed=1, aligned=2, pending=3)
    logger.event("round_summary", processed=1, aligned=2, pending=3)

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split(" | ")[2] == lines[1].split(" | ")[2]


def test_values_with_spaces_are_quoted():
    line = format_line("draft_written", {"detail": "回写成功 1 份"})
    assert 'detail="回写成功 1 份"' in line


def test_level_filters_debug_but_keeps_warning():
    stream = io.StringIO()
    logger = setup_logging(level="INFO", stream=stream)

    logger.debug("tick", n=1)
    logger.info("tick", n=2)
    logger.warning("boom", n=3)

    text = stream.getvalue()
    assert "n=1" not in text
    assert "n=2" in text
    assert "n=3" in text
    assert "WARNING" in text


def test_setup_logging_writes_to_file_and_does_not_duplicate_lines(tmp_path):
    log_file = tmp_path / "logs" / "daemon.log"

    # 反复装日志（配置重载、嵌入方各装一次）之后记一条事件 ——
    # 这一行只该出现一次：旧 handler 没摘干净的话会写两遍。
    setup_logging(level="INFO", path=log_file)
    setup_logging(level="INFO", path=log_file)
    setup_logging(level="INFO", path=log_file).event("round_start")

    text = log_file.read_text(encoding="utf-8")
    assert text.count("event=round_start") == 1


def test_unknown_log_level_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        setup_logging(level="TRACE", stream=io.StringIO())
    assert "TRACE" in str(excinfo.value)


# ------------------------------------------------------------------ 脱敏


def test_patient_identifiable_fields_are_dropped_entirely():
    fields = redact_fields(
        {
            "patient_name": "张三",
            "姓名": "李四",
            "idcard": "110101199001011234",
            "phone": "13812345678",
            "item_code": "IHC-CD3",
        }
    )

    assert "patient_name" not in fields
    assert "姓名" not in fields
    assert "idcard" not in fields
    assert "phone" not in fields
    assert fields["item_code"] == "IHC-CD3"


def test_result_values_are_masked_by_default():
    fields = redact_fields({"value": "阴性", "item_code": "IHC-CD3"})

    assert fields["value"] == PLACEHOLDER_RESULT_VALUE
    assert "阴性" not in json.dumps(fields, ensure_ascii=False)


def test_result_values_need_an_explicit_switch():
    fields = redact_fields({"value": "阴性"}, include_result_values=True)
    assert fields["value"] == "阴性"


def test_free_text_identifiers_are_masked():
    fields = redact_fields({"detail": "回报单 110101199001011234 手机 13812345678"})

    text = fields["detail"]
    assert "110101199001011234" not in text
    assert "13812345678" not in text


def test_redaction_is_idempotent():
    once = redact_fields({"detail": "手机 13812345678", "value": "阴性"})
    twice = redact_fields(once)
    assert once == twice


def test_structured_logger_masks_result_values_by_default():
    stream = io.StringIO()
    logger = StructuredLogger(
        logging.getLogger("patho_supplement.test"), include_result_values=False
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.logger.addHandler(handler)
    logger.logger.setLevel(logging.INFO)

    logger.event("probe", value="阴性")

    assert "阴性" not in stream.getvalue()
    assert PLACEHOLDER_RESULT_VALUE in stream.getvalue()


# ------------------------------------------------------------------ 运行摘要


def test_round_summary_carries_the_five_counters_and_elapsed():
    summary = build_round_summary(
        SimpleNamespace(
            accepted=7,
            rationale_count=5,
            pending_count=1,
            drafts_written=1,
            drafts_failed=0,
            elapsed_seconds=0.42,
        )
    )

    assert summary.processed == 7
    assert summary.aligned == 5
    assert summary.pending == 1
    assert summary.writeback_ok == 1
    assert summary.writeback_failed == 0
    assert summary.elapsed_seconds == 0.42

    fields = summary.as_fields()
    for key in (
        "processed",
        "aligned",
        "pending",
        "writeback_ok",
        "writeback_failed",
        "elapsed_seconds",
    ):
        assert key in fields

    line = summary.summary_line()
    assert "处理 7 条" in line
    assert "对齐 5 条" in line
    assert "挂起 1 条" in line
    assert "写回成功 1 份" in line
    assert "耗时 0.42 秒" in line


def test_round_summary_reads_pending_from_a_list_when_no_counter():
    summary = build_round_summary(
        SimpleNamespace(accepted=3, rationale_count=2, pending=["a", "b"])
    )

    assert summary.pending == 2
    assert summary.writeback_ok == 0  # 批处理重放不回写病理系统


def test_round_summary_is_json_serialisable():
    summary = build_round_summary(SimpleNamespace(accepted=1, elapsed_seconds=0.1))
    payload = json.loads(json.dumps(summary.as_dict(), ensure_ascii=False))
    assert payload["processed"] == 1
    assert payload["elapsed_seconds"] == 0.1


def test_round_summary_defaults_are_zero():
    summary = RoundSummary()
    assert summary.as_dict()["processed"] == 0
    assert summary.as_dict()["aligned"] == 0


# ------------------------------------------------------------------ 守护一轮


def _config(inbox: Path, base_url: str, db_path: Path, **extra) -> Config:
    env = {
        "PATHOLOGY_BASE_URL": base_url,
        "PATHOLOGY_API_TOKEN": "test-token",
        "PATHOLOGY_RESULT_INBOX": str(inbox),
        "PATHOLOGY_STATE_DB": str(db_path),
    }
    env.update(extra)
    return Config.from_env(env)


def test_daemon_round_logs_summary_line_without_result_values(
    fake_pathology, inbox, tmp_path
):
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-03-ki67-no-block.hl7", inbox)

    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    daemon = Daemon(
        config,
        client=fake_pathology.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda message: None,
    )
    outcome = daemon.run_once()
    daemon.close()

    text = stream.getvalue()

    # 摘要行在：处理 / 对齐 / 挂起 / 写回成功 / 写回失败 / 耗时一个不缺
    assert "event=round_summary" in text
    assert f"processed={outcome.accepted}" in text
    assert f"aligned={outcome.rationale_count}" in text
    assert f"pending={outcome.pending_count}" in text
    assert f"writeback_ok={outcome.drafts_written}" in text
    assert "writeback_failed=0" in text
    assert "elapsed_seconds=" in text

    # 病理号在日志里（运维要知道是哪一份报告）
    assert "ACC-EXAMPLE-0001" in text

    # 结果值不在日志里（阴性 / 约15% 是样例回报里的判读结果）
    assert "阴性" not in text
    assert "约15%" not in text


def test_daemon_round_writes_round_summary_file(
    fake_pathology, inbox, tmp_path
):
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)

    setup_logging(level="INFO", stream=io.StringIO())
    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    daemon = Daemon(
        config,
        client=fake_pathology.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda message: None,
    )
    outcome = daemon.run_once()
    daemon.close()

    payload = json.loads(
        (tmp_path / "out" / "round_summary.json").read_text(encoding="utf-8")
    )
    assert payload["summary"]["processed"] == outcome.accepted
    assert payload["summary"]["writeback_ok"] == outcome.drafts_written
    assert "本轮处理" in payload["summary_line"]
    assert payload["started_at"]


def test_daemon_logs_rejected_message_without_result_values(
    fake_pathology, inbox, tmp_path
):
    """坏报文被拒收时也要留一行日志，但不得带上结果值。"""
    (inbox / "broken.hl7").write_text(
        "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101130500"
        "||ORU^R01|MSG-BROKEN|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\r\n"
        "ZBR|B1|子宫体|1\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||Q|||20260101130000\r\n",
        encoding="utf-8",
    )

    stream = io.StringIO()
    setup_logging(level="INFO", stream=stream)

    config = _config(inbox, fake_pathology.base_url, tmp_path / "state.db")
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    daemon = Daemon(
        config,
        client=fake_pathology.client(),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda message: None,
    )
    outcome = daemon.run_once()
    daemon.close()

    text = stream.getvalue()
    assert outcome.malformed == 1
    assert "event=message_rejected" in text
    assert "WARNING" in text
    assert "阴性" not in text


# ------------------------------------------------------------------ 配置面


def test_log_level_from_env_is_applied():
    assert Config.from_env({"LOG_LEVEL": "debug"}).log_level == "DEBUG"
    assert Config.from_env({}).log_level == "INFO"


def test_invalid_log_level_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"LOG_LEVEL": "TRACE"})
    assert "LOG_LEVEL" in str(excinfo.value)


def test_result_values_in_log_is_off_by_default_and_configurable():
    assert Config.from_env({}).log_result_values is False
    assert Config.from_env({"LOG_RESULT_VALUES": "true"}).log_result_values is True


def test_log_settings_show_up_in_doctor_output():
    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(FIXTURE_INBOX),
            "LOG_LEVEL": "WARNING",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", "doctor"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "LOG_LEVEL" in result.stdout
    assert "LOG_FILE" in result.stdout
    assert "LOG_RESULT_VALUES" in result.stdout
    assert "WARNING" in result.stdout


def test_daemon_cli_emits_structured_lines_on_stderr(tmp_path):
    """命令行跑一轮：结构化摘要行落在标准错误（业务输出留在标准输出）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)

    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(tmp_path / "state.db"),
            "WATCH_SETTLE_SECONDS": "0",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "patho_supplement.cli",
            "daemon",
            "--once",
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "event=round_start" in result.stderr
    assert "event=round_summary" in result.stderr
    assert "processed=" in result.stderr
    # 取基线不可达 —— 但摘要照样出来，运维看得到这一轮出了什么事
    assert "event=baseline_failed" in result.stderr
