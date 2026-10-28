"""审计流水 + 按病理号导出。

审计流水是「这份补充报告当初为什么这么归属」的凭证 —— 签发之前它就得在，
签发之后它得能被带走。这条线保四件事：

- **行数口径说得清** —— 对齐依据行数 = 并入草稿的条目数、草稿份数 = 写回回执
  数、状态迁移次数 = 被接受的迁移数；重复投递与畸形拒收**各自单独计数**，
  不混进对齐依据行数（混进去「对上了几条」就失真了）。
- **按病理号导得出** —— 一次导一份报告的事，行序 = 事情发生的顺序。
- **带得走** —— CSV 能进 Excel 排序筛选，文本能直接打印，件头写明口径。
- **不带患者可识别明细** —— 姓名 / 身份证 / 联系方式在导出前遮蔽。
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.align.rationale import record_alignment_audit
from patho_supplement.audit.export import (
    AUDIT_EXPORT_COLUMNS,
    build_audit_rows,
    export_audit,
    render_audit_csv,
    render_audit_report,
)
from patho_supplement.audit.ledger import (
    CATEGORY_ALIGN,
    CATEGORY_INGEST,
    CATEGORY_NOTIFY,
    CATEGORY_STATE,
    CATEGORY_WRITEBACK,
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
    EVENT_DUPLICATE,
    EVENT_MALFORMED,
    EVENT_NOTIFY_SENT,
    EVENT_STATE_TRANSITION,
    EVENT_STATE_TRANSITION_REJECTED,
    EVENT_WRITEBACK_ACCEPTED,
    AuditCounts,
    audit_counts,
    count_audit_rows,
    read_audit_rows,
    record_audit,
)
from patho_supplement.compose.state_machine import (
    CheckItemState,
    DraftState,
    register_check_item,
    transition_check_item_state,
    transition_draft_state,
)
from patho_supplement.config import Config
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.idempotency import IdempotencyLedger
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.models import Alignment, MatchLevel, ResultEntry, ResultStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

ACCESSION = "ACC-EXAMPLE-0001"
OTHER_ACCESSION = "ACC-EXAMPLE-0002"


# --------------------------------------------------------------- 夹具


@pytest.fixture()
def db(tmp_path: Path):
    with Database(tmp_path / "state.db") as database:
        yield database


def _alignment(
    accession_no: str = ACCESSION,
    *,
    item_code: str = "IHC-CD3",
    block_no: str = "B1",
    pending: bool = False,
) -> Alignment:
    """一条对齐依据（已并入草稿，或没对上挂起）。"""
    if pending:
        return Alignment(
            accession_no=accession_no,
            item_code=item_code,
            match_level=MatchLevel.L4_PENDING,
            score=0.0,
            recall_key=None,
            block_no=block_no,
            region_seq=1,
            region_desc="子宫体内侧",
            source_system="免疫组化室",
            observed_at="20260115103000",
            pending_reason="回报没带蜡块号",
            missing_fields=("block_no",),
        )
    return Alignment(
        accession_no=accession_no,
        item_code=item_code,
        match_level=MatchLevel.L1_BLOCK_EXACT,
        score=1.0,
        recall_key=f"block|{block_no}|{item_code}",
        block_no=block_no,
        region_seq=1,
        region_desc="子宫体内侧",
        source_system="免疫组化室",
        observed_at="20260115103000",
    )


def _entry(accession_no: str = ACCESSION, item_code: str = "IHC-CD3") -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="CD3 免疫组化",
        value="阳性",
        status=ResultStatus.FINAL,
        observed_at="20260115103000",
        order_no="ORD-EXAMPLE-001",
        block_no="B1",
        region_seq=1,
        region_desc="子宫体内侧",
        source_system="免疫组化室",
        source_file="oru-cd3.hl7",
    )


# --------------------------------------------------------------- 写入口径


def test_对齐依据行数等于并入草稿的条目数(db):
    """每并入一条结果留一行依据 —— 依据行数就是条目数，不是「大概齐」。"""
    for item_code in ("IHC-CD3", "IHC-CD20", "IHC-CK7"):
        record_alignment_audit(db, _alignment(item_code=item_code))

    counts = audit_counts(db)
    assert counts.rationale_rows == 3
    assert count_audit_rows(db, event=EVENT_ALIGNMENT_RATIONALE) == 3
    # 依据行归在「对齐」这一类下 —— 按分类筛也对得上
    assert count_audit_rows(db, category=CATEGORY_ALIGN) == 3


def test_挂起依据另计不混进对齐依据(db):
    """挂起的依据也留一行，但它不是「对上了几条」。"""
    record_alignment_audit(db, _alignment())
    record_alignment_audit(
        db, _alignment(item_code="IHC-KI67", pending=True), event=EVENT_ALIGNMENT_PENDING
    )

    counts = audit_counts(db)
    assert counts.rationale_rows == 1
    assert counts.pending_rows == 1


def test_草稿份数等于写回回执行数(db):
    """一份草稿写回成功 = 一行回执 —— 草稿份数就取这一类行。"""
    record_audit(
        db,
        category=CATEGORY_WRITEBACK,
        event=EVENT_WRITEBACK_ACCEPTED,
        accession_no=ACCESSION,
        detail={"status": "accepted", "version": "draft-v2", "draft_id": "SD-0001"},
    )

    counts = audit_counts(db)
    assert counts.draft_rows == 1


def test_状态迁移次数只算被接受的(db):
    """被拒的迁移另计 —— 迁移次数说的是「走了几次」，不是「试了几次」。"""
    db.execute(
        "INSERT INTO drafts(accession_no, version, base_version, state, entry_count,"
        " content_hash, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ACCESSION, 1, "base-v1", "generated", 1, "h1", "正文", "2026-01-15 10:30:00"),
    )
    draft_id = int(db.query_value("SELECT id FROM drafts WHERE accession_no = ?", (ACCESSION,)))

    transition_draft_state(db, draft_id, DraftState.WRITTEN_BACK, accession_no=ACCESSION)
    transition_draft_state(db, draft_id, DraftState.SIGNED, accession_no=ACCESSION)
    # 已签发的草稿不许退回「已生成」—— 非法迁移被拒并留一行痕迹
    with pytest.raises(Exception):
        transition_draft_state(db, draft_id, DraftState.GENERATED, accession_no=ACCESSION)

    counts = audit_counts(db)
    assert counts.state_transition_rows == 2
    assert counts.state_transition_rejected_rows == 1


def test_通知发送行数单独计(db):
    record_audit(
        db,
        category=CATEGORY_NOTIFY,
        event=EVENT_NOTIFY_SENT,
        accession_no=ACCESSION,
        detail={"channel": "smtp", "recipients": ["duty@example.invalid"]},
    )

    assert audit_counts(db).notify_rows == 1


def test_检查项状态迁移同样记审计行(db):
    """检查项的迁移也留痕 —— 状态迁移次数不只算草稿那一侧。"""
    transition_check_item_state(
        db,
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        order_no="ORD-EXAMPLE-001",
        block_no="B1",
        target=CheckItemState.RETURNED,
    )
    transition_check_item_state(
        db,
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        order_no="ORD-EXAMPLE-001",
        block_no="B1",
        target=CheckItemState.ALIGNED,
    )

    counts = audit_counts(db)
    assert counts.state_transition_rows == 2
    events = {
        row["event"]
        for row in db.query_all("SELECT event FROM audit_log WHERE category = ?", (CATEGORY_STATE,))
    }
    assert EVENT_STATE_TRANSITION in events
    assert EVENT_STATE_TRANSITION_REJECTED not in events
    assert register_check_item(
        db, accession_no=OTHER_ACCESSION, item_code="IHC-CD3"
    ) > 0  # 登记不是迁移，不该多出一行迁移审计


# --------------------------------------------------------------- 单独计数


def test_重复投递单独计数不混进对齐依据(db):
    """「又投了一遍」有自己的计数器 —— 混进对齐依据行数，口径就失真了。"""
    record_alignment_audit(db, _alignment())
    record_alignment_audit(db, _alignment(item_code="IHC-CD20"))
    for _ in range(3):
        record_audit(
            db,
            category=CATEGORY_INGEST,
            event=EVENT_DUPLICATE,
            accession_no=ACCESSION,
            detail={"item_code": "IHC-CD3", "block_no": "B1"},
        )

    counts = audit_counts(db)
    assert counts.rationale_rows == 2
    assert counts.duplicate_deliveries == 3
    assert counts.total_rows == 5


def test_畸形拒收单独计数不混进对齐依据(db):
    """报文畸形是批次级的事 —— 不算「对上了一条」，也不算「挂起了一条」。"""
    record_alignment_audit(db, _alignment())
    record_audit(
        db,
        category=CATEGORY_INGEST,
        event=EVENT_MALFORMED,
        accession_no=None,
        detail={"file": "bad.hl7", "code": "missing_msh9"},
    )

    counts = audit_counts(db)
    assert counts.rationale_rows == 1
    assert counts.pending_rows == 0
    assert counts.malformed_rejected == 1


def test_重复投递落审计行且不重复入账(tmp_path):
    """同一条结果投两次：库里只有一条，另一条单独记一行重复投递。"""
    with Database(tmp_path / "state.db") as db:
        ledger = IdempotencyLedger(db)
        entry = _entry()

        assert ledger.accept(entry, source_file="oru-cd3.hl7") is True
        assert ledger.accept(entry, source_file="oru-cd3.hl7") is False

        assert db.query_value("SELECT COUNT(*) FROM idempotency_keys") == 1
        rows = db.query_all(
            "SELECT event, accession_no, detail_json FROM audit_log ORDER BY id"
        )
        assert len(rows) == 1
        assert rows[0]["event"] == EVENT_DUPLICATE
        assert rows[0]["accession_no"] == ACCESSION
        detail = json.loads(rows[0]["detail_json"])
        assert detail["item_code"] == "IHC-CD3"
        assert detail["block_no"] == "B1"
        # 重复投递不带结果值 —— 审计行不该变成另一份病历副本
        assert "value" not in detail


def test_口径摘要可读且分清两类计数(db):
    record_alignment_audit(db, _alignment())
    record_audit(
        db, category=CATEGORY_INGEST, event=EVENT_DUPLICATE, accession_no=ACCESSION
    )

    summary = audit_counts(db).summary()
    assert "对齐依据 1" in summary
    assert "重复投递 1" in summary
    assert "畸形拒收 0" in summary


def test_按病理号统计只看这个病理号的行(db):
    record_alignment_audit(db, _alignment())
    record_alignment_audit(db, _alignment(accession_no=OTHER_ACCESSION))

    counts = audit_counts(db, accession_no=ACCESSION)
    assert counts.rationale_rows == 1
    assert counts.total_rows == 1


# --------------------------------------------------------------- 导出


def _seed(db) -> None:
    """造一份典型的一轮审计流水。"""
    record_alignment_audit(db, _alignment())
    record_alignment_audit(db, _alignment(item_code="IHC-CD20", block_no="B2"))
    record_alignment_audit(
        db,
        _alignment(item_code="IHC-KI67", pending=True),
        event=EVENT_ALIGNMENT_PENDING,
    )
    record_audit(
        db,
        category=CATEGORY_WRITEBACK,
        event=EVENT_WRITEBACK_ACCEPTED,
        accession_no=ACCESSION,
        detail={"status": "accepted", "version": "draft-v4", "draft_id": "SD-0001"},
    )
    record_audit(
        db,
        category=CATEGORY_INGEST,
        event=EVENT_DUPLICATE,
        accession_no=ACCESSION,
        detail={"item_code": "IHC-CD3", "block_no": "B1"},
    )
    record_alignment_audit(db, _alignment(accession_no=OTHER_ACCESSION))


def test_按病理号导出的行数与写入行数一致(db, tmp_path):
    """导出件的行数 = 库里这个病理号的行数 —— 一条都不能少。"""
    _seed(db)
    written = count_audit_rows(db, accession_no=ACCESSION)
    rows = read_audit_rows(db, accession_no=ACCESSION)

    export = export_audit(
        rows,
        tmp_path / "out",
        counts=audit_counts(db, accession_no=ACCESSION),
        accession_no=ACCESSION,
    )

    assert written == 5
    assert export.row_count == written
    assert export.counts.total_rows == written


def test_按病理号导出只含该病理号的行(db, tmp_path):
    """另一个病理号的行不该混进来 —— 导出是「一份报告一件事」。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)

    export = export_audit(
        rows,
        tmp_path / "out",
        counts=audit_counts(db, accession_no=ACCESSION),
        accession_no=ACCESSION,
    )

    text = export.text_path.read_text(encoding="utf-8")
    csv_text = export.csv_path.read_text(encoding="utf-8-sig")
    assert OTHER_ACCESSION not in text
    assert OTHER_ACCESSION not in csv_text


