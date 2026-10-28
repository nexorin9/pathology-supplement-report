"""人工补齐与从挂起点续跑。

对不上的结果被拍成快照落库（见 :mod:`patho_supplement.pending.store`），
技术员查完蜡块登记本把缺的字段补上，然后**从挂起的那一步接着跑** —— 不是把
整批回报重跑一遍。

续跑的路径只有一条::

    读挂起行（原始快照 + 已记录的补齐值）
      → 续跑前完整性校验（缺节点即报错，不静默跳过）
      → 叠加补齐值，重走一次对齐
      → 对上了：并入草稿，产出**新一版**草稿（旧版本链保留）
      → 还是对不上：挂起行原地重拍（尝试次数 +1、原因与候选更新），继续等人

**为什么只重跑这一条**：整批重跑会把已经成稿的结果再算一遍，把版本链搅乱，
还会在幂等账本上撞车。挂起行记的就是「当时卡住的那一下」，从这里接着跑
就够了 —— 源产品的断点续跑也是这个意思（快照记的是断点处的状态，续跑从
断点接着走，见 ``github_ref/haystack/haystack/core/pipeline/breakpoint.py``）。

**补齐值不覆盖原始快照**：原始回报留在 ``result_json`` 里，补齐值另落一张表。
事后要问「当时回报里到底有没有蜡块号」，答得上来。

**补齐后仍对不上，就还是挂起**：补齐值造成同分歧义时一样不硬绑 —— 宁可多
等人判一次，也不让一条结果挂到错的块上。

**两个人同时补同一行**：谁先抢到这一行的互斥锁谁续跑，另一个人拿到「行已被
处理」的提示，等对方跑完重试即可（补齐值不会丢 —— 值在锁之外登记）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..align.lexicon import Lexicon
from ..align.matcher import MatchCandidate, MatchThresholds, align_entry
from ..align.query_derivation import PendingQueryStore
from ..align.rationale import (
    build_alignment,
    build_pending_alignment,
    record_alignment_audit,
)
from ..audit.ledger import (
    CATEGORY_PENDING,
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
    EVENT_PENDING_FILLED,
    EVENT_PENDING_LOCK_CONFLICT,
    EVENT_PENDING_LOCK_TAKEN_OVER,
    EVENT_PENDING_RESOLVED,
    EVENT_PENDING_RESUME_FAILED,
    record_audit,
)
from ..compose.draft_renderer import derive_awaiting, render_draft
from ..compose.draft_store import (
    load_draft_entries,
    load_latest_draft,
    next_draft_version,
    persist_draft,
)
from ..db import Database
from ..models import Alignment, Block, ReportBaseline, ResultEntry, ResultStatus
from .fills import FIELD_LABELS, FieldFill
from .integrity import load_resumable
from .lock import DEFAULT_LOCK_TTL, PendingLock
from .reasons import (
    classify_reason,
    missing_fields_for_reason,
    missing_fields_for_state,
)
from .records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    PendingRecord,
)
from .resolve_keys import RESOLVE_OUTCOME_MERGED, ResolveKeyLedger
from .store import PendingSnapshot, PendingStore

__all__ = [
    "RESOLVE_STATUS_MERGED",
    "RESOLVE_STATUS_STILL_PENDING",
    "PendingLockedError",
    "ResumeError",
    "ResumeOutcome",
    "keep_pending",
    "merge_into_draft",
    "record_fills",
    "resolve_key_for",
    "resume_pending",
]

#: 续跑结果：已并入草稿。
RESOLVE_STATUS_MERGED = "merged"
#: 续跑结果：仍对不上，继续等人补齐。
RESOLVE_STATUS_STILL_PENDING = "still_pending"


class ResumeError(RuntimeError):
    """续跑这件事本身做不成：行不在、快照不齐、草稿重建不出来。

    **报错而不是跳过**：续跑做不成却静默跳过，等于这条结果没人管了。
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class PendingLockedError(ResumeError):
    """这一行正在被别的人补齐续跑 —— 抢互斥锁没抢到。

    与「续跑做不成」分开：**这不是失败，是要等一下**。补齐值已经登记在库里
    （值在锁之外登记），等对方跑完直接重试即可，不会丢也不会重复并入草稿。
    """

    def __init__(self, detail: str, *, pending_id: int | None = None) -> None:
        self.pending_id = pending_id
        super().__init__(detail)


