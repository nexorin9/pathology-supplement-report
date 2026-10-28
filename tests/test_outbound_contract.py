"""出站契约测试：GET 基线 / POST 草稿**逐字段**断言 + 三条失败路径的可观测错误码。

本文件是任务 id 10 的落点：出站是本产品**唯一会改变真实业务状态**的动作，
所以两条接口的契约必须逐字段可核对 —— 不是一个「调用成功」的断言，而是
「请求路径对不对、Bearer 头带没带、请求体每个字段是什么、响应每个字段映射到
哪、失败时抛哪个错、错误码是哪一个」。

三条硬规矩：

1. **替身只在测试数据层**（标准库 ``http.server`` 起本地服务）。产品代码走的是
   与生产完全相同的一条路径 —— 没有任何为测试另写的分支（见
   ``test_product_module_has_no_test_only_branch``）。
2. **凭据从环境变量来**。契约测试用 ``Config.from_env`` 造配置，验证 Bearer 头
   的内容确实来自 ``PATHOLOGY_API_TOKEN``，而不是写死在代码里。
3. **失败不静默**：5xx / 超时 / 版本冲突三条路径各自映射到一个机器可读的
   错误码，并落进审计流水 —— 值班的人分得清「对面 500」和「我们这边超时」。

与 ``tests/test_outbound.py`` 的分工：那边是**守护一轮的集成视角**（目录监听、
归档、状态落库）；本文件是**出站契约的逐字段视角**（一个字段一个断言）。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from patho_supplement.compose.pathology_client import (
    ERROR_CODE_CONTRACT_VIOLATION,
    ERROR_CODE_HTTP_STATUS,
    ERROR_CODE_TIMEOUT,
    ERROR_CODE_UNREACHABLE,
    ERROR_CODE_VERSION_CONFLICT,
    REPORT_PATH_TEMPLATE,
    SUPPLEMENTAL_DRAFT_PATH_TEMPLATE,
    PathologyClient,
    PathologyClientError,
    PathologyConflictError,
    PathologyHttpError,
    PathologyResponseError,
    PathologyTimeoutError,
    PathologyTransportError,
)
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

#: 契约里约定的 GET 响应字段全集（基线）。
BASELINE_FIELDS = ("accession_no", "base_version", "blocks")
#: 契约里约定的单个取材部位字段全集。
BLOCK_FIELDS = ("region_seq", "region_desc", "block_no", "order_no", "ordered_items")
#: 契约里约定的 POST 请求体顶层字段全集（封闭集合：一个字段都不能多也不能少）。
POST_BODY_FIELDS = ("accession_no", "base_version", "body", "entries")
#: 契约里约定的单条草稿条目字段全集。
ENTRY_FIELDS = (
    "item_code",
    "item_name",
    "value",
    "region_seq",
    "region_desc",
    "block_no",
    "source_system",
    "observed_at",
    "alignment",
    "placeholder",
)


# ------------------------------------------------------------------ 契约替身


@dataclass
class _Route:
    """一条病理号的替身行为（契约由测试逐个病理号配置）。"""

    baseline: dict[str, Any] | None = None
    get_status: int = 200
    get_body: bytes | None = None
    post_status: int = 200
    post_body: bytes | None = None
    delay: float = 0.0


@dataclass
class _Stub:
    """本地协议替身：记录收到的每个请求，按病理号返回预设响应。"""

    routes: dict[str, _Route] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    # ---------------------------------------------------------- 生命周期

    def start(self) -> "_Stub":
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            # ---------------------------------------------- 路由

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                stub._record("GET", self.path, self.headers, b"")
                route = stub._route_for(self.path)
                if route.delay:
                    time.sleep(route.delay)
                if route.get_body is not None:
                    self._respond_raw(route.get_status, route.get_body)
                    return
                if route.get_status != 200:
                    self._respond(route.get_status, {"message": "契约替身：预设状态"})
                    return
                if route.baseline is None:
                    self._respond(404, {"message": "契约替身：未配置的病理号"})
                    return
                self._respond(200, route.baseline)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub._record("POST", self.path, self.headers, body)
                route = stub._route_for(self.path)
                if route.delay:
                    time.sleep(route.delay)
                if route.post_body is not None:
                    self._respond_raw(route.post_status, route.post_body)
                    return
                if route.post_status != 200:
                    self._respond(route.post_status, {"message": "契约替身：预设状态"})
                    return
                accession = stub._accession_of(self.path)
                self._respond(
                    200,
                    {
                        "status": "accepted",
                        "version": "draft-v2",
                        "draft_id": f"SD-EXAMPLE-{accession[-4:]}",
                    },
                )

            # ---------------------------------------------- 响应

            def _respond(self, status: int, payload: dict) -> None:
                self._respond_raw(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def _respond_raw(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    # ---------------------------------------------------------- 内部

    def _record(self, method: str, path: str, headers, body: bytes) -> None:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "headers": {key.lower(): value for key, value in headers.items()},
                "body": body,
            }
        )

    def _route_for(self, path: str) -> _Route:
        return self.routes.get(self._accession_of(path), _Route())

    @staticmethod
    def _accession_of(path: str) -> str:
        """从请求路径里取病理号（``/api/v1/pathology-reports/{acc}[/...]``）。

        病理号段按 ``pathology-reports`` 定位，容忍地址里带的路径前缀；取到的
        段做 URL 解码，才能对上含空格或斜杠的病理号。
        """
        parts = [unquote(part) for part in urlsplit(path).path.strip("/").split("/")]
        if "pathology-reports" in parts:
            index = parts.index("pathology-reports")
            if index + 1 < len(parts):
                return parts[index + 1]
        return ""

    # ---------------------------------------------------------- 查询

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def of(self, method: str) -> list[dict[str, Any]]:
        return [item for item in self.requests if item["method"] == method]

    def client(self, *, token: str = "contract-token", timeout: float = 2.0) -> PathologyClient:
        return PathologyClient(self.base_url, token, timeout=timeout)


#: 一份字段齐全的基线（契约里逐字段断言的对象）。
FULL_BASELINE = {
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
            "ordered_items": ["IHC-CK7"],
        },
    ],
}


@pytest.fixture()
def stub():
    server = _Stub().start()
    try:
        yield server
    finally:
        server.close()


def _route(**kwargs) -> _Route:
    route = _Route(**kwargs)
    return route


# ------------------------------------------------------------------ GET 契约


def test_get_request_path_matches_contract(stub):
    """GET 基线：请求路径就是契约里那一条，病理号拼在最后一段。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    request = stub.of("GET")[0]
    assert request["path"] == "/api/v1/pathology-reports/ACC-EXAMPLE-0001"
    assert request["path"] == REPORT_PATH_TEMPLATE.format(accession_no="ACC-EXAMPLE-0001")