def test_导出顺序按行号升序(db, tmp_path):
    """行序 = 事情发生的顺序 —— 复核的人是从上往下读的。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export_rows = build_audit_rows(rows)

    assert [int(row.row_id) for row in export_rows] == sorted(
        int(row.row_id) for row in export_rows
    )


def test_csv带表头与列序且带BOM(db, tmp_path):
    """CSV 落 utf-8-sig：Excel 打开中文不乱码（清单是给人看的）。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export = export_audit(
        rows,
        tmp_path / "out",
        counts=audit_counts(db, accession_no=ACCESSION),
        accession_no=ACCESSION,
    )

    raw = export.csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")

    content = raw.decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(content)))
    assert tuple(parsed[0]) == AUDIT_EXPORT_COLUMNS
    assert len(parsed) == 1 + 5
    assert parsed[1][4] == ACCESSION


def test_文本件带件头口径摘要与明细(db, tmp_path):
    """件头写明口径 —— 看件的人不用回来问「这几行是怎么数的」。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export = export_audit(
        rows,
        tmp_path / "out",
        counts=audit_counts(db, accession_no=ACCESSION),
        accession_no=ACCESSION,
    )

    text = export.text_path.read_text(encoding="utf-8")
    assert "补充报告审计流水（导出）" in text
    assert f"范围：{ACCESSION}" in text
    assert "条目数：5 行" in text
    assert "对齐依据：2 行" in text
    assert "挂起依据：1 行" in text
    assert "草稿份数：1 份" in text
    assert "重复投递 1 条" in text
    assert "—— 明细 ——" in text
    assert "对齐依据（已并入草稿）" in text


def test_导出的说明列说得清依据(db, tmp_path):
    """「凭什么算这条」在导出件里也要看得见 —— 命中方式、置信、来源、回报时间。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export_rows = build_audit_rows(rows)
    text = "\n".join(row.detail_text for row in export_rows)

    assert "命中 L1 蜡块号精确" in text
    assert "置信 高" in text
    assert "来源 免疫组化室" in text
    assert "回报时间 2026-01-15 10:30" in text


