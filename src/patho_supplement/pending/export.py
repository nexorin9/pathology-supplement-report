"""挂起清单导出：带得走的「待人工处理清单」。

清单的用途是**流转**：技术员拿在手里去查蜡块登记本，回来照着补。所以它得是
两样东西：

- **CSV** —— 能进 Excel 排序筛选。一份清单几十行，要按原因、按病理号分组挨个
  处理；CSV 用 ``utf-8-sig`` 落盘（带 BOM），Excel 打开不乱码。
- **人类可读文本** —— 直接打印贴在工位上。带表头、带统计摘要（哪类原因最多、
  几条已经超了尝试上限要转人工）。

导出内容**不含患者可识别明细**（姓名 / 身份证 / 联系方式等）：清单会被带走，
带走的件不该再带出患者身份。自由文本（结果值、部位描述、来源文件名、说明）
一律先过 :mod:`patho_supplement.privacy` 的脱敏。

导出的每行都带**行 ID**：技术员补齐后可以直接 ``pending resolve --id <行ID>``
从挂起点续跑，不用回终端里翻行号。
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from ..models import ResultEntry
from ..privacy import desensitize_text
from ..timefmt import format_observed_at, now_text
from ..compose.conflict import ConflictRecord, render_conflict_entries
from .fills import FIELD_LABELS
from .records import PendingRecord, render_pending_entries

__all__ = [
    "EXPORT_COLUMNS",
    "PendingExport",
    "PendingExportRow",
    "build_export_rows",
    "build_conflict_rows",
    "export_pending",
    "render_pending_csv",
    "render_pending_report",
]

#: CSV 表头（顺序即列序）。列名说人话 —— 导出的表是给技术员看的，不是给程序看的。
EXPORT_COLUMNS: tuple[str, ...] = (
    "行ID",
    "病理号",
    "检查项目码",
    "挂起原因",
    "需补齐字段",
    "已补字段",
    "候选部位",
    "尝试次数",
    "快照时间",
    "回报时间",
    "来源系统",
    "状态",
    "结果值",
    "补齐指引",
)


@dataclass(frozen=True)
class PendingExportRow:
    """导出的一行（字段都已是给人看的形式）。"""

    row_id: str
    accession_no: str
    item_code: str
    reason: str
    missing_fields: str
    filled_fields: str
    candidates: str
    attempts: str
    snapshot_at: str
    observed_at: str
    source_system: str
    state: str
    value: str
    hint: str

    def as_row(self) -> tuple[str, ...]:
        """按 :data:`EXPORT_COLUMNS` 的列序取值。"""
        return (
            self.row_id,
            self.accession_no,
            self.item_code,
            self.reason,
            self.missing_fields,
            self.filled_fields,
            self.candidates,
            self.attempts,
            self.snapshot_at,
            self.observed_at,
            self.source_system,
            self.state,
            self.value,
            self.hint,
        )


@dataclass(frozen=True)
class PendingExport:
    """一次导出的结果（落了两个文件）。"""

    csv_path: Path
    text_path: Path
    row_count: int
    reason_counts: tuple[tuple[str, int], ...] = ()
    #: 随清单一起导出的版本冲突条数（冲突也要人处理，单独一行行落在 CSV 里）。
    conflict_count: int = 0

    @property
    def paths(self) -> tuple[Path, Path]:
        return (self.csv_path, self.text_path)


# ---------------------------------------------------------------- 脱敏


def _scrub_entry(entry: ResultEntry | None) -> ResultEntry | None:
    """结果条目里的自由文本过一遍脱敏（原条目不动，返回新条目）。"""
    if entry is None:
        return None
    data = entry.to_dict()
    for key in ("value", "region_desc", "item_name", "source_file"):
        if data.get(key):
            data[key] = desensitize_text(str(data[key]))
    return ResultEntry.from_dict(data)


def _scrub_record(record: PendingRecord) -> PendingRecord:
    """把一条挂起记录里的自由文本过一遍脱敏（返回副本，原记录不动）。"""
    candidates = tuple(
        replace(block, region_desc=desensitize_text(block.region_desc))
        for block in record.candidates
    )
    return replace(
        record,
        entry=_scrub_entry(record.entry),
        candidates=candidates,
        value=desensitize_text(record.value),
        source_file=desensitize_text(record.source_file),
        note=desensitize_text(record.note),
    )


def _scrub_records(records: Sequence[PendingRecord]) -> tuple[PendingRecord, ...]:
    return tuple(_scrub_record(record) for record in records)


# ---------------------------------------------------------------- 行


def _state_label(record: PendingRecord) -> str:
    """这条挂起项现在什么状态（与技术员看到的说法一致）。"""
    if record.escalated:
        return "转人工"
    if record.filled_fields:
        return "补过仍挂起"
    return "等人补齐"


def _field_label(name: str) -> str:
    """字段名 → 中文标签。"""
    return FIELD_LABELS.get(name, name)


def build_export_rows(records: Sequence[PendingRecord]) -> tuple[PendingExportRow, ...]:
    """把挂起记录整成导出行（自由文本已脱敏）。"""
    rows: list[PendingExportRow] = []
    for record in _scrub_records(records):
        rows.append(
            PendingExportRow(
                row_id=str(record.pending_id) if record.pending_id is not None else "",
                accession_no=record.accession_no,
                item_code=record.item_code,
                reason=record.reason.label,
                missing_fields="、".join(
                    _field_label(name) for name in record.missing_fields
                )
                or "无",
                filled_fields="、".join(
                    _field_label(name) for name in record.filled_fields
                ),
                candidates=record.candidate_text(),
                attempts=str(record.attempt),
                snapshot_at=record.snapshot_at or "",
                observed_at=format_observed_at(record.observed_at),
                source_system=record.source_system or "未知",
                state=_state_label(record),
                value=desensitize_text(record.value),
                hint=record.hint,
            )
        )
    return tuple(rows)


def build_conflict_rows(
    conflicts: Sequence[ConflictRecord],
) -> tuple[PendingExportRow, ...]:
    """把版本冲突整成导出行。

    冲突是**整份草稿**写不回去（不是某一条结果对不上），所以「检查项目码」一列
    写明是哪一版草稿；「状态」一列写「版本冲突，转人工」—— 技术员一眼能分出
    这行跟「等人补齐」不是一回事。
    """
    rows: list[PendingExportRow] = []
    for conflict in conflicts:
        version_text = conflict.version_text
        rows.append(
            PendingExportRow(
                row_id=f"冲突#{conflict.id}",
                accession_no=conflict.accession_no,
                item_code=f"整份草稿 {version_text}",
                reason=conflict.label,
                missing_fields="基础版本号",
                filled_fields="",
                candidates="",
                attempts="1",
                snapshot_at=conflict.detected_at or "",
                observed_at="",
                source_system="病理信息系统",
                state="版本冲突，转人工",
                value="",
                hint=conflict.hint,
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------- 渲染


def render_pending_csv(
    records: Sequence[PendingRecord], *, conflicts: Sequence[ConflictRecord] = ()
) -> str:
    """渲染 CSV 文本（首行表头）。

    冲突行跟在挂起行后面 —— 一份清单一个文件，技术员不用分头找两处。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for row in build_export_rows(records):
        writer.writerow(row.as_row())
    for row in build_conflict_rows(conflicts):
        writer.writerow(row.as_row())
    return buffer.getvalue()


