"""通知通道的公共底座：消息数据类、通道结果、错误码与异常。

通知是「成稿之后」的那一步：草稿已经写回病理系统草稿区了，通知只是把
「该签发了」这件事告诉当班的人。所以本模块定下三条硬规矩：

1. **通知失败不牵连成稿。** 成稿已经落库、已经回写，通知发不出去只能是
   「通知失败」，不能是「这一轮失败」。
2. **失败要能归类。** 每类失败带一个**机器可读的错误码**（``error_code``）：
   认证失败、连接不可达、超时、对方报错，是四件不同的事，值班的人要能分开看。
3. **消息里不放患者可识别信息。** 通知正文只带病理号、版本号与条目数这些
   业务标识；正文在出门前还要再过一遍脱敏。

通道协议很小：一个通道能回答「配好了吗」和「把这条消息发出去」，其余
（重试、降级、审计）由 :mod:`patho_supplement.notify.dispatcher` 统一管。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ChannelResult",
    "NotifyChannel",
    "NotifyError",
    "NotifyConfigError",
    "NotifyTransportError",
    "NotifyTimeoutError",
    "NotifyAuthError",
    "NotifyHttpError",
    "NotifyResponseError",
    "NotifyMessage",
    "KIND_DRAFT",
    "KIND_ESCALATION",
    "STATUS_SENT",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "ERROR_CODE_CONFIG_INVALID",
    "ERROR_CODE_UNREACHABLE",
    "ERROR_CODE_TIMEOUT",
    "ERROR_CODE_AUTH_FAILED",
    "ERROR_CODE_HTTP_STATUS",
    "ERROR_CODE_CONTRACT_VIOLATION",
]

# --- 可观测错误码（机器可读；审计流水与运行摘要按这一套口径统计） -----------

#: 配置不全（缺地址 / 缺收件人 / 协议不对 / 认证项只填一半）。
ERROR_CODE_CONFIG_INVALID = "notify_config_invalid"
#: 连接不可达 / 连接被拒 / 连接中断。
ERROR_CODE_UNREACHABLE = "notify_unreachable"
#: 请求超时。
ERROR_CODE_TIMEOUT = "notify_timeout"
#: 认证失败（账号密码不对、令牌过期）。
ERROR_CODE_AUTH_FAILED = "notify_auth_failed"
#: 对方返回非 2xx。
ERROR_CODE_HTTP_STATUS = "notify_http_status"
#: 对方返回 2xx 但响应体不合契约（不是合法 JSON、错误码非零）。
ERROR_CODE_CONTRACT_VIOLATION = "notify_contract_violation"

# --- 消息种类 ---------------------------------------------------------------

#: 补充报告草稿已回写、等待签发。
KIND_DRAFT = "draft"
#: 草稿写不回去了，转人工处置。
KIND_ESCALATION = "escalation"

# --- 通道结果状态 -----------------------------------------------------------

#: 发出去了。
STATUS_SENT = "sent"
#: 没发出去。
STATUS_FAILED = "failed"
#: 没发（通道没配或没有收件人）。
STATUS_SKIPPED = "skipped"


# ------------------------------------------------------------------ 异常


class NotifyError(RuntimeError):
    """通知发送失败（基类）。

    每种失败都带一个**机器可读的错误码**：同一类故障在不同轮次、不同机器上
    要能归到同一个桶里 —— 否则值班的人只能说「又没发出去」。
    """

    #: 本类错误的默认错误码；子类可覆盖。
    ERROR_CODE = ERROR_CODE_CONFIG_INVALID

    def __init__(
        self,
        detail: str,
        *,
        channel: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        self.detail = detail
        self.channel = channel
        self.status_code = status_code
        self.error_code = error_code or type(self).ERROR_CODE
        code = f"（HTTP {status_code}）" if status_code is not None else ""
        super().__init__(f"{detail}{code}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "channel": self.channel,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_type": type(self).__name__,
        }


class NotifyConfigError(NotifyError):
    """配置不全：缺地址、缺收件人、认证项只填一半。"""

    ERROR_CODE = ERROR_CODE_CONFIG_INVALID


class NotifyTransportError(NotifyError):
    """连接层失败：连不上、被拒、连接中断。"""

    ERROR_CODE = ERROR_CODE_UNREACHABLE


class NotifyTimeoutError(NotifyTransportError):
    """请求超时。"""

    ERROR_CODE = ERROR_CODE_TIMEOUT


class NotifyAuthError(NotifyError):
    """认证失败：账号密码不对、令牌过期。"""

    ERROR_CODE = ERROR_CODE_AUTH_FAILED


class NotifyHttpError(NotifyError):
    """对方返回非 2xx。"""

    ERROR_CODE = ERROR_CODE_HTTP_STATUS


class NotifyResponseError(NotifyError):
    """对方返回 2xx，但响应体不合契约。"""

    ERROR_CODE = ERROR_CODE_CONTRACT_VIOLATION


# ------------------------------------------------------------------ 消息


@dataclass(frozen=True)
class NotifyMessage:
    """一条待发的通知。

    ``body`` 是纯文本正文（邮箱用），``markdown`` 是加粗正文（企业微信群机器人
    用）；``recipients`` 为空时由通道自己取默认收件人。
    """

    subject: str
    body: str
    markdown: str = ""
    recipients: tuple[str, ...] = ()
    accession_no: str | None = None
    kind: str = KIND_DRAFT
    #: 附加的业务事实（条目数、版本号等），随审计一起留痕。
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "accession_no": self.accession_no,
            "kind": self.kind,
            "recipients": list(self.recipients),
            "facts": dict(self.facts),
        }


@dataclass(frozen=True)
class ChannelResult:
    """一个通道发一条消息的结果。"""

    channel: str
    status: str
    detail: str = ""
    error_code: str | None = None
    attempts: int = 0
    recipient_count: int = 0

    @property
    def sent(self) -> bool:
        return self.status == STATUS_SENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "detail": self.detail,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "recipient_count": self.recipient_count,
        }


class NotifyChannel:
    """通知通道的接口。

    通道只管「发」这一件事：判断自己配好了没有、把一条消息发出去、发出去或
    发不出去各返回什么。重试、降级、审计由调度器统一管 —— 否则每个通道各写
    一遍重试，口径迟早会飘。
    """

    #: 通道名（出现在审计与摘要里，一眼认出是哪条路）。
    name: str = "channel"

    def is_configured(self) -> bool:
        """这条通道现在能不能用。"""
        raise NotImplementedError

    def send(self, message: NotifyMessage) -> ChannelResult:
        """把消息发出去。

        失败一律抛 :class:`NotifyError` 的子类（带错误码），不要自己吞掉 ——
        重试与降级由调度器决定。
        """
        raise NotImplementedError
