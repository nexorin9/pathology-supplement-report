"""SMTP 邮件通道。

发信这件事在院内很常见：科室有公邮、有院内邮件服务器、有 SMTP 中继。
凭据全部来自环境变量（``SMTP_HOST`` / ``SMTP_PORT`` / ``SMTP_USER`` /
``SMTP_PASSWORD`` / ``SMTP_FROM``），收件人来自消息本身或 ``NOTIFY_RECIPIENTS``。

失败口径（每类一个机器可读错误码）：

=========================  ===========================================  ==========================
场景                       抛出的异常                                   错误码
=========================  ===========================================  ==========================
缺主机 / 缺收件人          :class:`NotifyConfigError`                   ``notify_config_invalid``
认证失败                   :class:`NotifyAuthError`                     ``notify_auth_failed``
连接超时                   :class:`NotifyTimeoutError`                  ``notify_timeout``
连不上 / 连接中断          :class:`NotifyTransportError`                 ``notify_unreachable``
=========================  ===========================================  ==========================

只依赖标准库 :mod:`smtplib`：院内机上不装第三方邮件客户端。
"""

from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage
from typing import Any, Sequence

from .base import (
    STATUS_SENT,
    ChannelResult,
    NotifyAuthError,
    NotifyChannel,
    NotifyConfigError,
    NotifyMessage,
    NotifyTimeoutError,
    NotifyTransportError,
)

__all__ = ["SmtpSender", "DEFAULT_SMTP_PORT", "DEFAULT_HELO_NAME", "split_recipients"]

#: 没填 ``SMTP_PORT`` 时的缺省端口（587 = STARTTLS）。
DEFAULT_SMTP_PORT = 587
#: EHLO/HELO 里报的名字。
#:
#: 标准库 :mod:`smtplib` 在没给这个名字时会去反查本机域名 —— 院内机的 DNS 经常
#: 配不全，反查一次可能卡住几十秒，通知就跟着卡住。这里给死一个名字，
#: 发信不再等 DNS。
DEFAULT_HELO_NAME = "localhost"


def split_recipients(raw: str | None) -> tuple[str, ...]:
    """把一串收件人拆开（逗号、中文逗号、分号都能分隔）。"""
    if not raw:
        return ()
    parts: list[str] = []
    for chunk in raw.replace("，", ",").replace("；", ",").replace(";", ",").split(","):
        value = chunk.strip()
        if value:
            parts.append(value)
    return tuple(parts)


def _looks_like_timeout(exc: BaseException) -> bool:
    """这个连接异常原本是不是超时。

    ``smtplib`` 把读超时包成了「连接被意外关闭」，原始的超时信息只留在
    异常消息里。这里把它还原出来，让超时与「连不上」拿到不同的错误码。
    """
    cause = exc.__cause__
    if isinstance(cause, TimeoutError):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