@dataclass(frozen=True)
class ResumeOutcome:
    """一次续跑的结论。"""

    pending_id: int
    accession_no: str
    item_code: str
    #: ``merged``（已并入草稿）或 ``still_pending``（仍等人）。
    status: str
    #: 命中方式（并入草稿时给出）。
    match_level: str | None = None
    #: 置信等级（并入草稿时给出）。
    confidence: str | None = None
    #: 挂起原因（仍等人时给出）。
    reason: str | None = None
    #: 还需要人工补齐的字段（仍等人时给出）。
    missing_fields: tuple[str, ...] = ()
    #: 候选部位（仍等人时给出）。
    candidates: tuple[Block, ...] = ()
    #: 本次续跑后的尝试次数。
    attempts: int = 0
    #: 是否已超尝试上限转人工处置。
    escalated: bool = False
    #: 新草稿版本号（并入草稿时给出）。
    draft_version: int | None = None
    #: 新草稿行 ID（并入草稿时给出）。
    draft_id: int | None = None
    #: 新草稿的条目数（并入草稿时给出）—— 只数**并入的结果**。
    draft_entry_count: int = 0
    #: 新草稿里的「待回报」占位条数（并入草稿时给出）。
    draft_placeholder_count: int = 0
    #: 本次套用的补齐值。
    fills: tuple[FieldFill, ...] = ()
    #: 本次是**幂等命中**：上一次同一组补齐值已经生效过，这次只是重放，
    #: 没有产出新的草稿。
    idempotent: bool = False

    @property
    def merged(self) -> bool:
        """是否已并入草稿。"""
        return self.status == RESOLVE_STATUS_MERGED

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "status": self.status,
            "match_level": self.match_level,
            "confidence": self.confidence,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
            "candidates": len(self.candidates),
            "attempts": self.attempts,
            "escalated": self.escalated,
            "draft_version": self.draft_version,
            "draft_id": self.draft_id,
            "draft_entry_count": self.draft_entry_count,
            "draft_placeholder_count": self.draft_placeholder_count,
            "fills": [fill.as_dict() for fill in self.fills],
            "idempotent": self.idempotent,
        }


# --------------------------------------------------------------- 补齐值


def resolve_key_for(pending_id: int, fills: Sequence[FieldFill]) -> str:
    """补齐的幂等键：挂起行 ID + 字段 + 值。

    同一行、同一组补齐值重复提交 → 同一个键 → 不会产生第二份草稿。
    """
    parts = [str(pending_id)]
    parts.extend(f"{fill.field_name}={fill.value}" for fill in sorted(fills, key=lambda f: f.field_name))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _load_snapshot(
    database: Database, store: PendingStore, pending_id: int
) -> PendingSnapshot:
    """取挂起行，并做续跑前的完整性校验。

    两种「取不到」分开报：

    - **行不存在** → :class:`ResumeError`（说清是第几行）；
    - **行读不出来 / 缺节点** → :class:`SnapshotIntegrityError`（缺一即报错，
      不静默跳过，也不当成已对齐；顺带记一行审计说明为什么没续跑）。
    """
    exists = database.query_one(
        "SELECT id FROM pending_items WHERE id = ?", (pending_id,)
    )
    if exists is None:
        raise ResumeError(f"状态库里没有第 {pending_id} 行挂起项")
    return load_resumable(store, snapshot_ids=[pending_id])[0]


def _validate_fill_targets(snapshot: PendingSnapshot, fills: Sequence[FieldFill]) -> None:
    """补齐值只能落在「这一行要补的字段」上。

    两种情况允许：

    - 字段在当前待补字段里 —— 这正是补齐入口的用途；
    - 字段此前已经补过 —— 补错了要能改，否则技术员只能干等。

    其余一律拒收：补齐入口是「补一个字段」，不是「把整条结果重投一遍」。
    """
    missing = set(snapshot.missing_fields)
    already = set(snapshot.fill_map)
    for fill in fills:
        if fill.field_name in missing or fill.field_name in already:
            continue
        wanted = "、".join(
            FIELD_LABELS.get(name, name) for name in snapshot.missing_fields
        ) or "（本行没有待补字段）"
        raise ResumeError(
            f"第 {snapshot.snapshot_id} 行不需要补「{FIELD_LABELS.get(fill.field_name, fill.field_name)}」："
            f"这一行要补的是 {wanted}"
        )


