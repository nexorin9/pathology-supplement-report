"""本地病理系统替身 —— 只为**本机跑通真实出站路径**而存在。

**这不是产品的一部分。** 产品出厂时对接的是院内真实的病理信息系统
（``GET /api/v1/pathology-reports/{accession_no}`` 取基线，
``POST /api/v1/pathology-reports/{accession_no}/supplemental-drafts`` 回写草稿）。
本机没有院内系统，采集实测截图时需要一个能说同一种话的服务端，于是有了它。

关键点：替身只换掉**服务端**，出站客户端（``compose/pathology_client.py``）、
字段规范、凭据位（``Authorization: Bearer``）走的都是产品代码本体那条路径，
与院内对接时完全一致。

用法::

    python -m tools.local_pathology_stub --baseline tests/fixtures/inbox/baseline.json
    # 打印一行：本地病理系统替身已启动：http://127.0.0.1:<port>
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: 收到的出站请求（方法、路径、请求体），供采集脚本打印出来做证据。
RECEIVED: list[tuple[str, str, bytes]] = []


class LocalPathologyStub:
    """按院内接口约定提供基线查询与草稿回写。"""

    def __init__(self, baseline: dict[str, dict], host: str = "127.0.0.1", port: int = 0):
        self._reports = {r["accession_no"]: r for r in baseline.get("reports", [])}
        self.server = ThreadingHTTPServer((host, port), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _make_handler(self):
        reports = self._reports

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                RECEIVED.append(("GET", self.path, b""))
                accession = self.path.strip("/").split("/")[3]
                report = reports.get(accession)
                if report is None:
                    self._respond(404, {"message": "报告不存在"})
                    return
                self._respond(200, report)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                RECEIVED.append(("POST", self.path, body))
                accession = self.path.strip("/").split("/")[3]
                self._respond(
                    200,
                    {
                        "status": "accepted",
                        "version": "draft-v2",
                        "draft_id": f"SD-EXAMPLE-{accession[-4:]}",
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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地病理系统替身（采集实测截图用）")
    parser.add_argument("--baseline", default="tests/fixtures/inbox/baseline.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    stub = LocalPathologyStub(baseline, host=args.host, port=args.port)
    print(f"本地病理系统替身已启动：{stub.base_url}", flush=True)
    try:
        stub.thread.join()
    except KeyboardInterrupt:
        stub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
