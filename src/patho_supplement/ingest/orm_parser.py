"""申请侧报文解析（ORM^O01）。

一条 ``ORM^O01`` 说明「某份报告的某块上又加做了哪几项检查，以及这些检查现在还
做不做」。它是**申请侧**事件流：结果侧是 ``ORU^R01``（回报了什么），申请侧是
``ORM^O01``（要做什么、还要不要做）。

字段位按 HL7 v2 第 5 章报文结构落位，与结果侧同一套口径：

===========================  ==============================================
字段                         含义
===========================  ==============================================
``ORC-1``                    订单控制动作（``NW`` 新增 / ``CA`` 取消 /
                             ``HD`` 暂停 / ``DC`` 终止 / ``RE`` 结果随后）
``OBR-2``                    病理号
``OBR-3``                    加做申请号
``OBR-4``                    申请项目码（组件 1 为码，组件 2 为名称）
``OBR-7``                    申请时间
``ZBR-1/2/3``                蜡块号 / 取材部位描述 / 部位序号（可选）
===========================  ==============================================

**坏的一条不拖累好的一条**：一份报文里可能有多组 ``ORC``/``OBR``，其中一组写错了
（控制动作非法、缺申请号、缺项目码），只拒收**这一条**并记审计，其余照常解析。
整份报文报废会让一批加做全部停摆，那比拒一条糟糕得多。

订单控制动作 → 检查项状态：

===========================  ==============================================
动作                         检查项状态
===========================  ==============================================
``NW`` 新增                 待回报
``CA`` 取消                 已取消
``HD`` 暂停                 已暂停
``DC`` 终止                 已终止
``RE`` 结果随后             待回报（订单已确认、结果将至）
===========================  ==============================================

``RE`` 与 ``NW`` 同落「待回报」：两者都在等结果，区别只在动作本身 —— 动作保留在
:class:`~patho_supplement.models.OrderRequest` 与审计流水里，不另立第七个检查项
状态（检查项状态表以 ``spec.md`` 为准）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..audit.ledger import CATEGORY_INGEST, EVENT_ORDER_REJECTED, record_audit
from ..db import Database
from ..models import CheckItemState, OrderAction, OrderRequest
from .er7 import Er7ParseError, Message, Segment
from .zbr import ZbrInfo, parse_zbr

__all__ = [
    "ACTION_TO_ITEM_STATE",
    "OrderCounters",
    "OrderIngestor",
    "OrderIngestOutcome",
    "OrderParseResult",
    "RejectedOrder",
    "build_order_event_key",
    "item_state_for_action",
    "parse_orm",
]

#: 允许的 ``ORC-1`` 订单控制动作（键为报文里的原始值，已去空白并转大写）。
_ALLOWED_ACTIONS: dict[str, OrderAction] = {action.value: action for action in OrderAction}

#: 订单控制动作 → 检查项状态。
ACTION_TO_ITEM_STATE: dict[OrderAction, CheckItemState] = {
    OrderAction.NEW: CheckItemState.AWAITING,
    OrderAction.CANCEL: CheckItemState.CANCELLED,
    OrderAction.HOLD: CheckItemState.HELD,
    OrderAction.TERMINATE: CheckItemState.TERMINATED,
    # RE（结果随后）：订单已确认、结果将至 —— 仍在「待回报」态。
    OrderAction.RESULT_FOLLOWS: CheckItemState.AWAITING,
}

#: 申请侧事件的幂等键分隔符（与结果侧一致：ASCII 单元分隔符）。
_KEY_SEP = "\x1f"


def item_state_for_action(action: OrderAction) -> CheckItemState:
    """按订单控制动作取检查项状态。"""
    return ACTION_TO_ITEM_STATE[action]


def build_order_event_key(order: OrderRequest) -> str:
    """申请侧事件的幂等键。

    键 = 病理号 + 加做申请号 + 项目码 + **控制动作**。

    前三段标识「哪个检查项」，第四段是这次事件的动作。动作必须带进键里：
    同一个检查项先 ``NW``（新增）后 ``CA``（取消）是**两件事** ——
    只按前三段去重会把取消当成重复投递吞掉，那一项就永远取消不掉。
    「重复投递」指同一条事件再投一次，那时动作自然相同，键自然撞上。
    """
    return _KEY_SEP.join(
        (order.accession_no, order.order_no or "", order.item_code, order.action.value)
    )


@dataclass(frozen=True)
class RejectedOrder:
    """被拒收的一条申请侧条目。

    拒收原因分两类：``reason`` 是机器可读的短码（便于统计与检索），
    ``detail`` 是给技术人员看的原话（含具体字段与取值）。
    """

    reason: str
    detail: str
    segment: str | None = None
    line_no: int | None = None
    source: str | None = None
    accession_no: str | None = None
    order_no: str | None = None
    item_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "segment": self.segment,
            "line_no": self.line_no,
            "source": self.source,
            "accession_no": self.accession_no,
            "order_no": self.order_no,
            "item_code": self.item_code,
        }


@dataclass
class OrderParseResult:
    """一份申请侧报文的解析结果。

    ``orders`` 是通过校验的申请条目；``rejected`` 是被拒收的条目（逐条带原因）。
    """

    orders: list[OrderRequest] = field(default_factory=list)
    rejected: list[RejectedOrder] = field(default_factory=list)

    @property
    def malformed(self) -> int:
        """被拒收的条目数（畸形条目计数）。"""
        return len(self.rejected)


@dataclass
class OrderCounters:
    """申请侧入站计数。三类不能混：首次接收、重复投递、畸形拒收。"""

    accepted: int = 0
    duplicate: int = 0
    malformed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "malformed": self.malformed,
        }


@dataclass
class OrderIngestOutcome:
    """一轮申请侧入站的产出。"""

    orders: list[OrderRequest] = field(default_factory=list)
    rejected: list[RejectedOrder] = field(default_factory=list)
    accepted: int = 0
    duplicate: int = 0
    malformed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "malformed": self.malformed,
            "orders": [
                {
                    "accession_no": order.accession_no,
                    "order_no": order.order_no,
                    "item_code": order.item_code,
                    "action": order.action.value,
                    "state": item_state_for_action(order.action).value,
                }
                for order in self.orders
            ],
            "rejected": [rejected.to_dict() for rejected in self.rejected],
        }


@dataclass
class _OrderGroup:
    """报文里的一组 ``ORC`` / ``OBR``（一次加做申请）。"""

    orc: Segment
    action_raw: str
    obr: Segment | None = None
    zbr: ZbrInfo | None = None
    zbr_error: Er7ParseError | None = None


def _reject(
    reason: str,
    detail: str,
    source: str | None,
    *,
    segment: str | None = None,
    line_no: int | None = None,
    accession_no: str | None = None,
    order_no: str | None = None,
    item_code: str | None = None,
) -> RejectedOrder:
    """造一条拒收记录。"""
    return RejectedOrder(
        reason=reason,
        detail=detail,
        segment=segment,
        line_no=line_no,
        source=source,
        accession_no=accession_no,
        order_no=order_no,
        item_code=item_code,
    )


def _reject_segment(
    reason: str, detail: str, segment: Segment, source: str | None, **extra: Any
) -> RejectedOrder:
    """造一条与某个段相关的拒收记录。"""
    return _reject(
        reason,
        detail,
        source,
        segment=segment.name,
        line_no=segment.line_no,
        **extra,
    )


def _build_order(
    group: _OrderGroup, message: Message, source: str | None
) -> tuple[OrderRequest | None, RejectedOrder | None]:
    """校验一组申请，产出申请条目或拒收记录。

    校验顺序按「先粗后细」：控制动作 → 有没有 ``OBR`` → 病理号 → 申请号 → 项目码。
    前面缺了，后面的字段读出来也没意义。
    """
    if group.zbr_error is not None:
        return None, _reject(
            "invalid_zbr",
            f"ZBR 段不可解析：{group.zbr_error.detail}",
            source,
            segment=group.zbr_error.segment or "ZBR",
            line_no=group.zbr_error.line_no,
        )

    action_raw = group.action_raw.strip().upper()
    if not action_raw:
        return None, _reject_segment(
            "missing_control_action",
            "ORC-1 订单控制动作为空",
            group.orc,
            source,
        )
    action = _ALLOWED_ACTIONS.get(action_raw)
    if action is None:
        return None, _reject_segment(
            "illegal_control_action",
            f"ORC-1 订单控制动作取值非法：{group.action_raw.strip()!r}"
            "（允许 NW / CA / HD / DC / RE）",
            group.orc,
            source,
        )

    if group.obr is None:
        return None, _reject_segment(
            "missing_obr",
            "ORC 段没有对应的 OBR 段",
            group.orc,
            source,
        )

    obr = group.obr
    accession_no = obr.field(2).strip()
    order_no = obr.field(3).strip()
    item_code = obr.component(4, 1).strip()
    item_name = obr.component(4, 2).strip()
    order_time = obr.field(7).strip()

    if not accession_no:
        return None, _reject_segment(
            "missing_accession_no",
            "OBR-2 病理号为空，无法归属报告",
            obr,
            source,
        )
    if not order_no:
        return None, _reject_segment(
            "missing_order_no",
            "OBR-3 加做申请号为空，无法与检查项对应",
            obr,
            source,
            accession_no=accession_no,
        )
    if not item_code:
        return None, _reject_segment(
            "missing_item_code",
            "OBR-4 申请项目码为空",
            obr,
            source,
            accession_no=accession_no,
            order_no=order_no,
        )

    zbr = group.zbr or ZbrInfo()
    return (
        OrderRequest(
            accession_no=accession_no,
            order_no=order_no,
            item_code=item_code,
            action=action,
            item_name=item_name or None,
            order_time=order_time or None,
            region_seq=zbr.region_seq,
            block_no=zbr.block_no,
            region_desc=zbr.region_desc,
            source_system=message.sending_application or "pathology-system",
        ),
        None,
    )


def _close_group(
    group: _OrderGroup,
    message: Message,
    source: str | None,
    result: OrderParseResult,
) -> None:
    """一组申请读完，收尾：要么成条目，要么成拒收记录。"""
    order, rejected = _build_order(group, message, source)
    if order is not None:
        result.orders.append(order)
    elif rejected is not None:
        result.rejected.append(rejected)


def parse_orm(message: Message, *, source_file: str | None = None) -> OrderParseResult:
    """把 ``ORM^O01`` 报文解析成申请条目。

    报文类型不对直接报错（调用方按报文类型分流）；报文类型对但**某一条**写错了，
    那一条进 ``rejected``，其余照常返回。
    """
    if message.message_code.upper() != "ORM" or message.message_structure.upper() != "O01":
        raise Er7ParseError(
            f"不是 ORM^O01 报文（MSH-9={message.message_type!r}）",
            segment="MSH",
            line_no=message.msh.line_no,
            source=message.source,
            code="not_orm",
        )

    # 拒收记录要指明「哪个文件带来的」：报文自带来源时用它，
    # 没有时退回调用方给的报文文件名。
    source = message.source or source_file

    result = OrderParseResult()
    group: _OrderGroup | None = None

    for segment in message.segments[1:]:
        if segment.name == "ORC":
            if group is not None:
                _close_group(group, message, source, result)
            group = _OrderGroup(orc=segment, action_raw=segment.field(1))
            continue
        if segment.name == "OBR":
            if group is None:
                result.rejected.append(
                    _reject_segment(
                        "missing_orc",
                        "OBR 段没有对应的 ORC 段（取不到控制动作）",
                        segment,
                        source,
                    )
                )
                continue
            group.obr = segment
            continue
        if segment.name == "ZBR":
            if group is None:
                result.rejected.append(
                    _reject_segment(
                        "missing_orc",
                        "ZBR 段没有对应的 ORC 段",
                        segment,
                        source,
                    )
                )
                continue
            try:
                group.zbr = parse_zbr(segment)
            except Er7ParseError as exc:
                group.zbr_error = exc
            continue
        # 其余段（PID / PV1 / NTE …）本产品用不到，属于正常报文内容，跳过即可。

    if group is not None:
        _close_group(group, message, source, result)
    elif not result.orders and not result.rejected:
        result.rejected.append(
            _reject(
                "empty_message",
                "ORM^O01 报文里没有 ORC / OBR 段，没有可解析的申请",
                source,
                segment="MSH",
                line_no=message.msh.line_no,
            )
        )

    return result


class OrderIngestor:
    """申请侧入站处理：解析 → 去重 → 拒收记审计。

    审计只在给了状态库时才写：批处理重放（``replay``）默认不落库，
    守护进程（有状态库）才会把拒收原因写进 ``audit_log``。
    """

    def __init__(self, database: Database | None = None) -> None:
        self._db = database
        self._seen: set[str] = set()
        self.counters = OrderCounters()

    def ingest(self, message: Message, *, source_file: str | None = None) -> OrderIngestOutcome:
        """处理一条 ``ORM^O01`` 报文。

        返回本次**新增**的申请条目（重复投递的不重复给）与被拒收的条目。
        """
        parsed = parse_orm(message, source_file=source_file)
        outcome = OrderIngestOutcome(rejected=list(parsed.rejected))

        for rejected in parsed.rejected:
            self.counters.malformed += 1
            outcome.malformed += 1
            self._audit_rejection(rejected)

        for order in parsed.orders:
            key = build_order_event_key(order)
            if key in self._seen:
                self.counters.duplicate += 1
                outcome.duplicate += 1
                continue
            self._seen.add(key)
            self.counters.accepted += 1
            outcome.accepted += 1
            outcome.orders.append(order)

        return outcome

    def _audit_rejection(self, rejected: RejectedOrder) -> None:
        """把一条拒收写进审计流水。"""
        if self._db is None:
            return
        record_audit(
            self._db,
            category=CATEGORY_INGEST,
            event=EVENT_ORDER_REJECTED,
            accession_no=rejected.accession_no,
            detail=rejected.to_dict(),
        )
