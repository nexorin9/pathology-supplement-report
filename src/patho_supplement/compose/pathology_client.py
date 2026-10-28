"""病理信息系统出站客户端。

这是本产品**唯一会改变真实业务状态**的动作：把成好的补充报告草稿 POST 回病理
系统的草稿区。因此这里的两条接口必须逐字段可核对，契约写在模块头部，代码与
契约一一对应。

两条出站接口（凭据位全部来自环境变量，不在代码里出现任何真实地址与密钥）：

===========================  ==========================================  ==========
方向                         契约                                        凭据位
===========================  ==========================================  ==========
读基线                       ``GET {base}/api/v1/pathology-reports/``     ``Bearer``
                             ``{accession_no}``
写草稿                       ``POST {base}/api/v1/pathology-reports/``   ``Bearer``
                             ``{accession_no}/supplemental-drafts``
===========================  ==========================================  ==========

**GET 响应字段**（病理报告基线，与本地基线快照同一套词汇）::

    {
      "accession_no": "ACC-EXAMPLE-0001",
      "base_version": "base-v3",
      "blocks": [
        {"region_seq": 1, "region_desc": "子宫体", "block_no": "B1",
         "order_no": "ORD-EXAMPLE-0001", "ordered_items": ["IHC-CD3"]}
      ]
    }

**POST 请求体**::

    {
      "accession_no": "ACC-EXAMPLE-0001",
      "base_version": "base-v3",
      "body": "病理号：ACC-EXAMPLE-0001\\n补充报告草稿（版本 1）\\n…",
      "entries": [
        {"item_code": "IHC-CD3", "value": "阴性", "block_no": "B1",
         "region_seq": 1, "region_desc": "子宫体",
         "source_system": "IMMUNO-LAB", "observed_at": "20260101130000",
         "placeholder": false, "alignment": {...}}
      ]
    }

**POST 响应体**（回执）::

    {"status": "accepted", "version": "draft-v2", "draft_id": "SD-EXAMPLE-0001"}

错误语义（全部是可观测的结构化错误，不静默吞掉）。每一类失败都有**机器可读的
错误码**（``error_code``）—— 同一类故障在不同轮次、不同机器上必须能归到同一个
桶里，否则值班的人只能说「又失败了」，说不出「哪一类失败」：

===========================  ==========================================  ==========================
场景                         抛出的异常                                错误码
===========================  ==========================================  ==========================
配置不全（缺地址 / 缺凭据）    :class:`PathologyClientError`               ``pathology_config_invalid``
接口不可达 / 连接中断          :class:`PathologyTransportError`            ``pathology_unreachable``
请求超时                     :class:`PathologyTimeoutError`              ``pathology_timeout``
返回非 2xx                    :class:`PathologyHttpError`（带 ``status_code``） ``pathology_http_status``
返回 409 / 回执状态冲突         :class:`PathologyConflictError`（版本冲突）   ``pathology_version_conflict``
返回 2xx 但响应体不合契约      :class:`PathologyResponseError`              ``pathology_contract_violation``
===========================  ==========================================  ==========================

错误码随异常一起落进审计流水（``to_dict()`` 里带 ``error_code``），清单与
运行摘要按这一套口径统计，不会把「超时」和「对面 500」混成同一件事。
"""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

from ..models import DraftEntry, ReportBaseline
from .baseline import BaselineError, baseline_from_payload

__all__ = [
    "PathologyClient",
    "PathologyClientError",
    "PathologyTransportError",
    "PathologyTimeoutError",
    "PathologyHttpError",
    "PathologyConflictError",
    "PathologyResponseError",
    "WritebackReceipt",
    "REPORT_PATH_TEMPLATE",
    "SUPPLEMENTAL_DRAFT_PATH_TEMPLATE",
    "ERROR_CODE_CONFIG_INVALID",
    "ERROR_CODE_UNREACHABLE",
    "ERROR_CODE_TIMEOUT",
    "ERROR_CODE_HTTP_STATUS",
    "ERROR_CODE_VERSION_CONFLICT",
    "ERROR_CODE_CONTRACT_VIOLATION",
]

#: 取基线的路径模板（``{accession_no}`` 会被 URL 编码后填入）。
REPORT_PATH_TEMPLATE = "/api/v1/pathology-reports/{accession_no}"

#: 回写草稿的路径模板。
SUPPLEMENTAL_DRAFT_PATH_TEMPLATE = (
    "/api/v1/pathology-reports/{accession_no}/supplemental-drafts"
)

#: 回执里表示「已接受」的状态取值。
_ACCEPTED_STATUSES = frozenset({"accepted", "ok"})

# --- 可观测错误码（机器可读，审计流水与清单按这一套口径统计） ---------------