def test_get_request_carries_bearer_token_read_from_env():
    """Bearer 头的内容来自环境变量 PATHOLOGY_API_TOKEN，不是写死在代码里。"""
    env = {
        "PATHOLOGY_BASE_URL": "http://pathology.example.internal",
        "PATHOLOGY_API_TOKEN": "env-token-abc",
    }
    config = Config.from_env(env)
    client = PathologyClient.from_config(config)
    assert client._token == "env-token-abc"  # noqa: SLF001 - 契约要看凭据位


def test_get_request_headers_match_contract(stub):
    """请求头：Bearer + Accept + User-Agent；GET 不带请求体所以没有 Content-Type。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    headers = stub.of("GET")[0]["headers"]
    assert headers["authorization"] == "Bearer contract-token"
    assert headers["accept"] == "application/json"
    assert headers["user-agent"].startswith("patho-supplement/")
    assert "content-type" not in headers


def test_get_response_maps_top_level_fields(stub):
    """响应顶层字段：病理号、基础版本号、部位列表。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    baseline = stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    assert baseline.accession_no == "ACC-EXAMPLE-0001"
    assert baseline.base_version == "base-v3"
    assert len(baseline.blocks) == 2


def test_get_response_maps_every_block_field(stub):
    """逐块逐字段：部位序号、部位描述、蜡块号、加做申请号、已申请项目。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    baseline = stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    first, second = baseline.blocks
    assert (first.region_seq, first.region_desc, first.block_no) == (1, "子宫体", "B1")
    assert first.order_no == "ORD-EXAMPLE-0001"
    assert first.ordered_items == ("IHC-CD3", "IHC-CD20")
    assert (second.region_seq, second.region_desc, second.block_no) == (2, "宫颈", "B2")
    assert second.order_no == "ORD-EXAMPLE-0002"
    assert second.ordered_items == ("IHC-CK7",)


def test_get_response_maps_empty_optional_fields_to_none(stub):
    """可空字段：部位序号 / 部位描述 / 蜡块号 / 加做申请号缺失时是 None，不是空串。"""
    stub.routes["ACC-EXAMPLE-0002"] = _route(
        baseline={
            "accession_no": "ACC-EXAMPLE-0002",
            "base_version": "base-v1",
            "blocks": [{"region_seq": None, "region_desc": None, "block_no": None,
                        "order_no": None, "ordered_items": []}],
        }
    )
    baseline = stub.client().fetch_baseline("ACC-EXAMPLE-0002")

    block = baseline.blocks[0]
    assert block.region_seq is None
    assert block.region_desc is None
    assert block.block_no is None
    assert block.order_no is None
    assert block.ordered_items == ()


def test_get_url_encodes_accession_number(stub):
    """病理号里有空格或斜杠时必须 URL 编码，不能拼出越界的请求路径。"""
    stub.routes["ACC EXAMPLE/1"] = _route(baseline={**FULL_BASELINE, "accession_no": "ACC EXAMPLE/1"})
    stub.client().fetch_baseline("ACC EXAMPLE/1")

    assert stub.of("GET")[0]["path"] == "/api/v1/pathology-reports/ACC%20EXAMPLE%2F1"


def test_get_keeps_base_url_path_prefix(stub):
    """地址里带路径前缀（挂在院内网关后面）时，前缀必须保留。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    client = PathologyClient(f"{stub.base_url}/pathology-gateway", "contract-token", timeout=2.0)
    client.fetch_baseline("ACC-EXAMPLE-0001")

    assert stub.of("GET")[0]["path"] == "/pathology-gateway/api/v1/pathology-reports/ACC-EXAMPLE-0001"


