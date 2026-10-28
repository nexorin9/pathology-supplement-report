"""采集真实运行截图到 ``docs/screenshots/``。

**这不是产品的一部分**，是重新采集实测证据的脚本。产品运行时零第三方依赖；
本脚本用到 Pillow（``pip install pillow``）把终端输出渲染成 PNG。

采集的是**真跑出来的输出**，不是手写的演示稿：

1. 起一个本地病理系统替身（``tools/local_pathology_stub.py``）与一个本地 SMTP
   收信端，产品出站客户端走的是与院内对接完全相同的代码路径与凭据位；
2. 把 ``tests/fixtures/inbox/`` 的脱敏样例复制到临时目录（不污染仓库里的夹具）；
3. 真实执行 ``cli doctor`` / ``cli daemon --once`` / ``cli replay`` /
   ``cli pending list`` / ``cli pending export``，抓下 stdout + stderr 原文；
4. 把原文渲染成终端样式的 PNG 落进 ``docs/screenshots/``。

用法::

    python tools/capture_screenshots.py
"""

from __future__ import annotations

import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_pathology_stub import RECEIVED, LocalPathologyStub  # noqa: E402
from render_terminal_shot import render  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHOTS_DIR = PROJECT_ROOT / "docs" / "screenshots"
FIXTURE_INBOX = PROJECT_ROOT / "tests" / "fixtures" / "inbox"


