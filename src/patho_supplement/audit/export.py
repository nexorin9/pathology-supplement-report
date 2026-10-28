"""审计流水导出：按病理号把一份报告的来龙去脉导出来。

导出件是**要被人带走的** —— 医师复核、内审、飞检语境下，「这一份报告当初
为什么这么归属、谁写回的、通知给谁了」得当场答得上来。所以导出有三条硬规矩：

- **按病理号**：一次只导一份报告的事，行序就是事情发生的顺序（按行号升序）。
- **口径写在件里**：件头写明「对齐依据几行 = 并入草稿几条、草稿几份 =
  写回几份、状态迁移几次」，看件的人不用回来问口径是怎么定的。
- **不带患者可识别明细**：姓名 / 身份证 / 联系方式在导出前一律遮蔽；带走的
  件不该再带出患者身份。

两样产物：

- **CSV**（``utf-8-sig``，Excel 打开不乱码）—— 能排序、能筛选、能进台账；
- **人类可读文本** —— 能直接打印贴在病案柜上。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..privacy import desensitize_text
from ..timefmt import format_observed_at, now_text
from .ledger import (
    CATEGORY_ALIGN,
    CATEGORY_INGEST,
    CATEGORY_NOTIFY,
    CATEGORY_PENDING,
    CATEGORY_RUNTIME,
    CATEGORY_STATE,
    CATEGORY_WRITEBACK,
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
    EVENT_CORRECTION_APPLIED,
    EVENT_CORRECTION_INVERTED,
    EVENT_DAEMON_LOCK_CONFLICT,
    EVENT_DAEMON_RECOVERED,
    EVENT_DEFERRED,
    EVENT_DUPLICATE,
    EVENT_ENCODING_DETECTED,
    EVENT_LATE_ARRIVAL_AMBIGUOUS,
    EVENT_LATE_ARRIVAL_MERGED,
    EVENT_LATE_ARRIVAL_REQUEUED,
    EVENT_MALFORMED,
    EVENT_NOTIFY_DEGRADED,
    EVENT_NOTIFY_FAILED,
    EVENT_NOTIFY_ROUTED,
    EVENT_NOTIFY_SENT,
    EVENT_NOTIFY_SKIPPED,
    EVENT_ORDER_ACCEPTED,
    EVENT_ORDER_REJECTED,
    EVENT_PENDING_FILLED,
    EVENT_PENDING_INTEGRITY_REJECTED,
    EVENT_PENDING_LOCK_CONFLICT,
    EVENT_PENDING_LOCK_TAKEN_OVER,
    EVENT_PENDING_RESOLVED,
    EVENT_PENDING_RESUME_FAILED,
    EVENT_REJECTED,
    EVENT_STATE_TRANSITION,
    EVENT_STATE_TRANSITION_REJECTED,
    EVENT_WRITEBACK_ACCEPTED,
    EVENT_WRITEBACK_CONFLICT,
    EVENT_WRITEBACK_CONFLICT_RESOLVED,
    EVENT_WRITEBACK_ESCALATED,
    EVENT_WRITEBACK_QUEUED,
    EVENT_WRITEBACK_RETRY_FAILED,
    EVENT_WRITEBACK_RETRY_SUCCEEDED,
    AuditCounts,
    AuditRow,
)

__all__ = [
    "AUDIT_EXPORT_COLUMNS",
    "AuditExport",
    "AuditExportRow",
    "build_audit_rows",
    "export_audit",
    "render_audit_csv",
    "render_audit_report",
]


#: CSV 表头（顺序即列序）。列名说人话：拿件的人不该先看一遍字段字典。
AUDIT_EXPORT_COLUMNS: tuple[str, ...] = (
    "行号",
    "时间",
    "分类",
    "事件",
    "病理号",
    "说明",
)

#: 审计分类 → 中文标签。
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_INGEST: "入站",
    CATEGORY_ALIGN: "对齐",
    CATEGORY_STATE: "状态",
    CATEGORY_WRITEBACK: "写回",
    CATEGORY_NOTIFY: "通知",
    CATEGORY_PENDING: "挂起",
    CATEGORY_RUNTIME: "运行",
}

#: 审计事件 → 中文标签（件里给人看的说法）。
EVENT_LABELS: dict[str, str] = {
    EVENT_ALIGNMENT_RATIONALE: "对齐依据（已并入草稿）",
    EVENT_ALIGNMENT_PENDING: "对齐依据（未对上，挂起）",
    EVENT_STATE_TRANSITION: "状态迁移（已接受）",
    EVENT_STATE_TRANSITION_REJECTED: "状态迁移（被拒）",
    EVENT_WRITEBACK_ACCEPTED: "写回回执",
    EVENT_WRITEBACK_QUEUED: "写回失败进队列",
    EVENT_WRITEBACK_RETRY_SUCCEEDED: "写回重试成功",
    EVENT_WRITEBACK_RETRY_FAILED: "写回重试失败",
    EVENT_WRITEBACK_ESCALATED: "写回升级告警",
    EVENT_WRITEBACK_CONFLICT: "写回版本冲突",
    EVENT_WRITEBACK_CONFLICT_RESOLVED: "写回冲突了结",
    EVENT_NOTIFY_SENT: "通知已发出",
    EVENT_NOTIFY_FAILED: "通知未发出",
    EVENT_NOTIFY_DEGRADED: "通知降级",
    EVENT_NOTIFY_SKIPPED: "通知未发送",
    EVENT_NOTIFY_ROUTED: "收件人路由",
    EVENT_PENDING_FILLED: "人工补齐字段",
    EVENT_PENDING_RESOLVED: "挂起项了结",
    EVENT_PENDING_RESUME_FAILED: "补齐后仍未对上",
    EVENT_PENDING_INTEGRITY_REJECTED: "续跑前校验未过",
    EVENT_PENDING_LOCK_CONFLICT: "挂起行被并发占用",
    EVENT_PENDING_LOCK_TAKEN_OVER: "接管过期锁",
    EVENT_LATE_ARRIVAL_REQUEUED: "迟到结果重新入队",
    EVENT_LATE_ARRIVAL_MERGED: "迟到结果已并入草稿",
    EVENT_LATE_ARRIVAL_AMBIGUOUS: "迟到结果候选不唯一",
    EVENT_MALFORMED: "报文畸形，拒收",
    EVENT_REJECTED: "报文拒收",
    EVENT_DEFERRED: "申请侧报文暂缓",
    EVENT_ORDER_ACCEPTED: "申请侧条目已接收",
    EVENT_ORDER_REJECTED: "申请侧条目拒收",
    EVENT_CORRECTION_APPLIED: "更正生效",
    EVENT_CORRECTION_INVERTED: "更正未生效（时间倒挂）",
    EVENT_DUPLICATE: "重复投递（未重复入账）",
    EVENT_ENCODING_DETECTED: "报文解码方式",
    EVENT_DAEMON_RECOVERED: "启动恢复",
    EVENT_DAEMON_LOCK_CONFLICT: "单实例锁被占用",
}

#: 各事件在「说明」列里取哪几个字段、按什么顺序。字段名 → 中文标签。
#: 没列到的字段不展示 —— 件是给人看的，不是把 JSON 摊开让人自己找。
_EVENT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    EVENT_ALIGNMENT_RATIONALE: (
        ("item_code", "项目"),
        ("block_no", "蜡块"),
        ("region_desc", "部位"),
        ("match_level_label", "命中"),
        ("score", "得分"),
        ("source_system", "来源"),
        ("observed_at", "回报时间"),
        ("confidence", "置信"),
        ("recall_key", "召回键"),
    ),
    EVENT_ALIGNMENT_PENDING: (
        ("item_code", "项目"),
        ("block_no", "蜡块"),
        ("region_desc", "部位"),
        ("pending_reason", "未对上原因"),
        ("missing_fields", "待补字段"),
        ("source_system", "来源"),
        ("observed_at", "回报时间"),
    ),
    EVENT_STATE_TRANSITION: (
        ("from_label", "从"),
        ("to_label", "到"),
        ("accepted", "是否接受"),
    ),
    EVENT_STATE_TRANSITION_REJECTED: (
        ("from_label", "从"),
        ("to_label", "到"),
        ("reason", "拒绝原因"),
    ),
    EVENT_WRITEBACK_ACCEPTED: (
        ("status", "回执状态"),
        ("version", "接受版本"),
        ("draft_id", "草稿ID"),
    ),
    EVENT_WRITEBACK_QUEUED: (
        ("draft_version", "草稿版本"),
        ("error_code", "错误码"),
        ("error", "错误"),
    ),
    EVENT_WRITEBACK_RETRY_SUCCEEDED: (
        ("attempts", "已试次数"),
        ("draft_version", "草稿版本"),
    ),
    EVENT_WRITEBACK_RETRY_FAILED: (
        ("attempts", "已试次数"),
        ("escalated", "是否升级"),
    ),
    EVENT_WRITEBACK_ESCALATED: (
        ("reason", "升级原因"),
        ("attempts", "已试次数"),
        ("level", "级别"),
    ),
    EVENT_WRITEBACK_CONFLICT: (
        ("draft_version", "草稿版本"),
        ("base_version", "基础版本"),
        ("reason", "冲突原因"),
    ),
    EVENT_NOTIFY_SENT: (
        ("channel", "通道"),
        ("recipients", "收件人"),
        ("degraded", "是否降级"),
    ),
    EVENT_NOTIFY_FAILED: (
        ("channel", "通道"),
        ("error_code", "错误码"),
        ("attempts", "已试次数"),
    ),
    EVENT_NOTIFY_DEGRADED: (
        ("channel", "通道"),
        ("note", "降级说明"),
    ),
    EVENT_NOTIFY_ROUTED: (
        ("recipients", "收件人"),
        ("basis", "路由依据"),
        ("degraded", "是否降级"),
    ),
    EVENT_PENDING_FILLED: (
        ("field_name", "补的字段"),
        ("field_value", "补成"),
    ),
    EVENT_PENDING_RESOLVED: (
        ("draft_version", "草稿版本"),
        ("pending_id", "挂起行ID"),
    ),
    EVENT_PENDING_RESUME_FAILED: (
        ("pending_id", "挂起行ID"),
        ("attempts", "尝试次数"),
    ),
    EVENT_PENDING_INTEGRITY_REJECTED: (
        ("pending_id", "挂起行ID"),
        ("missing_nodes", "缺哪些节点"),
    ),
    EVENT_PENDING_LOCK_CONFLICT: (
        ("pending_id", "挂起行ID"),
        ("owner", "占用者"),
    ),
    EVENT_PENDING_LOCK_TAKEN_OVER: (
        ("pending_id", "挂起行ID"),
        ("owner", "原占用者"),
    ),
    EVENT_LATE_ARRIVAL_REQUEUED: (
        ("pending_id", "挂起行ID"),
        ("attempts", "尝试次数"),
    ),
    EVENT_LATE_ARRIVAL_MERGED: (
        ("pending_id", "挂起行ID"),
        ("draft_version", "草稿版本"),
    ),
    EVENT_MALFORMED: (
        ("file", "文件"),
        ("code", "错误码"),
        ("reason", "原因"),
    ),
    EVENT_REJECTED: (
        ("file", "文件"),
        ("reason", "原因"),
    ),
    EVENT_ORDER_REJECTED: (
        ("item_code", "项目"),
        ("reason", "原因"),
    ),
    EVENT_CORRECTION_APPLIED: (
        ("item_code", "项目"),
        ("old_value", "旧值"),
        ("new_value", "新值"),
    ),
    EVENT_CORRECTION_INVERTED: (
        ("item_code", "项目"),
        ("correction_value", "更正值"),
        ("kept_value", "保留值"),
    ),
    EVENT_DUPLICATE: (
        ("item_code", "项目"),
        ("block_no", "蜡块"),
        ("observed_at", "回报时间"),
        ("reason", "重复来路"),
        ("source_file", "来源文件"),
    ),
    EVENT_ENCODING_DETECTED: (
        ("file", "文件"),
        ("encoding", "解码方式"),
        ("fallback", "是否回退"),
    ),
    EVENT_DAEMON_RECOVERED: (
        ("pending_items", "挂起项"),
        ("escalated_items", "转人工"),
        ("writeback_pending", "回写待重试"),
        ("writeback_escalated", "回写已升级"),
        ("idempotency_keys", "已落定结果"),
        ("leftover_files", "目录待处理"),
        ("stale_locks_released", "释放过期锁"),
    ),
    EVENT_DAEMON_LOCK_CONFLICT: (
        ("lock_file", "锁文件"),
        ("holder_pid", "占用进程号"),
    ),
}


@dataclass(frozen=True)
class AuditExportRow:
    """导出的一行（字段都已是给人看的形式）。"""

    row_id: str
    ts: str
    category: str
    event: str
    accession_no: str
    detail_text: str

    def as_row(self) -> tuple[str, ...]:
        """按 :data:`AUDIT_EXPORT_COLUMNS` 的列序取值。"""
        return (
            self.row_id,
            self.ts,
            self.category,
            self.event,
            self.accession_no,
            self.detail_text,
        )


@dataclass(frozen=True)
class AuditExport:
    """一次导出的结果（落了两个文件）。"""

    csv_path: Path
    text_path: Path
    row_count: int
    counts: AuditCounts

    @property
    def paths(self) -> tuple[Path, Path]:
        return (self.csv_path, self.text_path)


# ------------------------------------------------------------------ 说明


def _stringify(value: Any) -> str:
    """明细里的一个值怎么变成一句话。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if str(item).strip()]
        return "、".join(items)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    text = str(value).strip()
    return "" if text in ("None", "null") else text


