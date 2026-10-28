"""当班医师路由测试：排班命中 / 排班缺失降级 / 路由留痕。

覆盖任务 id 41 的验收点：

- 排班表命中当前时段 → 收件人是当班医师，不是全科室
- 排班缺失（文件不存在 / 非法 JSON / 已过期 / 当前时段没排班）→ 降级到
  科室默认收件人，**降级在运行摘要里看得见**
- 路由结果随通知一起记进审计（收件人 + 路由依据）
- 守护跑一轮时，通知确实按排班发给了当班的人；排班缺失时确实降级了

替身只出现在**测试数据层**：病理系统是本地 ``http.server``；通知通道注入
一个只记录不发送的通道 —— 通道本身的发信行为由 ``tests/test_smtp_wecom.py``
覆盖，本文件只关心「通知发给了谁」。
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.notify.base import (
    STATUS_SENT,
    ChannelResult,
    NotifyChannel,
)
from patho_supplement.notify.dispatcher import NotifyDispatcher
from patho_supplement.notify.router import (
    ROUTE_DEFAULT,
    ROUTE_NONE,
    ROUTE_SHIFT,
    DutyRosterError,
    DutyRouter,
    load_duty_roster,
)

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 排班命中的当班医师（脱敏占位邮箱）。
ONDUTY = "pathologist-day@example.invalid"
NIGHT_DUTY = "pathologist-night@example.invalid"
#: 科室默认收件人（排班没命中时的兜底）。
DEPARTMENT_DEFAULT = "dept-duty@example.invalid"

#: 路由时用的固定时刻（避免测试随真实时间漂移）。
#: 2026-10-26 是星期一：白班 08:00–18:00 命中；凌晨 03:00 命中夜班。
MONDAY_DAYTIME = datetime(2026, 10, 26, 10, 30)
MONDAY_EARLY_MORNING = datetime(2026, 10, 26, 3, 0)
SATURDAY = datetime(2026, 10, 31, 10, 30)


# ------------------------------------------------------------------ 排班表样例


def _roster_payload(
    *,
    version: str = "2026.10",
    valid_from: str | None = None,
    valid_to: str | None = None,
    shifts: list[dict] | None = None,
) -> dict:
    payload: dict = {"version": version, "department": "病理科"}
    if valid_from:
        payload["valid_from"] = valid_from
    if valid_to:
        payload["valid_to"] = valid_to
    payload["shifts"] = shifts if shifts is not None else _default_shifts()
    return payload


def _default_shifts() -> list[dict]:
    return [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "days": ["MON", "TUE", "WED", "THU", "FRI"],
            "recipients": [ONDUTY],
        },
        {
            "id": "night",
            "name": "夜班",
            "start": "18:00",
            "end": "08:00",
            "recipients": [NIGHT_DUTY],
        },
    ]


def _write_roster(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _router(
    tmp_path: Path,
    *,
    payload: dict | None = None,
    default_recipients: tuple[str, ...] = (DEPARTMENT_DEFAULT,),
    roster_name: str = "duty-roster.json",
) -> DutyRouter:
    """装一个路由器（排班表写在临时目录里）。"""
    if payload is not None:
        path = _write_roster(tmp_path / roster_name, payload)
    else:
        path = tmp_path / roster_name
    return DutyRouter(
        roster_path=path, default_recipients=default_recipients
    )


# ------------------------------------------------------------------ 路由：排班命中


def test_shift_hit_returns_duty_recipients(tmp_path):
    """排班命中：收件人是当班医师，不是全科室。"""
    router = _router(tmp_path, payload=_roster_payload())

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_SHIFT
    assert route.degraded is False
    assert route.recipients == (ONDUTY,)
    assert route.shift_id == "day"
    assert route.shift_name == "白班"
    assert route.roster_version == "2026.10"
    assert "排班命中" in route.summary_line()
    assert "白班" in route.summary_line()


def test_night_shift_covers_after_midnight(tmp_path):
    """夜班跨零点：凌晨 3 点仍归夜班（按班次开始那一天算星期）。"""
    router = _router(tmp_path, payload=_roster_payload())

    route = router.route(at=MONDAY_EARLY_MORNING)

    assert route.basis == ROUTE_SHIFT
    assert route.recipients == (NIGHT_DUTY,)
    assert route.shift_id == "night"


def test_evening_shift_switches_to_night(tmp_path):
    """晚上 8 点起归夜班，不是白班。"""
    router = _router(tmp_path, payload=_roster_payload())

    route = router.route(at=datetime(2026, 10, 26, 20, 0))

    assert route.recipients == (NIGHT_DUTY,)


def test_weekday_restriction_picks_weekend_shift(tmp_path):
    """星期限制：周六命中周末值班，不命中工作日白班。"""
    shifts = _default_shifts() + [
        {
            "id": "weekend",
            "name": "周末值班",
            "start": "08:00",
            "end": "08:00",
            "days": ["SAT", "SUN"],
            "recipients": ["pathologist-weekend@example.invalid"],
        }
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=SATURDAY)

    assert route.recipients == ("pathologist-weekend@example.invalid",)
    assert route.shift_id == "weekend"


def test_chinese_weekday_writing_is_accepted(tmp_path):
    """「周一」这种中文写法也要认。"""
    shifts = [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "days": ["周一", "周二", "周三", "周四", "周五"],
            "recipients": [ONDUTY],
        }
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.recipients == (ONDUTY,)


# ------------------------------------------------------------------ 路由：降级


def test_missing_roster_file_falls_back_to_department_default(tmp_path):
    """排班表文件不存在 → 降级到科室默认收件人，降级要说得清原因。"""
    router = _router(tmp_path, payload=None)

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.degraded is True
    assert route.recipients == (DEPARTMENT_DEFAULT,)
    assert "不存在" in route.detail
    assert "降级到科室默认收件人" in route.summary_line()


def test_broken_roster_json_falls_back_and_warns(tmp_path):
    """排班表被改坏（非法 JSON）→ 降级 + 告警，主流程不中断。"""
    path = tmp_path / "duty-roster.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    router = DutyRouter(roster_path=path, default_recipients=(DEPARTMENT_DEFAULT,))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.recipients == (DEPARTMENT_DEFAULT,)
    assert route.warnings
    assert "JSON" in route.warnings[0]


def test_roster_without_any_usable_shift_falls_back(tmp_path):
    """一条可用班次都没有 → 降级。"""
    path = _write_roster(
        tmp_path / "duty-roster.json",
        {"version": "2026.10", "shifts": [{"name": "缺 id 的班次"}]},
    )
    router = DutyRouter(roster_path=path, default_recipients=(DEPARTMENT_DEFAULT,))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.recipients == (DEPARTMENT_DEFAULT,)


def test_expired_roster_falls_back(tmp_path):
    """整张排班表过期 → 降级，并写明有效到哪天。"""
    router = _router(
        tmp_path,
        payload=_roster_payload(valid_from="2026-01-01", valid_to="2026-06-30"),
    )

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.degraded is True
    assert "已过期" in route.detail
    assert "2026-06-30" in route.detail


def test_roster_not_effective_yet_falls_back(tmp_path):
    """排班表还没生效 → 降级。"""
    router = _router(
        tmp_path,
        payload=_roster_payload(valid_from="2026-12-01", valid_to="2026-12-31"),
    )

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert "尚未生效" in route.detail


def test_expired_shift_falls_back(tmp_path):
    """班次自己过期（有效期已过）→ 降级。"""
    shifts = [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "recipients": [ONDUTY],
            "valid_to": "2026-10-01",
        }
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.recipients == (DEPARTMENT_DEFAULT,)
    assert "已过期" in route.detail


def test_shift_not_on_that_weekday_says_so(tmp_path):
    """班次不在这一天的排班日 → 降级说明要写「不在排班日」（与过期分开）。"""
    shifts = [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "days": ["FRI"],
            "recipients": [ONDUTY],
        }
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert "不在排班日" in route.detail
    assert "已过期" not in route.detail


def test_no_shift_at_this_time_falls_back(tmp_path):
    """当前时段没有排班 → 降级。"""
    shifts = [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "recipients": [ONDUTY],
        }
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_EARLY_MORNING)

    assert route.basis == ROUTE_DEFAULT
    assert "当前时段没有排班" in route.detail


def test_shift_without_recipients_falls_back(tmp_path):
    """当班班次没写收件人 → 降级（不猜当班是谁）。"""
    shifts = [
        {"id": "day", "name": "白班", "start": "08:00", "end": "18:00", "recipients": []}
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_DEFAULT
    assert route.recipients == (DEPARTMENT_DEFAULT,)
    assert "没有配置收件人" in route.detail


def test_no_roster_and_no_default_recipient_has_nobody_to_notify(tmp_path):
    """既没有排班、也没有科室默认收件人 → 如实说没有收件人。"""
    router = _router(tmp_path, payload=None, default_recipients=())

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_NONE
    assert route.recipients == ()
    assert route.degraded is True
    assert "没有收件人" in route.summary_line()


def test_multiple_matching_shifts_pick_latest_start_with_warning(tmp_path):
    """多个班次同时命中 → 取最晚开始的那个，并留下告警（结果与文件顺序无关）。"""
    shifts = [
        {
            "id": "early",
            "name": "早班",
            "start": "06:00",
            "end": "20:00",
            "recipients": ["early@example.invalid"],
        },
        {
            "id": "late",
            "name": "晚班",
            "start": "09:00",
            "end": "20:00",
            "recipients": ["late@example.invalid"],
        },
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.recipients == ("late@example.invalid",)
    assert route.shift_id == "late"
    assert any("命中 2 个班次" in warning for warning in route.warnings)

    # 换个文件顺序，结果不变（取最晚开始的那个，与排班表写法无关）。
    reversed_router = _router(
        tmp_path,
        payload=_roster_payload(shifts=list(reversed(shifts))),
        roster_name="duty-roster-reversed.json",
    )
    reversed_route = reversed_router.route(at=MONDAY_DAYTIME)
    assert reversed_route.recipients == route.recipients


# ------------------------------------------------------------------ 装载与重载


def test_roster_is_reloaded_when_file_changes(tmp_path):
    """排班表改了要能生效（文件改了自动重载，不用重启守护进程）。"""
    path = _write_roster(tmp_path / "duty-roster.json", _roster_payload())
    router = DutyRouter(roster_path=path, default_recipients=(DEPARTMENT_DEFAULT,))

    first = router.route(at=MONDAY_DAYTIME)
    assert first.recipients == (ONDUTY,)

    _write_roster(
        path,
        _roster_payload(
            version="2026.11",
            shifts=[
                {
                    "id": "day",
                    "name": "白班",
                    "start": "08:00",
                    "end": "18:00",
                    "days": ["MON", "TUE", "WED", "THU", "FRI"],
                    "recipients": ["new-duty@example.invalid"],
                }
            ],
        ),
    )

    second = router.route(at=MONDAY_DAYTIME)
    assert second.recipients == ("new-duty@example.invalid",)
    assert second.roster_version == "2026.11"


def test_strict_mode_raises_on_broken_file(tmp_path):
    """严格模式（自检用）遇到坏排班表要报错，不静默降级。"""
    path = tmp_path / "duty-roster.json"
    path.write_text("{ 坏文件", encoding="utf-8")

    with pytest.raises(DutyRosterError):
        load_duty_roster(path, strict=True)


def test_single_broken_shift_is_skipped_not_whole_roster(tmp_path):
    """一条班次写错只跳过这一条，其余照常生效。"""
    shifts = [
        {"name": "缺 id 的班次"},
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "recipients": [ONDUTY],
        },
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_SHIFT
    assert route.recipients == (ONDUTY,)
    assert any("缺 id" in warning for warning in route.warnings)


def test_shift_without_recipients_does_not_shadow_usable_shift(tmp_path):
    """重叠班次里，没写收件人的那一条不挡着真正能通知人的那一条。"""
    shifts = [
        {
            "id": "day",
            "name": "白班",
            "start": "08:00",
            "end": "18:00",
            "recipients": [],
        },
        {
            "id": "day2",
            "name": "白班二",
            "start": "08:00",
            "end": "18:00",
            "recipients": [ONDUTY],
        },
    ]
    router = _router(tmp_path, payload=_roster_payload(shifts=shifts))

    route = router.route(at=MONDAY_DAYTIME)

    assert route.basis == ROUTE_SHIFT
    assert route.recipients == (ONDUTY,)


# ------------------------------------------------------------------ 脱敏


def test_route_result_carries_no_patient_identifiers(tmp_path):
    """路由结果里只有业务标识与收件人，没有患者可识别明细。"""
    router = _router(tmp_path, payload=_roster_payload())

    payload = router.route(at=MONDAY_DAYTIME).to_dict()

    text = json.dumps(payload, ensure_ascii=False)
    assert "身份证" not in text
    assert "姓名" not in text
    assert "13800000000" not in text


# ------------------------------------------------------------------ 通知通道替身


class _RecordingChannel(NotifyChannel):
    """只记录不发送的通知通道（仅测试数据层）。"""

    name = "recording"

    def __init__(self) -> None:
        self.messages = []

    def is_configured(self) -> bool:
        return True

    def describe(self) -> str:
        return "记录通道（测试替身）"

    def send(self, message) -> ChannelResult:
        self.messages.append(message)
        return ChannelResult(
            channel=self.name,
            status=STATUS_SENT,
            detail=f"已发给 {len(message.recipients)} 个收件人",
            attempts=1,
            recipient_count=len(message.recipients),
        )


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


class _FakePathology:
    """病理系统替身（仅测试数据层）：GET 基线 + POST 草稿。"""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802
                parts = self.path.strip("/").split("/")
                accession = parts[3] if len(parts) > 3 else ""
                payload = BASELINES.get(accession)
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
                raw = self.rfile.read(length) if length else b""
                fake.posts.append(json.loads(raw.decode("utf-8")))
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
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

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


def _daemon_env(tmp_path, fake_pathology, monkeypatch, roster_path: Path | None, **extra):
    """给守护进程配好环境变量（凭据位全部来自环境变量）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for source in sorted(FIXTURE_INBOX.glob("*.hl7")):
        shutil.copy2(source, inbox / source.name)
    monkeypatch.setenv("PATHOLOGY_BASE_URL", fake_pathology.base_url)
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "test-token")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", str(inbox))
    monkeypatch.setenv("PATHOLOGY_STATE_DB", str(tmp_path / "state.db"))
    if roster_path is not None:
        monkeypatch.setenv("NOTIFY_DUTY_ROSTER_PATH", str(roster_path))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    return inbox