def test_get_rejects_baseline_of_another_report(stub):
    """替身返回别的病理号时必须报错，不能把别的报告当这份报告的基线。"""
    stub.routes["ACC-OTHER-0002"] = _route(baseline=FULL_BASELINE)
    with pytest.raises(PathologyResponseError) as excinfo:
        stub.client().fetch_baseline("ACC-OTHER-0002")
    assert excinfo.value.error_code == ERROR_CODE_CONTRACT_VIOLATION


# ------------------------------------------------------------------ POST 契约


def test_post_request_path_matches_contract(stub):
    """POST 草稿：路径是基线路径下挂 supplemental-drafts。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文", base_version="base-v3")

    request = stub.of("POST")[0]
    assert request["path"] == "/api/v1/pathology-reports/ACC-EXAMPLE-0001/supplemental-drafts"
    assert request["path"] == SUPPLEMENTAL_DRAFT_PATH_TEMPLATE.format(
        accession_no="ACC-EXAMPLE-0001"
    )


def test_post_request_headers_match_contract(stub):
    """POST 请求头：Bearer + JSON Content-Type。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")

    headers = stub.of("POST")[0]["headers"]
    assert headers["authorization"] == "Bearer contract-token"
    assert headers["content-type"].startswith("application/json")
    assert headers["accept"] == "application/json"


