"""迟到结果自动续挂。

加做结果经常晚于报告发出：先发报告、后回免疫组化；或者第一次回报没带蜡块号，
实验室核对登记本之后重新回一条带蜡块号的。这两类「晚到的结果」在业务上很常见，
却不该靠人记得去翻清单 —— 清单上写着「缺蜡块号」，人还得自己去猜这条新回报
是不是同一条。

本模块把这件事做成自动的：**结果回报到达时，状态库里若已有同一条结果的挂起行，
就自动把它接回去** ——

- **对上了**（蜡块号补上了、部位描述写清了）→ 这条挂起行了结，结果并入草稿；
- **还是对不上** → 挂起行原地重拍（尝试次数 +1、快照时间前移、原因与候选更新），
  继续等人；
- **有多条挂起行都说得通** → **不猜**，退回常规路径并记一行审计。

**按什么键接回去**：病理号 + 项目码 + 蜡块号。蜡块号按归一化口径比对
（``B-1`` ≡ ``b01`` ≡ ``B1``）。挂起的原因常常就是「回报没带蜡块号」，晚到的
那条若带了蜡块号，就该接回同一行 —— 但只有落在挂起行记下的候选部位里才算数，
否则宁可不接（接错了等于把一条结果挂到另一块上）。**晚到的那条自己也没带
蜡块号时不接回**：键不可比，两条结果可能来自两块，接回去会把其中一条的快照
盖掉 —— 丢结果比多挂一行严重得多。

**接回去不等于绑定了**：归属仍然由四级匹配（L1 蜡块号精确 → L2 部位描述精确 →
L3 词法模糊 → L4 挂起）决定，与常规路径同一套阈值。本模块只决定「这条结果该
续跑哪一条挂起行」，不决定「这条结果属于哪一块」—— 错绑定数仍然是 0 的承诺。

**人工补齐不被覆盖**：技术员补过的值只在晚到结果**仍然缺**的那个字段上补上；
晚到结果自己带了值的地方，一律以晚到结果为准 —— 回报比人工补齐更权威。

**补齐值仍然不写进原始快照**：晚到结果换的是「这一行现在记的是什么」，
补齐值另有落点（``pending_resolutions``），事后说得清「哪一步是人补的」。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..align.lexicon import Lexicon
from ..align.matcher import MatchCandidate, MatchThresholds, align_entry
from ..align.normalizer import normalize_block_no
from ..align.rationale import build_pending_alignment, record_alignment_audit
from ..audit.ledger import (
    CATEGORY_PENDING,
    EVENT_ALIGNMENT_PENDING,
    EVENT_LATE_ARRIVAL_AMBIGUOUS,
    EVENT_LATE_ARRIVAL_MERGED,
    EVENT_LATE_ARRIVAL_REQUEUED,
    EVENT_PENDING_RESOLVED,
    record_audit,
)
from ..db import Database
from ..models import Block, ReportBaseline, ResultEntry
from .fills import RESOLVABLE_FIELDS, FieldFill, apply_fills
from .records import PendingRecord
from .reasons import (
    classify_reason,
    missing_fields_for_reason,
    missing_fields_for_state,
)
from .resume import merge_into_draft, resolve_key_for
from .store import PendingSnapshot, PendingStore

__all__ = [
    "LATE_STATUS_AMBIGUOUS",
    "LATE_STATUS_MERGED",
    "LATE_STATUS_NO_PENDING",
    "LATE_STATUS_STILL_PENDING",
    "LateArrivalOutcome",
    "apply_late_arrival",
    "find_open_pending",
    "handle_late_arrival",
    "resolve_after_merge",
]

#: 这条结果没有对应的未了结挂起行 —— 按常规路径走。
LATE_STATUS_NO_PENDING = "no_pending"
#: 接回来了，而且对上了：结果可并入草稿。
LATE_STATUS_MERGED = "merged"
#: 接回来了，还是对不上：挂起行已重拍，继续等人。
LATE_STATUS_STILL_PENDING = "still_pending"
#: 有多条挂起行都说得通 —— 不猜，退回常规路径等人判。
LATE_STATUS_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LateArrivalOutcome:
    """一次「迟到结果接回」的结论。"""

    #: 四种状态之一（见本模块顶部的常量）。
    status: str
    #: 这条结果（``still_pending`` / ``merged`` 时是叠加了人工补齐值的版本）。
    entry: ResultEntry
    #: 挂起行 ID（接上了才有）。
    pending_id: int | None = None
    accession_no: str = ""
    item_code: str = ""
    #: 命中部位（``merged`` 时给出；归属由匹配器判定，不是这里猜的）。
    candidate: MatchCandidate | None = None
    #: 命中方式（L1 / L2 / L3）。
    match_level: str | None = None
    #: 置信等级（高 / 中 / 低）。
    confidence: str | None = None
    #: 挂起原因（``still_pending`` 时给出）。
    reason: str | None = None
    #: 还要人工补齐的字段（``still_pending`` 时给出）。
    missing_fields: tuple[str, ...] = ()
    #: 候选部位（``still_pending`` 时给出）。
    candidates: tuple[Block, ...] = ()
    #: 本次之后的尝试次数。
    attempts: int = 0
    #: 是否已超尝试上限转人工处置。
    escalated: bool = False
    #: 说得通的挂起行（``ambiguous`` 时给出全部行号）。
    ambiguous_rows: tuple[int, ...] = ()
    #: 新草稿行 ID / 版本号 / 条目数（并入草稿后才有）。
    draft_id: int | None = None
    draft_version: int | None = None
    draft_entry_count: int = 0

    @property
    def merged(self) -> bool:
        """这条晚到的结果对上了。"""
        return self.status == LATE_STATUS_MERGED

    @property
    def still_pending(self) -> bool:
        """接回来了，但仍对不上。"""
        return self.status == LATE_STATUS_STILL_PENDING

    @property
    def no_pending(self) -> bool:
        """没有对应的挂起行（按常规路径走）。"""
        return self.status == LATE_STATUS_NO_PENDING

    @property
    def ambiguous(self) -> bool:
        """多条挂起行都说得通（不猜）。"""
        return self.status == LATE_STATUS_AMBIGUOUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pending_id": self.pending_id,
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "match_level": self.match_level,
            "confidence": self.confidence,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
            "candidates": len(self.candidates),
            "attempts": self.attempts,
            "escalated": self.escalated,
            "ambiguous_rows": list(self.ambiguous_rows),
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "draft_entry_count": self.draft_entry_count,
        }


# --------------------------------------------------------------- 找挂起行


def _is_blank(value: Any) -> bool:
    """字段是不是空的（没填 / 只有空白）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _ordered_blocks(
    baseline: ReportBaseline | None, item_code: str
) -> tuple[Block, ...]:
    """基线里申请过这个项目的部位。"""
    if baseline is None:
        return ()
    return tuple(
        block for block in baseline.blocks if item_code in (block.ordered_items or ())
    )