def record_fills(
    database: Database,
    *,
    pending_id: int,
    fills: Sequence[FieldFill],
    recorded_at: str | None = None,
) -> tuple[FieldFill, ...]:
    """把补齐值记到挂起行上（**不覆盖原始快照**）。

    记完不续跑 —— 补齐与续跑分成两步，是为了让「补了但没跑成」这件事看得见
    （补齐值在库里，人可以再跑一次）。

    ``fills`` 为空时：**这一行已经补过**就按库里那份重跑（登记本更正之后
    再试一次是常事，不必把补齐值重打一遍）；一行都没补过才报错。
    """
    store = PendingStore(database)
    # 取行即校验：补齐入口守同一条线。往一份残缺的快照上补值，
    # 补完也续不了跑 —— 只会把「快照坏了」这件事越拖越久。
    snapshot = _load_snapshot(database, store, pending_id)
    if not fills:
        existing = tuple(
            FieldFill(field_name=name, value=value)
            for name, value in snapshot.fill_map.items()
        )
        if not existing:
            raise ResumeError("没有要补的字段：请用 --set 字段名=值 指定补齐内容")
        return existing
    _validate_fill_targets(snapshot, fills)
    # 值没变的重复提交也要走一遍 record_fill：提交次数 +1 是「这是重放」的信号
    #（续跑据此幂等返回上一次结论，而不是再并一份草稿）。
    already = store.load_fills(pending_id)
    changed = [fill for fill in fills if already.get(fill.field_name) != fill.value]
    for fill in fills:
        store.record_fill(
            pending_id, fill.field_name, fill.value, recorded_at=recorded_at
        )
    # 幂等台账：这一组补齐值算作**一次提交**（值没变也算 —— 点两下就是提交两次）。
    # 键按**登记后的完整补齐值**算，与续跑时算出来的键一致。
    effective = store.load_fills(pending_id)
    effective_fills = tuple(
        FieldFill(field_name=name, value=value) for name, value in effective.items()
    )
    ResolveKeyLedger(database).record_submission(
        resolve_key_for(pending_id, effective_fills),
        pending_id=pending_id,
        accession_no=snapshot.accession_no,
        item_code=snapshot.item_code,
    )
    if changed:
        record_audit(
            database,
            category=CATEGORY_PENDING,
            event=EVENT_PENDING_FILLED,
            accession_no=snapshot.accession_no,
            detail={
                "pending_id": pending_id,
                "item_code": snapshot.item_code,
                "fills": [fill.as_dict() for fill in fills],
                "resolve_key": resolve_key_for(pending_id, fills),
            },
        )
    return tuple(fills)


# --------------------------------------------------------------- 续跑


def _draft_entry_counts(database: Database, draft_id: Any) -> tuple[int, int]:
    """一份草稿的条目数：（并入的结果条数，「待回报」占位条数）。

    草稿行的 ``entry_count`` 是**总行数**（含占位）。对外报「并了几条」时
    要把占位分开算 —— 占位不是结果。
    """
    merged = database.query_value(
        "SELECT COUNT(*) FROM draft_entries WHERE draft_id = ? AND placeholder = 0",
        (draft_id,),
        default=0,
    )
    placeholders = database.query_value(
        "SELECT COUNT(*) FROM draft_entries WHERE draft_id = ? AND placeholder = 1",
        (draft_id,),
        default=0,
    )
    return int(merged or 0), int(placeholders or 0)


