"""通知通道测试：SMTP 邮件 + 企业微信群机器人 + 降级与「不阻塞主流程」。

替身只出现在**测试数据层**：SMTP 替身是一个最小 SMTP 服务（socket），
企业微信与病理系统替身是本地 ``http.server``。**产品代码走的是与生产完全
相同的一条路径** —— 没有为测试另写的分支。

覆盖任务 id 40 的验收点：

- SMTP 凭据全从环境变量读取；超时与认证失败转为可观测的结构化错误
- 企业微信 webhook 推送；返回错误码 / 非 2xx 时报错可观测
- 企微未配置时降级为只走 SMTP，并在运行摘要里显式标注
- SMTP 不可达时重试后降级并记审计，**不阻塞主流程**（成稿照常完成）
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.notify.base import (
    KIND_DRAFT,
    KIND_ESCALATION,
    NotifyAuthError,
    NotifyConfigError,
    NotifyHttpError,
    NotifyMessage,
    NotifyResponseError,
    NotifyTimeoutError,
    NotifyTransportError,
)
from patho_supplement.notify.dispatcher import NotifyDispatcher
from patho_supplement.notify.messages import (
    build_draft_notice,
    build_escalation_notice,
)
from patho_supplement.notify.smtp_sender import SmtpSender, split_recipients
from patho_supplement.notify.wecom_sender import WeComSender

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"


# ------------------------------------------------------------------ SMTP 替身


class _FakeSmtpServer:
    """最小 SMTP 替身（仅测试数据层）。

    只实现产品代码用到的那几条命令：EHLO / AUTH / MAIL FROM / RCPT TO /
    DATA / QUIT。收到的邮件原文记在 ``messages`` 里，供断言逐字段核对。

    参数：

    - ``auth_password``：给了就通告 ``AUTH PLAIN``；
    - ``auth_code``：认证回应码（235 成功 / 535 失败）；
    - ``hang``：连上不回话（用来制造「读超时」）。
    """

    def __init__(
        self,
        *,
        auth_password: str | None = None,
        auth_code: int = 235,
        hang: bool = False,
    ) -> None:
        self.messages: list[str] = []
        self.auth_password = auth_password
        self.auth_code = auth_code
        self.hang = hang
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        if self.hang:
            # 连上就不回话：客户端会在读欢迎语时超时。
            threading.Event().wait(5)
            conn.close()
            return
        conn.sendall(b"220 fake ESMTP ready\r\n")
        buffer = b""
        in_data = False
        data_lines: list[bytes] = []
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\r\n" in buffer:
                line, buffer = buffer.split(b"\r\n", 1)
                if in_data:
                    if line == b".":
                        self.messages.append(
                            b"\r\n".join(data_lines).decode("utf-8", "replace")
                        )
                        data_lines = []
                        in_data = False
                        conn.sendall(b"250 OK message accepted\r\n")
                    else:
                        data_lines.append(line)
                    continue
                text = line.decode("utf-8", "replace")
                upper = text.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    conn.sendall(b"250-fake\r\n250-AUTH PLAIN\r\n250 SIZE 1024\r\n")
                elif upper.startswith("AUTH"):
                    if self.auth_code == 235:
                        conn.sendall(b"235 Authentication successful\r\n")
                    else:
                        conn.sendall(b"535 Authentication credentials invalid\r\n")
                elif upper.startswith("DATA"):
                    in_data = True
                    conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                elif upper.startswith("QUIT"):
                    conn.sendall(b"221 Bye\r\n")
                    conn.close()
                    return
                else:
                    conn.sendall(b"250 OK\r\n")


@pytest.fixture()
def fake_smtp():
    server = _FakeSmtpServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def fake_smtp_auth_fail():
    server = _FakeSmtpServer(auth_password="right-pwd", auth_code=535)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def fake_smtp_hang():
    server = _FakeSmtpServer(hang=True)
    try:
        yield server
    finally:
        server.close()


def _closed_port() -> int:
    """一个确定没人监听的端口（用来制造「连不上」）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _decode_mail(raw: str) -> tuple[str, str]:
    """解析替身收到的邮件原文，返回 ``(解码后的标题, 正文)``。

    中文标题在邮件头里是 RFC 2047 编码的，不解码就断言不到；正文是 8bit
    直传（``raw-unicode-escape`` 那条路会把中文转义掉），所以按传输编码
    分别处理。
    """
    import email
    import email.header
    import email.policy

    message = email.message_from_string(raw, policy=email.policy.default)
    subject = str(
        email.header.make_header(email.header.decode_header(message.get("Subject", "")))
    )
    cte = str(message.get("Content-Transfer-Encoding", "")).strip().lower()
    payload = message.get_payload(decode=cte in ("base64", "quoted-printable"))
    if isinstance(payload, list):
        payload = payload[0].get_payload(decode=False)
    if isinstance(payload, bytes):
        payload = payload.decode(message.get_content_charset() or "utf-8", "replace")
    return subject, str(payload)


