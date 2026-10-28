"""挂起原因分类、缺失字段映射与原因历史。

挂起不能只写一句「对不上」—— 技术人员要一眼看出**缺哪个字段、为什么缺**，
才知道该去查蜡块登记还是查回报口径。每一类原因都对应一个可补齐的入口。

原因分两路来：

- **对齐不上**：结果本身没问题，是它跟基线里的部位对不上（缺蜡块号、部位描述缺失、
  未命中部位、待查项未登记、病理号无基线）。
- **结果本身有问题**：无法获得（``OBX-11=X``）、值为空、回报时间倒挂。
  这几条与对齐无关 —— 它们进挂起清单不是因为「对不上」，而是因为**不能写进报告**。

**七类主因**（见 :data:`SEVEN_REASON_FAMILIES`）是人工补齐的入口分类：每一类
都写清「去哪儿补什么」，补完这条就能重新对齐。其余分类是主因的补充分支，
同样是可读的挂起原因。

**原因会变**：同一条结果补齐一次字段后，原因可能从「缺蜡块号」变成「同分多候选」。
原因历史（:class:`ReasonHistory`）把变化留痕 —— 每次变化带时间戳，
**当前原因取最新一条**，历史不被覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from ..align.matcher import MatchState
from ..models import Block, ResultEntry, ResultStatus
from ..timefmt import now_text

__all__ = [
    "PendingReason",
    "ReasonGuidance",
    "ReasonHistory",
    "ReasonHistoryEntry",
    "SEVEN_REASON_FAMILIES",
    "classify_reason",
    "guidance_for",
    "guidance_for_outcome",
    "missing_fields_for_entry",
    "missing_fields_for_reason",
    "missing_fields_for_state",
    "parse_reason",
    "reason_for_entry",
    "reason_for_outcome",
    "reason_for_state",
]


class PendingReason(str, Enum):
    """挂起原因。值即展示用的中文标签。"""

    # ---------------------------------------------------------- 七类主因
    MISSING_BLOCK_NO = "缺蜡块号"
    REGION_DESC_MISSING = "部位描述缺失"
    AMBIGUOUS = "同分多候选"
    NO_RECALL_HITS = "召回命中 0 条"
    ITEM_CANCELLED = "待查项已取消"
    ITEM_NOT_ORDERED = "待查项未登记"
    TIME_INVERSION = "回报时间倒挂"

    # ---------------------------------------------------------- 其余分类
    NO_MATCH = "未命中部位"
    NO_BASELINE = "病理号无基线"
    CONFLICT = "部位与蜡块号冲突"
    FIELDS_MISSING = "字段缺失"
    ITEM_TERMINATED = "待查项已终止"
    RESULT_NOT_OBTAINED = "结果无法获得"
    EMPTY_VALUE = "结果值为空"

    @property
    def label(self) -> str:
        return self.value


#: 七类主因：人工补齐的入口分类。顺序即技术员的排查顺序。
SEVEN_REASON_FAMILIES: tuple[PendingReason, ...] = (
    PendingReason.MISSING_BLOCK_NO,
    PendingReason.REGION_DESC_MISSING,
    PendingReason.AMBIGUOUS,
    PendingReason.NO_RECALL_HITS,
    PendingReason.ITEM_CANCELLED,
    PendingReason.ITEM_NOT_ORDERED,
    PendingReason.TIME_INVERSION,
)

#: 早期口径的标签 → 当前分类。老库里存着这些值，仍然认得。
_LEGACY_LABELS: dict[str, PendingReason] = {
    "项目未登记": PendingReason.ITEM_NOT_ORDERED,
}

#: 别的模块给出的原因措辞 → 当前分类。措辞可以更细，归类只有一个口径。
_REASON_ALIASES: dict[str, PendingReason] = {
    # 过滤层的降级措辞：硬过滤没捞到东西（降级与否都属于「召回命中 0 条」）。
    "硬过滤命中 0 条，降级模糊匹配": PendingReason.NO_RECALL_HITS,
    "硬过滤键字段不全": PendingReason.NO_RECALL_HITS,
    # 词法没达阈值：定位不上块，要补的是蜡块号。
    "词法匹配未达阈值": PendingReason.MISSING_BLOCK_NO,
}

#: 对齐状态 → 挂起原因。
_STATE_TO_REASON: dict[str, PendingReason] = {
    MatchState.MISSING_BLOCK_NO: PendingReason.MISSING_BLOCK_NO,
    MatchState.NO_BLOCK: PendingReason.NO_MATCH,
    MatchState.ITEM_NOT_ORDERED: PendingReason.ITEM_NOT_ORDERED,
    MatchState.NO_BASELINE: PendingReason.NO_BASELINE,
    MatchState.AMBIGUOUS: PendingReason.AMBIGUOUS,
    MatchState.CONFLICT: PendingReason.CONFLICT,
    MatchState.FIELDS_MISSING: PendingReason.FIELDS_MISSING,
    MatchState.ITEM_CANCELLED: PendingReason.ITEM_CANCELLED,
    MatchState.ITEM_TERMINATED: PendingReason.ITEM_TERMINATED,
}

#: 对齐状态 → 需要人工补齐的字段。
_STATE_TO_FIELDS: dict[str, tuple[str, ...]] = {
    MatchState.MISSING_BLOCK_NO: ("block_no",),
    MatchState.NO_BLOCK: ("region_seq", "block_no"),
    MatchState.ITEM_NOT_ORDERED: ("item_code",),
    MatchState.NO_BASELINE: ("baseline",),
    MatchState.AMBIGUOUS: ("block_no", "region_desc"),
    MatchState.CONFLICT: ("block_no", "region_desc"),
    MatchState.FIELDS_MISSING: ("block_no", "region_desc"),
    # 已取消 / 已终止：缺的不是字段 —— 没有可补的字段，要人决定这条结果怎么办。
    MatchState.ITEM_CANCELLED: (),
    MatchState.ITEM_TERMINATED: (),
}

#: 挂起原因 → 需要人工补齐的字段。
#:
#: 「要补什么」是挂起清单的第一用途：技术员拿到清单直接照着字段去查，
#: 不用反推「到底缺什么」。
_REASON_TO_FIELDS: dict[PendingReason, tuple[str, ...]] = {
    PendingReason.MISSING_BLOCK_NO: ("block_no",),
    PendingReason.REGION_DESC_MISSING: ("region_desc",),
    PendingReason.AMBIGUOUS: ("block_no", "region_desc"),
    PendingReason.NO_RECALL_HITS: ("region_seq", "block_no"),
    PendingReason.ITEM_CANCELLED: (),
    PendingReason.ITEM_NOT_ORDERED: ("item_code",),
    PendingReason.TIME_INVERSION: ("observed_at",),
    PendingReason.NO_MATCH: ("region_seq", "block_no"),
    PendingReason.NO_BASELINE: ("baseline",),
    PendingReason.CONFLICT: ("block_no", "region_desc"),
    PendingReason.FIELDS_MISSING: ("block_no", "region_desc"),
    PendingReason.ITEM_TERMINATED: (),
    PendingReason.RESULT_NOT_OBTAINED: ("result_value",),
    PendingReason.EMPTY_VALUE: ("result_value",),
}

#: 挂起原因 → 给技术员的一句话（去哪儿补什么）。
_REASON_HINTS: dict[PendingReason, str] = {
    PendingReason.MISSING_BLOCK_NO: "回报没带蜡块号：按部位描述在蜡块登记本上核对，补填蜡块号",
    PendingReason.REGION_DESC_MISSING: "回报没带取材部位描述：补填部位描述后才能定到哪一块",
    PendingReason.AMBIGUOUS: "多个候选同分，分不出是哪一块：补蜡块号或部位描述指定其中一块",
    PendingReason.NO_RECALL_HITS: "召回没捞到任何候选：补部位序号或蜡块号后重跑一次",
    PendingReason.ITEM_CANCELLED: "申请侧已取消这项加做：这条结果不该进报告，请与开单医师确认后处置",
    PendingReason.ITEM_NOT_ORDERED: "这一项没在这块上登记过加做申请：核对申请单与蜡块登记本",
    PendingReason.TIME_INVERSION: "这条的回报时间比被取代的那条还早：核对两边的回报时间，确认哪条为准",
    PendingReason.NO_MATCH: "基线里没有对应的部位：核对取材登记，确认这条结果属于哪一块",
    PendingReason.NO_BASELINE: "病理号在病理系统里查不到基线：确认病理号有没有写错",
    PendingReason.CONFLICT: "蜡块号与部位描述互相矛盾：核对是哪一侧登记错了",
    PendingReason.FIELDS_MISSING: "蜡块号与部位描述都没带：两个定位字段都要补",
    PendingReason.ITEM_TERMINATED: "申请侧已终止这项加做：请与开单医师确认后处置",
    PendingReason.RESULT_NOT_OBTAINED: "回报标记为「无法获得」：确认是否需要重新取材或复检",
    PendingReason.EMPTY_VALUE: "回报结果值为空：确认是漏填还是确实无结果",
}


def parse_reason(text: str) -> PendingReason:
    """把一段原因文本解析成挂起原因。

    认不出来的**直接报错** —— 原因不认识就不能进挂起清单（宁可报错，
    也不把它糊弄成「未命中部位」）。
    """
    try:
        return PendingReason(text)
    except ValueError:
        pass
    alias = _REASON_ALIASES.get(text) or _LEGACY_LABELS.get(text)
    if alias is not None:
        return alias
    raise ValueError(f"挂起原因不认识：{text!r}")


def reason_for_state(state: str) -> PendingReason:
    """按对齐状态给出挂起原因。"""
    return _STATE_TO_REASON.get(state, PendingReason.NO_MATCH)


def reason_for_outcome(outcome: Any) -> PendingReason:
    """按对齐结论给出挂起原因。

    **优先用结论自带的细标签**：同一个状态下原因可能更细 —— 蜡块号与部位
    描述都缺时，状态是「缺定位字段」，但给技术员看的标签应是「字段缺失」
    （要补的是两个字段，不是一个）。细标签不认识时退回状态兜底。
    """
    pending_reason = getattr(outcome, "pending_reason", None)
    if pending_reason:
        try:
            return parse_reason(pending_reason)
        except ValueError:
            pass
    return reason_for_state(getattr(outcome, "state", ""))


def missing_fields_for_state(state: str) -> tuple[str, ...]:
    """按对齐状态给出需要人工补齐的字段。"""
    return _STATE_TO_FIELDS.get(state, ("region_seq", "block_no"))


def missing_fields_for_reason(reason: PendingReason) -> tuple[str, ...]:
    """按挂起原因给出需要人工补齐的字段。"""
    return _REASON_TO_FIELDS.get(reason, ("region_seq", "block_no"))


def reason_for_entry(entry: ResultEntry) -> PendingReason | None:
    """按结果本身的状态给出挂起原因；没问题就返回 ``None``。

    - **无法获得**（``OBX-11=X``）：已回报但拿不到结果 —— 报告里不能写空项。
    - **值为空**：空行在报告里跟「无表达」长得一样，一个空值可能被读成阴性结论。
    """
    if entry.status is ResultStatus.NOT_OBTAINED:
        return PendingReason.RESULT_NOT_OBTAINED
    if not (entry.value or "").strip():
        return PendingReason.EMPTY_VALUE
    return None


def missing_fields_for_entry(entry: ResultEntry) -> tuple[str, ...]:
    """结果本身缺哪个字段（只有需要补结果值时才算缺）。"""
    if reason_for_entry(entry) is None:
        return ()
    return ("result_value",)


def classify_reason(entry: ResultEntry, outcome: Any) -> PendingReason:
    """给一条对不上的结果定最终原因。

    判定顺序：

    1. **结果本身有问题**（无法获得 / 值为空）→ 与对齐无关，直接定；
    2. **对齐结论自带的标签**（含「召回命中 0 条」这类细标签）；
    3. **定位字段兜底**：结论是「未命中部位」而回报压根没带部位描述时，
       要补的其实是部位描述 —— 标「部位描述缺失」，别让人去查一个根本没填的字段。
    """
    reason = reason_for_entry(entry)
    if reason is not None:
        return reason
    reason = reason_for_outcome(outcome)
    if reason is PendingReason.NO_MATCH and not (entry.region_desc or "").strip():
        return PendingReason.REGION_DESC_MISSING
    return reason


@dataclass(frozen=True)
class ReasonGuidance:
    """一条挂起的补齐指引：**缺哪个字段 + 候选是谁 + 去哪儿补**。"""

    reason: PendingReason
    missing_fields: tuple[str, ...] = ()
    candidates: tuple[Block, ...] = ()
    hint: str = ""

    @property
    def label(self) -> str:
        return self.reason.label

    @property
    def candidate_text(self) -> str:
        """候选部位的可读形式。"""
        if not self.candidates:
            return "无"
        return "；".join(
            f"{block.region_desc or '部位未登记'}/蜡块 {block.block_no or '无'}"
            for block in self.candidates
        )

    def as_dict(self) -> dict[str, Any]:
        """可打印的摘要视图（写进运行摘要与审计可查）。"""
        return {
            "reason": self.reason.value,
            "missing_fields": list(self.missing_fields),
            "candidates": len(self.candidates),
            "hint": self.hint,
        }


def guidance_for(
    reason: PendingReason, candidates: Sequence[Block] = ()
) -> ReasonGuidance:
    """按挂起原因给出补齐指引（缺哪个字段 + 候选是谁 + 去哪儿补）。"""
    return ReasonGuidance(
        reason=reason,
        missing_fields=missing_fields_for_reason(reason),
        candidates=tuple(candidates),
        hint=_REASON_HINTS.get(reason, ""),
    )


def guidance_for_outcome(outcome: Any) -> ReasonGuidance:
    """按对齐结论给出补齐指引。"""
    return guidance_for(
        reason_for_outcome(outcome),
        getattr(outcome, "candidates", ()) or (),
    )


@dataclass(frozen=True)
class ReasonHistoryEntry:
    """一次原因记录（带时间戳）。"""

    reason: PendingReason
    recorded_at: str
    detail: str | None = None

    @property
    def label(self) -> str:
        return self.reason.label

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "recorded_at": self.recorded_at,
            "detail": self.detail,
        }


class ReasonHistory:
    """同一条挂起项的原因历史。

    **原因会变**：补齐一次字段后，原因可能从「缺蜡块号」变成「同分多候选」。
    历史留痕、不被覆盖；**当前原因取最新一条**。
    """

    def __init__(self, entries: Sequence[ReasonHistoryEntry] = ()) -> None:
        self._entries: list[ReasonHistoryEntry] = list(entries)

    def append(
        self,
        reason: PendingReason,
        *,
        at: str | None = None,
        detail: str | None = None,
    ) -> ReasonHistoryEntry:
        """记一条原因（带时间戳）。"""
        entry = ReasonHistoryEntry(
            reason=reason, recorded_at=at or now_text(), detail=detail
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[ReasonHistoryEntry, ...]:
        """全部历史（按记录顺序）。"""
        return tuple(self._entries)

    @property
    def current(self) -> ReasonHistoryEntry | None:
        """最新一条（即当前原因）。"""
        return self._entries[-1] if self._entries else None

    @property
    def current_reason(self) -> PendingReason | None:
        """当前原因。"""
        current = self.current
        return current.reason if current is not None else None

    @property
    def previous_reasons(self) -> tuple[PendingReason, ...]:
        """当前原因之前的历史原因（按记录顺序）。"""
        return tuple(entry.reason for entry in self._entries[:-1])

    @property
    def changed(self) -> bool:
        """原因变过没有（出现过两种以上不同的原因）。"""
        return len({entry.reason for entry in self._entries}) > 1

    def reasons(self) -> tuple[PendingReason, ...]:
        """历史原因序列（按记录顺序）。"""
        return tuple(entry.reason for entry in self._entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current_reason.value if self.current_reason else None,
            "changed": self.changed,
            "entries": [entry.as_dict() for entry in self._entries],
        }