def test_导出不含患者可识别明细(db, tmp_path):
    """带走的件不该再带出患者身份 —— 姓名 / 身份证 / 联系方式在出门前遮蔽。"""
    record_audit(
        db,
        category=CATEGORY_INGEST,
        event=EVENT_MALFORMED,
        accession_no=ACCESSION,
        detail={
            "file": "oru-bad.hl7",
            "reason": "患者张三 身份证 110101199001011234 联系方式 13812345678 回报异常",
        },
    )
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export = export_audit(
        rows,
        tmp_path / "out",
        counts=audit_counts(db, accession_no=ACCESSION),
        accession_no=ACCESSION,
    )

    text = export.text_path.read_text(encoding="utf-8")
    csv_text = export.csv_path.read_text(encoding="utf-8-sig")
    for payload in (text, csv_text):
        assert "张三" not in payload
        assert "110101199001011234" not in payload
        assert "13812345678" not in payload
    assert "【姓名已隐去】" in text
    assert "【身份证号已隐去】" in text
    assert "【电话已隐去】" in text


def test_导出件不带结果值(db, tmp_path):
    """结果值是病理内容 —— 审计行带走的只是「凭什么」，不是「判成了什么」。"""
    record_alignment_audit(db, _alignment())
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export_rows = build_audit_rows(rows)

    # 对齐依据明细里没有结果值这一项
    assert all("阳性" not in row.detail_text for row in export_rows)


