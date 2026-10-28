"""企业微信群机器人通道。

企业微信是院内实际在用的通知方式（科室群、值班群），但不是每家医院都开通了
—— 所以这条通道**必须可降级**：没开通时自动只走 SMTP，并把这件事写进运行
摘要。「通知没发出去而没人知道」比「不发通知」更危险，所以降级要看得见。

接口契约（企业微信群机器人 webhook）::

    POST {WECOM_WEBHOOK_URL}
    Content-Type: application/json; charset=utf-8

    {"msgtype": "markdown", "markdown": {"content": "**病理号：…**"}}

    响应：{"errcode": 0, "errmsg": "ok"}

失败口径：

=========================  ===========================================  ==========================
场景                       抛出的异常                                   错误码
=========================  ===========================================  ==========================
地址缺失 / 协议不对         :class:`NotifyConfigError`                   ``notify_config_invalid``
连接超时                   :class:`NotifyTimeoutError`                  ``notify_timeout``
连不上 / 连接中断          :class:`NotifyTransportError`                 ``notify_unreachable``
返回非 2xx                 :class:`NotifyHttpError`                      ``notify_http_status``
返回 2xx 但 errcode 非 0    :class:`NotifyResponseError`                 ``notify_contract_violation``
=========================  ===========================================  ==========================

只用标准库 :mod:`http.client`。
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import urlsplit

from .base import (
    STATUS_SENT,
    ChannelResult,
    NotifyChannel,
    NotifyConfigError,
    NotifyHttpError,
    NotifyMessage,
    NotifyResponseError,
    NotifyTimeoutError,
    NotifyTransportError,
)

__all__ = ["WeComSender", "MAX_CONTENT_BYTES"]

#: 企业微信 markdown 消息体的内容上限（字节）。超了对方会拒收，
#: 所以这里自己截断并标注，而不是让消息凭空消失。
MAX_CONTENT_BYTES = 4096


class WeComSender(NotifyChannel):
    """企业微信群机器人通道。"""

    name = "wecom"

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 10.0,
        max_content_bytes: int = MAX_CONTENT_BYTES,
        user_agent: str = "patho-supplement/0.1",
    ) -> None:
        url = (webhook_url or "").strip()
        if not url:
            raise NotifyConfigError(
                "缺少企业微信 webhook 地址（环境变量 WECOM_WEBHOOK_URL）"
            )
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise NotifyConfigError(
                f"企业微信 webhook 的协议只能是 http 或 https，当前为 {parts.scheme!r}"
            )
        if not parts.hostname:
            raise NotifyConfigError(f"企业微信 webhook 缺少主机名：{url!r}")

        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._path = parts.path or "/"
        if parts.query:
            self._path = f"{self._path}?{parts.query}"
        self._timeout = float(timeout)
        self._max_content_bytes = int(max_content_bytes)
        self._user_agent = user_agent

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, config: Any) -> "WeComSender":
        """按配置面构造企业微信通道（地址取自环境变量）。"""
        return cls(
            config.wecom_webhook_url or "",
            timeout=config.http_timeout,
        )

    # ------------------------------------------------------------ 属性

    @property
    def host(self) -> str:
        return self._host

    def is_configured(self) -> bool:
        return bool(self._host)

    def describe(self) -> str:
        """一句话描述（doctor 与摘要用），不回显地址里的 key。"""
        return f"企业微信群机器人 {self._host}（webhook 路径不回显）"

    # ------------------------------------------------------------ 发送

    def send(self, message: NotifyMessage) -> ChannelResult:
        """往群里发一条 markdown 消息。"""
        content = message.markdown or message.body
        content = self._truncate(content)
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self._user_agent,
            "Content-Length": str(len(body)),
        }

        conn = self._connect()
        try:
            conn.request("POST", self._path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            raw = response.read()
        except TimeoutError as exc:
            raise NotifyTimeoutError(
                f"POST {self._path} 超时（{self._timeout}s）", channel=self.name
            ) from exc
        except http.client.HTTPException as exc:
            raise NotifyTransportError(
                f"POST {self._path} 响应不合法：{exc}", channel=self.name
            ) from exc
        except OSError as exc:
            raise NotifyTransportError(
                f"POST {self._path} 连接失败：{exc}", channel=self.name
            ) from exc
        finally:
            conn.close()

        if not 200 <= status < 300:
            raise NotifyHttpError(
                "企业微信推送失败", channel=self.name, status_code=status
            )

        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotifyResponseError(
                f"企业微信响应体不是合法 JSON：{exc}", channel=self.name
            ) from exc
        if not isinstance(result, dict):
            raise NotifyResponseError("企业微信响应体顶层必须是 JSON 对象", channel=self.name)

        errcode = result.get("errcode")
        if errcode is None:
            raise NotifyResponseError("企业微信响应缺 errcode 字段", channel=self.name)
        try:
            code = int(errcode)
        except (TypeError, ValueError) as exc:
            raise NotifyResponseError(
                f"企业微信响应 errcode 不是整数：{errcode!r}", channel=self.name
            ) from exc
        if code != 0:
            raise NotifyResponseError(
                f"企业微信返回错误码 {code}：{result.get('errmsg')!r}",
                channel=self.name,
                status_code=status,
            )

        return ChannelResult(
            channel=self.name,
            status=STATUS_SENT,
            detail="已推送到企业微信群",
            attempts=1,
            recipient_count=1,
        )

    # ------------------------------------------------------------ 内部

    def _connect(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _truncate(self, content: str) -> str:
        """内容超长就截断并标注（对方拒收超长消息，截断比消失强）。"""
        encoded = content.encode("utf-8")
        if len(encoded) <= self._max_content_bytes:
            return content
        marker = "…（内容过长已截断）"
        budget = self._max_content_bytes - len(marker.encode("utf-8"))
        clipped = encoded[:budget].decode("utf-8", errors="ignore")
        return f"{clipped}{marker}"