def _is_same_result(
    row: PendingSnapshot, entry: ResultEntry, baseline: ReportBaseline | None
) -> bool:
    """这条晚到的结果，是不是这条挂起行的「晚到版本」。

    接回的键是「病理号 + 蜡块号 + 项目码」，蜡块号**必须可比**才算接得上：

    - **两侧都有蜡块号** → 归一化后必须相等（``B-1`` ≡ ``b01``）；
    - **只有晚到那条带了蜡块号**（挂起时没带）→ 该蜡块号必须落在挂起行记下的
      候选部位里；没有候选可依时，只在基线里这个项目**只对应一块**时才认
      （多块就是真歧义，不猜）；
    - **晚到那条没带蜡块号** → **不接回**。没有可比的键，两条结果可能来自两块
      —— 接回去等于把其中一条的快照覆盖掉，结果就丢了。宁可让它作为一条新
      结果单独挂起，技术人员看清单时也分得清。
    """
    row_block = normalize_block_no(row.block_no)
    entry_block = normalize_block_no(entry.block_no)

    if row_block is not None and entry_block is not None:
        return row_block == entry_block

    if entry_block is None:
        # 晚到的那条没带蜡块号：键不可比，不接回。
        return False

    candidate_blocks = {
        normalized
        for normalized in (
            normalize_block_no(block.block_no) for block in row.candidates
        )
        if normalized
    }
    if candidate_blocks:
        return entry_block in candidate_blocks
    return len(_ordered_blocks(baseline, entry.item_code)) == 1


def find_open_pending(
    store: PendingStore,
    entry: ResultEntry,
    *,
    baseline: ReportBaseline | None = None,
) -> tuple[PendingSnapshot, ...]:
    """找出这条结果对得上的**未了结**挂起行。

    按「病理号 + 项目码」取候选行，再按蜡块号口径筛（见
    :func:`_is_same_result`）。返回的行按行号升序。
    """
    rows = store.find_open(
        accession_no=entry.accession_no, item_code=entry.item_code
    )
    return tuple(row for row in rows if _is_same_result(row, entry, baseline))