#: 明细里是时间的字段 —— 这些值要写成 ``YYYY-MM-DD HH:MM`` 给人看，
#: 而不是把 HL7 时间戳（``20260115103000``）原样摊在件上。
_TIME_FIELDS: frozenset[str] = frozenset(
    {"observed_at", "snapshot_at", "detected_at", "resolved_at"}
)


def _present(key: str, text: str) -> str:
    """一个字段值怎么摆进件里（时间格式化 + 自由文本脱敏）。"""
    if key in _TIME_FIELDS:
        return format_observed_at(text)
    return desensitize_text(text)


def _describe(event: str, detail: dict[str, Any]) -> str:
    """把一行审计明细说成一句话（自由文本已脱敏）。"""
    fields = _EVENT_FIELDS.get(event)
    if fields is None:
        # 没登记过的事件：把明细摊成键值对，别让人以为这一行没内容。
        if not detail:
            return ""
        parts = []
        for key, value in detail.items():
            text = _stringify(value)
            if text:
                parts.append(f"{key} {_present(key, text)}")
        return "｜".join(parts)

    parts: list[str] = []
    for key, label in fields:
        text = _stringify(detail.get(key))
        if text:
            parts.append(f"{label} {_present(key, text)}")
    if parts:
        return "｜".join(parts)
    # 登记过的事件但明细是空的：仍把已知的键值对兜底说出来。
    for key, value in detail.items():
        text = _stringify(value)
        if text:
            parts.append(f"{key} {_present(key, text)}")
    return "｜".join(parts)