class SmtpSender(NotifyChannel):
    """SMTP 通道。

    连接按调用建立、用完即关：守护进程常驻，但单次发信不该留着半开连接。
    """

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_SMTP_PORT,
        *,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
        recipients: Sequence[str] = (),
        use_ssl: bool = False,
        starttls: bool = True,
        timeout: float = 10.0,
        helo_name: str = DEFAULT_HELO_NAME,
    ) -> None:
        if not (host or "").strip():
            raise NotifyConfigError("缺少 SMTP 主机（环境变量 SMTP_HOST）")
        if user and not password:
            # 只填一半比完全不填更危险：看起来配好了，一发就失败。
            raise NotifyConfigError(
                "配置了 SMTP_USER 却没配 SMTP_PASSWORD，认证项要成对填写"
            )
        if use_ssl and starttls:
            raise NotifyConfigError(
                "SMTP_USE_SSL 与 SMTP_STARTTLS 不能同时开启（隐式 TLS 与 STARTTLS 二选一）"
            )
        self._host = host.strip()
        self._port = int(port)
        self._user = (user or "").strip() or None
        self._password = password or None
        self._from = (from_addr or "").strip() or self._user or "patho-supplement@localhost"
        self._recipients = tuple(r.strip() for r in recipients if r and r.strip())
        self._use_ssl = bool(use_ssl)
        self._starttls = bool(starttls)
        self._timeout = float(timeout)
        self._helo_name = (helo_name or DEFAULT_HELO_NAME).strip() or DEFAULT_HELO_NAME

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, config: Any) -> "SmtpSender":
        """按配置面构造 SMTP 通道（凭据位全部来自环境变量）。"""
        return cls(
            config.smtp_host or "",
            config.smtp_port or DEFAULT_SMTP_PORT,
            user=config.smtp_user,
            password=config.smtp_password,
            from_addr=config.smtp_from,
            recipients=config.notify_recipients,
            use_ssl=config.smtp_use_ssl,
            starttls=config.smtp_starttls,
            timeout=config.notify_timeout,
            helo_name=config.smtp_helo_name,
        )

    # ------------------------------------------------------------ 属性

    @property
    def recipients(self) -> tuple[str, ...]:
        return self._recipients

    @property
    def host(self) -> str:
        return self._host

    def is_configured(self) -> bool:
        return bool(self._host) and bool(self._recipients)

    def describe(self) -> str:
        """一句话描述（doctor 与摘要用），不回显凭据。"""
        return f"SMTP {self._host}:{self._port}（默认收件人 {len(self._recipients)} 个）"

    # ------------------------------------------------------------ 发送

    def send(self, message: NotifyMessage) -> ChannelResult:
        """发一封邮件。

        收件人优先用消息里带的（排班表命中的当班医师），没有就用通道默认的。
        两个都没有 → 配置错误，不猜收件人。
        """
        recipients = tuple(message.recipients) or self._recipients
        if not recipients:
            raise NotifyConfigError(
                "没有收件人：消息未指定收件人，且未配置 NOTIFY_RECIPIENTS"
            )
        if not self._host:
            raise NotifyConfigError("缺少 SMTP 主机（环境变量 SMTP_HOST）")

        mail = EmailMessage()
        mail["From"] = self._from
        mail["To"] = ", ".join(recipients)
        mail["Subject"] = message.subject
        mail.set_content(message.body)

        try:
            if self._use_ssl:
                server = smtplib.SMTP_SSL(
                    self._host,
                    self._port,
                    timeout=self._timeout,
                    local_hostname=self._helo_name,
                )
            else:
                server = smtplib.SMTP(
                    self._host,
                    self._port,
                    timeout=self._timeout,
                    local_hostname=self._helo_name,
                )
            with server:
                server.ehlo()
                if self._starttls and not self._use_ssl:
                    server.starttls()
                    server.ehlo()
                if self._user:
                    server.login(self._user, self._password or "")
                server.send_message(mail, from_addr=self._from, to_addrs=list(recipients))
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyAuthError(f"SMTP 认证失败：{exc}", channel=self.name) from exc
        except smtplib.SMTPResponseException as exc:
            raise NotifyTransportError(
                f"SMTP 服务器拒绝：{exc.smtp_code} {exc.smtp_error!r}", channel=self.name
            ) from exc
        except smtplib.SMTPServerDisconnected as exc:
            # smtplib 把「读超时」也包成了「连接被意外关闭」—— 超时与连不上
            # 是两件事（前者多半是对方卡住，后者多半是地址/防火墙），错误码
            # 必须分开，否则值班的人分不清该找谁。
            if _looks_like_timeout(exc):
                raise NotifyTimeoutError(
                    f"SMTP 连接超时（{self._timeout}s）", channel=self.name
                ) from exc
            raise NotifyTransportError(
                f"SMTP 连接失败：{exc}", channel=self.name
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise NotifyTimeoutError(
                f"SMTP 连接超时（{self._timeout}s）", channel=self.name
            ) from exc
        except smtplib.SMTPException as exc:
            raise NotifyTransportError(f"SMTP 发送失败：{exc}", channel=self.name) from exc
        except OSError as exc:
            raise NotifyTransportError(f"SMTP 连接失败：{exc}", channel=self.name) from exc

        return ChannelResult(
            channel=self.name,
            status=STATUS_SENT,
            detail=f"已发给 {len(recipients)} 个收件人",
            attempts=1,
            recipient_count=len(recipients),
        )
