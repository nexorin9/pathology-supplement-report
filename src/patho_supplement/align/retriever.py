"""多路召回扇出（线程池并发 + 每路失败隔离）。

**这一层在源产品主路径上的位置**：源实现
``github_ref/haystack/haystack/components/retrievers/multi_query_embedding_retriever.py::run``
L127-152 是「多组查询并发检索 → 合并 → 去重 → 按分排序」四段骨架里的第一段::

    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
        queries_results = executor.map(lambda query: self._run_on_thread(query, retriever_kwargs), queries)
        for result in queries_results:
            if not result:
                continue
            docs.extend(result)

本模块保留这段骨架（**线程池扇出、每键一路**），把召回后端从向量相似度换成
结构化硬过滤 + 中文词法打分（见 :mod:`patho_supplement.align.filtering` 与
:mod:`patho_supplement.align.scorer`），并补上源实现没有的一条硬规矩。

**每路独立失败隔离 —— 为什么必须自己加**：源实现用 ``executor.map``，迭代结果
时任一路抛错会**整体抛出**，一次坏检索就毁掉整批查询。在本产品里这条路更不能
走：一把检索键对应「某一块的某一项检查」，一路失败可能只是那一路的报文脏了、
蜡块号写错了。**一路失败不得牵连其余路** —— 否则一份已经到手的加做结果会
因为另一把钥匙的异常凭空消失，而报告里少一项，医师看不出来。

**单路超时也不牵连整体**：某一路卡住（词典文件在慢盘上、某次打分特别慢）时，
到点就把**这一路**标记为超时并放弃，**其余路的结果照常回流**。整批的耗时
上限因此有界（``route_timeout``），而不是被一把卡住的钥匙拖到天荒地老。

**结果携带召回键与来源类型标记**：每一路命中都带着它是用哪把钥匙、
哪一路（``block`` / ``region`` / ``order``）召回的回到合并段。少了这两个标记，
下游就说不清「这条为什么被召回」—— 而这正是对齐依据（id 28）要写的东西。

**合并、去重与排序不在本模块**：本模块只负责扇出与失败隔离，把各路结果按
**路序**（= 传入的键序）合并成候选集。幂等键去重与按分排序是下一段
（id 25）的事 —— 这里的顺序只保证**可复现**：同一批输入，两次扇出得到的
候选集逐项相同。
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .filtering import (
    FilterCandidate,
    FilterKey,
    FilterPolicy,
    FilterResult,
    apply_filter,
)
from .query_derivation import RecallKey
from .scorer import LexicalScorer

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_ROUTE_TIMEOUT",
    "FanoutResult",
    "REASON_ROUTE_FAILED",
    "REASON_ROUTE_TIMEOUT",
    "RetrievedItem",
    "ROUTE_FAILED",
    "ROUTE_OK",
    "ROUTE_TIMED_OUT",
    "RouteContractError",
    "RouteOutcome",
    "RouteRunner",
    "default_route_runner",
    "fanout_params_from_config",
    "fanout_retrieve",
]

#: 扇出并发上限默认值（与配置面 ``FANOUT_MAX_WORKERS`` 的默认值一致）。
DEFAULT_MAX_WORKERS = 4

#: 单路召回超时默认值，单位秒（与配置面 ``FANOUT_ROUTE_TIMEOUT`` 一致）。
DEFAULT_ROUTE_TIMEOUT = 5.0

#: 收集结果的轮询间隔（秒）。超时判定精度由此决定，取 20 毫秒。
_POLL_INTERVAL = 0.02

#: 一路召回的几种状态。
ROUTE_OK = "ok"                # 正常返回
ROUTE_FAILED = "failed"        # 这一路抛错（已被隔离，不牵连其余路）
ROUTE_TIMED_OUT = "timed_out"  # 这一路超时（已被放弃，不牵连其余路）

#: 召回层的失败原因标签。
#:
#: 注意这两个**不是对齐层的挂起原因**（对齐层七类原因见
#: :mod:`patho_supplement.pending.reasons`）：召回层失败说的是「这一路没跑
#: 出来」，对齐层挂起说的是「这一条对不上某一块、缺哪个字段」。两者混用会
#: 让技术员查错方向。
REASON_ROUTE_TIMEOUT = "单路召回超时"
REASON_ROUTE_FAILED = "单路召回失败"


@dataclass(frozen=True)
class RetrievedItem:
    """一路召回里命中的一条候选。

    ``key`` 与 ``kind`` 是**召回键与来源类型标记** —— 下游合并段要靠它们把
    「命中」和「凭什么召回」一起带走；``kind`` 为 ``block`` / ``region`` /
    ``order``。
    """

    candidate: FilterCandidate
    key: RecallKey
    route_id: str
    kind: str
    score: float = 0.0
    pending: bool = False
    pending_reason: str | None = None

    @property
    def payload(self) -> object:
        """原对象（结果条目 / 待查项）。"""
        return self.candidate.payload

    @property
    def recall_key(self) -> str:
        """召回键文本。"""
        return self.key.text

    def to_dict(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "kind": self.kind,
            "recall_key": self.key.text,
            "score": self.score,
            "pending": self.pending,
            "pending_reason": self.pending_reason,
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True)
class RouteOutcome:
    """一路召回的结论。

    - ``status``：``ok`` / ``failed`` / ``timed_out``；
    - ``items``：这一路命中的候选（带召回键与来源类型）；
    - ``pending`` / ``pending_reason``：这一路整体是否需要挂起（0 命中时为真），
      以及原因；
    - ``error`` / ``error_type``：失败或超时时的可读原因与异常类型。
    """

    route_id: str
    key: RecallKey
    kind: str
    status: str
    items: tuple[RetrievedItem, ...] = ()
    pending: bool = False
    pending_reason: str | None = None
    error: str | None = None
    error_type: str | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == ROUTE_OK

    @property
    def failed(self) -> bool:
        return self.status == ROUTE_FAILED

    @property
    def timed_out(self) -> bool:
        return self.status == ROUTE_TIMED_OUT

    def to_dict(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "kind": self.kind,
            "recall_key": self.key.text,
            "status": self.status,
            "pending": self.pending,
            "pending_reason": self.pending_reason,
            "error": self.error,
            "error_type": self.error_type,
            "elapsed_ms": self.elapsed_ms,
            "item_count": len(self.items),
        }


@dataclass(frozen=True)
class FanoutResult:
    """一次扇出的整体结论。

    ``routes`` 按**传入的键序**排列（不按完成顺序）—— 线程完成顺序受调度影响，
    拿它当结果顺序，同一批数据两次跑出来的草稿就可能不一样。
    """

    routes: tuple[RouteOutcome, ...] = ()
    max_workers: int = DEFAULT_MAX_WORKERS
    route_timeout: float = DEFAULT_ROUTE_TIMEOUT
    elapsed_ms: int = 0

    @property
    def items(self) -> tuple[RetrievedItem, ...]:
        """合并后的候选集（按路序）。去重与排序在 id 25 落。"""
        merged: list[RetrievedItem] = []
        for route in self.routes:
            merged.extend(route.items)
        return tuple(merged)

    @property
    def ok_routes(self) -> int:
        return sum(1 for route in self.routes if route.ok)

    @property
    def failed_routes(self) -> int:
        """失败路数（抛错 + 超时）—— 计入统计，在运行摘要里可见。"""
        return sum(1 for route in self.routes if not route.ok)

    @property
    def failed_route_count(self) -> int:
        """因抛错而失败的路数。"""
        return sum(1 for route in self.routes if route.failed)

    @property
    def timed_out_routes(self) -> int:
        return sum(1 for route in self.routes if route.timed_out)

    @property
    def pending_routes(self) -> int:
        """需要挂起的路数（0 命中或降级模糊）。"""
        return sum(1 for route in self.routes if route.pending)

    @property
    def pending_items(self) -> int:
        return sum(1 for item in self.items if item.pending)

    @property
    def failures(self) -> tuple[RouteOutcome, ...]:
        """失败与超时的路。"""
        return tuple(route for route in self.routes if not route.ok)

    def summary(self) -> dict[str, object]:
        """运行摘要视图（写进运行摘要可查）。"""
        return {
            "routes_total": len(self.routes),
            "routes_ok": self.ok_routes,
            "routes_failed": self.failed_routes,
            "routes_failed_by_error": self.failed_route_count,
            "routes_timed_out": self.timed_out_routes,
            "routes_pending": self.pending_routes,
            "items": len(self.items),
            "pending_items": self.pending_items,
            "max_workers": self.max_workers,
            "route_timeout": self.route_timeout,
            "elapsed_ms": self.elapsed_ms,
            "failures": [route.to_dict() for route in self.failures],
        }


#: 一路召回的执行体：拿一把钥匙，返回这一路的过滤结论。
RouteRunner = Callable[[RecallKey], FilterResult]


def default_route_runner(
    candidates: Sequence[FilterCandidate],
    policy: FilterPolicy | str = FilterPolicy.MERGE,
    scorer: LexicalScorer | None = None,
) -> RouteRunner:
    """默认的一路召回实现：拿钥匙做硬过滤，0 命中时按策略降级。

    ``scorer`` 给定时全路共用同一份打分器 —— 各路的分必须**可比**，否则合并
    段按分排序就没有意义。
    """

    pool = tuple(candidates)
    resolved = FilterPolicy(policy)

    def _run(key: RecallKey) -> FilterResult:
        return apply_filter(
            FilterKey.from_recall_key(key), pool, policy=resolved, scorer=scorer
        )

    return _run


def fanout_params_from_config(config) -> tuple[int, float]:
    """从配置面取扇出参数：(并发上限, 单路超时秒数)。

    配置面（``config.py``）已经收了两项：``FANOUT_MAX_WORKERS``（1–32）与
    ``FANOUT_ROUTE_TIMEOUT``（0.1–600 秒）。越界在配置面就被拒，这里不再重复
    校验，只做取值与兜底。
    """
    workers = int(getattr(config, "fanout_max_workers", DEFAULT_MAX_WORKERS) or DEFAULT_MAX_WORKERS)
    timeout = float(
        getattr(config, "fanout_route_timeout", DEFAULT_ROUTE_TIMEOUT) or DEFAULT_ROUTE_TIMEOUT
    )
    return max(1, workers), max(0.001, timeout)


@dataclass
class _Execution:
    """一路召回在 worker 线程里的执行结果。"""

    result: FilterResult | None = None
    error: BaseException | None = None
    elapsed_ms: int = 0


class RouteContractError(RuntimeError):
    """召回执行体违反了约定（返回的不是过滤结论）。"""


def _run_route(key: RecallKey, runner: RouteRunner) -> _Execution:
    """在 worker 线程里跑一路召回。

    **异常在这里就被吃掉**：一路抛错只记进这一路的 :class:`_Execution`，
    绝不让它顺着 future 冒到整批召回上。这是「每路独立失败隔离」的落点。

    执行体返回了非 :class:`FilterResult` 的东西同样按失败处理 —— 契约破了就
    是这一路坏了，不能拖着整批一起崩。
    """
    started = time.monotonic()
    try:
        result = runner(key)
    except Exception as exc:  # noqa: BLE001 - 隔离是本层的职责
        return _Execution(
            error=exc, elapsed_ms=int((time.monotonic() - started) * 1000)
        )
    if not isinstance(result, FilterResult):
        return _Execution(
            error=RouteContractError(
                f"召回执行体返回了非法结果：{type(result).__name__}（应为 FilterResult）"
            ),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    return _Execution(
        result=result, elapsed_ms=int((time.monotonic() - started) * 1000)
    )


def _to_item(
    hit, key: RecallKey, route_id: str, result: FilterResult
) -> RetrievedItem:
    """把一条过滤命中包装成带召回键与来源类型标记的召回项。"""
    return RetrievedItem(
        candidate=hit.candidate,
        key=key,
        route_id=route_id,
        kind=key.kind,
        score=hit.score,
        pending=bool(hit.pending or result.pending),
        # 原因取**根因**：结果级原因（如「硬过滤键字段不全」）比条目级原因
        # （「降级模糊匹配」）更可操作 —— 技术员看到前者知道该去补部位序号或
        # 蜡块号，而不是再去查回报。
        pending_reason=result.pending_reason or hit.pending_reason,
    )


def _outcome_from_execution(
    execution: _Execution, key: RecallKey
) -> RouteOutcome:
    """把 worker 的执行结果转成一路的结论。"""
    route_id = key.text
    if execution.error is not None:
        return RouteOutcome(
            route_id=route_id,
            key=key,
            kind=key.kind,
            status=ROUTE_FAILED,
            pending=True,
            pending_reason=REASON_ROUTE_FAILED,
            error=str(execution.error) or execution.error.__class__.__name__,
            error_type=execution.error.__class__.__name__,
            elapsed_ms=execution.elapsed_ms,
        )

    result = execution.result
    if result is None:  # pragma: no cover - worker 只会给出 result 或 error 之一
        return RouteOutcome(
            route_id=route_id,
            key=key,
            kind=key.kind,
            status=ROUTE_FAILED,
            pending=True,
            pending_reason=REASON_ROUTE_FAILED,
            error="召回执行体没有给出结果",
            error_type="RouteContractError",
            elapsed_ms=execution.elapsed_ms,
        )
    items = tuple(
        _to_item(hit, key, route_id, result) for hit in result.items
    )
    return RouteOutcome(
        route_id=route_id,
        key=key,
        kind=key.kind,
        status=ROUTE_OK,
        items=items,
        pending=result.pending,
        pending_reason=result.pending_reason,
        elapsed_ms=execution.elapsed_ms,
    )


def _timeout_outcome(key: RecallKey, timeout: float) -> RouteOutcome:
    """一路超时的结论。"""
    return RouteOutcome(
        route_id=key.text,
        key=key,
        kind=key.kind,
        status=ROUTE_TIMED_OUT,
        pending=True,
        pending_reason=REASON_ROUTE_TIMEOUT,
        error=f"单路召回超时（超过 {timeout} 秒）",
        error_type="RouteTimeout",
    )


def fanout_retrieve(
    keys: Iterable[RecallKey],
    candidates: Iterable[FilterCandidate] = (),
    *,
    policy: FilterPolicy | str = FilterPolicy.MERGE,
    scorer: LexicalScorer | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    route_timeout: float = DEFAULT_ROUTE_TIMEOUT,
    route_runner: RouteRunner | None = None,
) -> FanoutResult:
    """多路并发召回。

    每把检索键走一路；一路抛错只废掉这一路（其余路结果完整返回），一路超时
    只废掉这一路（其余路结果完整返回）。失败与超时都计入统计，并在
    :meth:`FanoutResult.summary` 里可见。

    参数：

    - ``keys``：检索键（每把走一路）；
    - ``candidates``：被召回的候选集（结果条目转成的 :class:`FilterCandidate`）；
    - ``policy``：硬过滤策略（``REPLACE`` / ``MERGE``），语义见
      :func:`patho_supplement.align.filtering.apply_filter`；
    - ``scorer``：词法打分器；缺省时按候选部位描述集合建一份（各路共用，
      分数才可比）；
    - ``max_workers``：并发上限（默认 4）；
    - ``route_timeout``：单路超时秒数（默认 5.0）；
    - ``route_runner``：替换单路的执行体（测试注入异常/超时用）。产品代码走
      默认实现，测试替身只出现在测试数据层。
    """
    key_list = tuple(keys)
    pool = tuple(candidates)

    resolved_policy = FilterPolicy(policy)
    workers = int(max_workers)
    if workers < 1:
        raise ValueError(f"扇出并发上限必须至少为 1，当前值 {max_workers}")
    timeout = float(route_timeout)
    if timeout <= 0:
        raise ValueError(f"单路召回超时必须为正数秒数，当前值 {route_timeout}")

    started = time.monotonic()
    if not key_list:
        return FanoutResult(
            routes=(),
            max_workers=workers,
            route_timeout=timeout,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if scorer is None and resolved_policy is FilterPolicy.MERGE:
        # 各路共用一份打分器：语料一致，分数才可比。
        scorer = LexicalScorer(
            [candidate.region_desc for candidate in pool if candidate.region_desc]
        )

    runner = route_runner or default_route_runner(pool, resolved_policy, scorer)

    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="psr-fanout"
    )
    outcomes: list[RouteOutcome | None] = [None] * len(key_list)
    try:
        pending: dict[Future, tuple[int, RecallKey, float]] = {}
        for index, key in enumerate(key_list):
            future = executor.submit(_run_route, key, runner)
            pending[future] = (index, key, time.monotonic())

        while pending:
            done, _ = wait(
                list(pending), timeout=_POLL_INTERVAL, return_when=FIRST_COMPLETED
            )
            now = time.monotonic()
            for future in done:
                index, key, _submitted_at = pending.pop(future)
                try:
                    execution = future.result()
                except Exception as exc:  # noqa: BLE001 - 兜底：worker 已捕获
                    execution = _Execution(error=exc)
                outcomes[index] = _outcome_from_execution(execution, key)

            if not pending:
                break

            # 单路超时：到点即放弃**这一路**，其余路照常回流。
            for future, (index, key, submitted_at) in list(pending.items()):
                if now - submitted_at >= timeout:
                    pending.pop(future)
                    future.cancel()
                    outcomes[index] = _timeout_outcome(key, timeout)
    finally:
        # 不等卡住的路：否则一把坏钥匙能把整批拖住。
        executor.shutdown(wait=False, cancel_futures=True)

    resolved = tuple(
        outcome
        for outcome in outcomes
        if outcome is not None
    )
    return FanoutResult(
        routes=resolved,
        max_workers=workers,
        route_timeout=timeout,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