def _rebuild_pairs(
    database: Database, accession_no: str
) -> list[tuple[ResultEntry, MatchCandidate]]:
    """把上一版草稿的条目重建回来（续跑要在此基础上追加新条目）。

    重建依据是草稿条目行与其对齐依据 —— 草稿正文与回写请求体里都不带结果
    状态（更正 / 初步 / 终报在原始快照与审计流水里查），这里按终报落位，
    不影响正文与归属判定。

    **「待回报」占位条目不重建**：占位不是一条结果，它没有值也没有对齐依据，
    重建不出来。占位是**派生**出来的（已申请 − 已回报），续跑时按当前状态
    重新派生一遍 —— 补齐了一项，剩下的占位自动少一行，没补齐的照旧占着。
    """
    row = load_latest_draft(database, accession_no)
    if row is None:
        return []
    pairs: list[tuple[ResultEntry, MatchCandidate]] = []
    for entry_row in load_draft_entries(database, int(row["id"])):
        if entry_row["placeholder"]:
            # 占位行由渲染层按「已申请 − 已回报」重新派生，不从上一版搬。
            continue
        rationale = entry_row["rationale_json"]
        if not rationale:
            raise ResumeError(
                f"第 {row['id']} 行草稿的第 {entry_row['id']} 条没有对齐依据，"
                "无法重建续跑内容（草稿行损坏，不要绕过）"
            )
        try:
            alignment = Alignment.from_dict(json.loads(rationale))
        except (TypeError, ValueError) as exc:
            raise ResumeError(
                f"第 {row['id']} 行草稿的第 {entry_row['id']} 条对齐依据读不出来：{exc}"
            ) from exc
        entry = ResultEntry(
            accession_no=accession_no,
            item_code=entry_row["item_code"] or "",
            item_name=entry_row["item_name"],
            value=entry_row["value"],
            status=ResultStatus.FINAL,
            observed_at=entry_row["observed_at"],
            region_seq=entry_row["region_seq"],
            block_no=entry_row["block_no"],
            region_desc=entry_row["region_desc"],
            source_system=entry_row["source_system"],
        )
        candidate = MatchCandidate(
            block=Block(
                region_seq=alignment.region_seq,
                region_desc=alignment.region_desc,
                block_no=alignment.block_no,
            ),
            match_level=alignment.match_level,
            score=alignment.score,
            recall_key=alignment.recall_key or "",
            matched_terms=tuple(alignment.matched_terms),
        )
        pairs.append((entry, candidate))
    return pairs


def _remaining_fields(
    outcome: Any, reason: Any, filled: Mapping[str, str]
) -> tuple[str, ...]:
    """还要补哪些字段：按**原因**给，已补过的去掉。

    原因与待补字段必须对得上：清单上写着「部位描述缺失」，待补字段就该是部位
    描述 —— 两处说法不一致，技术员就不知道该去补哪张单了。

    已补齐的字段从清单里去掉 —— 技术员看到的是「现在还缺什么」，不是「从头
    到尾缺过什么」。全补齐了仍对不上时退回原因给的字段（此时该由人判断是补
    错了还是别的问题）。
    """
    mapped = (
        missing_fields_for_reason(reason)
        or tuple(outcome.missing_fields)
        or missing_fields_for_state(getattr(outcome, "state", ""))
    )
    remaining = tuple(name for name in mapped if name not in filled)
    return remaining or tuple(mapped)


def _outcome_from_ledger(
    database: Database,
    snapshot: PendingSnapshot,
    fills: tuple[FieldFill, ...],
    record: Any,
) -> ResumeOutcome:
    """按幂等台账里的记录还原上一次的结论。"""
    detail = record.detail or {}
    draft_id = record.draft_id
    merged_count, placeholder_count = _draft_entry_counts(database, draft_id)
    return ResumeOutcome(
        pending_id=snapshot.snapshot_id,
        accession_no=snapshot.accession_no,
        item_code=snapshot.item_code,
        status=RESOLVE_STATUS_MERGED,
        match_level=detail.get("match_level"),
        confidence=detail.get("confidence"),
        attempts=snapshot.attempts,
        draft_version=record.draft_version,
        draft_id=draft_id,
        draft_entry_count=merged_count,
        draft_placeholder_count=placeholder_count,
        fills=fills,
        idempotent=True,
    )


