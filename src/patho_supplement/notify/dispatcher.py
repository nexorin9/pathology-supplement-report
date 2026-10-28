"""通知调度器：多通道扇出、重试、降级与留痕。

通知是成稿之后的一步，因此本模块的第一条规矩是：**调度器永远不抛异常**。
成稿已经写回病理系统了，通知发不出去只是「通知失败」，不能让这一轮白跑。

四条口径：

1. **一条消息走全部可用通道。** SMTP 与企业微信各发一遍；哪条通道坏了，
   另一条照发。
2. **失败先重试，再降级。** 同一通道默认重试 2 次（间隔递增）；仍失败就记
   审计并降级 —— 不是把异常抛回主流程。
3. **降级要看得见。** 一条通道没配（比如这家医院没开通企业微信），在运行
   摘要里显式写明「已降级为只走 SMTP」；一条都没配，也要写明「本轮未发送」。
4. **失败要留痕。** 发出、降级、失败、没发，四种情况都写审计流水，明细里带
   通道名与错误码 —— 事后能答出「当时到底发出去了没有」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..audit.ledger import (
    CATEGORY_NOTIFY,
    EVENT_NOTIFY_DEGRADED,
    EVENT_NOTIFY_FAILED,
    EVENT_NOTIFY_SENT,
    EVENT_NOTIFY_SKIPPED,
    record_audit,
)
from .base import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    ChannelResult,
    NotifyChannel,
    NotifyError,
    NotifyMessage,
)
from .smtp_sender import SmtpSender
from .wecom_sender import WeComSender

__all__ = [
    "DispatchResult",
    "NotifyDispatcher",
    "DEFAULT_NOTIFY_MAX_ATTEMPTS",
    "DEFAULT_NOTIFY_RETRY_DELAY_SECONDS",
]

#: 单条通道的默认重试次数（首发 + 重试 1 次）。
DEFAULT_NOTIFY_MAX_ATTEMPTS = 2
#: 重试间隔基数（秒）：第 1 次重试等这么久，第 2 次等两倍。
DEFAULT_NOTIFY_RETRY_DELAY_SECONDS = 1.0

#: 通道的中文名（摘要与审计明细里用）。
CHANNEL_LABELS = {"smtp": "SMTP 邮件", "wecom": "企业微信群机器人"}


@dataclass
class DispatchResult:
    """一次派发的结果。

    ``status`` 四选一：

    - ``sent``：所有可用通道都发成功了；
    - ``partial``：有的发成功、有的失败；
    - ``failed``：可用通道全失败；
    - ``skipped``：一个通道都没配，本轮没发。
    """

    status: str = STATUS_SENT
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    results: list[ChannelResult] = field(default_factory=list)

    @property
    def attempted(self) -> bool:
        """本轮是不是真的尝试发了。"""
        return bool(self.results)

    def channel_label(self, name: str) -> str:
        return CHANNEL_LABELS.get(name, name)

    def summary_line(self) -> str:
        """一句话摘要（进守护进程每轮运行摘要）。"""
        if self.status == STATUS_SKIPPED:
            return "；".join(self.notes) if self.notes else "未配置通知通道"
        parts = [f"{self.channel_label(name)}成功" for name in self.sent]
        parts += [f"{self.channel_label(name)}失败" for name in self.failed]
        line = "、".join(parts) if parts else "无通道可发"
        if self.degraded and self.notes:
            line = f"{line}（{'；'.join(self.notes)}）"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sent": list(self.sent),
            "failed": list(self.failed),
            "degraded": self.degraded,
            "notes": list(self.notes),
            "results": [item.to_dict() for item in self.results],
        }


class NotifyDispatcher:
    """通知调度器。

    通道是注入进来的：生产走真实 SMTP / 企业微信，测试走本地替身 ——
    但**同一条代码路径**，不为测试另写分支。
    """

    def __init__(
        self,
        channels: Sequence[NotifyChannel] = (),
        *,
        database: Any = None,
        max_attempts: int = DEFAULT_NOTIFY_MAX_ATTEMPTS,
        retry_delay: float = DEFAULT_NOTIFY_RETRY_DELAY_SECONDS,
        echo: Callable[[str], None] | None = None,
        missing_notes: Iterable[str] = (),
    ) -> None:
        self._channels = list(channels)
        self._database = database
        self._max_attempts = max(1, int(max_attempts))
        self._retry_delay = max(0.0, float(retry_delay))
        self._echo = echo
        self._missing_notes = list(missing_notes)

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        database: Any = None,
        echo: Callable[[str], None] | None = None,
    ) -> "NotifyDispatcher":
        """按配置面装配通道。

        通道凭据全部来自环境变量；某条通道配了一半（只填了账号没填密码）时，
        这条通道**不算可用**，但配错的原因会写进降级说明 —— 运维一眼能看出
        是「没开通」还是「配错了」。
        """
        channels: list[NotifyChannel] = []
        missing: list[str] = []

        if (config.smtp_host or "").strip():
            try:
                sender = SmtpSender.from_config(config)
            except NotifyError as exc:
                missing.append(f"SMTP 通道不可用：{exc.detail}")
            else:
                if sender.is_configured():
                    channels.append(sender)
                else:
                    missing.append("SMTP 通道不可用：未配置收件人（NOTIFY_RECIPIENTS）")
        else:
            missing.append("SMTP 通道未配置")

        if (config.wecom_webhook_url or "").strip():
            try:
                sender = WeComSender.from_config(config)
            except NotifyError as exc:
                missing.append(f"企业微信通道不可用：{exc.detail}")
            else:
                if sender.is_configured():
                    channels.append(sender)
                else:
                    missing.append("企业微信通道不可用：webhook 地址不完整")
        else:
            missing.append("企业微信通道未配置")

        return cls(
            channels,
            database=database,
            max_attempts=config.notify_max_attempts,
            retry_delay=config.notify_retry_delay_seconds,
            echo=echo,
            missing_notes=missing,
        )

    # ------------------------------------------------------------ 属性

    @property
    def channels(self) -> tuple[NotifyChannel, ...]:
        return tuple(self._channels)

    def channel_status(self) -> list[dict[str, Any]]:
        """各通道的可用状态（doctor 打印用；不回显凭据）。"""
        rows: list[dict[str, Any]] = []
        for channel in self._channels:
            describe = getattr(channel, "describe", None)
            rows.append(
                {
                    "channel": channel.name,
                    "label": CHANNEL_LABELS.get(channel.name, channel.name),
                    "configured": True,
                    "detail": describe() if callable(describe) else "",
                }
            )
        for note in self._missing_notes:
            rows.append(
                {
                    "channel": _channel_of_note(note),
                    "label": CHANNEL_LABELS.get(_channel_of_note(note), _channel_of_note(note)),
                    "configured": False,
                    "detail": note,
                }
            )
        return rows

    # ------------------------------------------------------------ 派发

    def dispatch(self, message: NotifyMessage, *, reason: str = "") -> DispatchResult:
        """把一条消息发出去（永不抛异常）。

        ``reason`` 是这次为什么发通知（「草稿待签发」「回写升级告警」），
        写进审计明细，事后能答出「这条通知是为什么发的」。
        """
        result = DispatchResult()
        if not self._channels:
            # 一条通道都没配：写明「本轮未发送」，并留一行审计 ——
            # 「没发通知」这件事本身要可查。
            result.status = STATUS_SKIPPED
            result.notes = list(self._missing_notes) or ["未配置任何通知通道"]
            result.notes.append("本轮未发送")
            self._audit(
                EVENT_NOTIFY_SKIPPED,
                message,
                {"reason": reason, "notes": list(result.notes)},
            )
            return result

        if len(self._channels) < 2 and self._missing_notes:
            # 少了一条通道 → 显式降级，并在摘要里写明降级到哪条。
            result.degraded = True
            result.notes = list(self._missing_notes)
            kept = "、".join(CHANNEL_LABELS.get(c.name, c.name) for c in self._channels)
            result.notes.append(f"已降级为只走 {kept}")

        for channel in self._channels:
            channel_result = self._send_with_retry(channel, message)
            result.results.append(channel_result)
            if channel_result.status == STATUS_SENT:
                result.sent.append(channel.name)
                self._audit(
                    EVENT_NOTIFY_SENT,
                    message,
                    {
                        "reason": reason,
                        "channel": channel.name,
                        "detail": channel_result.detail,
                        "recipients": list(message.recipients),
                        "attempts": channel_result.attempts,
                        **message.facts,
                    },
                )
            else:
                result.failed.append(channel.name)
                self._audit(
                    EVENT_NOTIFY_FAILED,
                    message,
                    {
                        "reason": reason,
                        "channel": channel.name,
                        "error_code": channel_result.error_code,
                        "detail": channel_result.detail,
                        "attempts": channel_result.attempts,
                        **message.facts,
                    },
                )

        if result.failed:
            result.degraded = True
            result.status = "partial" if result.sent else STATUS_FAILED
        else:
            result.status = STATUS_SENT

        if result.degraded:
            self._audit(
                EVENT_NOTIFY_DEGRADED,
                message,
                {
                    "reason": reason,
                    "notes": list(result.notes),
                    "failed_channels": list(result.failed),
                    "sent_channels": list(result.sent),
                },
            )

        if self._echo is not None:
            self._echo(f"通知：{result.summary_line()}")
        return result

    # ------------------------------------------------------------ 内部

    def _send_with_retry(self, channel: NotifyChannel, message: NotifyMessage) -> ChannelResult:
        """发一条消息，失败按间隔递增重试。

        重试用尽也不抛异常 —— 返回一条失败结果，由调度器统一记审计与降级。
        """
        attempts = 0
        last: NotifyError | None = None
        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            try:
                return channel.send(message)
            except NotifyError as exc:
                last = exc
            except Exception as exc:  # noqa: BLE001 - 通道的意外异常也不能拖垮成稿
                last = NotifyError(f"通道异常：{exc}", channel=channel.name)
            if attempt < self._max_attempts and self._retry_delay > 0:
                time.sleep(self._retry_delay * attempt)
        assert last is not None  # 循环至少跑一次
        return ChannelResult(
            channel=channel.name,
            status=STATUS_FAILED,
            detail=last.detail,
            error_code=last.error_code,
            attempts=attempts,
        )

    def _audit(self, event: str, message: NotifyMessage, detail: dict[str, Any]) -> None:
        if self._database is None:
            return
        record_audit(
            self._database,
            category=CATEGORY_NOTIFY,
            event=event,
            accession_no=message.accession_no,
            detail=detail,
        )


def _channel_of_note(note: str) -> str:
    """从一句降级说明里认出是哪条通道（用于 doctor 的通道清单）。"""
    if note.startswith("SMTP") or "SMTP" in note:
        return "smtp"
    if "企业微信" in note:
        return "wecom"
    return "unknown"