def build_audit_rows(rows: Sequence[AuditRow]) -> tuple[AuditExportRow, ...]:
    """把审计行整成导出行（自由文本已脱敏）。"""
    result: list[AuditExportRow] = []
    for row in rows:
        result.append(
            AuditExportRow(
                row_id=str(row.id),
                ts=row.ts,
                category=CATEGORY_LABELS.get(row.category, row.category),
                event=EVENT_LABELS.get(row.event, row.event),
                accession_no=row.accession_no or "（批次级）",
                detail_text=_describe(row.event, row.detail),
            )
        )
    return tuple(result)


# ------------------------------------------------------------------ 渲染


def render_audit_csv(rows: Sequence[AuditExportRow]) -> str:
    """渲染 CSV 文本（首行表头）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(AUDIT_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(row.as_row())
    return buffer.getvalue()


def render_audit_report(
    rows: Sequence[AuditExportRow],
    counts: AuditCounts,
    *,
    accession_no: str | None = None,
    generated_at: str | None = None,
) -> str:
    """渲染人类可读文本（件头 + 口径摘要 + 逐条明细）。"""
    stamp = generated_at or now_text()
    scope = accession_no or "全部病理号"
    lines: list[str] = [
        "补充报告审计流水（导出）",
        f"生成时间：{stamp}",
        f"范围：{scope}",
        f"条目数：{len(rows)} 行",
        "",
        "—— 行数口径 ——",
        f"对齐依据：{counts.rationale_rows} 行（并入草稿的结果条目数）",
        f"挂起依据：{counts.pending_rows} 行（没对上、等人判的条目数）",
        f"草稿份数：{counts.draft_rows} 份（写回回执行数）",
        f"状态迁移：{counts.state_transition_rows} 次"
        f"（被拒 {counts.state_transition_rejected_rows} 次，不计入）",
        f"通知发送：{counts.notify_rows} 条",
        "单独计数（不混进对齐依据行数）："
        f"重复投递 {counts.duplicate_deliveries} 条、"
        f"畸形拒收 {counts.malformed_rejected} 条",
        f"审计行合计：{counts.total_rows} 行",
        "",
        "—— 明细 ——",
        "",
    ]
    if not rows:
        lines.append("（这个范围内没有审计记录）")
    else:
        for row in rows:
            lines.append(
                f"[{row.row_id}] {row.ts}｜{row.category}·{row.event}"
                f"｜{row.accession_no}"
            )
            if row.detail_text:
                lines.append(f"    {row.detail_text}")
    lines.extend(
        [
            "",
            "说明：畸形报文拒收属批次级记录（报文解析不出病理号），",
            "      按病理号导出时不随报告导出；全量口径见「单独计数」一行。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ 落盘


def export_audit(
    rows: Sequence[AuditRow],
    out_dir: Path | str,
    *,
    counts: AuditCounts,
    accession_no: str | None = None,
    generated_at: str | None = None,
    stem: str = "audit_trail",
) -> AuditExport:
    """把审计流水导出成两个文件：CSV（能排序筛选）+ 文本（能打印）。

    CSV 用 ``utf-8-sig`` 落盘：Excel 打开中文不乱码。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{stem}.csv"
    text_path = directory / f"{stem}.txt"
    export_rows = build_audit_rows(rows)
    csv_path.write_text(render_audit_csv(export_rows), encoding="utf-8-sig")
    text_path.write_text(
        render_audit_report(
            export_rows,
            counts,
            accession_no=accession_no,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    return AuditExport(
        csv_path=csv_path,
        text_path=text_path,
        row_count=len(export_rows),
        counts=counts,
    )
