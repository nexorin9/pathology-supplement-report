"""挂起记录与待人工处理清单。

一条挂起记录要说清四件事：**谁没对上**、**缺哪个字段**、**候选是谁**、
**为什么**。技术人员拿着清单去查登记，不用翻终端回滚。

导出内容**不含**姓名 / 身份证 / 联系方式等患者可识别明细 —— 清单是流转件，
会被人带走。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..models import Block, ResultEntry
from ..timefmt import format_observed_at
from ..compose.conflict import render_conflict_entries
from .fills import FIELD_LABELS
from .reasons import PendingReason, guidance_for

__all__ = [
    "PendingRecord",
    "render_pending_entries",
    "render_pending_text",
    "decide_attempt",
    "AttemptDecision",
    "PENDING_STATE_PENDING",
    "PENDING_STATE_ESCALATED",
    "PENDING_STATE_RESOLVED",
]

#: 挂起项状态：等人补齐，可自动续跑。
PENDING_STATE_PENDING = "pending"
#: 挂起项状态：已超尝试次数上限，转人工处置，不再自动续跑。
PENDING_STATE_ESCALATED = "escalated"
#: 挂起项状态：补齐后已并入草稿，这条挂起项了结。
PENDING_STATE_RESOLVED = "resolved"


@dataclass(frozen=True)
class AttemptDecision:
    """一次挂起尝试的处置决定。"""

    #: 本次落库的尝试次数（历史次数 + 1）。
    attempts: int
    #: 落库状态（``pending`` / ``escalated``）。
    state: str
    #: 是否已超上限转人工处置。
    escalated: bool
    #: 给人看的说明（超限时写清已试几次、上限几次）。
    note: str | None = None


def decide_attempt(previous_attempts: int, max_attempts: int) -> AttemptDecision:
    """算出本次挂起的尝试次数与落库状态。

    同一条结果反复对不上时，每重新挂起一次就多试一次；超过上限就不再自动续跑，
    转人工处置 —— 否则一条永远对不上的结果会在库里无限累积，把真正需要看的
    异常淹掉。上限由配置面 ``PENDING_MAX_ATTEMPTS`` 给出。
    """
    limit = max(1, int(max_attempts))
    attempts = max(0, int(previous_attempts)) + 1
    if attempts > limit:
        return AttemptDecision(
            attempts=attempts,
            state=PENDING_STATE_ESCALATED,
            escalated=True,
            note=f"已尝试 {attempts} 次，超过挂起尝试上限 {limit} 次，转人工处置",
        )
    return AttemptDecision(
        attempts=attempts,
        state=PENDING_STATE_PENDING,
        escalated=False,
    )



@dataclass(frozen=True)
class PendingRecord:
    """一条待人工处理的挂起记录。"""

    accession_no: str
    item_code: str
    reason: PendingReason
    missing_fields: tuple[str, ...] = ()
    candidates: tuple[Block, ...] = ()
    source_file: str | None = None
    source_system: str | None = None
    observed_at: str | None = None
    value: str | None = None
    attempt: int = 1
    snapshot_at: str | None = None
    #: 是否已超尝试次数上限（转人工处置，不再自动续跑）。
    escalated: bool = False
    #: 超限说明（给人看的）。
    note: str | None = None
    #: 原始结果条目（进对齐之前的样子）。快照存它，续跑才能重算 ——
    #: 只留结论的话，补齐字段后还得回头翻原始报文。
    entry: ResultEntry | None = None
    #: 原因变化前的历史原因（按记录顺序）。当前原因是 ``reason``。
    previous_reasons: tuple[str, ...] = ()
    #: 挂起行 ID（状态库里的行号）。补齐时按它定位 —— 清单上直接标出来，
    #: 技术员不用回终端里翻。
    pending_id: int | None = None
    #: 已经补过的字段名（按补齐顺序）。清单要标出「补过还是没补过」：
    #: 补过仍挂起的条目说明补的值不对，得换个思路查，不是再补一遍。
    filled_fields: tuple[str, ...] = ()

    @property
    def hint(self) -> str:
        """补齐指引（去哪儿补什么）—— 按当前原因给出。"""
        return guidance_for(self.reason).hint

    @classmethod
    def from_entry(
        cls,
        entry: ResultEntry,
        reason: PendingReason,
        *,
        missing_fields: Sequence[str] = (),
        candidates: Sequence[Block] = (),
    ) -> "PendingRecord":
        return cls(
            accession_no=entry.accession_no,
            item_code=entry.item_code,
            reason=reason,
            missing_fields=tuple(missing_fields),
            candidates=tuple(candidates),
            source_file=entry.source_file,
            source_system=entry.source_system,
            observed_at=entry.observed_at,
            value=entry.value,
            snapshot_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            entry=entry,
        )

    def candidate_text(self) -> str:
        """候选部位的可读形式。"""
        if not self.candidates:
            return "无"
        return "；".join(
            f"{block.region_desc or '部位未登记'}/蜡块 {block.block_no or '无'}"
            for block in self.candidates
        )


def render_pending_entries(records: Sequence[PendingRecord]) -> list[str]:
    """逐条明细行（清单正文）。

    运行期清单与导出的清单**共用这一份正文渲染** —— 两处各写一遍，明细迟早
    会漂（导出的清单是流转件，漂了就等于给出去一份错的清单）。
    """
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        lines.append(
            f"{index}. 病理号 {record.accession_no}｜项目 {record.item_code}｜原因 {record.reason.label}"
        )
        missing = "、".join(
            FIELD_LABELS.get(name, name) for name in record.missing_fields
        )
        lines.append(f"   需补齐字段：{missing or '无'}")
        lines.append(f"   候选部位：{record.candidate_text()}")
        hint = record.hint
        if hint:
            lines.append(f"   怎么补：{hint}")
        if record.previous_reasons:
            lines.append(
                f"   历史原因：{' → '.join(record.previous_reasons)} → {record.reason.label}"
            )
        lines.append(
            f"   来源：{record.source_system or '未知'}｜回报时间 "
            f"{format_observed_at(record.observed_at)}"
            + (f"｜报文 {record.source_file}" if record.source_file else "")
        )
        if record.escalated:
            lines.append(
                "   状态：已超挂起尝试上限，转人工处置"
                + (f"（{record.note}）" if record.note else "")
            )
        lines.append("")
    return lines


def render_pending_text(
    records: Sequence[PendingRecord],
    *,
    conflicts: Sequence[Any] = (),
    generated_at: str | None = None,
) -> str:
    """渲染待人工处理清单（人类可读文本）。

    ``conflicts`` 是版本冲突待处置的条目 —— 它们同样要人处理，因此跟挂起项
    印在同一张清单上（单独一段），技术员不用去两个地方找。
    """
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    lines.append("待人工处理清单")
    lines.append(f"生成时间：{stamp}")
    lines.append(f"条目数：{len(records)} 条")
    if conflicts:
        lines.append(f"版本冲突待处置：{len(conflicts)} 条")
    lines.append("")

    if not records:
        if not conflicts:
            lines.append("（本批没有需要人工处理的结果）")
            return "\n".join(lines) + "\n"
    else:
        lines.extend(render_pending_entries(records))

    if conflicts:
        lines.append("—— 版本冲突待处置 ——")
        lines.extend(render_conflict_entries(conflicts))

    return "\n".join(lines).rstrip() + "\n"
