"""合并、去重与按分排序 —— 多路召回结果集的收口段。

**这一层在源产品主路径上的位置**：源实现
``github_ref/haystack/haystack/components/retrievers/multi_query_embedding_retriever.py::run``
L149-151 是「扇出 → 合并 → 去重 → 按分排序」四段骨架里的后两段::

    # de-duplicate and sort
    docs = _deduplicate_documents(docs)
    docs.sort(key=lambda x: x.score or 0.0, reverse=True)

本模块保留这段控制流，把两处语义换成医院域里说得清的两样东西：

- **去重键**：文档 id → **业务幂等键**（病理号 + 蜡块号 + 项目码 + 观察时间 +
  结果状态）。这与入站层的幂等账本
  （:mod:`patho_supplement.ingest.idempotency`）用的是**同一把钥匙** ——
  同一条结果在入站层算一次，在这里也只算一次。
- **排序键**：向量相似度 → **对齐分**（中文 BM25 词法分，见
  :mod:`patho_supplement.align.scorer`）。

**同分稳定序 —— 源实现没有、但本产品必须有的一条**：源实现的 ``sort`` 键只有
分数，是**稳定排序** —— 两条同分文档谁在前取决于它们进入列表的顺序，而这个
顺序由线程池的完成顺序决定。同一批数据跑两次，报告里的条目顺序可能不同；
更糟的是 ``_deduplicate_documents`` 保留的也是「先到的那条」，于是并行调度
一变，**留下来的那条**也可能变。

本产品给同分再加一层**由内容决定的次序**（召回键字典序 → 观察时间 → 业务
幂等键 → 部位），使最终结果集与召回顺序、与线程调度、与机器负载**完全
无关**：同一批输入，两次跑出来的候选集逐项相同。这是「错绑定数 = 0」能被
承诺的前提之一 —— 一个连顺序都不确定的系统，没资格承诺结果不变。

**去重保留「分最高的一条」**：同一份结果被多把钥匙召回时，它在每路的分可能
不同（硬过滤命中分恒为 1.0，模糊降级分是词法分）。留下分最高的那条，其余
计入 ``duplicates``。分再相同，按稳定序取第一条 —— 仍然与输入顺序无关。

**没有业务身份就不去重**：候选缺病理号时算不出业务幂等键。此时**全部保留**
（一条都不合并）。理由是去重的方向性：合并错了是「两份结果被吞成一份，
报告少一项而没人知道」；不合并顶多是候选集大一点，下游还会再判一次。宁可
多留，不可少吞。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..ingest.idempotency import build_idempotency_key_from_parts
from .filtering import FilterCandidate
from .retriever import RetrievedItem, RouteOutcome

__all__ = [
    "DedupeOutcome",
    "MergeOutcome",
    "business_key_of",
    "dedupe_by_business_key",
    "merge_and_rank",
    "merge_fanout",
    "merge_routes",
    "order_key",
    "rank_by_score",
]


def business_key_of(candidate: FilterCandidate) -> str | None:
    """候选的业务幂等键；算不出来时返回 ``None``。

    算不出来的唯一情形是**缺病理号** —— 少了它，同一份报告里的两条结果就
    分不出谁是谁，硬凑一把钥匙等于把两件事并成一件。
    """
    if not candidate.accession_no:
        return None
    return build_idempotency_key_from_parts(
        candidate.accession_no,
        candidate.block_no,
        candidate.item_code,
        candidate.observed_at,
        candidate.result_status,
    )


def _region_rank(candidate: FilterCandidate) -> tuple[int, int]:
    """部位序号参与排序时的取值（空值排在有值之后）。"""
    if candidate.region_seq is None:
        return (1, 0)
    return (0, candidate.region_seq)


def order_key(item: RetrievedItem) -> tuple:
    """候选的**确定性次序键**。

    分降序 → 召回键字典序 → 观察时间 → 业务幂等键 → 部位。前三段是
    「同分时的稳定序」，后两段兜底：万一两条候选连召回键与观察时间都相同
    （同一路模糊降级捞出两个写法相近的部位），仍能分出先后，不至于把
    顺序交给输入顺序决定。

    分若是 NaN，按 0.0 处理 —— 排序里出现 NaN 会让比较结果依赖顺序，
    正是这一层要消灭的东西。
    """
    score = item.score
    if score != score:  # NaN 自等为假
        score = 0.0
    candidate = item.candidate
    return (
        -score,
        item.key.text,
        candidate.observed_at or "",
        business_key_of(candidate) or "",
        _region_rank(candidate),
        candidate.normalized_block_no or "",
        candidate.region_desc or "",
    )


@dataclass(frozen=True)
class DedupeOutcome:
    """去重段的产出。

    - ``items``：保留下来的候选（每条业务幂等键至多一条），按确定性次序排好；
    - ``duplicates``：被去重掉的条数；
    - ``unkeyed``：缺业务身份、算不出幂等键而**全部保留**的条数；
    - ``dropped_keys``：被去重掉的业务幂等键（按出现次序，便于排查）。
    """

    items: tuple[RetrievedItem, ...] = ()
    duplicates: int = 0
    unkeyed: int = 0
    dropped_keys: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.items)

    def summary(self) -> dict[str, object]:
        return {
            "kept": len(self.items),
            "duplicates": self.duplicates,
            "unkeyed": self.unkeyed,
            "dropped_keys": list(self.dropped_keys),
        }


@dataclass(frozen=True)
class MergeOutcome:
    """合并 → 去重 → 排序三段的整体产出。

    - ``items``：最终候选集（已去重、已按分排序、顺序与召回顺序无关）；
    - ``merged_count``：合并进来的总条数（去重前）；
    - ``duplicates``：被去重掉的条数；
    - ``unkeyed``：缺业务身份因而未参与去重的条数。
    """

    items: tuple[RetrievedItem, ...] = ()
    merged_count: int = 0
    duplicates: int = 0
    unkeyed: int = 0
    dropped_keys: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        """去重后的条数。"""
        return len(self.items)

    @property
    def pending_count(self) -> int:
        """候选集里带 pending 标记的条数。"""
        return sum(1 for item in self.items if item.pending)

    def summary(self) -> dict[str, object]:
        """可打印的摘要（写进运行摘要可查）。"""
        return {
            "merged": self.merged_count,
            "kept": self.count,
            "duplicates": self.duplicates,
            "unkeyed": self.unkeyed,
            "pending_items": self.pending_count,
            "dropped_keys": list(self.dropped_keys),
        }


# ---------------------------------------------------------------- 合并段


def merge_routes(routes: Iterable[RouteOutcome]) -> tuple[RetrievedItem, ...]:
    """合并段：各路结果按**路序**合并成候选集。

    路序 = 传入的检索键序，不是线程完成顺序 —— 完成顺序受调度影响，拿它
    当结果顺序，同一批数据两次跑出来的草稿就可能不一样。扇出层
    （:mod:`patho_supplement.align.retriever`）已经保证 ``routes`` 按键序
    排列，这里照单接收即可。

    失败与超时的路没有条目，自然不参与合并（它们的失败另有统计）。
    """
    merged: list[RetrievedItem] = []
    for route in routes:
        merged.extend(route.items)
    return tuple(merged)


# ---------------------------------------------------------------- 去重段


def dedupe_by_business_key(items: Sequence[RetrievedItem]) -> DedupeOutcome:
    """去重段：按业务幂等键去重，保留分最高的一条。

    口径：

    - 同一业务幂等键的多条命中 → 只留**分最高**的那条，其余计入
      ``duplicates``；
    - 分再相同 → 按 :func:`order_key` 取第一条（与输入顺序无关）；
    - **算不出业务幂等键的候选（缺病理号）全部保留**，一条都不合并。
    """
    best: dict[str, RetrievedItem] = {}
    order: list[str] = []
    dropped: list[str] = []
    unkeyed_items: list[RetrievedItem] = []

    for item in items:
        key = business_key_of(item.candidate)
        if key is None:
            unkeyed_items.append(item)
            continue

        current = best.get(key)
        if current is None:
            best[key] = item
            order.append(key)
            continue

        dropped.append(key)
        if order_key(item) < order_key(current):
            best[key] = item

    kept = [best[key] for key in order] + unkeyed_items
    return DedupeOutcome(
        items=tuple(sorted(kept, key=order_key)),
        duplicates=len(dropped),
        unkeyed=len(unkeyed_items),
        dropped_keys=tuple(dropped),
    )


# ---------------------------------------------------------------- 排序段


def rank_by_score(items: Iterable[RetrievedItem]) -> tuple[RetrievedItem, ...]:
    """排序段：按对齐分降序，同分按稳定序。

    结果与输入顺序无关 —— 把输入打乱，结果逐项相同。
    """
    return tuple(sorted(items, key=order_key))


# ---------------------------------------------------------------- 三段合起来


def merge_and_rank(items: Iterable[RetrievedItem]) -> MergeOutcome:
    """合并 → 去重 → 排序，一次做完。

    这是本模块的主入口：给它一批各路召回的结果，它给出一个**去重干净、
    顺序确定**的候选集。
    """
    materialized = tuple(items)
    dedupe = dedupe_by_business_key(materialized)
    ranked = rank_by_score(dedupe.items)
    return MergeOutcome(
        items=ranked,
        merged_count=len(materialized),
        duplicates=dedupe.duplicates,
        unkeyed=dedupe.unkeyed,
        dropped_keys=dedupe.dropped_keys,
    )


def merge_fanout(routes: Iterable[RouteOutcome]) -> MergeOutcome:
    """从各路结论直接走完三段（合并 → 去重 → 排序）。"""
    return merge_and_rank(merge_routes(routes))