def test_post_body_has_exactly_the_contract_fields(stub):
    """请求体顶层字段是封闭集合：四个字段，一个都不能多、一个都不能少。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001", body="草稿正文", base_version="base-v3"
    )

    payload = json.loads(stub.of("POST")[0]["body"].decode("utf-8"))
    assert tuple(sorted(payload)) == tuple(sorted(POST_BODY_FIELDS))


def test_post_body_scalar_fields_match_contract(stub):
    """请求体三个标量字段逐个核对：病理号、基础版本号、草稿正文。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    body_text = "病理号：ACC-EXAMPLE-0001\n补充报告草稿（版本 4）\n"
    stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001", body=body_text, base_version="base-v3"
    )

    payload = json.loads(stub.of("POST")[0]["body"].decode("utf-8"))
    assert payload["accession_no"] == "ACC-EXAMPLE-0001"
    assert payload["base_version"] == "base-v3"
    assert payload["body"] == body_text
    assert "补充报告草稿" in payload["body"]


def test_post_body_entry_has_exactly_the_contract_fields(stub):
    """草稿条目字段也是封闭集合。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001",
        body="草稿正文",
        entries=[
            {
                "item_code": "IHC-CD3",
                "item_name": "CD3",
                "value": "阴性",
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "source_system": "IMMUNO-LAB",
                "observed_at": "20260101130000",
                "alignment": {"level": "L1", "confidence": "高"},
                "placeholder": False,
            }
        ],
    )

    payload = json.loads(stub.of("POST")[0]["body"].decode("utf-8"))
    entry = payload["entries"][0]
    assert tuple(sorted(entry)) == tuple(sorted(ENTRY_FIELDS))


def test_post_body_entry_values_match_contract(stub):
    """条目每个字段的值逐个核对（含中文与对齐依据）。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001",
        body="草稿正文",
        entries=[
            {
                "item_code": "IHC-CD3",
                "item_name": "CD3",
                "value": "阴性",
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "source_system": "IMMUNO-LAB",
                "observed_at": "20260101130000",
                "alignment": {"level": "L1", "confidence": "高"},
                "placeholder": False,
            }
        ],
    )

    entry = json.loads(stub.of("POST")[0]["body"].decode("utf-8"))["entries"][0]
    assert entry["item_code"] == "IHC-CD3"
    assert entry["item_name"] == "CD3"
    assert entry["value"] == "阴性"
    assert entry["region_seq"] == 1
    assert entry["region_desc"] == "子宫体"
    assert entry["block_no"] == "B1"
    assert entry["source_system"] == "IMMUNO-LAB"
    assert entry["observed_at"] == "20260101130000"
    assert entry["placeholder"] is False
    assert entry["alignment"]["level"] == "L1"
    assert entry["alignment"]["confidence"] == "高"


def test_post_body_allows_empty_entry_list(stub):
    """没有条目时 entries 是空数组 —— 不是 null，也不是缺字段。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")

    payload = json.loads(stub.of("POST")[0]["body"].decode("utf-8"))
    assert payload["entries"] == []


def test_post_body_is_utf8_json(stub):
    """请求体按 UTF-8 编码，中文不被转义成 \\uXXXX（院内系统看着不乱码）。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001", body="病理号：ACC-EXAMPLE-0001\n取材部位：子宫体\n"
    )

    raw = stub.of("POST")[0]["body"]
    assert "子宫体".encode("utf-8") in raw
    assert b"\\u5b50" not in raw


def test_post_receipt_parses_status_version_and_draft_id(stub):
    """回执解析：接受状态、接受版本号、草稿号。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE)
    receipt = stub.client().post_supplemental_draft(
        "ACC-EXAMPLE-0001", body="草稿正文", base_version="base-v3"
    )

    assert receipt.accepted is True
    assert receipt.status == "accepted"
    assert receipt.version == "draft-v2"
    assert receipt.draft_id == "SD-EXAMPLE-0001"
    assert receipt.accession_no == "ACC-EXAMPLE-0001"


def test_post_receipt_accepts_alternative_version_field(stub):
    """对面把版本号放在 accepted_version 里也要认。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(
        baseline=FULL_BASELINE,
        post_body=json.dumps({"status": "accepted", "accepted_version": "draft-v7"}).encode(
            "utf-8"
        ),
    )
    receipt = stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")

    assert receipt.accepted is True
    assert receipt.version == "draft-v7"