# --------------------------------------------------------------- 接回


def _apply_existing_fills(
    entry: ResultEntry, fill_map: Mapping[str, str]
) -> ResultEntry:
    """把人工补齐值补到晚到结果的**空字段**上。

    晚到结果自带值的字段一律以它为准 —— 回报比人工补齐更权威；只有它仍然
    缺的字段，才用技术员查登记本补过的值顶上。
    """
    if not fill_map:
        return entry
    usable: dict[str, str] = {}
    for field_name, value in fill_map.items():
        target = RESOLVABLE_FIELDS.get(field_name)
        if target is None:
            continue
        if not _is_blank(getattr(entry, target, None)):
            continue
        if target == "region_seq":
            try:
                int(value)
            except (TypeError, ValueError):
                continue  # 补的序号不是整数，这一项不拿来用
        usable[field_name] = value
    if not usable:
        return entry
    return apply_fills(entry, usable)


def handle_late_arrival(
    database: Database,
    *,
    entry: ResultEntry,
    baseline: ReportBaseline | None,
    thresholds: MatchThresholds | None = None,
    guard: Any | None = None,
    max_attempts: int = 3,
    lexicon: Lexicon | None = None,
) -> LateArrivalOutcome:
    """把一条到达的结果接回对应的挂起行。

    三种结果：

    - ``no_pending``：没有对应的挂起行 —— 调用方按常规路径走；
    - ``merged``：接回来了且对上了 —— 调用方把结果并入草稿，再调
      :func:`resolve_after_merge` 把挂起行了结；
    - ``still_pending``：接回来了但还是对不上 —— 挂起行已重拍（尝试次数 +1、
      快照时间前移、原因与候选更新），继续等人；
    - ``ambiguous``：多条挂起行都说得通 —— 不猜，退回常规路径。
    """
    store = PendingStore(database)
    rows = find_open_pending(store, entry, baseline=baseline)

    if not rows:
        return LateArrivalOutcome(
            status=LATE_STATUS_NO_PENDING,
            entry=entry,
            accession_no=entry.accession_no,
            item_code=entry.item_code,
        )

    if len(rows) > 1:
        row_ids = tuple(row.snapshot_id for row in rows)
        record_audit(
            database,
            category=CATEGORY_PENDING,
            event=EVENT_LATE_ARRIVAL_AMBIGUOUS,
            accession_no=entry.accession_no,
            detail={
                "item_code": entry.item_code,
                "block_no": entry.block_no,
                "candidate_rows": list(row_ids),
                "detail": "多条挂起行都说得通，不猜：退回常规路径等人判",
            },
        )
        return LateArrivalOutcome(
            status=LATE_STATUS_AMBIGUOUS,
            entry=entry,
            accession_no=entry.accession_no,
            item_code=entry.item_code,
            ambiguous_rows=row_ids,
        )

    row = rows[0]
    effective = _apply_existing_fills(entry, row.fill_map)
    outcome = align_entry(
        effective,
        baseline,
        thresholds=thresholds,
        lexicon=lexicon,
        guard=guard,
    )

    if outcome.matched and outcome.candidate is not None:
        return LateArrivalOutcome(
            status=LATE_STATUS_MERGED,
            entry=effective,
            pending_id=row.snapshot_id,
            accession_no=row.accession_no,
            item_code=row.item_code,
            candidate=outcome.candidate,
            match_level=outcome.candidate.match_level.value,
            confidence=outcome.candidate.confidence,
            attempts=row.attempts,
        )

    reason = classify_reason(effective, outcome)
    missing = (
        tuple(outcome.missing_fields)
        or missing_fields_for_reason(reason)
        or missing_fields_for_state(outcome.state)
    )
    record = PendingRecord.from_entry(
        effective,
        reason,
        missing_fields=missing,
        candidates=outcome.candidates,
    )
    # 重拍这一行：尝试次数 +1、快照时间前移、原因与候选换成最新的。
    # 快照内容换成**晚到这条原始回报**（补齐值不写进来 —— 它另有落点）。
    updated = store.restamp(
        row.snapshot_id,
        record=record,
        max_attempts=max_attempts,
        entry=entry,
    )
    record_alignment_audit(
        database,
        build_pending_alignment(effective, outcome),
        event=EVENT_ALIGNMENT_PENDING,
    )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_LATE_ARRIVAL_REQUEUED,
        accession_no=updated.accession_no,
        detail={
            "pending_id": updated.snapshot_id,
            "item_code": updated.item_code,
            "attempts": updated.attempts,
            "reason": reason.value,
            "missing_fields": list(missing),
            "candidates": len(outcome.candidates),
            "source_system": entry.source_system,
            "source_file": entry.source_file,
            "observed_at": entry.observed_at,
        },
    )
    return LateArrivalOutcome(
        status=LATE_STATUS_STILL_PENDING,
        entry=effective,
        pending_id=updated.snapshot_id,
        accession_no=updated.accession_no,
        item_code=updated.item_code,
        reason=reason.value,
        missing_fields=tuple(updated.missing_fields),
        candidates=outcome.candidates,
        attempts=updated.attempts,
        escalated=updated.escalated,
    )