def _reason_counts(records: Sequence[PendingRecord]) -> Counter[str]:
    return Counter(record.reason.label for record in records)


def _field_counts(records: Sequence[PendingRecord]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for record in records:
        for name in record.missing_fields:
            counter[_field_label(name)] += 1
    return counter


def render_pending_report(
    records: Sequence[PendingRecord],
    *,
    conflicts: Sequence[ConflictRecord] = (),
    generated_at: str | None = None,
) -> str:
    """渲染人类可读文本清单（表头 + 统计摘要 + 逐条明细）。"""
    scrubbed = _scrub_records(records)
    stamp = generated_at or now_text()
    reason_counts = _reason_counts(scrubbed)
    field_counts = _field_counts(scrubbed)

    lines: list[str] = [
        "待人工处理清单（导出）",
        f"生成时间：{stamp}",
        f"条目数：{len(scrubbed)} 条",
        f"涉及病理号：{len({record.accession_no for record in scrubbed})} 个",
        f"需转人工处置：{sum(1 for r in scrubbed if r.escalated)} 条",
        f"补过仍挂起：{sum(1 for r in scrubbed if r.filled_fields and not r.escalated)} 条",
    ]
    if conflicts:
        lines.append(f"版本冲突待处置：{len(conflicts)} 条")
    lines.extend(["", "—— 原因分布 ——"])
    if reason_counts:
        lines.extend(
            f"{label}：{count} 条" for label, count in reason_counts.most_common()
        )
    else:
        lines.append("（无）")
    lines.extend(["", "—— 待补字段分布 ——"])
    if field_counts:
        lines.extend(
            f"{label}：{count} 条" for label, count in field_counts.most_common()
        )
    else:
        lines.append("（无）")
    lines.extend(["", "—— 明细 ——", ""])
    if not scrubbed:
        lines.append("（本批没有需要人工处理的结果）")
    else:
        lines.extend(render_pending_entries(scrubbed))
    if conflicts:
        lines.extend(["", "—— 版本冲突待处置 ——", ""])
        lines.extend(render_conflict_entries(conflicts))

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 落盘


def export_pending(
    records: Sequence[PendingRecord],
    out_dir: Path | str,
    *,
    conflicts: Sequence[ConflictRecord] = (),
    generated_at: str | None = None,
    stem: str = "pending_list",
) -> PendingExport:
    """把清单导出成两个文件：CSV（可排序筛选）+ 文本（可打印）。

    CSV 用 ``utf-8-sig`` 落盘：Excel 打开中文不乱码 —— 清单是给技术员看的，
    乱码的表没人看第二眼。

    ``conflicts`` 是版本冲突待处置的条目：同样写进两个文件（CSV 里单独一行，
    文本里单独一段），一份清单带走所有要人处理的事。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{stem}.csv"
    text_path = directory / f"{stem}.txt"
    csv_path.write_text(
        render_pending_csv(records, conflicts=conflicts), encoding="utf-8-sig"
    )
    text_path.write_text(
        render_pending_report(records, conflicts=conflicts, generated_at=generated_at),
        encoding="utf-8",
    )
    counts = _reason_counts(_scrub_records(records))
    return PendingExport(
        csv_path=csv_path,
        text_path=text_path,
        row_count=len(records),
        reason_counts=tuple(counts.most_common()),
        conflict_count=len(conflicts),
    )