def _legacy_previous_outcome(
    database: Database,
    snapshot: PendingSnapshot,
    fills: tuple[FieldFill, ...],
    resolve_key: str,
) -> ResumeOutcome | None:
    """老库的凭据：从审计流水里取回上一次的结论。

    台账建立之前已了结的行走这里 —— 判据是「了结之后又提交过一次」
    （``fills_revision`` 比 ``resolved_revision`` 大）。没再提交就直接重跑，
    那是有人手工重跑，不算「重复提交」，该报错拦下。
    """
    if not snapshot.resolve_key:
        return None
    if snapshot.resolve_key != resolve_key:
        return None
    if snapshot.resolved_revision is None:
        return None
    if snapshot.fills_revision <= snapshot.resolved_revision:
        return None

    rows = database.query_all(
        "SELECT detail_json FROM audit_log WHERE category = ? AND event = ?"
        " AND accession_no = ? ORDER BY id DESC",
        (CATEGORY_PENDING, EVENT_PENDING_RESOLVED, snapshot.accession_no),
    )
    for row in rows:
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        if detail.get("pending_id") != snapshot.snapshot_id:
            continue
        draft_id = detail.get("draft_id")
        merged_count, placeholder_count = _draft_entry_counts(database, draft_id)
        return ResumeOutcome(
            pending_id=snapshot.snapshot_id,
            accession_no=snapshot.accession_no,
            item_code=snapshot.item_code,
            status=RESOLVE_STATUS_MERGED,
            match_level=detail.get("match_level"),
            confidence=detail.get("confidence"),
            attempts=snapshot.attempts,
            draft_version=detail.get("draft_version"),
            draft_id=draft_id,
            draft_entry_count=merged_count,
            draft_placeholder_count=placeholder_count,
            fills=fills,
            idempotent=True,
        )
    return None


def _previous_resolve_outcome(
    database: Database, snapshot: PendingSnapshot, fills: tuple[FieldFill, ...]
) -> ResumeOutcome | None:
    """上一次续跑的结论（幂等命中时返回）；对不上就给 ``None``。

    判据两条，缺一不可：

    1. 这一组补齐值**已经生效过**（幂等台账里 status=merged；老库走
       ``pending_items.resolve_key`` 兜底）—— 补了别的值就是另一件事，
       不该拿旧结论顶；
    2. 这一组补齐值被**提交过至少两次** —— 只提交过一次就重跑，那是有人
       手工重跑，不算「重复提交」，该报错拦下。

    **为什么看台账而不是只看行上的计数**：并发补齐时，两次提交可能都发生在
    续跑之前，行上的「了结时提交次数」会被并发的另一次提交顶高，分不清
    「重复提交」与「手工重跑」。台账按键记次数，判据与并发无关。
    """
    resolve_key = resolve_key_for(snapshot.snapshot_id, fills)
    record = ResolveKeyLedger(database).find(resolve_key)
    if record is not None and record.applied:
        if not record.duplicate:
            # 只提交过一次 —— 手工重跑，不拿旧结论顶
            return None
        return _outcome_from_ledger(database, snapshot, fills, record)
    return _legacy_previous_outcome(database, snapshot, fills, resolve_key)