def resolve_after_merge(
    database: Database, *, outcome: LateArrivalOutcome
) -> None:
    """晚到结果已并入草稿：把那条挂起行了结。

    只做「了结」这一件事 —— 草稿由调用方统一成稿（守护进程是本轮草稿，
    独立路径是 :func:`apply_late_arrival`），这里不碰草稿，避免同一轮里出两份。
    """
    if outcome.pending_id is None:
        return
    store = PendingStore(database)
    snapshot = store.load_by_id(outcome.pending_id)
    fill_map = snapshot.fill_map if snapshot is not None else {}
    fills = tuple(
        FieldFill(field_name=name, value=value) for name, value in fill_map.items()
    )
    store.mark_resolved(
        outcome.pending_id,
        resolve_key=resolve_key_for(outcome.pending_id, fills) if fills else None,
    )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_LATE_ARRIVAL_MERGED,
        accession_no=outcome.accession_no,
        detail={
            "pending_id": outcome.pending_id,
            "item_code": outcome.item_code,
            "match_level": outcome.match_level,
            "confidence": outcome.confidence,
            "attempts": outcome.attempts,
        },
    )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_PENDING_RESOLVED,
        accession_no=outcome.accession_no,
        detail={
            "pending_id": outcome.pending_id,
            "item_code": outcome.item_code,
            "match_level": outcome.match_level,
            "confidence": outcome.confidence,
            "source": "late_arrival",
        },
    )


def apply_late_arrival(
    database: Database,
    *,
    entry: ResultEntry,
    baseline: ReportBaseline | None,
    thresholds: MatchThresholds | None = None,
    guard: Any | None = None,
    max_attempts: int = 3,
    lexicon: Lexicon | None = None,
) -> LateArrivalOutcome:
    """接回 + 当场并入草稿（不需要守护进程的一条完整路径）。

    守护进程走的是「接回 → 并入**本轮**草稿 → 了结挂起行」三步（草稿由本轮
    统一成稿）；本函数给的是独立用法：接回后直接产出**新一版**草稿，旧版本链
    保留，挂起行随之了结。
    """
    outcome = handle_late_arrival(
        database,
        entry=entry,
        baseline=baseline,
        thresholds=thresholds,
        guard=guard,
        max_attempts=max_attempts,
        lexicon=lexicon,
    )
    if not outcome.merged or outcome.pending_id is None or outcome.candidate is None:
        return outcome

    store = PendingStore(database)
    snapshot = store.load_by_id(outcome.pending_id)
    if snapshot is None:  # pragma: no cover - 行刚查过，不该消失
        return outcome
    fills = tuple(
        FieldFill(field_name=name, value=value)
        for name, value in snapshot.fill_map.items()
    )
    merged = merge_into_draft(
        database,
        store=store,
        snapshot=snapshot,
        entry=outcome.entry,
        candidate=outcome.candidate,
        baseline=baseline,
        fills=fills,
    )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_LATE_ARRIVAL_MERGED,
        accession_no=outcome.accession_no,
        detail={
            "pending_id": outcome.pending_id,
            "item_code": outcome.item_code,
            "match_level": outcome.match_level,
            "confidence": outcome.confidence,
            "draft_id": merged.draft_id,
            "draft_version": merged.draft_version,
        },
    )
    return replace(
        outcome,
        draft_id=merged.draft_id,
        draft_version=merged.draft_version,
        draft_entry_count=merged.draft_entry_count,
    )