def test_空范围导出给空件而不是报错(db, tmp_path):
    """这个病理号没有审计记录时，导出一份说清「没有」的件。"""
    export = export_audit(
        read_audit_rows(db, accession_no="ACC-NOT-EXIST"),
        tmp_path / "out",
        counts=audit_counts(db, accession_no="ACC-NOT-EXIST"),
        accession_no="ACC-NOT-EXIST",
    )

    assert export.row_count == 0
    text = export.text_path.read_text(encoding="utf-8")
    assert "条目数：0 行" in text
    assert "（这个范围内没有审计记录）" in text


def test_渲染函数可单独用(db):
    """渲染不依赖落盘 —— 上层要拼自己的件时用得上。"""
    _seed(db)
    rows = read_audit_rows(db, accession_no=ACCESSION)
    export_rows = build_audit_rows(rows)

    csv_text = render_audit_csv(export_rows)
    assert csv_text.splitlines()[0] == ",".join(AUDIT_EXPORT_COLUMNS)

    report = render_audit_report(
        export_rows,
        AuditCounts(total_rows=5, rationale_rows=2, draft_rows=1),
        accession_no=ACCESSION,
        generated_at="2026-01-15 10:30:00",
    )
    assert "生成时间：2026-01-15 10:30:00" in report


# --------------------------------------------------------------- 命令行