#: 配置不全（缺地址、缺凭据、协议不对）。
ERROR_CODE_CONFIG_INVALID = "pathology_config_invalid"
#: 接口不可达 / 连接被拒 / 连接中断。
ERROR_CODE_UNREACHABLE = "pathology_unreachable"
#: 请求超时。
ERROR_CODE_TIMEOUT = "pathology_timeout"
#: 返回非 2xx。
ERROR_CODE_HTTP_STATUS = "pathology_http_status"
#: 版本冲突（HTTP 409，或回执状态为 conflict）。
ERROR_CODE_VERSION_CONFLICT = "pathology_version_conflict"
#: 返回 2xx 但响应体不合契约（缺字段 / 不是合法 JSON / 状态非接受）。
ERROR_CODE_CONTRACT_VIOLATION = "pathology_contract_violation"


# ------------------------------------------------------------------ 异常


class PathologyClientError(RuntimeError):
    """出站调用失败（基类）。

    每种失败都带一个**机器可读的错误码**（``error_code``）：同一类故障在不同
    轮次、不同机器上要能归到同一个桶里，值班的人才分得清「对面 500」和
    「我们这边超时」。
    """

    #: 本类错误的默认错误码；子类可覆盖。
    ERROR_CODE = ERROR_CODE_CONFIG_INVALID

    def __init__(
        self,
        detail: str,
        *,
        path: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        self.detail = detail
        self.path = path
        self.status_code = status_code
        self.error_code = error_code or type(self).ERROR_CODE
        where = f"[{path}] " if path else ""
        code = f"（HTTP {status_code}）" if status_code is not None else ""
        super().__init__(f"{where}{detail}{code}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "path": self.path,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_type": type(self).__name__,
        }


class PathologyTransportError(PathologyClientError):
    """连接层失败：接口不可达、连接被拒、连接中断。"""

    ERROR_CODE = ERROR_CODE_UNREACHABLE


class PathologyTimeoutError(PathologyTransportError):
    """请求超时。"""

    ERROR_CODE = ERROR_CODE_TIMEOUT


class PathologyHttpError(PathologyClientError):
    """返回非 2xx。``status_code`` 是判断怎么处理的关键信息。"""

    ERROR_CODE = ERROR_CODE_HTTP_STATUS


class PathologyConflictError(PathologyHttpError):
    """版本冲突：本地草稿的基础版本与病理系统当前版本对不上。

    冲突时**不能覆盖**别人的版本，也不能把草稿丢掉 —— 只能停手交给人。
    """

    ERROR_CODE = ERROR_CODE_VERSION_CONFLICT


class PathologyResponseError(PathologyClientError):
    """返回 2xx，但响应体不符合契约（缺字段、不是合法 JSON、状态非接受）。"""

    ERROR_CODE = ERROR_CODE_CONTRACT_VIOLATION


# ------------------------------------------------------------------ 回执


@dataclass(frozen=True)
class WritebackReceipt:
    """草稿回写回执。"""

    accession_no: str
    status: str
    version: str
    draft_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status in _ACCEPTED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "status": self.status,
            "version": self.version,
            "draft_id": self.draft_id,
        }


# ------------------------------------------------------------------ 客户端


