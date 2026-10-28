"""挂起清单导出（可带走的待人工处理清单）。

这条线保的是三件事：

- **带得走** —— CSV 能进 Excel 排序筛选，文本能直接打印；两个文件都要有表头
  与统计摘要，技术员拿到手就知道「哪类原因最多、几条已经要转人工」。
- **不含患者可识别明细** —— 清单是流转件，带走的件不该再带出患者身份。
- **对得上号** —— 导出的行数 = 挂起行数，每行都带行 ID、病理号、缺失字段、
  原因、候选、尝试次数与快照时间，技术员照着就能补齐后直接续跑。
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from patho_supplement.db import Database
from patho_supplement.models import Block, ResultEntry, ResultStatus
from patho_supplement.pending.export import (
    EXPORT_COLUMNS,
    build_export_rows,
    export_pending,
    render_pending_csv,
    render_pending_report,
)
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import PendingRecord
from patho_supplement.pending.store import PendingStore
from patho_supplement.privacy import desensitize_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- 样例


def _entry(
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-KI67",
    *,
    value: str | None = "阳性",
    block_no: str | None = None,
    region_seq: int | None = None,
    region_desc: str | None = "宫颈部",
    source_file: str | None = "oru-ki67.hl7",
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="KI-67 免疫组化",
        value=value,
        status=ResultStatus.FINAL,
        observed_at="20260115103000",
        order_no="ORD-EXAMPLE-001",
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file=source_file,
    )


def _record(
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-KI67",
    *,
    reason: PendingReason = PendingReason.MISSING_BLOCK_NO,
    missing_fields: tuple[str, ...] = ("block_no",),
    candidates: tuple[Block, ...] = (
        Block(region_seq=1, region_desc="子宫体内侧", block_no="B1"),
        Block(region_seq=2, region_desc="宫颈部", block_no="B2"),
    ),
    value: str | None = "阳性",
    region_desc: str | None = "宫颈部",
    pending_id: int | None = 7,
    escalated: bool = False,
    filled_fields: tuple[str, ...] = (),
    note: str | None = None,
) -> PendingRecord:
    return PendingRecord(
        accession_no=accession_no,
        item_code=item_code,
        reason=reason,
        missing_fields=missing_fields,
        candidates=candidates,
        source_file="oru-ki67.hl7",
        source_system="ihc-lab",
        observed_at="20260115103000",
        value=value,
        attempt=1,
        snapshot_at="2026-01-15 10:35:00",
        escalated=escalated,
        note=note,
        entry=_entry(
            accession_no, item_code, value=value, region_desc=region_desc
        ),
        pending_id=pending_id,
        filled_fields=filled_fields,
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


def _seed(db, count: int = 2) -> None:
    """往状态库里落几条挂起项。"""
    store = PendingStore(db)
    store.save(
        PendingRecord(
            accession_no="ACC-EXAMPLE-0001",
            item_code="IHC-KI67",
            reason=PendingReason.MISSING_BLOCK_NO,
            missing_fields=("block_no",),
            candidates=(Block(region_seq=1, region_desc="子宫体内侧", block_no="B1"),),
            source_file="oru-ki67.hl7",
            source_system="ihc-lab",
            observed_at="20260115103000",
            value="阳性",
            entry=_entry(),
        ),
        max_attempts=3,
    )
    store.save(
        PendingRecord(
            accession_no="ACC-EXAMPLE-0002",
            item_code="IHC-CD20",
            reason=PendingReason.AMBIGUOUS,
            missing_fields=("block_no", "region_desc"),
            candidates=(
                Block(region_seq=3, region_desc="乳腺组织", block_no="B1"),
                Block(region_seq=4, region_desc="乳腺组织", block_no="B2"),
            ),
            source_file="oru-cd20.hl7",
            source_system="ihc-lab",
            observed_at="20260115104000",
            value="阴性",
            entry=_entry("ACC-EXAMPLE-0002", "IHC-CD20", value="阴性"),
        ),
        max_attempts=3,
    )


# --------------------------------------------------------------- 脱敏


def test_desensitize_masks_identifier_shapes():
    """形态明确的标识符直接遮蔽，并留占位符（让人知道这里原本有东西）。"""
    masked = desensitize_text(
        "身份证 110101199003074615，手机 13812345678，座机 010-88886666，"
        "邮箱 case@example.org"
    )
    assert "110101199003074615" not in masked
    assert "13812345678" not in masked
    assert "010-88886666" not in masked
    assert "case@example.org" not in masked
    assert "已隐去" in masked


def test_desensitize_masks_names_with_cue():
    """带提示词的姓名要遮。"""
    masked = desensitize_text("患者张三，男，50岁")
    assert "张三" not in masked
    assert "【姓名已隐去】" in masked


def test_desensitize_keeps_business_terms():
    """业务词不能被当成姓名抹掉 —— 抹了部位描述，清单就没用了。"""
    for term in ("子宫体内侧", "宫颈部", "乳腺组织", "白细胞浸润", "石蜡块", "苏木染色"):
        assert desensitize_text(term) == term


def test_desensitize_keeps_title_not_name():
    """带职称的称呼不算姓名，不遮。"""
    assert desensitize_text("请王主任确认") == "请王主任确认"


# --------------------------------------------------------------- CSV


def test_csv_header_matches_columns():
    """CSV 首行是表头，列序与 EXPORT_COLUMNS 一致。"""
    text = render_pending_csv([_record()])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(EXPORT_COLUMNS)


def test_csv_row_count_equals_pending_count():
    """导出行数 = 挂起行数（表头不算数据行）。"""
    records = [
        _record("ACC-EXAMPLE-0001", "IHC-KI67", pending_id=1),
        _record("ACC-EXAMPLE-0002", "IHC-CD20", pending_id=2),
        _record("ACC-EXAMPLE-0003", "IHC-P53", pending_id=3),
    ]
    rows = list(csv.reader(io.StringIO(render_pending_csv(records))))
    assert len(rows) - 1 == len(records)


def test_csv_carries_the_columns_a_technician_needs():
    """每行带行 ID、病理号、缺失字段、原因、候选、尝试次数、快照时间。"""
    rows = list(csv.reader(io.StringIO(render_pending_csv([_record()]))))
    row = dict(zip(rows[0], rows[1]))
    assert row["行ID"] == "7"
    assert row["病理号"] == "ACC-EXAMPLE-0001"
    assert row["检查项目码"] == "IHC-KI67"
    assert row["挂起原因"] == "缺蜡块号"
    assert row["需补齐字段"] == "蜡块号"
    assert row["候选部位"] == "子宫体内侧/蜡块 B1；宫颈部/蜡块 B2"
    assert row["尝试次数"] == "1"
    assert row["快照时间"] == "2026-01-15 10:35:00"
    assert row["状态"] == "等人补齐"
    assert row["补齐指引"]


def test_csv_state_marks_filled_and_escalated():
    """补过仍挂起与转人工要标出来 —— 两种都要人处理，但处理方式不同。"""
    records = [
        _record(filled_fields=("block_no",)),
        _record(escalated=True, note="已尝试 4 次，超过挂起尝试上限 3 次，转人工处置"),
    ]
    rows = list(csv.reader(io.StringIO(render_pending_csv(records))))
    assert rows[1][EXPORT_COLUMNS.index("状态")] == "补过仍挂起"
    assert rows[1][EXPORT_COLUMNS.index("已补字段")] == "蜡块号"
    assert rows[2][EXPORT_COLUMNS.index("状态")] == "转人工"


def test_export_has_no_patient_identifiers():
    """导出内容不含姓名 / 身份证 / 联系方式等患者可识别明细。"""
    leaky = _record(
        value="阳性（患者张三，身份证 110101199003074615，手机 13812345678）",
        region_desc="宫颈部（李四取材）",
        candidates=(
            Block(region_seq=2, region_desc="宫颈部（李四取材）", block_no="B2"),
        ),
    )
    csv_text = render_pending_csv([leaky])
    report_text = render_pending_report([leaky])
    for text in (csv_text, report_text):
        assert "110101199003074615" not in text
        assert "13812345678" not in text
        assert "张三" not in text
        assert "李四" not in text
        assert "已隐去" in text


def test_export_of_clean_record_keeps_business_content():
    """干净的记录不该被脱敏误伤 —— 部位与结果值要原样留下。"""
    text = render_pending_csv([_record()])
    assert "宫颈部" in text
    assert "阳性" in text


# --------------------------------------------------------------- 文本清单


def test_report_has_header_and_summary():
    """文本清单带表头与统计摘要。"""
    text = render_pending_report(
        [
            _record("ACC-EXAMPLE-0001", "IHC-KI67", pending_id=1),
            _record("ACC-EXAMPLE-0002", "IHC-CD20", pending_id=2),
            _record(
                "ACC-EXAMPLE-0002",
                "IHC-P53",
                pending_id=3,
                reason=PendingReason.AMBIGUOUS,
            ),
        ],
        generated_at="2026-01-15 11:00:00",
    )
    assert "待人工处理清单（导出）" in text
    assert "生成时间：2026-01-15 11:00:00" in text
    assert "条目数：3 条" in text
    assert "涉及病理号：2 个" in text
    assert "—— 原因分布 ——" in text
    assert "缺蜡块号：2 条" in text
    assert "同分多候选：1 条" in text
    assert "—— 待补字段分布 ——" in text
    assert "—— 明细 ——" in text
    # 逐条明细：谁没对上、缺哪个字段、候选是谁
    assert "病理号 ACC-EXAMPLE-0001" in text
    assert "候选部位：子宫体内侧/蜡块 B1；宫颈部/蜡块 B2" in text


def test_report_counts_escalated_rows():
    """超了尝试上限的条目要在摘要里单列出来。"""
    text = render_pending_report([_record(escalated=True)])
    assert "需转人工处置：1 条" in text


def test_report_of_empty_list_says_so():
    """没有待处理项时，清单也要说得清楚，不能是一张空表。"""
    text = render_pending_report([])
    assert "条目数：0 条" in text
    assert "（本批没有需要人工处理的结果）" in text


# --------------------------------------------------------------- 落盘


def test_export_pending_writes_csv_and_text(tmp_path):
    """导出落两个文件：CSV 与文本。"""
    export = export_pending([_record()], tmp_path / "out")
    assert export.csv_path.exists()
    assert export.text_path.exists()
    assert export.row_count == 1
    assert export.csv_path.name.endswith(".csv")
    assert export.text_path.name.endswith(".txt")
    # CSV 带 BOM：Excel 打开中文不乱码
    assert export.csv_path.read_text(encoding="utf-8-sig").startswith("行ID")
    assert "待人工处理清单（导出）" in export.text_path.read_text(encoding="utf-8")


def test_export_pending_creates_output_dir(tmp_path):
    """导出目录不存在就建出来。"""
    target = tmp_path / "nested" / "out"
    export = export_pending([], target)
    assert target.is_dir()
    assert export.csv_path.exists()


def test_export_row_count_equals_store_count(db, tmp_path):
    """从状态库导出的行数 = 挂起行数（库里几条就导出几条）。"""
    _seed(db)
    store = PendingStore(db)
    loaded = store.load_all(states=("pending", "escalated"), strict=False)
    records = [snapshot.to_record() for snapshot in loaded.snapshots]
    export = export_pending(records, tmp_path / "out")
    assert export.row_count == len(loaded.snapshots) == 2
    rows = list(csv.reader(io.StringIO(export.csv_path.read_text(encoding="utf-8-sig"))))
    assert len(rows) - 1 == 2
    # 每行都带行 ID，技术员照着就能 pending resolve --id
    assert all(row[0].isdigit() for row in rows[1:])


def test_export_rows_carry_pending_id(db):
    """快照转记录时带上行 ID。"""
    _seed(db)
    store = PendingStore(db)
    loaded = store.load_all(states=("pending", "escalated"), strict=False)
    rows = build_export_rows([s.to_record() for s in loaded.snapshots])
    assert [row.row_id for row in rows] == [
        str(s.snapshot_id) for s in loaded.snapshots
    ]


# --------------------------------------------------------------- 命令行


def _run_cli(args: list[str], env_overrides: dict[str, str] | None = None):
    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(PROJECT_ROOT / "tests" / "fixtures" / "inbox"),
        }
    )
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_pending_export_writes_both_files(db, tmp_path):
    """cli pending export 产出 CSV 与文本两种文件。"""
    _seed(db)
    out_dir = tmp_path / "export"
    result = _run_cli(
        [
            "pending",
            "export",
            "--state-db",
            str(db.path),
            "--out",
            str(out_dir),
        ]
    )
    assert result.returncode == 0, result.stderr
    csv_path = out_dir / "pending_list.csv"
    text_path = out_dir / "pending_list.txt"
    assert csv_path.exists()
    assert text_path.exists()
    assert "条目数：2 条" in result.stdout
    assert "缺蜡块号 1 条" in result.stdout
    rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
    assert len(rows) - 1 == 2


def test_cli_pending_export_filters_by_accession(db, tmp_path):
    """只导某一个病理号时，其余病理号的一条都不进导出。"""
    _seed(db)
    out_dir = tmp_path / "export"
    result = _run_cli(
        [
            "pending",
            "export",
            "--state-db",
            str(db.path),
            "--out",
            str(out_dir),
            "--accession",
            "ACC-EXAMPLE-0001",
        ]
    )
    assert result.returncode == 0, result.stderr
    rows = list(csv.reader(io.StringIO((out_dir / "pending_list.csv").read_text(encoding="utf-8-sig"))))
    assert len(rows) - 1 == 1
    assert rows[1][EXPORT_COLUMNS.index("病理号")] == "ACC-EXAMPLE-0001"
    assert "条目数：1 条" in result.stdout


def test_cli_pending_export_refuses_without_state_db(tmp_path):
    """状态库不存在 → 明确报错并以非 0 退出。"""
    result = _run_cli(
        [
            "pending",
            "export",
            "--state-db",
            str(tmp_path / "missing.db"),
            "--out",
            str(tmp_path / "export"),
        ]
    )
    assert result.returncode == 1
    assert "状态库不存在" in result.stderr


def test_cli_pending_export_help_lists_formats():
    """--help 说清导出会出哪两种文件。"""
    result = _run_cli(["pending", "export", "--help"])
    assert result.returncode == 0
    assert "CSV" in result.stdout
    assert "文本" in result.stdout
