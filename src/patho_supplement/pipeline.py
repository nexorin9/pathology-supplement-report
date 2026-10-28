"""图式装配：轻量节点表 + 两道闸门推进。

**这一层在源产品主路径上的位置**：源实现
``github_ref/haystack/haystack/core/pipeline/component_checks.py::can_component_run``
L41-54 规定一个组件要能跑，必须**同时**过两道闸门::

    received_all_mandatory_inputs = are_all_sockets_ready(component, inputs, only_check_mandatory=True)
    received_trigger = has_any_trigger(component, inputs)
    return received_all_mandatory_inputs and received_trigger

``has_any_trigger`` L57-78 给出三个触发来源（前驱喂入 / 图外输入 / 无前驱首跑），
并写明一条硬规矩：**一个触发源只能让组件执行一次** —— 因为组件执行前会消费
前驱的输入（输入被删掉），图外输入与首跑只在 ``visits == 0`` 时成立。

本模块把这两条语义搬进医院产品。载体从「组件 + socket」换成「节点 + 键」，
**不引入 networkx**：节点表是八个具名节点，每个声明自己的必需输入、可选输入、
图外输入与产出键；推进靠上面两道闸门。

**为什么要把闸门做成硬约束**：本产品有一条不让步的红线 —— **没取回基线就不许
对齐**。基线没到手时，对齐引擎只能把每一条结果都判成「对不上」，那不是判断，
是瞎猜；瞎猜的结果会进草稿，草稿会进病理系统，医师会照着它签发。所以基线
取不到时，节点表在 ``align`` 之前就把这条路**停住**，并把「缺哪几个输入」记进
跳过流水 —— 停住要看得见，默默停住和不停住一样危险。

**同一事件不二次推进**：一次投递 = 一个事件（事件号由调用方给）。同一个事件号
再推一次，节点表里每个节点的**访问计数只增一次** —— 第二次推同一个事件不会
让任何节点再跑一遍。这条规矩对应源实现里「组件执行前消费输入」的效果：源产品
靠删输入实现一次触发，本产品靠**按事件记账**实现，两者的可观察结论一致
（重复触发不产生第二次副作用）。

**触发源只有图外输入与定时器**：整张图只有两个入口 —— 入站事件文件（图外
输入）与守护进程的周期兜底扫描（定时器）。节点之间互相喂数不算触发源，那是
图内的推进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "NODE_ALIGN",
    "NODE_COMPOSE",
    "NODE_DERIVE_ORDERS",
    "NODE_FETCH_BASELINE",
    "NODE_FANOUT_RETRIEVE",
    "NODE_INGEST",
    "NODE_NOTIFY",
    "NODE_SPECS",
    "NODE_WRITEBACK",
    "NodeSpec",
    "Pipeline",
    "PipelineRun",
    "PipelineState",
    "REASON_ALREADY_VISITED",
    "REASON_MISSING_INPUTS",
    "REASON_NODE_ERROR",
    "REASON_NOT_TRIGGERED",
    "TRIGGER_EXTERNAL",
    "TRIGGER_PREDECESSOR",
    "TRIGGER_TIMER",
    "SkipRecord",
    "are_all_sockets_ready",
    "has_any_trigger",
]


# ------------------------------------------------------------------ 节点名
NODE_INGEST = "ingest"
NODE_FETCH_BASELINE = "fetch_baseline"
NODE_DERIVE_ORDERS = "derive_orders"
NODE_FANOUT_RETRIEVE = "fanout_retrieve"
NODE_ALIGN = "align"
NODE_COMPOSE = "compose"
NODE_WRITEBACK = "writeback"
NODE_NOTIFY = "notify"


# ------------------------------------------------------------ 触发来源标签
#: 前驱节点喂了输入进来。
TRIGGER_PREDECESSOR = "前驱喂入"
#: 图外输入（入站事件文件 / 外部给的数据）。
TRIGGER_EXTERNAL = "图外输入"
#: 定时器（守护进程的周期兜底扫描）把图叫起来。
TRIGGER_TIMER = "定时器"


# ------------------------------------------------------------ 跳过原因标签
#: 必需输入没到齐 —— 节点不跑，下游也停在这里。
REASON_MISSING_INPUTS = "缺少必需输入"
#: 没有触发源（图外输入没来、前驱没喂数、定时器也没叫）。
REASON_NOT_TRIGGERED = "没有触发源"
#: 同一事件已经推进过这个节点，不二次推进。
REASON_ALREADY_VISITED = "同一事件已触发过，不二次推进"
#: 节点执行时报错 —— 记下来，不让异常掀翻整张图。
REASON_NODE_ERROR = "节点执行报错"


@dataclass(frozen=True)
class NodeSpec:
    """一个节点的声明。

    - ``required_inputs``：**必需输入**（闸门一查的就是它）。缺一个就不跑；
    - ``optional_inputs``：可选输入，缺了不影响推进；
    - ``external_inputs``：**图外输入** —— 由图外给进来的键（图外输入只在本
      事件首次访问时构成触发源）；
    - ``requires_nonempty``：必需输入里**还要求非空**的那些（空集合等于没到）；
    - ``outputs``：本节点产出的键，喂给下游节点。
    """

    name: str
    label: str
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    external_inputs: tuple[str, ...] = ()
    requires_nonempty: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @property
    def all_inputs(self) -> tuple[str, ...]:
        return tuple(self.required_inputs) + tuple(self.optional_inputs)


#: 节点表：一条报告的完整处理链。
#:
#: 顺序即拓扑序 —— 每个节点的输入都由它前面的节点产出（``accession_no`` 由
#: ``ingest`` 产出，``order_requests`` 也由 ``ingest`` 产出，``baseline`` 由
#: ``fetch_baseline`` 产出……）。
NODE_SPECS: tuple[NodeSpec, ...] = (
    NodeSpec(
        name=NODE_INGEST,
        label="入站解析",
        required_inputs=("raw_entries",),
        external_inputs=("raw_entries", "order_requests"),
        outputs=(
            "accession_no",
            "result_entries",
            "withheld_entries",
            "inverted_entries",
            "all_entries",
            "order_requests",
        ),
    ),
    NodeSpec(
        name=NODE_FETCH_BASELINE,
        label="取报告基线",
        required_inputs=("accession_no",),
        outputs=("baseline",),
    ),
    NodeSpec(
        name=NODE_DERIVE_ORDERS,
        label="派生待查项",
        required_inputs=("baseline", "order_requests"),
        outputs=("pending_queries", "recall_keys", "order_guard"),
    ),
    NodeSpec(
        name=NODE_FANOUT_RETRIEVE,
        label="多路召回",
        required_inputs=("recall_keys", "result_entries"),
        outputs=("retrieved_candidates", "recall_index", "fanout_stats"),
    ),
    NodeSpec(
        name=NODE_ALIGN,
        label="部位-蜡块对齐",
        required_inputs=("result_entries", "all_entries", "baseline"),
        optional_inputs=("retrieved_candidates", "recall_index"),
        requires_nonempty=("all_entries",),
        outputs=("matched", "unmatched", "pending_records", "late_outcomes"),
    ),
    NodeSpec(
        name=NODE_COMPOSE,
        label="成稿",
        required_inputs=("matched", "baseline", "pending_queries"),
        optional_inputs=("late_outcomes",),
        requires_nonempty=("matched",),
        outputs=("draft", "draft_id"),
    ),
    NodeSpec(
        name=NODE_WRITEBACK,
        label="回写草稿区",
        required_inputs=("draft",),
        outputs=("writeback_receipt",),
    ),
    NodeSpec(
        name=NODE_NOTIFY,
        label="通知当班医师",
        required_inputs=("writeback_receipt",),
        outputs=("notify_result",),
    ),
)

#: 节点名 → 节点声明。
NODE_BY_NAME: dict[str, NodeSpec] = {spec.name: spec for spec in NODE_SPECS}


@dataclass(frozen=True)
class SkipRecord:
    """一次「没往下走」的记录。

    **停住要看得见**：跳过原因、缺哪几个输入、哪个节点，都写在这里，
    运维在运行摘要与日志里都能查到「这一轮为什么没成稿」。
    """

    node: str
    label: str
    reason: str
    detail: str
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "label": self.label,
            "reason": self.reason,
            "detail": self.detail,
            "missing": list(self.missing),
        }


class PipelineState:
    """图内共享状态。

    - ``values``：键 → 值（节点产出与图外输入都放这里）；
    - ``visits``：节点 → **累计**访问次数（跨事件）；
    - ``event_visits``：节点 → 推进过它的事件号集合（同一事件不二次推进）；
    - ``produced_by``：键 → ``(事件号, 产出它的节点)``（跨事件留下的旧值不算
      前驱喂入 —— 否则上一轮的结果会反复触发下一轮）；
    - ``skips``：跳过流水；
    - ``notes``：节点之间传的旁注（不算数据流）。
    """

    def __init__(self, event_id: str = "") -> None:
        self.event_id = event_id
        self.values: dict[str, Any] = {}
        self.visits: dict[str, int] = {}
        self.event_visits: dict[str, set[str]] = {}
        self.produced_by: dict[str, tuple[str, str]] = {}
        self.skips: list[SkipRecord] = []
        self.errors: list[str] = []
        self.notes: dict[str, Any] = {}

    # ------------------------------------------------------------ 取值

    def set(self, key: str, value: Any) -> None:
        """放一个图外输入或节点产出。"""
        self.values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def has(self, key: str) -> bool:
        """这个键到没到（值存在且不是 ``None``）。"""
        return key in self.values and self.values[key] is not None

    # ------------------------------------------------------------ 记账

    def visit_count(self, node: str) -> int:
        """节点的累计访问次数。"""
        return self.visits.get(node, 0)

    def visited_in_event(self, node: str, event_id: str | None = None) -> bool:
        """这个节点在本事件里是否已经推进过。"""
        target = event_id if event_id is not None else self.event_id
        return target in self.event_visits.get(node, ())

    def record_skip(self, record: SkipRecord) -> None:
        self.skips.append(record)

    def mark_produced(self, key: str, node: str, event_id: str | None = None) -> None:
        target = event_id if event_id is not None else self.event_id
        self.produced_by[key] = (target, node)

    def mark_visit(self, node: str, event_id: str | None = None) -> None:
        target = event_id if event_id is not None else self.event_id
        self.visits[node] = self.visits.get(node, 0) + 1
        self.event_visits.setdefault(node, set()).add(target)

    # ------------------------------------------------------------ 摘要

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "visits": dict(self.visits),
            "skips": [record.to_dict() for record in self.skips],
            "errors": list(self.errors),
        }


def are_all_sockets_ready(
    spec: NodeSpec,
    state: PipelineState,
    *,
    only_check_mandatory: bool = True,
) -> tuple[bool, tuple[str, ...]]:
    """**闸门一**：节点的必需输入齐不齐。

    返回 ``(是否齐备, 缺哪些)``。缺哪些会原样出现在跳过流水里 —— 运维要能
    一眼看出「是没取到基线，还是没等到结果」。

    ``only_check_mandatory=False`` 时连可选输入一起查（本产品默认只看必需
    输入，可选输入缺了不该拦住推进）。
    """
    names = spec.required_inputs if only_check_mandatory else spec.all_inputs
    missing: list[str] = []
    for name in names:
        if not state.has(name):
            missing.append(name)
            continue
        if name in spec.requires_nonempty and not state.get(name):
            # 键到了但内容是空的 —— 等于没到。
            missing.append(name)
    return (not missing), tuple(missing)


def has_any_trigger(
    spec: NodeSpec,
    state: PipelineState,
    *,
    event_id: str | None = None,
) -> tuple[bool, str]:
    """**闸门二**：节点是否被触发。

    三个触发来源，对应源实现 ``has_any_trigger`` L74-78：

    1. **前驱喂入**：本事件里前驱节点产出过本节点的某个输入；
    2. **图外输入**：本节点有图外输入，且本事件里还没推进过它；
    3. **定时器**：本节点既不从图内取输入也不取图外输入（图跑起来就跑）。

    **同一事件不二次推进**：三种来源都在「本事件已推进过」时失效 —— 这正是
    源实现「组件执行前消费输入」的可观察效果（重复触发不产生第二次副作用）。
    """
    target = event_id if event_id is not None else state.event_id
    if target and state.visited_in_event(spec.name, target):
        return False, ""

    # 1. 前驱喂入：只认**本事件**产出的输入，跨事件留下的旧值不算。
    for name in spec.all_inputs:
        produced = state.produced_by.get(name)
        if produced is None:
            continue
        event, producer = produced
        if event != target:
            continue
        if producer != spec.name:
            return True, TRIGGER_PREDECESSOR

    # 2. 图外输入（图外输入只在本事件首次访问时构成触发源）。
    if any(name in spec.external_inputs for name in spec.all_inputs):
        return True, TRIGGER_EXTERNAL

    # 3. 定时器：没有输入的节点由图自己的周期扫描带起来。
    if not spec.all_inputs:
        return True, TRIGGER_TIMER

    return False, ""


@dataclass
class PipelineRun:
    """一次推进的结果。"""

    event_id: str
    trigger: str
    executed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[SkipRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def executed_nodes(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.executed)

    def skip_for(self, node: str) -> SkipRecord | None:
        for record in self.skipped:
            if record.node == node:
                return record
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "trigger": self.trigger,
            "executed": [{"node": name, "trigger": source} for name, source in self.executed],
            "skipped": [record.to_dict() for record in self.skipped],
            "errors": list(self.errors),
        }


class Pipeline:
    """轻量节点表执行器（不引入 networkx）。

    ``specs`` 是节点表（默认 :data:`NODE_SPECS`），``handlers`` 是节点名 →
    执行体。执行体收到 :class:`PipelineState`，返回一个「产出键 → 值」的映射；
    返回空映射表示**本节点没有产出**（比如成稿节点发现没什么可成），下游节点
    的必需输入因此到不齐，会被闸门一挡住 —— 这正是「产出为空就不往下走」的
    语义。
    """

    def __init__(
        self,
        specs: Sequence[NodeSpec] = NODE_SPECS,
        handlers: Mapping[str, Callable[[PipelineState], Mapping[str, Any]]] | None = None,
    ) -> None:
        self._specs = tuple(specs)
        self._handlers: dict[str, Callable[[PipelineState], Mapping[str, Any]]] = dict(
            handlers or {}
        )
        self._producers: dict[str, str] = {}
        for spec in self._specs:
            for output in spec.outputs:
                self._producers[output] = spec.name

    # ------------------------------------------------------------ 只读

    @property
    def specs(self) -> tuple[NodeSpec, ...]:
        return self._specs

    def spec(self, name: str) -> NodeSpec:
        for item in self._specs:
            if item.name == name:
                return item
        raise KeyError(f"节点表里没有节点 {name!r}")

    def register(self, name: str, handler: Callable[[PipelineState], Mapping[str, Any]]) -> None:
        """登记（或替换）一个节点的执行体。"""
        self._handlers[name] = handler

    # ------------------------------------------------------------ 推进

    def run(
        self,
        state: PipelineState,
        *,
        handlers: Mapping[str, Callable[[PipelineState], Mapping[str, Any]]] | None = None,
        trigger: str = TRIGGER_EXTERNAL,
    ) -> PipelineRun:
        """按节点表推进一轮。

        每个节点依次过两道闸门：

        - **闸门一**（必需输入齐不齐）：不齐 → 记跳过原因，**不往下走**；
        - **闸门二**（有没有触发源）：没有 → 记跳过原因，**不往下走**。

        过闸门才执行；执行体抛错不会掀翻整张图 —— 记一条错误，后续节点因为
        拿不到产出而自然停住。
        """
        active = dict(self._handlers)
        if handlers:
            active.update(handlers)

        result = PipelineRun(event_id=state.event_id, trigger=trigger)

        for spec in self._specs:
            if state.visited_in_event(spec.name):
                # 同一事件不二次推进 —— 访问计数不再增加。
                record = SkipRecord(
                    node=spec.name,
                    label=spec.label,
                    reason=REASON_ALREADY_VISITED,
                    detail=f"事件 {state.event_id} 已推进过该节点，不二次推进",
                )
                state.record_skip(record)
                result.skipped.append(record)
                continue

            ready, missing = are_all_sockets_ready(spec, state)
            if not ready:
                record = SkipRecord(
                    node=spec.name,
                    label=spec.label,
                    reason=REASON_MISSING_INPUTS,
                    detail="缺少必需输入：" + "、".join(missing),
                    missing=missing,
                )
                state.record_skip(record)
                result.skipped.append(record)
                continue

            triggered, source = has_any_trigger(spec, state)
            if not triggered:
                record = SkipRecord(
                    node=spec.name,
                    label=spec.label,
                    reason=REASON_NOT_TRIGGERED,
                    detail="没有触发源（图外输入未到、前驱未喂数、定时器未叫）",
                )
                state.record_skip(record)
                result.skipped.append(record)
                continue

            handler = active.get(spec.name)
            if handler is None:
                record = SkipRecord(
                    node=spec.name,
                    label=spec.label,
                    reason=REASON_NOT_TRIGGERED,
                    detail="节点没有登记执行体",
                )
                state.record_skip(record)
                result.skipped.append(record)
                continue

            try:
                produced = handler(state) or {}
            except Exception as exc:  # noqa: BLE001 - 一个节点报错不掀翻整张图
                message = f"{spec.label}（{spec.name}）执行报错：{exc}"
                state.errors.append(message)
                result.errors.append(message)
                record = SkipRecord(
                    node=spec.name,
                    label=spec.label,
                    reason=REASON_NODE_ERROR,
                    detail=str(exc),
                )
                state.record_skip(record)
                result.skipped.append(record)
                continue

            for key, value in produced.items():
                state.set(key, value)
                state.mark_produced(key, spec.name)
            state.mark_visit(spec.name)
            result.executed.append((spec.name, source))

        return result