def resume_pending(
    database: Database,
    *,
    pending_id: int,
    baseline: ReportBaseline | None,
    thresholds: MatchThresholds | None = None,
    guard: Any | None = None,
    max_attempts: int = 3,
    lexicon: Lexicon | None = None,
    lock_ttl: float = DEFAULT_LOCK_TTL,
    lock_owner: str | None = None,
) -> ResumeOutcome:
    """从挂起的那一步接着跑。

    ``baseline`` 是这份报告的基线（部位清单与已申请项目）—— 重新对齐要靠它。
    给了就只重跑**这一条**结果，不碰其它挂起项，也不重跑整批回报。

    **重复提交是幂等的**：同一组补齐值再提交一次，返回上一次的结论，
    **不产出第二份草稿**（技术员点两下「补齐并续跑」不该让报告里多一条）。

    **同一行只让一个人跑**：续跑前先抢这一行的互斥锁（见
    :mod:`patho_supplement.pending.lock`）。没抢到说明别人正在处理这一行 ——
    抛 :class:`PendingLockedError`，让对方跑完再重试。补齐值在锁之外登记，
    因此**不会丢**。
    """
    store = PendingStore(database)
    # 行在不在：与锁无关，先说清楚（「没有这一行」和「这一行正被别人处理」
    # 是两回事，不能混成一句）。
    exists = database.query_one(
        "SELECT id FROM pending_items WHERE id = ?", (pending_id,)
    )
    if exists is None:
        raise ResumeError(f"状态库里没有第 {pending_id} 行挂起项")

    lock = PendingLock(database, ttl=lock_ttl, owner=lock_owner)
    acquired = lock.acquire(pending_id)
    if not acquired.acquired:
        holder = acquired.holder
        detail = (
            f"第 {pending_id} 行挂起项正在被处理中"
            + (
                f"（占用者 {holder.owner or '未知'}，占用时间 {holder.locked_at}）"
                if holder is not None
                else ""
            )
            + "；已登记的补齐值不会丢，等对方跑完直接重试即可"
        )
        record_audit(
            database,
            category=CATEGORY_PENDING,
            event=EVENT_PENDING_LOCK_CONFLICT,
            detail={
                "pending_id": pending_id,
                "holder": holder.as_dict() if holder is not None else None,
                "lock_ttl": lock_ttl,
            },
        )
        raise PendingLockedError(detail, pending_id=pending_id)

    token = acquired.token or ""
    try:
        if acquired.stale:
            # 接管了一把过期锁：上一个持锁的进程没释放，留一行说明。
            record_audit(
                database,
                category=CATEGORY_PENDING,
                event=EVENT_PENDING_LOCK_TAKEN_OVER,
                detail={
                    "pending_id": pending_id,
                    "previous_holder": (
                        acquired.holder.as_dict() if acquired.holder is not None else None
                    ),
                    "lock_ttl": lock_ttl,
                },
            )
        # 取行即校验：快照缺节点就报错，不静默跳过，也不当成已对齐。
        snapshot = _load_snapshot(database, store, pending_id)

        fills = tuple(
            FieldFill(field_name=name, value=value)
            for name, value in snapshot.fill_map.items()
        )

        # 状态校验：已了结的行不再续跑。了结意味着这条结果已经并进过草稿，
        # 再跑一遍就会把同一条结果**第二次**并进去 —— 草稿里出现重复条目。
        if snapshot.resolved:
            previous = _previous_resolve_outcome(database, snapshot, fills)
            if previous is not None:
                # 幂等命中：这是同一次补齐的重放，返回上次的结论，不并第二份草稿。
                return previous
            raise ResumeError(
                f"第 {pending_id} 行挂起项已了结（补齐后已并入草稿），不再续跑"
            )

        entry = snapshot.effective_entry
        if entry is None:  # pragma: no cover - 完整性校验已挡住这一支
            raise ResumeError(f"第 {pending_id} 行挂起项里没有可重算的原始结果条目")

        outcome = align_entry(
            entry,
            baseline,
            thresholds=thresholds,
            lexicon=lexicon,
            guard=guard,
        )

        if outcome.matched and outcome.candidate is not None:
            return merge_into_draft(
                database,
                store=store,
                snapshot=snapshot,
                entry=entry,
                candidate=outcome.candidate,
                baseline=baseline,
                fills=fills,
            )
        return keep_pending(
            database,
            store=store,
            snapshot=snapshot,
            entry=entry,
            outcome=outcome,
            fills=fills,
            max_attempts=max_attempts,
        )
    finally:
        # 无论成没成，锁都要放掉 —— 锁不放，这一行就没人能再动了。
        lock.release(pending_id, token)