# ------------------------------------------------------------------ HTTP 替身


class _FakeWeCom:
    """企业微信群机器人替身（仅测试数据层）。"""

    def __init__(self, *, errcode: int = 0, status: int = 200, raw: str | None = None) -> None:
        self.requests: list[dict] = []
        self.errcode = errcode
        self.status = status
        self.raw = raw
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                fake.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if fake.raw is not None:
                    payload = fake.raw.encode("utf-8")
                else:
                    payload = json.dumps(
                        {"errcode": fake.errcode, "errmsg": "ok"}
                    ).encode("utf-8")
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.webhook_url = f"{self.base_url}/cgi-bin/webhook/send?key=TESTKEY"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    @property
    def last_payload(self) -> dict:
        return json.loads(self.requests[-1]["body"].decode("utf-8"))


@pytest.fixture()
def fake_wecom():
    server = _FakeWeCom()
    try:
        yield server
    finally:
        server.close()


class _FakePathology:
    """病理系统替身（仅测试数据层）：GET 基线 + POST 草稿两条路径。"""

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

    def __init__(self, *, post_status: int = 200) -> None:
        self.gets: list[str] = []
        self.posts: list[dict] = []
        self.post_status = post_status
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802
                fake.gets.append(self.path)
                parts = self.path.strip("/").split("/")
                accession = parts[3] if len(parts) > 3 else ""
                payload = fake.BASELINES.get(accession)
                if payload is None:
                    body = json.dumps({"message": "报告不存在"}).encode("utf-8")
                    self.send_response(404)
                else:
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                fake.posts.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw.decode("utf-8")),
                    }
                )
                if fake.post_status != 200:
                    body = json.dumps({"message": "回写失败"}).encode("utf-8")
                    self.send_response(fake.post_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps(
                    {
                        "status": "accepted",
                        "version": "draft-v2",
                        "draft_id": "SD-EXAMPLE-0001",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture()
def fake_pathology():
    server = _FakePathology()
    try:
        yield server
    finally:
        server.close()


# ------------------------------------------------------------------ 配置面


def _smtp_config(fake_smtp, recipients="duty@example.invalid") -> Config:
    """按替身拼一份配置（凭据位全部来自环境变量，不写死真实地址）。"""
    return Config(
        smtp_host=fake_smtp.host,
        smtp_port=fake_smtp.port,
        smtp_user="duty@example.invalid",
        smtp_password="secret",
        smtp_from="patho@example.invalid",
        smtp_use_ssl=False,
        smtp_starttls=False,
        notify_recipients=split_recipients(recipients),
        notify_max_attempts=2,
        notify_retry_delay_seconds=0.0,
        notify_timeout=5.0,
    )


# ------------------------------------------------------------------ SMTP 通道


def test_smtp_sends_message_to_recipients(fake_smtp):
    """SMTP 通道：邮件真的发出去了，收件人与正文对得上。"""
    sender = SmtpSender.from_config(_smtp_config(fake_smtp))
    message = build_draft_notice(
        accession_no="ACC-EXAMPLE-0001",
        version=4,
        entry_count=6,
        pending_count=1,
    )

    result = sender.send(message)

    assert result.status == "sent"
    assert result.recipient_count == 1
    assert len(fake_smtp.messages) == 1
    received = fake_smtp.messages[0]
    assert "duty@example.invalid" in received
    subject, body = _decode_mail(received)
    assert "ACC-EXAMPLE-0001" in subject
    assert "补充报告草稿 v4" in body
    assert "并入结果 6 条" in body


def test_smtp_uses_message_recipients_first(fake_smtp):
    """消息里带了收件人（排班命中的当班医师）就用消息里的。"""
    sender = SmtpSender.from_config(_smtp_config(fake_smtp, recipients="fallback@example.invalid"))
    message = NotifyMessage(
        subject="补充报告草稿待签发｜病理号 ACC-EXAMPLE-0001",
        body="正文",
        recipients=("onduty@example.invalid",),
    )

    sender.send(message)

    assert len(fake_smtp.messages) == 1
    assert "onduty@example.invalid" in fake_smtp.messages[0]
    assert "fallback@example.invalid" not in fake_smtp.messages[0]


def test_smtp_without_recipients_is_config_error(fake_smtp):
    """没有收件人就不猜 —— 配置错误，可观测。"""
    sender = SmtpSender.from_config(_smtp_config(fake_smtp, recipients=""))
    message = NotifyMessage(subject="标题", body="正文")

    with pytest.raises(NotifyConfigError) as excinfo:
        sender.send(message)

    assert excinfo.value.error_code == "notify_config_invalid"
    assert "收件人" in excinfo.value.detail


def test_smtp_auth_failure_is_observable_error(fake_smtp_auth_fail):
    """认证失败 → 可观测的结构化错误（错误码分得清是哪一类失败）。"""
    sender = SmtpSender.from_config(_smtp_config(fake_smtp_auth_fail))

    with pytest.raises(NotifyAuthError) as excinfo:
        sender.send(NotifyMessage(subject="标题", body="正文"))

    assert excinfo.value.error_code == "notify_auth_failed"
    assert "认证失败" in excinfo.value.detail


def test_smtp_unreachable_is_observable_error():
    """连不上 → 可观测的结构化错误（与认证失败分开）。"""
    sender = SmtpSender(
        "127.0.0.1",
        _closed_port(),
        recipients=("duty@example.invalid",),
        starttls=False,
        timeout=2.0,
    )

    with pytest.raises(NotifyTransportError) as excinfo:
        sender.send(NotifyMessage(subject="标题", body="正文"))

    assert excinfo.value.error_code == "notify_unreachable"


def test_smtp_timeout_is_observable_error(fake_smtp_hang):
    """读超时 → 可观测的超时错误（与「连不上」不是同一类）。"""
    sender = SmtpSender(
        fake_smtp_hang.host,
        fake_smtp_hang.port,
        recipients=("duty@example.invalid",),
        starttls=False,
        timeout=0.4,
    )

    with pytest.raises(NotifyTimeoutError) as excinfo:
        sender.send(NotifyMessage(subject="标题", body="正文"))

    assert excinfo.value.error_code == "notify_timeout"


def test_smtp_credentials_come_from_env_only(monkeypatch):
    """凭据位只从环境变量读：代码里没有写死的地址、端口与密钥。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "duty@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "from-env")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "a@example.invalid, b@example.invalid")

    config = Config.from_env()

    assert config.smtp_host == "smtp.example.invalid"
    assert config.smtp_port == 2525
    assert config.notify_recipients == ("a@example.invalid", "b@example.invalid")
    assert config.credential_status()["SMTP_PASSWORD"] is True

    sender = SmtpSender.from_config(config)
    assert sender.host == "smtp.example.invalid"
    assert sender.recipients == ("a@example.invalid", "b@example.invalid")


# ------------------------------------------------------------------ 企业微信


def test_wecom_posts_markdown_payload(fake_wecom):
    """企业微信通道：POST 路径、请求体逐字段与回执解析都对得上。"""
    sender = WeComSender.from_config(
        Config(wecom_webhook_url=fake_wecom.webhook_url, http_timeout=5.0)
    )
    message = build_draft_notice(
        accession_no="ACC-EXAMPLE-0001", version=4, entry_count=6, pending_count=0
    )

    result = sender.send(message)

    assert result.status == "sent"
    assert len(fake_wecom.requests) == 1
    request = fake_wecom.requests[0]
    assert request["path"].startswith("/cgi-bin/webhook/send")
    assert request["headers"]["content-type"].startswith("application/json")
    payload = fake_wecom.last_payload
    assert payload["msgtype"] == "markdown"
    assert "ACC-EXAMPLE-0001" in payload["markdown"]["content"]
    assert "**病理号" in payload["markdown"]["content"]


def test_wecom_nonzero_errcode_is_observable_error():
    """对方返回错误码 → 可观测的结构化错误（消息没发出去但说得清原因）。"""
    server = _FakeWeCom(errcode=93000)
    try:
        sender = WeComSender.from_config(
            Config(wecom_webhook_url=server.webhook_url, http_timeout=5.0)
        )
        with pytest.raises(NotifyResponseError) as excinfo:
            sender.send(NotifyMessage(subject="标题", body="正文"))
    finally:
        server.close()

    assert excinfo.value.error_code == "notify_contract_violation"
    assert "93000" in excinfo.value.detail


def test_wecom_http_500_is_observable_error():
    """对方 500 → 可观测的 HTTP 错误（带状态码）。"""
    server = _FakeWeCom(status=500)
    try:
        sender = WeComSender.from_config(
            Config(wecom_webhook_url=server.webhook_url, http_timeout=5.0)
        )
        with pytest.raises(NotifyHttpError) as excinfo:
            sender.send(NotifyMessage(subject="标题", body="正文"))
    finally:
        server.close()

    assert excinfo.value.error_code == "notify_http_status"
    assert excinfo.value.status_code == 500


def test_wecom_truncates_overlong_content(fake_wecom):
    """内容超长就截断并标注（对方拒收超长消息，截断比消失强）。"""
    sender = WeComSender.from_config(
        Config(wecom_webhook_url=fake_wecom.webhook_url, http_timeout=5.0)
    )
    message = NotifyMessage(subject="标题", body="正文", markdown="长" * 5000)

    sender.send(message)

    content = fake_wecom.last_payload["markdown"]["content"]
    assert len(content.encode("utf-8")) <= 4096
    assert "内容过长已截断" in content


# ------------------------------------------------------------------ 调度器


def test_dispatcher_degrades_when_wecom_not_configured(fake_smtp, tmp_path):
    """企微未配置 → 降级为只走 SMTP，并在结果里显式标注。"""
    dispatcher = NotifyDispatcher.from_config(_smtp_config(fake_smtp))

    result = dispatcher.dispatch(
        build_draft_notice(accession_no="ACC-EXAMPLE-0001", version=4, entry_count=6),
        reason="草稿待签发",
    )

    assert result.status == "sent"
    assert result.sent == ["smtp"]
    assert result.degraded is True
    assert any("企业微信" in note for note in result.notes)
    assert any("降级" in note for note in result.notes)
    assert "降级" in result.summary_line()


def test_dispatcher_retries_then_records_failure(tmp_path):
    """SMTP 不可达 → 重试到上限后降级记审计，不抛异常。"""
    database = Database(tmp_path / "state.db")
    database.init_schema()
    config = Config(
        smtp_host="127.0.0.1",
        smtp_port=_closed_port(),
        notify_recipients=("duty@example.invalid",),
        smtp_starttls=False,
        notify_max_attempts=3,
        notify_retry_delay_seconds=0.0,
        notify_timeout=2.0,
    )
    dispatcher = NotifyDispatcher.from_config(config, database=database)

    result = dispatcher.dispatch(
        build_draft_notice(accession_no="ACC-EXAMPLE-0001", version=4, entry_count=6),
        reason="草稿待签发",
    )

    assert result.status == "failed"
    assert result.failed == ["smtp"]
    assert result.degraded is True
    failed_rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = 'notify_failed'"
    )
    assert len(failed_rows) == 1
    detail = json.loads(failed_rows[0]["detail_json"])
    assert detail["channel"] == "smtp"
    assert detail["error_code"] == "notify_unreachable"
    assert detail["attempts"] == 3
    degraded_rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = 'notify_degraded'"
    )
    assert len(degraded_rows) == 1
    database.close()


def test_dispatcher_skips_when_no_channel_configured(tmp_path):
    """一个通道都没配 → 本轮没发，但「没发通知」这件事要可查。"""
    database = Database(tmp_path / "state.db")
    database.init_schema()
    dispatcher = NotifyDispatcher.from_config(Config(), database=database)

    result = dispatcher.dispatch(
        build_draft_notice(accession_no="ACC-EXAMPLE-0001", version=4, entry_count=6),
        reason="草稿待签发",
    )

    assert result.status == "skipped"
    assert result.attempted is False
    assert any("未配置" in note for note in result.notes)
    skipped_rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = 'notify_skipped'"
    )
    assert len(skipped_rows) == 1
    database.close()


def test_dispatcher_records_sent_message(tmp_path, fake_smtp):
    """发出去了 → 审计流水有一行 notify_sent（明细带通道与业务事实）。"""
    database = Database(tmp_path / "state.db")
    database.init_schema()
    config = _smtp_config(fake_smtp)
    dispatcher = NotifyDispatcher.from_config(config, database=database)

    dispatcher.dispatch(
        build_draft_notice(
            accession_no="ACC-EXAMPLE-0001", version=4, entry_count=6, pending_count=1
        ),
        reason="草稿待签发",
    )

    rows = database.query_all(
        "SELECT accession_no, detail_json FROM audit_log WHERE event = 'notify_sent'"
    )
    assert len(rows) == 1
    assert rows[0]["accession_no"] == "ACC-EXAMPLE-0001"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["channel"] == "smtp"
    assert detail["entry_count"] == 6
    assert detail["draft_version"] == 4
    database.close()


# ------------------------------------------------------------------ 主流程不阻塞


def _daemon_env(tmp_path, fake_pathology, monkeypatch, **extra):
    """给守护进程配好环境变量（凭据位全部来自环境变量）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for source in sorted(FIXTURE_INBOX.glob("*.hl7")):
        shutil.copy2(source, inbox / source.name)
    monkeypatch.setenv("PATHOLOGY_BASE_URL", fake_pathology.base_url)
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "test-token")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", str(inbox))
    monkeypatch.setenv("PATHOLOGY_STATE_DB", str(tmp_path / "state.db"))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    return inbox


def test_daemon_completes_draft_when_smtp_unreachable(
    tmp_path, fake_pathology, monkeypatch, capsys
):
    """SMTP 不可达：草稿照常写回病理系统，通知失败只落审计与摘要。"""
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=str(_closed_port()),
        SMTP_STARTTLS="false",
        NOTIFY_RECIPIENTS="duty@example.invalid",
        NOTIFY_MAX_ATTEMPTS="2",
        NOTIFY_RETRY_DELAY_SECONDS="0",
    )
    config = Config.from_env()
    daemon = Daemon(config, output_dir=tmp_path / "out")
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    # 主流程照常完成：草稿真的回写回病理系统了。
    assert len(fake_pathology.posts) >= 1
    assert outcome.drafts_written >= 1
    # 通知失败被观测到，但没把这一轮拖下水。
    assert outcome.notify_failed >= 1
    assert outcome.notify_degraded is True
    summary = daemon.render_round(outcome)
    assert "通知" in summary
    assert "失败" in summary
    # 审计流水里有通知失败与降级两行（每份草稿一条通知）。
    database = Database(tmp_path / "state.db")
    failed_rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = 'notify_failed'"
    )
    assert len(failed_rows) == outcome.drafts_written
    for row in failed_rows:
        detail = json.loads(row["detail_json"])
        assert detail["error_code"] == "notify_unreachable"
        assert detail["channel"] == "smtp"
    degraded_rows = database.query_all(
        "SELECT COUNT(*) AS n FROM audit_log WHERE event = 'notify_degraded'"
    )
    assert degraded_rows[0]["n"] == outcome.drafts_written
    database.close()


