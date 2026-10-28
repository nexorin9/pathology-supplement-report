"""待查项派生与检索键派生。

**待查项**回答一个问题：**这份报告的哪些检查项还在等结果**。
它是召回的输入 —— 没有待查项，就不知道该去找哪些结果；找错了，就是把
A 块的结果写进 B 块。

来源有两路：

- **基线已申请项**：病理系统 ``GET`` 回来的部位清单里，每块已申请的项目；
- **申请侧新增项**：``ORM^O01`` 里 ``ORC-1`` 为 ``NW`` / ``RE`` 的加做申请。

两路指向同一个检查项时**合成一条**（不重复派生）—— 一项检查只能有一个
待查项，否则下游对齐会把同一条结果匹配两次。合成口径与
:func:`patho_supplement.ingest.orm_parser.build_order_event_key` 保持同一套
「加做申请号 → 蜡块号 → 部位序号」的松紧顺序：申请号最可靠，部位信息
最不可靠。

``CA`` / ``HD`` / ``DC`` 三种控制动作改的是**状态**，不是派生量：取消、
暂停、终止都不再等结果，因此不计入「还在等结果」的那一份。终止项另有一层
含义：**它不能被下一轮的基线重新派生复活**。所以已终止项从**待查集合**
（还在等结果的集合）剔除，但状态行留在库里 —— 删掉它，下一轮基线一重新
派生，它又变回「在等结果」。

每条待查项再派生**多组检索键**，每组带来源类型标记：

==================  ====================================================
``block``           蜡块号最精确，一把钥匙开一块
``region``          部位序号 / 部位描述，蜡块号缺失时的次选
``order``           加做申请号 + 项目码，部位信息全缺时的兜底
==================  ====================================================

键上只放**有值的字段**：缺字段的钥匙是万能钥匙，万能钥匙会把结果挂错块。
缺哪个字段，正是挂起时要告诉技术员的那句话。

本模块对应源产品「一个组件可由多个触发源喂入」的扇出结构
（``github_ref/haystack/haystack/core/pipeline/component_checks.py::has_any_trigger``
L57-78）：申请侧事件流与基线是**两个触发源**，共同喂给「派生待查项」这一
段；两个来源都进同一张图，谁都不与谁抢触发。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from ..db import Database
from ..ingest.orm_parser import ACTION_TO_ITEM_STATE
from ..models import CheckItemState, OrderAction, OrderRequest, ReportBaseline
from ..timefmt import now_text

__all__ = [
    "KEY_KIND_BLOCK",
    "KEY_KIND_ORDER",
    "KEY_KIND_REGION",
    "PendingQuery",
    "PendingQuerySet",
    "PendingQueryStore",
    "RecallKey",
    "SOURCE_BASELINE",
    "SOURCE_ORDER",
    "StoreOutcome",
    "derive_all",
    "derive_pending_queries",
    "derive_recall_keys",
]

#: 检索键类型标记（来源类型）。
KEY_KIND_BLOCK = "block"
KEY_KIND_REGION = "region"
KEY_KIND_ORDER = "order"

#: 待查项来源标记。
SOURCE_BASELINE = "baseline"
SOURCE_ORDER = "order"

#: 检索键类型的排序权重（越精确越靠前）。
_KIND_RANK: dict[str, int] = {KEY_KIND_BLOCK: 0, KEY_KIND_REGION: 1, KEY_KIND_ORDER: 2}

#: 键文本里的字段分隔符。
_SEP = "|"

#: 键文本里表示「这个字段没有值」的占位。
_EMPTY = "-"


def _clean(value: str | None) -> str | None:
    """把字段值收拾成能直接拼进键文本的样子。

    分隔符出现在值里会破坏键的可解析性，替换成 ``/``；空串按「没有这个
    字段」处理 —— 空串不是值。
    """
    if value is None:
        return None
    text = value.replace(_SEP, "/").strip()
    return text or None


def _merge_source(left: str, right: str) -> str:
    """两个来源合成一个标记。"""
    if left == right:
        return left
    return f"{SOURCE_BASELINE}+{SOURCE_ORDER}"


def _match_by_location(
    order_no: str | None,
    region_seq: int | None,
    block_no: str | None,
    candidates: Sequence[tuple[str | None, int | None, str | None]],
) -> int | None:
    """在候选里找出与给定定位信息指向同一项的下标。

    判定顺序（先严后松）：

    1. **加做申请号** —— 同一份申请就是同一项检查，最可靠；
    2. **蜡块号** —— 一份报告里蜡块号唯一，部位序号冲突时不认；
    3. **部位序号** —— 一块上可能有多项检查，蜡块号冲突时不认；
    4. **唯一候选兜底** —— 候选自己没带部位信息、且同项目只剩一条时才算。

    找不到就返回 ``None`` —— 宁可多一条待查项，也不要把两项合成一项。
    """
    if order_no:
        for index, (c_order, _c_region, _c_block) in enumerate(candidates):
            if c_order and c_order == order_no:
                return index

    if block_no:
        for index, (_c_order, c_region, c_block) in enumerate(candidates):
            if not c_block or c_block != block_no:
                continue
            if region_seq is not None and c_region is not None and c_region != region_seq:
                continue
            return index

    if region_seq is not None:
        for index, (_c_order, c_region, c_block) in enumerate(candidates):
            if c_region is None or c_region != region_seq:
                continue
            if block_no and c_block and c_block != block_no:
                continue
            return index

    if region_seq is None and block_no is None and len(candidates) == 1:
        return 0

    return None


@dataclass(frozen=True)
class RecallKey:
    """一组检索键里的一把。

    ``kind`` 是这把钥匙的来源类型（``block`` / ``region`` / ``order``），
    下游扇出按类型分路走，某一路失败不影响其余路。
    """

    kind: str
    accession_no: str
    item_code: str
    region_seq: int | None = None
    block_no: str | None = None
    region_desc: str | None = None
    order_no: str | None = None

    @property
    def text(self) -> str:
        """键的文本形式：类型前缀 + 定位字段，便于排序、去重与写审计。"""
        if self.kind == KEY_KIND_BLOCK:
            body = (
                f"{self.region_seq if self.region_seq is not None else _EMPTY}"
                f"{_SEP}{self.block_no or _EMPTY}{_SEP}{self.item_code}"
            )
        elif self.kind == KEY_KIND_REGION:
            body = (
                f"{self.region_seq if self.region_seq is not None else _EMPTY}"
                f"{_SEP}{self.region_desc or _EMPTY}{_SEP}{self.item_code}"
            )
        else:
            body = f"{self.order_no or _EMPTY}{_SEP}{self.item_code}"
        return f"{self.kind}{_SEP}{self.accession_no}{_SEP}{body}"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "region_seq": self.region_seq,
            "block_no": self.block_no,
            "region_desc": self.region_desc,
            "order_no": self.order_no,
        }

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志与展示
        return self.text


@dataclass(frozen=True)
class PendingQuery:
    """一个在等（或曾经等过）结果的检查项。"""

    accession_no: str
    item_code: str
    item_name: str | None = None
    order_no: str | None = None
    region_seq: int | None = None
    block_no: str | None = None
    region_desc: str | None = None
    state: CheckItemState = CheckItemState.AWAITING
    source: str = SOURCE_BASELINE
    order_action: OrderAction | None = None
    recall_keys: tuple[RecallKey, ...] = ()

    @property
    def identity(self) -> tuple[str, int | None, str | None, str]:
        """归属标识：病理号 + 部位序号 + 蜡块号 + 项目码。

        **不含加做申请号**：申请号可能这一轮有、下一轮没有（基线不一定带），
        标识一变，同一项就会被当成两项。
        """
        return (self.accession_no, self.region_seq, self.block_no, self.item_code)

    @property
    def order_touched(self) -> bool:
        """状态是否被申请侧事件明确改过。"""
        return self.order_action is not None

    @property
    def awaiting(self) -> bool:
        """是否还在等结果。"""
        return self.state is CheckItemState.AWAITING


@dataclass(frozen=True)
class PendingQuerySet:
    """一个病理号的待查项集合与派生计数。"""

    accession_no: str
    items: tuple[PendingQuery, ...] = ()
    baseline_items: int = 0
    order_items: int = 0
    merged: int = 0

    @property
    def awaiting(self) -> tuple[PendingQuery, ...]:
        """还在等结果的待查项 —— **待查集合**。"""
        return tuple(item for item in self.items if item.awaiting)

    @property
    def cancelled(self) -> tuple[PendingQuery, ...]:
        return tuple(
            item for item in self.items if item.state is CheckItemState.CANCELLED
        )

    @property
    def held(self) -> tuple[PendingQuery, ...]:
        return tuple(item for item in self.items if item.state is CheckItemState.HELD)

    @property
    def terminated(self) -> tuple[PendingQuery, ...]:
        return tuple(
            item for item in self.items if item.state is CheckItemState.TERMINATED
        )

    @property
    def derived_count(self) -> int:
        """派生条数（合并后）。"""
        return len(self.items)

    @property
    def recall_keys(self) -> tuple[RecallKey, ...]:
        """所有还在等结果的待查项的检索键（去重、按精确度排序）。

        这是扇出段的输入：每把钥匙走一路召回，某一路失败不影响其余路。
        """
        keys = {key for item in self.awaiting for key in item.recall_keys}
        return tuple(sorted(keys, key=lambda k: (_KIND_RANK.get(k.kind, 9), k.text)))


def derive_recall_keys(query: PendingQuery) -> tuple[RecallKey, ...]:
    """给一条待查项派生多组检索键。

    只派发现有信息的钥匙：缺字段的钥匙是万能钥匙，会把结果挂到别的块上。
    """
    keys: list[RecallKey] = []

    block_no = _clean(query.block_no)
    if block_no:
        keys.append(
            RecallKey(
                kind=KEY_KIND_BLOCK,
                accession_no=query.accession_no,
                item_code=query.item_code,
                region_seq=query.region_seq,
                block_no=block_no,
            )
        )

    region_desc = _clean(query.region_desc)
    if query.region_seq is not None or region_desc:
        keys.append(
            RecallKey(
                kind=KEY_KIND_REGION,
                accession_no=query.accession_no,
                item_code=query.item_code,
                region_seq=query.region_seq,
                region_desc=region_desc,
            )
        )

    order_no = _clean(query.order_no)
    if order_no:
        keys.append(
            RecallKey(
                kind=KEY_KIND_ORDER,
                accession_no=query.accession_no,
                item_code=query.item_code,
                order_no=order_no,
            )
        )

    return tuple(keys)


def _with_recall_keys(query: PendingQuery) -> PendingQuery:
    """补上派生好的检索键。"""
    return replace(query, recall_keys=derive_recall_keys(query))


class _Derivation:
    """一个病理号的派生过程。

    两路来源合成一份待查项集合：基线已申请项 + 申请侧事件。
    """

    def __init__(self, accession_no: str) -> None:
        self.accession_no = accession_no
        self._items: list[PendingQuery] = []
        self.baseline_items = 0
        self.order_items = 0
        self.merged = 0

    # ------------------------------------------------------------ 入口

    def add_baseline(self, query: PendingQuery) -> None:
        self.baseline_items += 1
        self._merge_or_append(query)

    def add_order(self, query: PendingQuery) -> None:
        self.order_items += 1
        self._merge_or_append(query)

    # ------------------------------------------------------------ 合成

    def _candidates(self, candidate: PendingQuery) -> list[int]:
        """同病理号 + 同项目码的已有待查项下标。"""
        return [
            index
            for index, item in enumerate(self._items)
            if item.accession_no == candidate.accession_no
            and item.item_code == candidate.item_code
        ]

    def _merge_or_append(self, candidate: PendingQuery) -> None:
        indices = self._candidates(candidate)
        match = _match_by_location(
            candidate.order_no,
            candidate.region_seq,
            candidate.block_no,
            [
                (self._items[i].order_no, self._items[i].region_seq, self._items[i].block_no)
                for i in indices
            ],
        )
        if match is None:
            self._items.append(candidate)
            return

        index = indices[match]
        existing = self._items[index]
        self.merged += 1
        # 加做申请号：申请侧事件是**按项**报的（一条报文一个项目），
        # 基线里的是**按块**报的（一个块一个申请号，可能覆盖多个项目）。
        # 同一项两个号不一致时，取按项报的那个 —— 它才指向这一项检查。
        if candidate.source == SOURCE_ORDER:
            order_no = candidate.order_no or existing.order_no
        else:
            order_no = existing.order_no or candidate.order_no
        self._items[index] = replace(
            existing,
            item_name=existing.item_name or candidate.item_name,
            order_no=order_no,
            region_seq=(
                existing.region_seq
                if existing.region_seq is not None
                else candidate.region_seq
            ),
            block_no=existing.block_no or candidate.block_no,
            region_desc=existing.region_desc or candidate.region_desc,
            state=candidate.state if candidate.order_touched else existing.state,
            source=_merge_source(existing.source, candidate.source),
            order_action=(
                candidate.order_action
                if candidate.order_action is not None
                else existing.order_action
            ),
        )

    # ------------------------------------------------------------ 收口

    def finalize(self) -> PendingQuerySet:
        items = sorted(
            (_with_recall_keys(item) for item in self._items),
            key=lambda item: (
                item.accession_no,
                item.region_seq if item.region_seq is not None else -1,
                item.block_no or "",
                item.item_code,
            ),
        )
        return PendingQuerySet(
            accession_no=self.accession_no,
            items=tuple(items),
            baseline_items=self.baseline_items,
            order_items=self.order_items,
            merged=self.merged,
        )


def _baseline_queries(baseline: ReportBaseline) -> list[PendingQuery]:
    """从基线的部位清单派生待查项：每块已申请的项目各一条。"""
    queries: list[PendingQuery] = []
    for block in baseline.blocks:
        seen: set[str] = set()
        for item_code in block.ordered_items:
            code = _clean(item_code)
            if not code or code in seen:
                continue
            seen.add(code)
            queries.append(
                PendingQuery(
                    accession_no=baseline.accession_no,
                    item_code=code,
                    order_no=_clean(block.order_no),
                    region_seq=block.region_seq,
                    block_no=_clean(block.block_no),
                    region_desc=_clean(block.region_desc),
                    state=CheckItemState.AWAITING,
                    source=SOURCE_BASELINE,
                )
            )
    return queries


def _order_queries(orders: Sequence[OrderRequest]) -> list[PendingQuery]:
    """从申请侧事件派生待查项：控制动作决定状态。"""
    queries: list[PendingQuery] = []
    for order in orders:
        action = order.action
        if action not in ACTION_TO_ITEM_STATE:
            # 非法动作在解析阶段就被拒收了，走不到这里；真到了就按新增处理，
            # 不让一条看不懂的申请把整个派生打断。
            action = OrderAction.NEW
        queries.append(
            PendingQuery(
                accession_no=order.accession_no,
                item_code=_clean(order.item_code) or "",
                item_name=_clean(order.item_name),
                order_no=_clean(order.order_no),
                region_seq=order.region_seq,
                block_no=_clean(order.block_no),
                region_desc=_clean(order.region_desc),
                state=ACTION_TO_ITEM_STATE[action],
                source=SOURCE_ORDER,
                order_action=action,
            )
        )
    return queries


def derive_pending_queries(
    baseline: ReportBaseline | None,
    orders: Sequence[OrderRequest] = (),
    *,
    accession_no: str | None = None,
) -> PendingQuerySet:
    """派生一份报告的待查项与检索键。

    派生条数 = 基线已申请项 + 申请侧事件数 − 合成条数；
    **还在等结果**的条数再减去已取消 / 已暂停 / 已终止项。
    """
    if accession_no is None:
        if baseline is not None:
            accession_no = baseline.accession_no
        elif orders:
            accession_no = orders[0].accession_no
        else:
            accession_no = ""
    acc = accession_no
    derivation = _Derivation(acc)

    if baseline is not None:
        for query in _baseline_queries(baseline):
            derivation.add_baseline(query)

    for query in _order_queries(orders):
        if query.accession_no != acc:
            continue
        derivation.add_order(query)

    return derivation.finalize()


def derive_all(
    baselines: Mapping[str, ReportBaseline],
    orders: Sequence[OrderRequest] = (),
) -> dict[str, PendingQuerySet]:
    """按病理号批量派生待查项。"""
    grouped: dict[str, list[OrderRequest]] = {}
    for order in orders:
        grouped.setdefault(order.accession_no, []).append(order)

    result: dict[str, PendingQuerySet] = {}
    for accession_no in sorted(set(baselines) | set(grouped)):
        result[accession_no] = derive_pending_queries(
            baselines.get(accession_no), grouped.get(accession_no, []), accession_no=accession_no
        )
    return result


@dataclass
class StoreOutcome:
    """待查项落库的计数。"""

    inserted: int = 0
    updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated}


class PendingQueryStore:
    """待查项的持久化（SQLite ``pending_queries`` 表）。

    跨重启去重靠这张表：同一项检查重复投递、或下一轮基线重新派生同一项，
    都是**更新原行**而不是新增一行。

    状态口径：**只有申请侧事件能改状态**。基线重新派生时，已取消 / 已暂停 /
    已终止的项保持原状态 —— 否则一份还没送到结果的申请会被基线「复活」。
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------ 写入

    def upsert(self, queries: Iterable[PendingQuery]) -> StoreOutcome:
        outcome = StoreOutcome()
        for query in queries:
            rows = self._db.query_all(
                "SELECT id, order_no, item_name, region_seq, block_no, region_desc, state"
                " FROM pending_queries WHERE accession_no = ? AND item_code = ?",
                (query.accession_no, query.item_code),
            )
            match = _match_by_location(
                query.order_no,
                query.region_seq,
                query.block_no,
                [(row["order_no"], row["region_seq"], row["block_no"]) for row in rows],
            )
            if match is None:
                self._insert(query)
                outcome.inserted += 1
                continue
            row = rows[match]
            self._update(row, query)
            outcome.updated += 1
        return outcome

    def _insert(self, query: PendingQuery) -> None:
        now = now_text()
        self._db.execute(
            "INSERT INTO pending_queries"
            " (accession_no, order_no, item_code, item_name, region_seq, block_no,"
            "  region_desc, state, source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query.accession_no,
                query.order_no,
                query.item_code,
                query.item_name,
                query.region_seq,
                query.block_no,
                query.region_desc,
                query.state.value,
                query.source,
                now,
                now,
            ),
        )

    def _update(self, row, query: PendingQuery) -> None:
        """更新一行；状态只有申请侧事件能改。"""
        existing_state = row["state"]
        if query.order_touched or existing_state == CheckItemState.AWAITING.value:
            state = query.state.value
        else:
            state = existing_state
        now = now_text()
        self._db.execute(
            "UPDATE pending_queries SET order_no = ?, item_name = ?, region_seq = ?,"
            " block_no = ?, region_desc = ?, state = ?, source = ?, updated_at = ?"
            " WHERE id = ?",
            (
                query.order_no or row["order_no"],
                query.item_name or row["item_name"],
                query.region_seq if query.region_seq is not None else row["region_seq"],
                query.block_no or row["block_no"],
                query.region_desc or row["region_desc"],
                state,
                query.source,
                now,
                row["id"],
            ),
        )

    # ------------------------------------------------------------ 读取

    def load(self, accession_no: str | None = None) -> list[PendingQuery]:
        """读回待查项（默认按病理号排序）。"""
        if accession_no:
            rows = self._db.query_all(
                "SELECT * FROM pending_queries WHERE accession_no = ?"
                " ORDER BY accession_no, region_seq, block_no, item_code",
                (accession_no,),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM pending_queries"
                " ORDER BY accession_no, region_seq, block_no, item_code"
            )
        return [self._from_row(row) for row in rows]

    def awaiting(self, accession_no: str | None = None) -> list[PendingQuery]:
        """读回**还在等结果**的待查项。"""
        return [item for item in self.load(accession_no) if item.awaiting]

    @staticmethod
    def _from_row(row) -> PendingQuery:
        query = PendingQuery(
            accession_no=row["accession_no"],
            item_code=row["item_code"],
            item_name=row["item_name"],
            order_no=row["order_no"],
            region_seq=row["region_seq"],
            block_no=row["block_no"],
            region_desc=row["region_desc"],
            state=CheckItemState(row["state"]),
            source=row["source"],
        )
        return _with_recall_keys(query)