def test_post_receipt_without_version_is_contract_violation(stub):
    """回执缺版本号 = 不合契约，不能当成写回成功。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(
        baseline=FULL_BASELINE,
        post_body=json.dumps({"status": "accepted"}).encode("utf-8"),
    )
    with pytest.raises(PathologyResponseError) as excinfo:
        stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")
    assert excinfo.value.error_code == ERROR_CODE_CONTRACT_VIOLATION


# ------------------------------------------------------------------ 三条失败路径


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_maps_to_http_status_error_code(stub, status):
    """失败路径一：5xx → 可观测的 http_status 错误码，带上状态码。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, get_status=status)
    with pytest.raises(PathologyHttpError) as excinfo:
        stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    assert excinfo.value.status_code == status
    assert excinfo.value.error_code == ERROR_CODE_HTTP_STATUS


def test_server_error_on_post_maps_to_http_status_error_code(stub):
    """POST 侧 5xx 同样是 http_status 错误码，不是版本冲突。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, post_status=500)
    with pytest.raises(PathologyHttpError) as excinfo:
        stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")

    assert excinfo.value.status_code == 500
    assert excinfo.value.error_code == ERROR_CODE_HTTP_STATUS
    assert not isinstance(excinfo.value, PathologyConflictError)


def test_timeout_maps_to_timeout_error_code(stub):
    """失败路径二：超时 → 可观测的 timeout 错误码，不与「不可达」混为一谈。"""
    stub.routes["ACC-SLOW"] = _route(baseline=FULL_BASELINE, delay=1.5)
    with pytest.raises(PathologyTimeoutError) as excinfo:
        stub.client(timeout=0.2).fetch_baseline("ACC-SLOW")

    assert excinfo.value.error_code == ERROR_CODE_TIMEOUT
    assert excinfo.value.error_code != ERROR_CODE_UNREACHABLE


def test_unreachable_endpoint_maps_to_unreachable_error_code():
    """连不上时是 unreachable 错误码 —— 与超时分开统计。"""
    client = PathologyClient("http://127.0.0.1:9", "contract-token", timeout=1.0)
    with pytest.raises(PathologyTransportError) as excinfo:
        client.fetch_baseline("ACC-EXAMPLE-0001")

    assert excinfo.value.error_code == ERROR_CODE_UNREACHABLE
    assert not isinstance(excinfo.value, PathologyTimeoutError)


def test_version_conflict_maps_to_conflict_error_code(stub):
    """失败路径三：409 → 可观测的 version_conflict 错误码，状态码原样带出。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, post_status=409)
    with pytest.raises(PathologyConflictError) as excinfo:
        stub.client().post_supplemental_draft(
            "ACC-EXAMPLE-0001", body="草稿正文", base_version="base-v3"
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_code == ERROR_CODE_VERSION_CONFLICT


def test_receipt_status_conflict_maps_to_conflict_error_code(stub):
    """对面回 200 但状态是 conflict，也是版本冲突 —— 不能当成写回成功。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(
        baseline=FULL_BASELINE,
        post_body=json.dumps({"status": "conflict", "version": "draft-v9"}).encode("utf-8"),
    )
    with pytest.raises(PathologyConflictError) as excinfo:
        stub.client().post_supplemental_draft("ACC-EXAMPLE-0001", body="草稿正文")

    assert excinfo.value.error_code == ERROR_CODE_VERSION_CONFLICT


def test_non_json_response_maps_to_contract_violation_code(stub):
    """2xx 但不是合法 JSON：不合契约，错误码可观测，不静默吞掉。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(
        baseline=FULL_BASELINE, get_body=b"<<not json>>"
    )
    with pytest.raises(PathologyResponseError) as excinfo:
        stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    assert excinfo.value.error_code == ERROR_CODE_CONTRACT_VIOLATION


def test_error_payload_carries_observable_fields(stub):
    """错误明细带着错误码、路径、状态码 —— 落进审计流水后能分得清哪类失败。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, get_status=503)
    with pytest.raises(PathologyClientError) as excinfo:
        stub.client().fetch_baseline("ACC-EXAMPLE-0001")

    detail = excinfo.value.to_dict()
    assert detail["error_code"] == ERROR_CODE_HTTP_STATUS
    assert detail["error_type"] == "PathologyHttpError"
    assert detail["status_code"] == 503
    assert detail["path"] == "/api/v1/pathology-reports/ACC-EXAMPLE-0001"
    assert detail["detail"]


def test_missing_report_is_a_contract_level_404(stub):
    """病理报告不存在 → 404 走 http_status 错误码（不是 conflict，也不是超时）。"""
    with pytest.raises(PathologyHttpError) as excinfo:
        stub.client().fetch_baseline("ACC-NOT-THERE")

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == ERROR_CODE_HTTP_STATUS


# ------------------------------------------------------------------ 替身只在测试层


def test_product_module_has_no_test_only_branch():
    """产品代码里不许出现为测试而设的分支、替身地址或测试依赖。"""
    source = Path(
        "src/patho_supplement/compose/pathology_client.py"
    ).read_text(encoding="utf-8")
    for token in ("pytest", "mock", "Mock", "fake", "Fake", "stub", "127.0.0.1", "localhost"):
        assert token not in source, f"产品代码里出现了测试专用标记：{token}"


def test_client_used_by_contract_is_the_production_class():
    """契约测的就是生产用的那个客户端类本身。"""
    client = PathologyClient("http://pathology.example.internal", "t")
    assert type(client) is PathologyClient
    assert client.__module__ == "patho_supplement.compose.pathology_client"


# ------------------------------------------------------------------ 失败码落审计


def _daemon(config: Config, client: PathologyClient, inbox: Path, tmp_path: Path):
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    return Daemon(
        config,
        client=client,
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda _message: None,
    )


def _config(inbox: Path, base_url: str, db_path: Path) -> Config:
    return Config.from_env(
        {
            "PATHOLOGY_BASE_URL": base_url,
            "PATHOLOGY_API_TOKEN": "contract-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(db_path),
        }
    )


def test_baseline_failure_error_code_lands_in_audit_ledger(stub, tmp_path):
    """取基线 5xx：错误码落进审计流水，值班的人查得到是哪类失败。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, get_status=503)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)

    daemon = _daemon(
        _config(inbox, stub.base_url, tmp_path / "state.db"),
        stub.client(),
        inbox,
        tmp_path,
    )
    outcome = daemon.run_once()
    daemon.close()

    assert outcome.drafts_failed == 1
    with Database(tmp_path / "state.db") as db:
        rows = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'fetch_failed'"
        )
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail_json"])
    assert detail["error_code"] == ERROR_CODE_HTTP_STATUS
    assert detail["status_code"] == 503


def test_writeback_conflict_error_code_lands_in_conflict_register(stub, tmp_path):
    """回写撞上 409：冲突登记里带版本冲突错误码，本地草稿留底不覆盖。"""
    stub.routes["ACC-EXAMPLE-0001"] = _route(baseline=FULL_BASELINE, post_status=409)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)

    daemon = _daemon(
        _config(inbox, stub.base_url, tmp_path / "state.db"),
        stub.client(),
        inbox,
        tmp_path,
    )
    outcome = daemon.run_once()
    daemon.close()

    assert outcome.conflicts == 1
    with Database(tmp_path / "state.db") as db:
        rows = db.query_all("SELECT detail, state FROM writeback_conflicts")
    assert len(rows) == 1
    assert rows[0]["state"] == "open"
    detail = json.loads(rows[0]["detail"])
    assert detail["error_code"] == ERROR_CODE_VERSION_CONFLICT
    assert detail["status_code"] == 409
