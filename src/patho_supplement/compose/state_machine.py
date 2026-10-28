"""业务状态机：草稿四态与检查项八态的合法迁移表。

院内的业务对象都有生命周期，而且**生命周期不能乱**：

- **草稿四态**：待生成 → 已生成 → 已回写 → 已签发。签发由院内系统回写，
  本系统只读；已签发的草稿不能退回「已生成」 —— 医师签过的东西不能被程序
  悄悄改回去。
- **检查项八态**：待回报 → 已回报 → 已对齐 → 已并入；旁路「待人工」（挂起等人
  补齐）；订单控制侧另有已取消 / 已暂停 / 已终止。

**非法迁移一律拒绝并记审计行**，不回退、不猜测。这是本产品「失败不静默改
数据」这条底线在状态层的落点：状态只许沿着合法路径往前走，任何「倒退」、
「跳过中间态直达终态以外」、「复活已终止项」的尝试都被挡下，并留下一行
可查的痕迹说明「谁、什么时候、想从哪一态到哪一态、被拒了」。

**审计行数 = 迁移尝试次数**：每一次迁移尝试（无论接受还是拒绝）恰好写一行
审计流水，分类 ``state``，事件 ``state_transition``（接受）或
``state_transition_rejected``（拒绝）。这样「状态迁移审计行数 = 变更次数」
这条验收口径可以直接对账 —— 库里有多少行状态审计，就说明有过多少次状态变更
企图。

**拒绝有两种用法**：默认 ``strict=True`` 直接抛 :class:`StateTransitionError`
（让调用方当场停下来，不许带着非法状态继续往下走）；``strict=False`` 时
返回 ``accepted=False`` 的结果对象，状态原样不动，但审计行照写 —— 给「记下来
然后继续做别的事」的场景用（例如批量校验里只想统计有几处不合法）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..align.normalizer import normalize_block_no
from ..audit.ledger import (
    CATEGORY_STATE,
    EVENT_STATE_TRANSITION,
    EVENT_STATE_TRANSITION_REJECTED,
    record_audit,
)
from ..db import Database
from ..models import CheckItemState, DraftState
from ..timefmt import now_text

__all__ = [
    "CHECK_ITEM_TRANSITIONS",
    "DRAFT_TRANSITIONS",
    "DRAFT_TERMINAL_STATES",
    "CHECK_ITEM_TERMINAL_STATES",
    "StateTransitionError",
    "TransitionOutcome",
    "allowed_check_item_targets",
    "allowed_draft_targets",
    "can_transition_check_item",
    "can_transition_draft",
    "describe_check_item_transitions",
    "describe_draft_transitions",
    "load_check_item_state",
    "register_check_item",
    "transition_check_item_state",
    "transition_draft_state",
]


#: 草稿合法迁移表：允许的迁移对显式枚举，不靠 if/else 推断。
#:
#: - 待生成 → 已生成：成稿那一刻；
#: - 已生成 → 已回写：病理系统草稿区接受之后；
#: - 已回写 → 已签发：由院内系统回写，本系统只读；
#: - 已签发是终态：签过的东西不退回。
DRAFT_TRANSITIONS: dict[DraftState, frozenset[DraftState]] = {
    DraftState.PENDING: frozenset({DraftState.GENERATED}),
    DraftState.GENERATED: frozenset({DraftState.WRITTEN_BACK}),
    DraftState.WRITTEN_BACK: frozenset({DraftState.SIGNED}),
    DraftState.SIGNED: frozenset(),
}

#: 草稿终态集合（到了就不能再动）。
DRAFT_TERMINAL_STATES: frozenset[DraftState] = frozenset(
    state for state, targets in DRAFT_TRANSITIONS.items() if not targets
)

#: 检查项合法迁移表：
#:
#: - 待回报 → 已回报 / 已对齐：结果回来了；对上了就直接对齐（一步到位）；
#: - 已回报 → 已对齐 / 待人工：回报了但对不上部位，挂起等人判；
#: - 已对齐 → 已并入 / 待人工：并入草稿，或又冒出歧义退回人工；
#: - 待人工 → 已对齐 / 已并入：人工补齐后接回来（补齐路径的主线）；
#: - 已暂停 → 待回报：暂停的申请恢复等待；
#: - 已取消 → 已终止：取消之后再终止；
#: - 已并入、已终止是终态：进了草稿的项与死掉的项都不再复活。
CHECK_ITEM_TRANSITIONS: dict[CheckItemState, frozenset[CheckItemState]] = {
    CheckItemState.AWAITING: frozenset({
        CheckItemState.RETURNED,
        CheckItemState.ALIGNED,
        CheckItemState.MANUAL,
        CheckItemState.CANCELLED,
        CheckItemState.HELD,
        CheckItemState.TERMINATED,
    }),
    CheckItemState.RETURNED: frozenset({
        CheckItemState.ALIGNED,
        CheckItemState.MANUAL,
        CheckItemState.CANCELLED,
        CheckItemState.HELD,
        CheckItemState.TERMINATED,
    }),
    CheckItemState.ALIGNED: frozenset({
        CheckItemState.MERGED,
        CheckItemState.MANUAL,
    }),
    CheckItemState.MANUAL: frozenset({
        CheckItemState.ALIGNED,
        CheckItemState.MERGED,
        CheckItemState.CANCELLED,
        CheckItemState.TERMINATED,
    }),
    CheckItemState.HELD: frozenset({
        CheckItemState.AWAITING,
        CheckItemState.CANCELLED,
        CheckItemState.TERMINATED,
    }),
    CheckItemState.CANCELLED: frozenset({CheckItemState.TERMINATED}),
    CheckItemState.MERGED: frozenset(),
    CheckItemState.TERMINATED: frozenset(),
}

#: 检查项终态集合。
CHECK_ITEM_TERMINAL_STATES: frozenset[CheckItemState] = frozenset(
    state for state, targets in CHECK_ITEM_TRANSITIONS.items() if not targets
)

#: 实体中文名（报错信息里用）。
_ENTITY_LABELS = {"draft": "草稿", "check_item": "检查项"}


class StateTransitionError(RuntimeError):
    """非法状态迁移。

    错误信息里必须带齐三样：**当前态**、**目标态**、**被拒原因** —— 排查的人
    拿到这一行就能知道「本来该在哪、想去哪、为什么不行」，不用去翻代码。
    """

    def __init__(
        self,
        *,
        entity: str,
        key: str,
        current: str,
        target: str,
        reason: str,
        allowed: Sequence[str] = (),
    ) -> None:
        self.entity = entity
        self.key = key
        self.current = current
        self.target = target
        self.reason = reason
        self.allowed = tuple(allowed)
        super().__init__(self._render())

    def _render(self) -> str:
        label = _ENTITY_LABELS.get(self.entity, self.entity)
        head = f"{label} {self.key}：{self.reason}"
        tail = f"（当前态「{self.current}」→ 目标态「{self.target}」）"
        if self.allowed:
            tail += f"；合法下一步：{'、'.join(self.allowed)}"
        return head + tail

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "key": self.key,
            "current": self.current,
            "target": self.target,
            "reason": self.reason,
            "allowed": list(self.allowed),
        }


@dataclass(frozen=True)
class TransitionOutcome:
    """一次迁移尝试的结果。"""

    entity: str
    key: str
    current: str
    target: str
    accepted: bool
    reason: str
    allowed: tuple[str, ...] = ()
    row_id: int | None = None

    @property
    def rejected(self) -> bool:
        return not self.accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "key": self.key,
            "current": self.current,
            "target": self.target,
            "accepted": self.accepted,
            "reason": self.reason,
            "allowed": list(self.allowed),
            "row_id": self.row_id,
        }


# --------------------------------------------------------------- 迁移表查询


def allowed_draft_targets(current: DraftState) -> frozenset[DraftState]:
    """草稿从这一态可以合法走到哪几态。"""
    return DRAFT_TRANSITIONS.get(DraftState(current), frozenset())


def allowed_check_item_targets(current: CheckItemState) -> frozenset[CheckItemState]:
    """检查项从这一态可以合法走到哪几态。"""
    return CHECK_ITEM_TRANSITIONS.get(CheckItemState(current), frozenset())


def can_transition_draft(current: DraftState, target: DraftState) -> bool:
    """这次草稿迁移是否合法。"""
    return DraftState(target) in allowed_draft_targets(current)


def can_transition_check_item(current: CheckItemState, target: CheckItemState) -> bool:
    """这次检查项迁移是否合法。"""
    return CheckItemState(target) in allowed_check_item_targets(current)


def describe_draft_transitions() -> str:
    """草稿迁移表的可读文本（给 ``doctor`` 与报错信息用）。"""
    lines = ["草稿状态迁移表（合法下一步）："]
    for state, targets in DRAFT_TRANSITIONS.items():
        label = state.label
        if targets:
            lines.append(f"  {label} → {'、'.join(t.label for t in sorted(targets, key=lambda s: s.value))}")
        else:
            lines.append(f"  {label} → （终态，不可再迁移）")
    return "\n".join(lines)


def describe_check_item_transitions() -> str:
    """检查项迁移表的可读文本。"""
    lines = ["检查项状态迁移表（合法下一步）："]
    for state, targets in CHECK_ITEM_TRANSITIONS.items():
        label = state.label
        if targets:
            lines.append(f"  {label} → {'、'.join(t.label for t in sorted(targets, key=lambda s: s.value))}")
        else:
            lines.append(f"  {label} → （终态，不可再迁移）")
    return "\n".join(lines)


# --------------------------------------------------------------- 内部工具


def _labels(targets: Iterable[Any]) -> tuple[str, ...]:
    """把目标态集合转成中文标签元组（报错信息里直接可读）。"""
    return tuple(sorted((t.label for t in targets), key=lambda text: text))


def _norm_order(order_no: str | None) -> str:
    """申请号归一：空值统一成空串（库里 NULL 与空串是同一个意思）。"""
    return (order_no or "").strip()


def _same_block(left: str | None, right: str | None) -> bool:
    """蜡块号是否同一块（按归一化口径比，缺字段不算相同）。"""
    lnorm = normalize_block_no(left)
    rnorm = normalize_block_no(right)
    if lnorm is None or rnorm is None:
        return False
    return lnorm == rnorm


def _reject_reason(
    entity: str,
    current: Any,
    target: Any,
    allowed: Iterable[Any],
) -> str:
    """生成可读的拒绝原因（含当前态与目标态）。"""
    allowed_set = frozenset(allowed)
    if current in DRAFT_TERMINAL_STATES or current in CHECK_ITEM_TERMINAL_STATES:
        return f"当前状态「{current.label}」是终态，不能再迁移"
    if not allowed_set:
        return f"当前状态「{current.label}」没有可用的下一步"
    return (
        f"当前状态「{current.label}」不能迁移到「{target.label}」"
    )


def _audit_transition(
    database: Database,
    *,
    entity: str,
    key: str,
    current: Any,
    target: Any,
    accepted: bool,
    reason: str,
    allowed: Iterable[Any],
    accession_no: str | None,
    note: str | None,
    row_id: int | None,
) -> None:
    """写状态迁移审计行 —— 接受与拒绝各一行，一次尝试一行。"""
    detail = {
        "entity": entity,
        "key": key,
        "from_state": current.value,
        "to_state": target.value,
        "from_label": current.label,
        "to_label": target.label,
        "accepted": accepted,
        "reason": reason,
        "allowed": list(allowed),
    }
    if row_id is not None:
        detail["row_id"] = row_id
    if note:
        detail["note"] = note
    record_audit(
        database,
        category=CATEGORY_STATE,
        event=EVENT_STATE_TRANSITION if accepted else EVENT_STATE_TRANSITION_REJECTED,
        accession_no=accession_no,
        detail=detail,
    )


# --------------------------------------------------------------- 草稿状态


def transition_draft_state(
    database: Database,
    draft_id: int,
    target: DraftState,
    *,
    accession_no: str | None = None,
    note: str | None = None,
    strict: bool = True,
) -> TransitionOutcome:
    """把一份草稿推进到目标状态。

    非法迁移时：状态**原样不动**（不回退、不猜测），写一行
    ``state_transition_rejected`` 审计流水，并按 ``strict`` 决定是抛错还是
    返回 ``accepted=False`` 的结果。

    ``strict=True``（默认）抛 :class:`StateTransitionError` —— 调用方当场
    停下来，不许带着非法状态继续往下走。
    """
    target_state = DraftState(target)
    row = database.query_one(
        "SELECT id, accession_no, version, state FROM drafts WHERE id = ?",
        (draft_id,),
    )
    if row is None:
        current = DraftState.PENDING
        reason = "草稿行不存在，无法迁移"
        _audit_transition(
            database,
            entity="draft",
            key=str(draft_id),
            current=current,
            target=target_state,
            accepted=False,
            reason=reason,
            allowed=allowed_draft_targets(current),
            accession_no=accession_no,
            note=note,
            row_id=None,
        )
        error = StateTransitionError(
            entity="draft",
            key=str(draft_id),
            current=current.label,
            target=target_state.label,
            reason=reason,
            allowed=_labels(allowed_draft_targets(current)),
        )
        if strict:
            raise error
        return TransitionOutcome(
            entity="draft",
            key=str(draft_id),
            current=current.label,
            target=target_state.label,
            accepted=False,
            reason=reason,
            allowed=_labels(allowed_draft_targets(current)),
        )

    current_state = DraftState(row["state"])
    key = f"{row['accession_no']}#v{row['version']}"
    allowed = allowed_draft_targets(current_state)

    if target_state not in allowed:
        reason = _reject_reason("draft", current_state, target_state, allowed)
        _audit_transition(
            database,
            entity="draft",
            key=key,
            current=current_state,
            target=target_state,
            accepted=False,
            reason=reason,
            allowed=allowed,
            accession_no=accession_no or row["accession_no"],
            note=note,
            row_id=int(row["id"]),
        )
        error = StateTransitionError(
            entity="draft",
            key=key,
            current=current_state.label,
            target=target_state.label,
            reason=reason,
            allowed=_labels(allowed),
        )
        if strict:
            raise error
        return TransitionOutcome(
            entity="draft",
            key=key,
            current=current_state.label,
            target=target_state.label,
            accepted=False,
            reason=reason,
            allowed=_labels(allowed),
            row_id=int(row["id"]),
        )

    database.execute(
        "UPDATE drafts SET state = ? WHERE id = ?",
        (target_state.value, int(row["id"])),
    )
    _audit_transition(
        database,
        entity="draft",
        key=key,
        current=current_state,
        target=target_state,
        accepted=True,
        reason="迁移合法",
        allowed=allowed,
        accession_no=accession_no or row["accession_no"],
        note=note,
        row_id=int(row["id"]),
    )
    return TransitionOutcome(
        entity="draft",
        key=key,
        current=current_state.label,
        target=target_state.label,
        accepted=True,
        reason="迁移合法",
        allowed=_labels(allowed),
        row_id=int(row["id"]),
    )


# --------------------------------------------------------------- 检查项状态


def _find_check_item_rows(
    database: Database,
    *,
    accession_no: str,
    item_code: str,
    order_no: str | None,
    block_no: str | None,
) -> list[sqlite3.Row]:
    """按（病理号, 项目码）取候选行，再按申请号与蜡块号收窄。

    收窄口径与全系统一致：申请号按空值归一，蜡块号按归一化后的值比。
    精确命中蜡块号优先 —— 同项目在两块上各有一行时，按块能定位到的那一行
    才是要的。
    """
    rows = database.query_all(
        "SELECT id, accession_no, order_no, item_code, state, block_no, region_seq"
        " FROM check_items WHERE accession_no = ? AND item_code = ?",
        (accession_no, item_code),
    )
    if not rows:
        return []
    want_order = _norm_order(order_no)
    order_matched = [r for r in rows if _norm_order(r["order_no"]) == want_order]
    if not order_matched:
        return []
    if block_no is not None:
        exact = [r for r in order_matched if _same_block(r["block_no"], block_no)]
        if exact:
            return exact
    return order_matched


def register_check_item(
    database: Database,
    *,
    accession_no: str,
    item_code: str,
    order_no: str | None = None,
    block_no: str | None = None,
    region_seq: int | None = None,
    state: CheckItemState = CheckItemState.AWAITING,
) -> int:
    """登记一个检查项（初始态：待回报），返回行 ID。

    登记不是迁移 —— 它是这条检查项在状态机里的**起点**，所以不写状态审计行。
    """
    cursor = database.execute(
        "INSERT INTO check_items(accession_no, order_no, item_code, state,"
        " updated_at, block_no, region_seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            accession_no,
            order_no or None,
            item_code,
            CheckItemState(state).value,
            now_text(),
            block_no,
            region_seq,
        ),
    )
    return int(cursor.lastrowid)


def load_check_item_state(
    database: Database,
    *,
    accession_no: str,
    item_code: str,
    order_no: str | None = None,
    block_no: str | None = None,
) -> CheckItemState | None:
    """读某个检查项当前的状态；查不到给 ``None``。"""
    rows = _find_check_item_rows(
        database,
        accession_no=accession_no,
        item_code=item_code,
        order_no=order_no,
        block_no=block_no,
    )
    if len(rows) != 1:
        return None
    return CheckItemState(rows[0]["state"])


def transition_check_item_state(
    database: Database,
    *,
    accession_no: str,
    item_code: str,
    target: CheckItemState,
    order_no: str | None = None,
    block_no: str | None = None,
    region_seq: int | None = None,
    note: str | None = None,
    strict: bool = True,
    create: bool = True,
) -> TransitionOutcome:
    """把一个检查项推进到目标状态。

    定位口径与归属键一致：病理号 + 项目码，必要时用申请号与蜡块号收窄。

    - 行还不存在且 ``create=True``：先登记为「待回报」，再从待回报走迁移；
    - 命中多行（同一项目在多块上都有行，又没给蜡块号）：**不猜** —— 拒绝并
      说明候选是哪几行，请调用方把蜡块号补上；
    - 非法迁移：状态原样不动，写一行拒绝审计，按 ``strict`` 决定抛错还是返回。
    """
    target_state = CheckItemState(target)
    rows = _find_check_item_rows(
        database,
        accession_no=accession_no,
        item_code=item_code,
        order_no=order_no,
        block_no=block_no,
    )
    key = f"{accession_no}/{item_code}"

    if len(rows) > 1:
        current = CheckItemState(rows[0]["state"])
        reason = (
            f"同一病理号下项目「{item_code}」命中 {len(rows)} 行检查项，"
            "请补蜡块号或申请号指明是哪一项"
        )
        _audit_transition(
            database,
            entity="check_item",
            key=key,
            current=current,
            target=target_state,
            accepted=False,
            reason=reason,
            allowed=allowed_check_item_targets(current),
            accession_no=accession_no,
            note=note,
            row_id=None,
        )
        error = StateTransitionError(
            entity="check_item",
            key=key,
            current=current.label,
            target=target_state.label,
            reason=reason,
            allowed=_labels(allowed_check_item_targets(current)),
        )
        if strict:
            raise error
        return TransitionOutcome(
            entity="check_item",
            key=key,
            current=current.label,
            target=target_state.label,
            accepted=False,
            reason=reason,
            allowed=_labels(allowed_check_item_targets(current)),
        )

    if not rows:
        if not create:
            current = CheckItemState.AWAITING
            reason = f"检查项「{item_code}」尚未登记，无法迁移"
            _audit_transition(
                database,
                entity="check_item",
                key=key,
                current=current,
                target=target_state,
                accepted=False,
                reason=reason,
                allowed=allowed_check_item_targets(current),
                accession_no=accession_no,
                note=note,
                row_id=None,
            )
            error = StateTransitionError(
                entity="check_item",
                key=key,
                current=current.label,
                target=target_state.label,
                reason=reason,
                allowed=_labels(allowed_check_item_targets(current)),
            )
            if strict:
                raise error
            return TransitionOutcome(
                entity="check_item",
                key=key,
                current=current.label,
                target=target_state.label,
                accepted=False,
                reason=reason,
                allowed=_labels(allowed_check_item_targets(current)),
            )
        row_id = register_check_item(
            database,
            accession_no=accession_no,
            item_code=item_code,
            order_no=order_no,
            block_no=block_no,
            region_seq=region_seq,
        )
        current_state = CheckItemState.AWAITING
    else:
        row_id = int(rows[0]["id"])
        current_state = CheckItemState(rows[0]["state"])

    allowed = allowed_check_item_targets(current_state)
    if target_state not in allowed:
        reason = _reject_reason("check_item", current_state, target_state, allowed)
        _audit_transition(
            database,
            entity="check_item",
            key=key,
            current=current_state,
            target=target_state,
            accepted=False,
            reason=reason,
            allowed=allowed,
            accession_no=accession_no,
            note=note,
            row_id=row_id,
        )
        error = StateTransitionError(
            entity="check_item",
            key=key,
            current=current_state.label,
            target=target_state.label,
            reason=reason,
            allowed=_labels(allowed),
        )
        if strict:
            raise error
        return TransitionOutcome(
            entity="check_item",
            key=key,
            current=current_state.label,
            target=target_state.label,
            accepted=False,
            reason=reason,
            allowed=_labels(allowed),
            row_id=row_id,
        )

    database.execute(
        "UPDATE check_items SET state = ?, updated_at = ? WHERE id = ?",
        (target_state.value, now_text(), row_id),
    )
    _audit_transition(
        database,
        entity="check_item",
        key=key,
        current=current_state,
        target=target_state,
        accepted=True,
        reason="迁移合法",
        allowed=allowed,
        accession_no=accession_no,
        note=note,
        row_id=row_id,
    )
    return TransitionOutcome(
        entity="check_item",
        key=key,
        current=current_state.label,
        target=target_state.label,
        accepted=True,
        reason="迁移合法",
        allowed=_labels(allowed),
        row_id=row_id,
    )