# ------------------------------------------------------------------ 守护集成


def test_daemon_notifies_the_duty_physician(
    tmp_path, fake_pathology, monkeypatch
):
    """排班命中时，通知发给当班医师（不是科室默认收件人）。"""
    roster = _write_roster(tmp_path / "duty-roster.json", _roster_payload())
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        roster,
        NOTIFY_RECIPIENTS=DEPARTMENT_DEFAULT,
    )
    # 固定「当前时刻」落在白班里：直接改路由器的时刻由守护进程内部取，
    # 这里改用一份**全天有效**的排班表，保证任何时刻都命中。
    _write_roster(
        roster,
        _roster_payload(
            shifts=[
                {
                    "id": "day",
                    "name": "白班",
                    "start": "00:00",
                    "end": "23:59",
                    "recipients": [ONDUTY],
                }
            ]
        ),
    )

    config = Config.from_env()
    channel = _RecordingChannel()
    daemon = Daemon(
        config,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=Database(tmp_path / "state.db")),
    )
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert outcome.drafts_written >= 1
    assert outcome.notify_sent >= 1
    assert outcome.notify_route["basis"] == ROUTE_SHIFT
    assert outcome.notify_route["recipients"] == [ONDUTY]
    # 通知确实发给了当班医师，科室默认收件人没被用上。
    assert channel.messages
    for message in channel.messages:
        assert message.recipients == (ONDUTY,)
    summary = daemon.render_round(outcome)
    assert "收件人路由：排班命中" in summary
    assert "白班" in summary