def merge_into_draft(
    database: Database,
    *,
    store: PendingStore,
    snapshot: PendingSnapshot,
    entry: ResultEntry,
    candidate: MatchCandidate,
    baseline: ReportBaseline | None,
    fills: tuple[FieldFill, ...],
) -> ResumeOutcome:
    """对上了：并入草稿，产出新一版（旧版本链保留）。"""
    pairs = _rebuild_pairs(database, snapshot.accession_no)
    pairs.append((entry, candidate))
    version = next_draft_version(
        database,
        snapshot.accession_no,
        baseline.base_version if baseline is not None else None,
    )
    # 未到项占位重新派生：补齐了一项，剩下的「待回报」照旧占着，补齐完的那项
    # 自动不再占位。占位不是搬上一版的 —— 它是「已申请 − 已回报」算出来的。
    reported = [pair_entry for pair_entry, _ in pairs]
    open_snapshots = store.load_all(
        states=[PENDING_STATE_PENDING, PENDING_STATE_ESCALATED]
    ).snapshots
    for open_snapshot in open_snapshots:
        if open_snapshot.accession_no != snapshot.accession_no:
            continue
        if open_snapshot.effective_entry is not None:
            reported.append(open_snapshot.effective_entry)
    queries = PendingQueryStore(database).load(snapshot.accession_no)
    awaiting = derive_awaiting(baseline, queries, reported=reported)
    draft = render_draft(
        snapshot.accession_no,
        pairs,
        baseline=baseline,
        awaiting=awaiting,
        version=version,
    )
    draft_id = persist_draft(database, draft)

    record_alignment_audit(
        database,
        build_alignment(entry, candidate),
        event=EVENT_ALIGNMENT_RATIONALE,
    )
    resolve_key = resolve_key_for(snapshot.snapshot_id, fills) if fills else None
    store.mark_resolved(
        snapshot.snapshot_id,
        resolve_key=resolve_key,
    )
    if resolve_key:
        # 幂等台账：记下这一组补齐值生效到了哪一版草稿。重复提交时按这把键
        # 直接返回本结论，不再并第二份草稿。
        ResolveKeyLedger(database).record_outcome(
            resolve_key,
            pending_id=snapshot.snapshot_id,
            accession_no=snapshot.accession_no,
            item_code=snapshot.item_code,
            status=RESOLVE_OUTCOME_MERGED,
            draft_id=draft_id,
            draft_version=version,
            detail={
                "match_level": candidate.match_level.value,
                "confidence": candidate.confidence,
                "entry_count": len(draft.entries),
            },
        )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_PENDING_RESOLVED,
        accession_no=snapshot.accession_no,
        detail={
            "pending_id": snapshot.snapshot_id,
            "item_code": snapshot.item_code,
            "draft_id": draft_id,
            "draft_version": version,
            "match_level": candidate.match_level.value,
            "confidence": candidate.confidence,
            "fills": [fill.as_dict() for fill in fills],
        },
    )
    return ResumeOutcome(
        pending_id=snapshot.snapshot_id,
        accession_no=snapshot.accession_no,
        item_code=snapshot.item_code,
        status=RESOLVE_STATUS_MERGED,
        match_level=candidate.match_level.value,
        confidence=candidate.confidence,
        attempts=snapshot.attempts,
        draft_version=version,
        draft_id=draft_id,
        draft_entry_count=draft.merged_count,
        draft_placeholder_count=draft.placeholder_count,
        fills=fills,
    )


def keep_pending(
    database: Database,
    *,
    store: PendingStore,
    snapshot: PendingSnapshot,
    entry: ResultEntry,
    outcome: Any,
    fills: tuple[FieldFill, ...],
    max_attempts: int,
) -> ResumeOutcome:
    """还是对不上：挂起行原地重拍（尝试次数 +1），继续等人。

    **不硬绑**：补齐值造成同分歧义时，走的还是挂起 —— 候选部位全部记下，
    等人指认是哪一块。
    """
    reason = classify_reason(entry, outcome)
    missing = _remaining_fields(outcome, reason, snapshot.fill_map)
    record = PendingRecord.from_entry(
        entry,
        reason,
        missing_fields=missing,
        candidates=outcome.candidates,
    )
    updated = store.restamp(
        snapshot.snapshot_id, record=record, max_attempts=max_attempts
    )

    record_alignment_audit(
        database,
        build_pending_alignment(entry, outcome),
        event=EVENT_ALIGNMENT_PENDING,
    )
    record_audit(
        database,
        category=CATEGORY_PENDING,
        event=EVENT_PENDING_RESUME_FAILED,
        accession_no=snapshot.accession_no,
        detail={
            "pending_id": snapshot.snapshot_id,
            "item_code": snapshot.item_code,
            "reason": reason.value,
            "state": outcome.state,
            "missing_fields": list(missing),
            "candidates": len(outcome.candidates),
            "attempts": updated.attempts,
            "escalated": updated.escalated,
            "fills": [fill.as_dict() for fill in fills],
        },
    )
    return ResumeOutcome(
        pending_id=updated.snapshot_id,
        accession_no=updated.accession_no,
        item_code=updated.item_code,
        status=RESOLVE_STATUS_STILL_PENDING,
        reason=reason.value,
        missing_fields=tuple(updated.missing_fields),
        candidates=outcome.candidates,
        attempts=updated.attempts,
        escalated=updated.escalated,
        fills=fills,
    )