def _run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=environment,
    )


def test_cli_audit_export_产出两个文件并打印口径(db, tmp_path):
    _seed(db)
    result = _run_cli(
        "audit",
        "export",
        "--state-db",
        str(db.path),
        "--accession",
        ACCESSION,
        "--out",
        str(tmp_path / "out"),
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "audit_trail.csv").is_file()
    assert (tmp_path / "out" / "audit_trail.txt").is_file()
    assert "审计流水导出" in result.stdout
    assert f"范围：{ACCESSION}" in result.stdout
    assert "导出条数：5 行" in result.stdout
    assert "对齐依据 2" in result.stdout
    assert "重复投递 1" in result.stdout


def test_cli_audit_export_不给病理号导全部(db, tmp_path):
    _seed(db)
    result = _run_cli(
        "audit", "export", "--state-db", str(db.path), "--out", str(tmp_path / "out")
    )

    assert result.returncode == 0, result.stderr
    assert "范围：全部病理号" in result.stdout
    assert "导出条数：6 行" in result.stdout


def test_cli_状态库不存在退出码1(tmp_path):
    result = _run_cli(
        "audit",
        "export",
        "--state-db",
        str(tmp_path / "nope.db"),
        "--out",
        str(tmp_path / "out"),
    )

    assert result.returncode == 1
    assert "状态库不存在" in result.stderr


def test_cli_audit不带子命令退出码2():
    result = _run_cli("audit")

    assert result.returncode == 2
    assert "export" in result.stdout


def test_cli_help_列出audit子命令():
    result = _run_cli("--help")

    assert result.returncode == 0
    assert "audit" in result.stdout


# --------------------------------------------------------------- 真实链路