def test_daemon_degrades_when_roster_missing(tmp_path, fake_pathology, monkeypatch):
    """排班缺失 → 通知改发给科室默认收件人，运行摘要里写明降级。"""
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        None,
        NOTIFY_RECIPIENTS=DEPARTMENT_DEFAULT,
    )

    config = Config.from_env()
    channel = _RecordingChannel()
    daemon = Daemon(
        config,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=Database(tmp_path / "state.db")),
    )
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    assert outcome.drafts_written >= 1
    assert outcome.notify_route["basis"] == ROUTE_DEFAULT
    assert outcome.notify_route["degraded"] is True
    assert outcome.notify_route["recipients"] == [DEPARTMENT_DEFAULT]
    for message in channel.messages:
        assert message.recipients == (DEPARTMENT_DEFAULT,)
    summary = daemon.render_round(outcome)
    assert "降级到科室默认收件人" in summary


def test_daemon_records_route_audit_row(tmp_path, fake_pathology, monkeypatch):
    """路由结果随通知记进审计：收件人与路由依据都查得到。"""
    roster = _write_roster(
        tmp_path / "duty-roster.json",
        _roster_payload(
            shifts=[
                {
                    "id": "day",
                    "name": "白班",
                    "start": "00:00",
                    "end": "23:59",
                    "recipients": [ONDUTY],
                }
            ]
        ),
    )
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        roster,
        NOTIFY_RECIPIENTS=DEPARTMENT_DEFAULT,
    )

    config = Config.from_env()
    channel = _RecordingChannel()
    daemon = Daemon(
        config,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=Database(tmp_path / "state.db")),
    )
    try:
        outcome = daemon.run_once()
    finally:
        daemon.close()

    database = Database(tmp_path / "state.db")
    rows = database.query_all(
        "SELECT accession_no, detail_json FROM audit_log WHERE event = 'notify_routed'"
    )
    assert len(rows) == outcome.notify_routed
    assert len(rows) >= 1
    detail = json.loads(rows[0]["detail_json"])
    assert detail["basis"] == ROUTE_SHIFT
    assert detail["basis_label"] == "排班命中"
    assert detail["recipients"] == [ONDUTY]
    assert detail["shift_name"] == "白班"
    assert detail["degraded"] is False
    database.close()


