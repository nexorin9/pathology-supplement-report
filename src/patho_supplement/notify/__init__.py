"""通知通道：SMTP 邮件 + 企业微信群机器人 + 当班医师路由。

通知是「成稿之后」的一步：把「该签发了」这件事告诉当班的人。

- :mod:`~patho_supplement.notify.smtp_sender` —— 邮件通道（标准库 smtplib）；
- :mod:`~patho_supplement.notify.wecom_sender` —— 企业微信群机器人通道；
- :mod:`~patho_supplement.notify.dispatcher` —— 调度器：重试、降级、审计；
- :mod:`~patho_supplement.notify.messages` —— 通知正文渲染；
- :mod:`~patho_supplement.notify.router` —— 收件人路由：排班表找当班医师，
  排班缺失时降级到科室默认收件人。

**通知失败不牵连成稿**：调度器永不抛异常，失败只落审计与运行摘要。
"""

from __future__ import annotations

from .base import (
    KIND_DRAFT,
    KIND_ESCALATION,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    ChannelResult,
    NotifyAuthError,
    NotifyChannel,
    NotifyConfigError,
    NotifyError,
    NotifyHttpError,
    NotifyMessage,
    NotifyResponseError,
    NotifyTimeoutError,
    NotifyTransportError,
)
from .dispatcher import DispatchResult, NotifyDispatcher
from .messages import build_draft_notice, build_escalation_notice
from .router import (
    ROUTE_DEFAULT,
    ROUTE_NONE,
    ROUTE_SHIFT,
    DutyRoster,
    DutyRosterError,
    DutyRouter,
    DutyShift,
    RouteResult,
    load_duty_roster,
)
from .smtp_sender import SmtpSender, split_recipients
from .wecom_sender import WeComSender

__all__ = [
    "ChannelResult",
    "DispatchResult",
    "DutyRoster",
    "DutyRosterError",
    "DutyRouter",
    "DutyShift",
    "KIND_DRAFT",
    "KIND_ESCALATION",
    "NotifyAuthError",
    "NotifyChannel",
    "NotifyConfigError",
    "NotifyDispatcher",
    "NotifyError",
    "NotifyHttpError",
    "NotifyMessage",
    "NotifyResponseError",
    "NotifyTimeoutError",
    "NotifyTransportError",
    "ROUTE_DEFAULT",
    "ROUTE_NONE",
    "ROUTE_SHIFT",
    "RouteResult",
    "STATUS_FAILED",
    "STATUS_SENT",
    "STATUS_SKIPPED",
    "SmtpSender",
    "WeComSender",
    "build_draft_notice",
    "build_escalation_notice",
    "load_duty_roster",
    "split_recipients",
]