class _SMTPHandler(socketserver.StreamRequestHandler):
    """最小 SMTP 收信端：只收不发（截图里不需要邮件内容）。"""

    def handle(self) -> None:
        self.wfile.write(b"220 localhost ESMTP capture-stub\r\n")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            cmd = raw.decode("utf-8", "replace").strip()
            head = cmd.upper()
            if head.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 10485760\r\n")
            elif head.startswith("AUTH"):
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif head.startswith("DATA"):
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                while True:
                    line = self.rfile.readline()
                    if not line or line.strip() == b".":
                        break
                self.wfile.write(b"250 OK queued\r\n")
            elif head.startswith("QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class _SMTPSink:
    def __init__(self) -> None:
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _rel(path: Path) -> str:
    """把路径写成相对项目根的形式 —— 命令在项目根执行，截图里读起来干净。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _copy_inbox(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for src in FIXTURE_INBOX.iterdir():
        if src.is_file() and src.suffix in {".hl7", ".json"}:
            shutil.copy2(src, dest / src.name)
    return dest


def _run(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    """跑一条命令并抓下 stdout + stderr 原文。"""
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def _shot(lines: list[str], name: str, title: str) -> Path:
    path = SHOTS_DIR / name
    render(lines, path, title=title)
    print(f"  ✔ {path.relative_to(PROJECT_ROOT)}")
    return path


def main() -> int:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    # 工作目录放在项目内 data/ 下（已被 .gitignore 排除）：命令更短、截图里
    # 读起来干净，发布前清理时一并删掉。
    work = PROJECT_ROOT / "data" / "capture"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    baseline = {
        "reports": [
            {
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
            {
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
        ]
    }

    stub = LocalPathologyStub(baseline)
    smtp = _SMTPSink()
    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": stub.base_url,
            "PATHOLOGY_API_TOKEN": "capture-token-local-only",
            "PATHOLOGY_RESULT_INBOX": _rel(work / "daemon-inbox"),
            "PATHOLOGY_STATE_DB": _rel(work / "state.db"),
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(smtp.port),
            "SMTP_STARTTLS": "false",
            "SMTP_USE_SSL": "false",
            "SMTP_USER": "capture@example.invalid",
            "SMTP_PASSWORD": "capture-local-only",
            "SMTP_FROM": "patho-system@example.invalid",
            "NOTIFY_RECIPIENTS": "pathologist-day@example.invalid",
            "LOG_LEVEL": "INFO",
        }
    )

    daemon_inbox = _copy_inbox(work / "daemon-inbox")
    replay_inbox = _copy_inbox(work / "replay-inbox")
    env["PATHOLOGY_RESULT_INBOX"] = _rel(daemon_inbox)

    py = sys.executable
    print("采集真实运行截图：")

    # ① doctor：凭据位 + 对外接口清单 + 出站连通性
    cmd = [py, "-m", "patho_supplement.cli", "doctor", "--accession", "ACC-EXAMPLE-0001"]
    code, out = _run(cmd, env)
    _shot(
        ["$ " + " ".join(cmd[1:]), f"[退出码 {code}]", "", *out.rstrip("\n").split("\n")],
        "02-doctor.png",
        "pathology-supplement — cli doctor",
    )

    # ② daemon --once：常驻形态跑一轮（取基线 → 对齐 → 成稿 → 回写 → 通知）
    cmd = [
        py,
        "-m",
        "patho_supplement.cli",
        "daemon",
        "--once",
        "--inbox",
        _rel(daemon_inbox),
        "--state-db",
        _rel(work / "state.db"),
        "--out",
        _rel(work / "daemon"),
    ]
    code, out = _run(cmd, env)
    _shot(
        ["$ " + " ".join(cmd[1:]), f"[退出码 {code}]", "", *out.rstrip("\n").split("\n")],
        "01-daemon-session.png",
        "pathology-supplement — cli daemon --once",
    )

    # ③ pending list：谁没对上、缺哪个字段、候选是谁
    cmd = [
        py,
        "-m",
        "patho_supplement.cli",
        "pending",
        "list",
        "--state-db",
        _rel(work / "state.db"),
    ]
    code, out = _run(cmd, env)
    _shot(
        ["$ " + " ".join(cmd[1:]), f"[退出码 {code}]", "", *out.rstrip("\n").split("\n")],
        "04-pending-list.png",
        "pathology-supplement — cli pending list",
    )

    # ④ pending export：导出可带走的清单（CSV + 文本）
    cmd = [
        py,
        "-m",
        "patho_supplement.cli",
        "pending",
        "export",
        "--state-db",
        _rel(work / "state.db"),
        "--out",
        _rel(work / "pending-export"),
    ]
    code, out = _run(cmd, env)
    export_txt = work / "pending-export" / "pending_list.txt"
    _shot(
        ["$ " + " ".join(cmd[1:]), f"[退出码 {code}]", "", *out.rstrip("\n").split("\n")],
        "05-pending-export-cli.png",
        "pathology-supplement — cli pending export",
    )
    if export_txt.is_file():
        body = export_txt.read_text(encoding="utf-8").rstrip("\n").split("\n")
        _shot(
            ["$ cat " + str(export_txt).replace(str(PROJECT_ROOT) + "/", ""), "", *body],
            "06-pending-list-file.png",
            "导出的待人工处理清单（文本件片段）",
        )

    # ⑤ 成稿文件片段：回写进病理系统的那份草稿正文
    drafts = sorted((work / "daemon" / "drafts").glob("*.txt"))
    if drafts:
        body = drafts[0].read_text(encoding="utf-8").rstrip("\n").split("\n")
        _shot(
            ["$ cat " + str(drafts[0]).replace(str(PROJECT_ROOT) + "/", ""), "", *body],
            "07-draft-file.png",
            "回写进病理系统的补充报告草稿（正文片段）",
        )

    # ⑥ replay：批处理重放
    cmd = [
        py,
        "-m",
        "patho_supplement.cli",
        "replay",
        "--inbox",
        _rel(replay_inbox),
        "--out",
        _rel(work / "replay"),
    ]
    code, out = _run(cmd, env)
    _shot(
        ["$ " + " ".join(cmd[1:]), f"[退出码 {code}]", "", *out.rstrip("\n").split("\n")],
        "03-replay.png",
        "pathology-supplement — cli replay",
    )

    # ⑦ 出站请求实录：真的发出去的 GET / POST
    lines = ["# 本地病理系统替身收到的出站请求（真实调用，非示意图）", ""]
    for method, path, body in RECEIVED:
        lines.append(f"{method} {path}")
        if body:
            import json

            payload = json.loads(body.decode("utf-8"))
            lines.append(f"  entries={len(payload.get('entries', []))} "
                         f"base_version={payload.get('base_version')}")
            lines.append(f"  body 首行：{payload.get('body', '').splitlines()[0][:60]}")
    _shot(lines, "08-outbound-requests.png", "出站请求实录（本地替身侧看到的调用）")

    stub.close()
    smtp.close()
    shutil.rmtree(work, ignore_errors=True)
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