def test_daemon_route_audit_row_has_no_patient_identifiers(
    tmp_path, fake_pathology, monkeypatch
):
    """路由审计行只带业务标识与收件人，没有患者可识别明细。"""
    _daemon_env(
        tmp_path,
        fake_pathology,
        monkeypatch,
        None,
        NOTIFY_RECIPIENTS=DEPARTMENT_DEFAULT,
    )

    config = Config.from_env()
    channel = _RecordingChannel()
    daemon = Daemon(
        config,
        output_dir=tmp_path / "out",
        notifier=NotifyDispatcher([channel], database=Database(tmp_path / "state.db")),
    )
    try:
        daemon.run_once()
    finally:
        daemon.close()

    database = Database(tmp_path / "state.db")
    rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE event = 'notify_routed'"
    )
    assert rows
    for row in rows:
        text = row["detail_json"]
        assert "身份证" not in text
        assert "姓名" not in text
        assert "13800000000" not in text
    database.close()


# ------------------------------------------------------------------ 配置面


def test_roster_path_comes_from_env_only(monkeypatch):
    """排班表路径与科室名只从环境变量读：代码里没有写死的路径。"""
    monkeypatch.setenv("NOTIFY_DUTY_ROSTER_PATH", "dictionaries/duty-roster.example.json")
    monkeypatch.setenv("NOTIFY_DUTY_DEPARTMENT", "病理科")

    config = Config.from_env()

    assert config.notify_duty_roster_path is not None
    assert config.notify_duty_roster_path.name == "duty-roster.example.json"
    assert config.notify_duty_department == "病理科"

    router = DutyRouter.from_config(config)
    assert router.roster_path == config.notify_duty_roster_path


def test_example_roster_file_is_loadable():
    """仓库里的排班表样例能被装载（用户照着改就能用）。"""
    sample = Path(__file__).resolve().parents[1] / "dictionaries" / "duty-roster.example.json"

    roster = load_duty_roster(sample)

    assert roster.loaded is True
    assert roster.shift_count == 3
    assert roster.department == "病理科"
    assert roster.warnings == ()
