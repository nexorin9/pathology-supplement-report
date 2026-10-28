"""申请侧状态闸门：已取消 / 已终止的待查项，其结果不得并入草稿。

**为什么需要这一层**：归属绑定解决的是「这条结果属于哪一块」，但有时答案
是「哪一块都不属于」—— 检验科那边把加做申请**取消**了（``ORC-1=CA``）或
**终止**了（``ORC-1=DC``），回报却照样送来了。把这种结果并进补充报告，等于
在报告里写上一项**已经不做**的检查，医师会以为这项做了。

所以对齐之前先过一道闸门：**这条结果对应的待查项，是不是已经被取消 / 终止了**。
是 → 不并入草稿，转挂起并标原因（待查项已取消 / 待查项已终止），等人处置。

**闸门怎么定位**：优先按**位置**定位（蜡块号 → 部位序号 → 加做申请号 →
唯一兜底）。闸门问的是「这一块上的这一项被取消了吗」，位置比申请号更具体 ——
一份加做申请可能覆盖多块，申请号一样不代表状态一样。蜡块号按归一化后比较
（基线写 ``B-1``、回报写 ``b01``，是同一块）。

**闸门查两遍**：对齐**之前**用回报自带的定位查一遍；对齐**之后**再用命中块的
位置查一遍。第二遍不能省 —— 回报没带蜡块号时，第一遍可能定位不到；而这一条
结果如果落在了被取消的那一块上，就更不能进草稿。

**暂停（``ORC-1=HD``）不拦**：暂停是「暂时不做」，申请还在账上；取消与终止是
「不做了」。把暂停也拦掉，会让「等结果但被叫停」的正常情形变成无谓挂起。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..models import Block, CheckItemState, ResultEntry
from .normalizer import normalize_block_no
from .query_derivation import PendingQuery, PendingQuerySet

__all__ = ["BLOCKED_STATES", "OrderStateGuard"]

#: 这两种状态下的待查项，其结果一律不并入草稿。
BLOCKED_STATES: tuple[CheckItemState, ...] = (
    CheckItemState.CANCELLED,
    CheckItemState.TERMINATED,
)


def _norm_item_code(value: str | None) -> str:
    """项目码归一：去空白 + 转大写。"""
    if value is None:
        return ""
    return str(value).strip().upper()


@dataclass(frozen=True)
class OrderStateGuard:
    """按申请侧状态拦截结果。

    闸门是**只读**的：它不改变任何状态，只回答「这条结果还能不能并入草稿」。
    """

    queries: tuple[PendingQuery, ...] = ()

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_query_sets(
        cls, query_sets: Mapping[str, PendingQuerySet] | Iterable[PendingQuerySet]
    ) -> "OrderStateGuard":
        """从派生好的待查项集合建闸门。"""
        iterable: Iterable[PendingQuerySet]
        if isinstance(query_sets, Mapping):
            iterable = query_sets.values()
        else:
            iterable = query_sets
        items: list[PendingQuery] = []
        for query_set in iterable:
            items.extend(query_set.items)
        return cls(queries=tuple(items))

    @classmethod
    def from_queries(cls, queries: Iterable[PendingQuery]) -> "OrderStateGuard":
        return cls(queries=tuple(queries))

    # ------------------------------------------------------------ 查询

    @property
    def blocked_queries(self) -> tuple[PendingQuery, ...]:
        """被闸门拦住的待查项（已取消 / 已终止）。"""
        return tuple(query for query in self.queries if query.state in BLOCKED_STATES)

    def candidates_for(self, entry: ResultEntry) -> tuple[PendingQuery, ...]:
        """同一病理号、同一项目码的待查项。"""
        item_code = _norm_item_code(entry.item_code)
        return tuple(
            query
            for query in self.queries
            if query.accession_no == entry.accession_no
            and _norm_item_code(query.item_code) == item_code
        )

    def match_query(
        self,
        entry: ResultEntry,
        *,
        region_seq: int | None = None,
        block_no: str | None = None,
        use_entry_location: bool = True,
    ) -> PendingQuery | None:
        """这条结果对应哪条待查项。

        ``region_seq`` / ``block_no`` 给了就用它们定位（对齐之后拿命中块的位置
        复核）；``use_entry_location=False`` 表示**只**用给定的位置，不用回报
        自带的定位字段。
        """
        candidates = self.candidates_for(entry)
        if not candidates:
            return None

        if use_entry_location:
            seq = entry.region_seq if region_seq is None else region_seq
            block_value = entry.block_no if block_no is None else block_no
            order_no = entry.order_no
        else:
            seq = region_seq
            block_value = block_no
            order_no = None

        index = _locate(seq, block_value, order_no, candidates)
        if index is None:
            return None
        return candidates[index]

    def blocked_state(
        self,
        entry: ResultEntry,
        *,
        region_seq: int | None = None,
        block_no: str | None = None,
    ) -> CheckItemState | None:
        """这条结果对应的待查项是不是已取消 / 已终止；不是则返回 ``None``。"""
        query = self.match_query(entry, region_seq=region_seq, block_no=block_no)
        if query is None:
            return None
        if query.state in BLOCKED_STATES:
            return query.state
        return None

    # ------------------------------------------------------------ 展示

    @staticmethod
    def query_block(query: PendingQuery) -> Block:
        """把一条待查项还原成部位视图（给挂起清单的「候选部位」用）。"""
        return Block(
            region_seq=query.region_seq,
            region_desc=query.region_desc,
            block_no=query.block_no,
            order_no=query.order_no,
        )


def _locate(
    region_seq: int | None,
    block_no: str | None,
    order_no: str | None,
    candidates: Sequence[PendingQuery],
) -> int | None:
    """在候选待查项里定位一条（位置优先，找不到就返回 ``None``）。

    定位顺序：蜡块号 → 部位序号 → 加做申请号 → 唯一兜底。

    唯一兜底只在**完全没有定位信息**时成立 —— 有定位信息却对不上，说明
    定位信息本身有问题，这时宁可定位失败（闸门放行、交给对齐层判挂起），
    也不凭一条候选就断定它被取消了。
    """
    block_key = normalize_block_no(block_no)

    # 1) 蜡块号（归一化后）+ 部位序号不冲突
    if block_key:
        for index, query in enumerate(candidates):
            query_block = normalize_block_no(query.block_no)
            if not query_block or query_block != block_key:
                continue
            if (
                region_seq is not None
                and query.region_seq is not None
                and query.region_seq != region_seq
            ):
                continue
            return index

    # 2) 部位序号
    if region_seq is not None:
        for index, query in enumerate(candidates):
            if query.region_seq is None or query.region_seq != region_seq:
                continue
            query_block = normalize_block_no(query.block_no)
            if block_key and query_block and query_block != block_key:
                continue
            return index

    # 3) 加做申请号
    if order_no:
        for index, query in enumerate(candidates):
            if query.order_no and query.order_no == order_no:
                return index

    # 4) 唯一兜底：只有一条候选，且手上没有任何定位信息
    if region_seq is None and block_key is None and len(candidates) == 1:
        return 0

    return None