def test_daemon_summary_marks_wecom_degradation(
    tmp_path, fake_pathology, fake_smtp, monkeypatch
):
    """企微未配置时，运行摘要里能看出「已降级为只走 SMTP」。"""
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        SMTP_HOST=fake_smtp.host,
        SMTP_PORT=str(fake_smtp.port),
        SMTP_STARTTLS="false",
        NOTIFY_RECIPIENTS="duty@example.invalid",
        NOTIFY_RETRY_DELAY_SECONDS="0",
    )
    config = Config.from_env()
    daemon = Daemon(config, output_dir=tmp_path / "out")
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert outcome.notify_sent >= 1
    assert outcome.notify_degraded is True
    summary = daemon.render_round(outcome)
    assert "企业微信通道未配置" in summary
    assert "降级" in summary
    assert len(fake_smtp.messages) >= 1


def test_daemon_escalation_notice_reaches_channel(
    tmp_path, fake_smtp, monkeypatch
):
    """回写升级告警也走通知通道（告警不只留在审计里）。

    病理系统一直回 500 → 草稿进回写队列 → 重试用尽 → 升级告警 → 通知当班的人。
    """
    fake_pathology = _FakePathology(post_status=500)
    try:
        _daemon_env(
            tmp_path,
            fake_pathology,
            monkeypatch,
            SMTP_HOST=fake_smtp.host,
            SMTP_PORT=str(fake_smtp.port),
            SMTP_STARTTLS="false",
            NOTIFY_RECIPIENTS="duty@example.invalid",
            NOTIFY_RETRY_DELAY_SECONDS="0",
            WRITEBACK_MAX_RETRIES="1",
            WRITEBACK_RETRY_BASE_SECONDS="1",
            WRITEBACK_RETRY_MAX_SECONDS="1",
        )
        config = Config.from_env()
        daemon = Daemon(config, output_dir=tmp_path / "out")
        try:
            daemon.run_once()
            # 等回写重试到期（退避基数 1 秒）。
            time.sleep(1.2)
            outcome = daemon.run_once()
        finally:
            daemon.close()

        # 升级告警既进审计，也走通知通道。
        database = Database(tmp_path / "state.db")
        escalated = database.query_all(
            "SELECT COUNT(*) AS n FROM audit_log WHERE event = 'writeback_escalated'"
        )
        assert escalated[0]["n"] >= 1
        notified = database.query_all(
            "SELECT COUNT(*) AS n FROM audit_log WHERE event = 'notify_sent'"
        )
        assert notified[0]["n"] >= 1
        database.close()
        assert outcome.writeback_escalated >= 1
        # 告警通知的正文写明「回写失败」与处理建议。
        decoded = [_decode_mail(message) for message in fake_smtp.messages]
        assert any("回写失败" in subject for subject, _ in decoded)
        assert any("回写病理系统失败" in body for _, body in decoded)
        assert any("请人工确认后重放" in body for _, body in decoded)
    finally:
        fake_pathology.close()


