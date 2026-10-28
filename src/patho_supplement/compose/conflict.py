"""版本冲突处置：拒绝写回、保留本地草稿、转人工处置。

病理系统的草稿区是**有人工编辑**的：医师可能已经改过、签发过，或者另一位
技术员刚写过一版。这时候本系统按老基础版本提交上去，对面会回 409。

三条硬规矩：

1. **拒绝写回**：不覆盖对方的版本，也不猜一个新版本号硬塞过去 —— 猜错就是
   把别人的修改抹掉。
2. **保留本地草稿**：正文与条目原样留在状态库里（草稿状态停在「已生成」），
   并落一份文本文件；医师要看的补充报告一份都没少。
3. **转人工处置**：冲突进待人工处理清单，写清冲突原因与本地版本号；**不进
   回写队列的常规重试路径** —— 对面版本不会因为我们重试而变回本地基础版本，
   反复重试只会把版本链越堆越乱。

冲突登记落在 ``writeback_conflicts`` 表：一行一条冲突，带本地草稿版本号与
基础版本号。后续版本成功回写时冲突随之了结（:func:`close_conflicts_for_accession`），
审计流水留痕 —— 「当时冲突过、后来怎么消的」要答得上来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..audit.ledger import (
    CATEGORY_WRITEBACK,
    EVENT_WRITEBACK_CONFLICT,
    EVENT_WRITEBACK_CONFLICT_RESOLVED,
    record_audit,
)
from ..db import Database
from ..timefmt import now_text

__all__ = [
    "CONFLICT_STATE_OPEN",
    "CONFLICT_STATE_RESOLVED",
    "CONFLICT_SOURCE_DIRECT",
    "CONFLICT_SOURCE_RETRY",
    "CONFLICT_REASON_LABEL",
    "CONFLICT_HINT",
    "ConflictRecord",
    "close_conflicts_for_accession",
    "count_open_conflicts",
    "list_open_conflicts",
    "record_writeback_conflict",
    "render_conflict_entries",
    "render_conflict_text",
]

#: 冲突状态：等人处置。
CONFLICT_STATE_OPEN = "open"
#: 冲突状态：已了结（后续版本写回成功，冲突不再成立）。
CONFLICT_STATE_RESOLVED = "resolved"

#: 冲突来源：首次回写就撞上版本冲突。
CONFLICT_SOURCE_DIRECT = "direct"
#: 冲突来源：回写队列重试时撞上版本冲突。
CONFLICT_SOURCE_RETRY = "retry"

#: 冲突原因的标签（清单上写给人看的那一句）。
CONFLICT_REASON_LABEL = "版本冲突"

#: 冲突的处置指引（清单上写给人看的那一句）。
CONFLICT_HINT = (
    "病理系统当前版本与本地基础版本不一致：请医师在病理系统里确认最新版本后，"
    "由信息科重新回写这份草稿"
)


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class ConflictRecord:
    """一条版本冲突登记。

    带齐四样东西，人才知道该干什么：病理号、本地草稿版本号、冲突原因、发现时间。
    """

    id: int
    accession_no: str
    draft_id: int | None
    draft_version: int | None
    base_version: str | None
    entry_count: int | None
    reason: str
    detail: str | None
    source: str
    state: str
    detected_at: str
    resolved_at: str | None = None
    resolution: str | None = None

    @property
    def label(self) -> str:
        """原因标签（清单上的一列）。"""
        return CONFLICT_REASON_LABEL

    @property
    def hint(self) -> str:
        """处置指引（去哪儿做什么）。"""
        return CONFLICT_HINT

    @property
    def version_text(self) -> str:
        """本地草稿版本号的可读形式。"""
        if self.draft_version is None:
            return "（版本号未知）"
        return f"v{self.draft_version}"

    @property
    def open(self) -> bool:
        """还在等人处置。"""
        return self.state == CONFLICT_STATE_OPEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "accession_no": self.accession_no,
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "base_version": self.base_version,
            "entry_count": self.entry_count,
            "reason": self.reason,
            "detail": self.detail,
            "source": self.source,
            "state": self.state,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
        }


# ------------------------------------------------------------------ 登记与查询


def _row_to_conflict(row: Any) -> ConflictRecord:
    keys = set(row.keys())
    return ConflictRecord(
        id=int(row["id"]),
        accession_no=str(row["accession_no"]),
        draft_id=(int(row["draft_id"]) if row["draft_id"] is not None else None),
        draft_version=(
            int(row["draft_version"]) if row["draft_version"] is not None else None
        ),
        base_version=row["base_version"],
        entry_count=(int(row["entry_count"]) if row["entry_count"] is not None else None),
        reason=str(row["reason"] or ""),
        detail=row["detail"],
        source=str(row["source"] or CONFLICT_SOURCE_DIRECT),
        state=str(row["state"] or CONFLICT_STATE_OPEN),
        detected_at=str(row["detected_at"] or ""),
        resolved_at=(row["resolved_at"] if "resolved_at" in keys else None),
        resolution=(row["resolution"] if "resolution" in keys else None),
    )


def record_writeback_conflict(
    database: Database,
    *,
    accession_no: str,
    draft_id: int | None = None,
    draft_version: int | None = None,
    base_version: str | None = None,
    entry_count: int | None = None,
    reason: str = "",
    detail: Any = None,
    source: str = CONFLICT_SOURCE_DIRECT,
    now: datetime | None = None,
) -> int:
    """登记一条版本冲突，返回冲突行 ID。

    登记同时写一行审计流水（``writeback_conflict``）：明细里带本地草稿版本号
    与基础版本号 —— 事后复盘「当时为什么没写回去」要答得上来。

    **登记不等于重试**：这条冲突不会进回写队列的常规退避路径，对面版本不会
    因为我们再打一次就变回本地基础版本。
    """
    stamp = now_text() if now is None else now.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(detail, Mapping) or isinstance(detail, (list, tuple)):
        detail_text = json.dumps(detail, ensure_ascii=False, default=str)
    else:
        detail_text = None if detail is None else str(detail)
    cursor = database.execute(
        "INSERT INTO writeback_conflicts"
        "(accession_no, draft_id, draft_version, base_version, entry_count, reason,"
        " detail, source, state, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            accession_no,
            draft_id,
            draft_version,
            base_version,
            entry_count,
            reason,
            detail_text,
            source,
            CONFLICT_STATE_OPEN,
            stamp,
        ),
    )
    conflict_id = int(cursor.lastrowid)
    record_audit(
        database,
        category=CATEGORY_WRITEBACK,
        event=EVENT_WRITEBACK_CONFLICT,
        accession_no=accession_no,
        detail={
            "conflict_id": conflict_id,
            "draft_id": draft_id,
            "draft_version": draft_version,
            "base_version": base_version,
            "entry_count": entry_count,
            "reason": reason,
            "source": source,
            "detected_at": stamp,
            "detail": detail,
        },
    )
    return conflict_id


def list_open_conflicts(
    database: Database, *, accession_no: str | None = None
) -> tuple[ConflictRecord, ...]:
    """列出还没了结的冲突（按发现时间排）。

    给 ``accession_no`` 就只看这一份报告的冲突。
    """
    if accession_no:
        rows = database.query_all(
            "SELECT * FROM writeback_conflicts WHERE state = ? AND accession_no = ?"
            " ORDER BY detected_at, id",
            (CONFLICT_STATE_OPEN, accession_no),
        )
    else:
        rows = database.query_all(
            "SELECT * FROM writeback_conflicts WHERE state = ? ORDER BY detected_at, id",
            (CONFLICT_STATE_OPEN,),
        )
    return tuple(_row_to_conflict(row) for row in rows)


def count_open_conflicts(database: Database) -> int:
    """还没了结的冲突条数。"""
    return int(
        database.query_value(
            "SELECT COUNT(*) FROM writeback_conflicts WHERE state = ?",
            (CONFLICT_STATE_OPEN,),
            default=0,
        )
        or 0
    )


def close_conflicts_for_accession(
    database: Database,
    accession_no: str,
    *,
    resolution: str,
    now: datetime | None = None,
) -> int:
    """这份报告的冲突全部了结（后续版本已写回，冲突不再成立）。

    冲突行**不删** —— 转为「已了结」并记下怎么消的，复盘时能答出「当时冲突过、
    后来是哪一版写回去的」。返回了结的条数。
    """
    stamp = now_text() if now is None else now.strftime("%Y-%m-%d %H:%M:%S")
    rows = database.query_all(
        "SELECT id, draft_version FROM writeback_conflicts"
        " WHERE state = ? AND accession_no = ? ORDER BY id",
        (CONFLICT_STATE_OPEN, accession_no),
    )
    for row in rows:
        database.execute(
            "UPDATE writeback_conflicts SET state = ?, resolved_at = ?, resolution = ?"
            " WHERE id = ?",
            (CONFLICT_STATE_RESOLVED, stamp, resolution, int(row["id"])),
        )
        record_audit(
            database,
            category=CATEGORY_WRITEBACK,
            event=EVENT_WRITEBACK_CONFLICT_RESOLVED,
            accession_no=accession_no,
            detail={
                "conflict_id": int(row["id"]),
                "draft_version": row["draft_version"],
                "resolution": resolution,
                "resolved_at": stamp,
            },
        )
    return len(rows)


# ------------------------------------------------------------------ 清单渲染


def render_conflict_entries(records: Sequence[ConflictRecord]) -> list[str]:
    """冲突的逐条明细（清单正文的一段）。

    每条写清四件事：**哪份报告、哪一版草稿、为什么冲突、接下来谁做什么**。
    """
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        count = f"（{record.entry_count} 条）" if record.entry_count is not None else ""
        lines.append(
            f"{index}. 病理号 {record.accession_no}｜本地草稿 {record.version_text}"
            f"{count}｜原因 {record.label}"
        )
        lines.append(f"   冲突原因：{record.reason or CONFLICT_REASON_LABEL}")
        lines.append(
            f"   本地基础版本：{record.base_version or '（未取到基础版本号）'}"
        )
        lines.append(f"   发现时间：{record.detected_at}")
        lines.append(f"   怎么处置：{record.hint}")
        lines.append("")
    return lines


def render_conflict_text(
    records: Sequence[ConflictRecord], *, generated_at: str | None = None
) -> str:
    """渲染冲突清单（人类可读文本）。"""
    stamp = generated_at or now_text()
    lines = [
        "版本冲突待处置清单",
        f"生成时间：{stamp}",
        f"条目数：{len(records)} 条",
        "",
    ]
    if not records:
        lines.append("（当前没有版本冲突）")
        return "\n".join(lines) + "\n"
    lines.extend(render_conflict_entries(records))
    return "\n".join(lines).rstrip() + "\n"