class _Handler(BaseHTTPRequestHandler):
    """本地协议替身（只在测试数据层）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002 - 静音标准库访问日志
        pass

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
        parts = self.path.strip("/").split("/")
        accession = parts[3] if len(parts) > 3 else ""
        payload = {
            "accession_no": accession,
            "base_version": "base-v3",
            "blocks": [
                {
                    "region_seq": 1,
                    "region_desc": "子宫体",
                    "block_no": "B1",
                    "order_no": "ORD-EXAMPLE-0001",
                    "ordered_items": ["IHC-CD3"],
                },
                {
                    "region_seq": 2,
                    "region_desc": "宫颈",
                    "block_no": "B2",
                    "order_no": "ORD-EXAMPLE-0002",
                    "ordered_items": ["IHC-CD20", "IHC-CK7"],
                },
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps(
            {"status": "accepted", "version": "draft-v2", "draft_id": "SD-0001"}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_pathology():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_守护跑一轮后导出的审计与产出对得上(fake_pathology, tmp_path):
    """真跑一轮：导出的依据行数 = 草稿里的条目数，回执行数 = 草稿份数。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7", inbox)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-03-ki67-no-block.hl7", inbox)

    config = Config.from_env(
        {
            "PATHOLOGY_BASE_URL": fake_pathology,
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(tmp_path / "state.db"),
        }
    )
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    daemon = Daemon(
        config,
        client=config_client(config),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda message: None,
    )
    outcome = daemon.run_once()
    daemon.close()

    with Database(tmp_path / "state.db") as db:
        counts = audit_counts(db, accession_no=ACCESSION)
        rows = read_audit_rows(db, accession_no=ACCESSION)
        merged_entries = int(
            db.query_value(
                "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0"
            )
            or 0
        )
        draft_count = int(db.query_value("SELECT COUNT(*) FROM drafts") or 0)

        export = export_audit(
            rows,
            tmp_path / "audit-out",
            counts=counts,
            accession_no=ACCESSION,
        )

    # 一条并入草稿的结果 = 一行对齐依据
    assert merged_entries == 1
    assert counts.rationale_rows == merged_entries
    # 挂起那条另有依据行
    assert counts.pending_rows == 1
    # 一份草稿写回成功 = 一行回执
    assert draft_count == 1
    assert counts.draft_rows == 1
    # 导出行数 = 库里这个病理号的行数
    assert export.row_count == counts.total_rows == len(rows)
    assert outcome.drafts_written == 1


def test_守护的重复投递单独计数与运行摘要对得上(fake_pathology, tmp_path):
    """运行摘要说「重复 1」，审计流水里就得有 1 条重复 —— 两处口径不能各说各话。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-03-ki67-no-block.hl7", inbox)
    shutil.copy(FIXTURE_INBOX / "acc-example-0001-04-ki67-duplicate.hl7", inbox)

    config = Config.from_env(
        {
            "PATHOLOGY_BASE_URL": fake_pathology,
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(tmp_path / "state.db"),
        }
    )
    watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
    daemon = Daemon(
        config,
        client=config_client(config),
        database=Database(tmp_path / "state.db"),
        watcher=watcher,
        output_dir=tmp_path / "out",
        echo=lambda message: None,
    )
    outcome = daemon.run_once()
    daemon.close()

    with Database(tmp_path / "state.db") as db:
        counts = audit_counts(db)
        duplicate_rows = db.query_all(
            "SELECT accession_no, detail_json FROM audit_log WHERE event = ?",
            (EVENT_DUPLICATE,),
        )

    assert outcome.duplicates == 1
    assert counts.duplicate_deliveries == 1
    assert len(duplicate_rows) == 1
    # 重复来路写进明细 —— 事后能分清是「本批重复」还是「此前已落定」
    assert json.loads(duplicate_rows[0]["detail_json"])["reason"] in (
        "本批重复投递",
        "此前已落定",
    )
    # 重复不混进对齐依据行数
    assert counts.rationale_rows == outcome.drafts_written


def config_client(config: Config):
    """按配置面造一个出站客户端（凭据从配置读，不硬编码）。"""
    from patho_supplement.compose.pathology_client import PathologyClient

    return PathologyClient.from_config(config)