# ------------------------------------------------------------------ 通知正文


def test_draft_notice_body_has_no_patient_identifiers():
    """通知正文只带业务标识：病理号、版本号、条目数，没有姓名与身份证。"""
    message = build_draft_notice(
        accession_no="ACC-EXAMPLE-0001",
        version=4,
        entry_count=6,
        pending_count=1,
    )

    assert message.kind == KIND_DRAFT
    assert "ACC-EXAMPLE-0001" in message.body
    assert "身份证" not in message.body
    assert "姓名" not in message.body
    assert "13800000000" not in message.body
    assert message.facts["entry_count"] == 6
    assert message.facts["draft_version"] == 4


def test_escalation_notice_says_what_to_do():
    """升级告警要说清：哪份草稿、试了几次、错在哪、下一步谁做什么。"""
    message = build_escalation_notice(
        accession_no="ACC-EXAMPLE-0001",
        draft_version=4,
        attempts=5,
        max_retries=5,
        last_error="回写草稿失败",
        action="草稿已保留在本地状态库，请人工确认后重放，或改由人工签发",
    )

    assert message.kind == KIND_ESCALATION
    assert "ACC-EXAMPLE-0001" in message.body
    assert "5 次" in message.body
    assert "回写草稿失败" in message.body
    assert "请人工确认后重放" in message.body