class PathologyClient:
    """病理信息系统出站客户端。

    只用标准库 :mod:`http.client`：院内机上不装第三方 HTTP 客户端，减少依赖面。
    连接按调用建立、用完即关 —— 守护进程是常驻的，但单次调用不该留着半开连接。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        user_agent: str = "patho-supplement/0.1",
    ) -> None:
        url = (base_url or "").strip()
        if not url:
            raise PathologyClientError("缺少病理系统地址（PATHOLOGY_BASE_URL）")
        if not (token or "").strip():
            raise PathologyClientError("缺少病理系统凭据（PATHOLOGY_API_TOKEN）")

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise PathologyClientError(
                f"病理系统地址的协议只能是 http 或 https，当前为 {parts.scheme!r}"
            )
        if not parts.hostname:
            raise PathologyClientError(f"病理系统地址缺少主机名：{url!r}")

        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._base_path = parts.path.rstrip("/")
        self._token = token.strip()
        self._timeout = float(timeout)
        self._user_agent = user_agent

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, config, *, timeout: float | None = None) -> "PathologyClient":
        """按配置面构造客户端；凭据缺失时报可读错误。"""
        missing = []
        if not config.pathology_base_url:
            missing.append("PATHOLOGY_BASE_URL")
        if not config.pathology_api_token:
            missing.append("PATHOLOGY_API_TOKEN")
        if missing:
            raise PathologyClientError(
                "缺少环境变量：" + "、".join(missing) + "（参考 .env.example 填写）"
            )
        return cls(
            config.pathology_base_url,
            config.pathology_api_token,
            timeout=timeout if timeout is not None else config.http_timeout,
        )

    # ------------------------------------------------------------ 对外接口

    def fetch_baseline(self, accession_no: str) -> ReportBaseline:
        """取一份病理报告的基线（部位列表与已申请项目）。"""
        accession = (accession_no or "").strip()
        if not accession:
            raise PathologyClientError("病理号为空，无法取基线")
        path = self._build_path(REPORT_PATH_TEMPLATE, accession)
        status, raw = self._request("GET", path)
        if status == 404:
            raise PathologyHttpError("病理报告基线不存在", path=path, status_code=status)
        if not 200 <= status < 300:
            raise PathologyHttpError("取基线失败", path=path, status_code=status)
        payload = self._decode_json(raw, path)
        try:
            return baseline_from_payload(payload, expected_accession_no=accession)
        except BaselineError as exc:
            raise PathologyResponseError(
                f"基线响应字段不合契约：{exc.detail}", path=path
            ) from exc

    def post_supplemental_draft(
        self,
        accession_no: str,
        *,
        body: str,
        base_version: str | None = None,
        entries: Sequence[DraftEntry | Mapping[str, Any]] = (),
    ) -> WritebackReceipt:
        """把补充报告草稿提交到病理系统的草稿区。

        返回回执（含接受版本号）。版本冲突时抛 :class:`PathologyConflictError`，
        调用方必须保留本地草稿并转人工处置，不得覆盖。
        """
        accession = (accession_no or "").strip()
        if not accession:
            raise PathologyClientError("病理号为空，无法回写草稿")
        path = self._build_path(SUPPLEMENTAL_DRAFT_PATH_TEMPLATE, accession)
        payload = {
            "accession_no": accession,
            "base_version": base_version,
            "body": body,
            "entries": [_entry_to_dict(entry) for entry in entries],
        }
        status, raw = self._request("POST", path, payload)
        if status == 409:
            raise PathologyConflictError(
                "草稿版本冲突，病理系统当前版本与本地基础版本不一致",
                path=path,
                status_code=status,
            )
        if not 200 <= status < 300:
            raise PathologyHttpError("回写草稿失败", path=path, status_code=status)

        receipt_payload = self._decode_json(raw, path)
        receipt_status = str(receipt_payload.get("status") or "").strip()
        if not receipt_status:
            raise PathologyResponseError("回执缺 status 字段", path=path)
        if receipt_status == "conflict":
            raise PathologyConflictError(
                "草稿版本冲突，病理系统当前版本与本地基础版本不一致",
                path=path,
                status_code=status,
            )
        if receipt_status not in _ACCEPTED_STATUSES:
            raise PathologyResponseError(
                f"回执状态非接受：{receipt_status!r}", path=path, status_code=status
            )
        version = str(
            receipt_payload.get("version") or receipt_payload.get("accepted_version") or ""
        ).strip()
        if not version:
            raise PathologyResponseError("回执缺版本号", path=path, status_code=status)
        draft_id = receipt_payload.get("draft_id")
        return WritebackReceipt(
            accession_no=accession,
            status=receipt_status,
            version=version,
            draft_id=(str(draft_id) if draft_id else None),
            raw=dict(receipt_payload),
        )

    def check_reachable(self) -> tuple[bool, str]:
        """连通性检查：只测 TCP 能不能连上，不带业务语义。

        返回 ``(是否可达, 说明)``。凭据不全时返回 ``(False, "未配置…")``，
        不去猜地址。
        """
        if not self._token:
            return False, "未配置 PATHOLOGY_API_TOKEN，跳过连通性检查"
        try:
            conn = self._connect()
        except PathologyClientError as exc:
            return False, exc.detail
        try:
            conn.connect()
        except TimeoutError:
            return False, f"连接 {self._host}:{self._port} 超时"
        except OSError as exc:
            return False, f"连接 {self._host}:{self._port} 失败：{exc}"
        finally:
            conn.close()
        return True, f"{self._host}:{self._port} 可连接"

    def check_baseline(self, accession_no: str) -> tuple[bool, str]:
        """带业务的连通性检查：真取一次基线。"""
        try:
            baseline = self.fetch_baseline(accession_no)
        except PathologyClientError as exc:
            return False, exc.detail
        return True, (
            f"基线可取（{baseline.accession_no}，{len(baseline.blocks)} 个取材部位）"
        )

    # ------------------------------------------------------------ 内部

    def _build_path(self, template: str, accession_no: str) -> str:
        encoded = quote(accession_no, safe="")
        return f"{self._base_path}{template.format(accession_no=encoded)}"

    def _connect(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> tuple[int, bytes]:
        """发一次请求，返回 ``(状态码, 响应体字节)``。

        连接层的问题一律转成 :class:`PathologyTransportError`；**不重试** ——
        重试与退避是回写队列的职责（任务 id 38），这里保持单次调用语义清晰。
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
        }
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        conn = self._connect()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, data
        except TimeoutError as exc:
            raise PathologyTimeoutError(
                f"{method} {path} 超时（{self._timeout}s）", path=path
            ) from exc
        except http.client.HTTPException as exc:
            raise PathologyTransportError(
                f"{method} {path} 响应不合法：{exc}", path=path
            ) from exc
        except OSError as exc:
            raise PathologyTransportError(
                f"{method} {path} 连接失败：{exc}", path=path
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _decode_json(raw: bytes, path: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PathologyResponseError(f"响应体不是合法 JSON：{exc}", path=path) from exc
        if not isinstance(payload, dict):
            raise PathologyResponseError("响应体顶层必须是 JSON 对象", path=path)
        return payload


def _entry_to_dict(entry: DraftEntry | Mapping[str, Any]) -> dict[str, Any]:
    """草稿条目 → 请求体里的字段结构。"""
    if isinstance(entry, DraftEntry):
        return entry.to_dict()
    return dict(entry)
