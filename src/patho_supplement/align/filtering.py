"""结构化硬过滤与过滤策略降级。

**为什么要有这一层**：源产品的检索后端是 embedding 相似度检索
（``github_ref/haystack/haystack/components/retrievers/in_memory/embedding_retriever.py``），
它在 ``__init__`` L51-96 收了三样东西 —— ``filters``（收窄检索空间）、
``top_k``（取回条数）、``filter_policy``（``REPLACE`` 覆盖 / ``MERGE`` 合并）。
源语义是「**先按结构化条件过滤，再按相似度排序**」，本产品保留这个控制流，
把过滤条件与排序后端换成医疗域里能落地的两样东西：

- **过滤条件** → (部位序号, 蜡块号, 项目码) 三元组硬过滤键。向量相似度给不出
  「这条就是 B3 块」这种确定性绑定 —— 0.87 分既可能是 B3 也可能是 B5，
  而在病理报告里**绑错一块就是一份签出去却写错部位的补充报告**；
- **排序后端** → 中文 BM25 词法打分（见 :mod:`patho_supplement.align.scorer`）。

**两档过滤策略**（与源项目同名同义，只是落到结构化字段上）：

==================  ========================================================
``REPLACE``         硬过滤**完全覆盖**检索空间。硬过滤命中 0 条 → 结果集为空，
                    本条挂起。模糊候选**不进入结果集**。
``MERGE``           硬过滤命中 0 条时**降级**为模糊匹配（部位描述词法打分），
                    降级出来的每一条都显式标 ``pending``。
==================  ========================================================

**「命中 0 条必须显式挂起，绝不静默丢弃」**：这是本层最硬的一条。硬过滤
命中 0 条只说明「字面对不上」，不代表这条结果不存在 —— 它可能只差蜡块号的
写法、只差部位别名。丢掉它等于让一份已经到手的加做结果凭空消失；
悄悄并进草稿则更糟 —— 那是错绑定。所以两条路都留痕：``REPLACE`` 留一条
「召回命中 0 条」的挂起，``MERGE`` 降级出的模糊候选逐条标 ``pending``
（带原因），由上层进待人工处理清单。

**模糊降级只放宽「位置」，不放宽「项目」**：降级阶段仍然要求项目码一致。
CD20 的回报不是 CK20 的回报 —— 放宽项目码就是把两项检查混成一项，
是「误并入」。部位序号与蜡块号才是可放宽的那一层。

**硬过滤键不全就不做硬过滤**：键里既没有部位序号也没有蜡块号时（只有
「申请号」那一路检索键），硬过滤没有可比的定位字段。这种键若拿去硬过滤，
等于没有过滤 —— 万能钥匙会把结果挂到别的块上。此时按「命中 0 条」处理，
走降级或挂起。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

from .normalizer import normalize_block_no
from .scorer import LexicalScorer

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行时不导入，避免循环依赖
    from ..models import ResultEntry
    from .query_derivation import RecallKey

__all__ = [
    "FilterCandidate",
    "FilterKey",
    "FilterOutcome",
    "FilterPolicy",
    "FilterResult",
    "FilteredCandidate",
    "REASON_FUZZY_DEGRADED",
    "REASON_INCOMPLETE_KEY",
    "REASON_NO_HIT",
    "apply_filter",
    "build_filter_key",
    "fuzzy_filter",
    "hard_filter",
]


class FilterPolicy(str, Enum):
    """硬过滤策略。

    - ``REPLACE``：硬过滤完全覆盖检索空间；模糊候选不进入结果集；
    - ``MERGE``：硬过滤命中 0 条时降级为模糊匹配，降级项显式标 ``pending``。
    """

    REPLACE = "REPLACE"
    MERGE = "MERGE"

    @property
    def label(self) -> str:
        return {
            FilterPolicy.REPLACE: "硬过滤完全覆盖",
            FilterPolicy.MERGE: "硬过滤未命中则降级模糊匹配",
        }[self]


class FilterOutcome(str, Enum):
    """一条候选进入结果集的方式。"""

    HARD = "hard"    # 硬过滤命中
    FUZZY = "fuzzy"  # 模糊降级命中（必带 pending）

    @property
    def label(self) -> str:
        return {FilterOutcome.HARD: "硬过滤命中", FilterOutcome.FUZZY: "模糊降级"}[self]


#: 挂起原因标签（与 id 31 的七类原因口径一致，本轮先落「召回命中 0 条」这一类）。
REASON_NO_HIT = "召回命中 0 条"
#: 硬过滤键字段不全（既无部位序号也无蜡块号）时的原因。
REASON_INCOMPLETE_KEY = "硬过滤键字段不全"
#: 硬过滤命中 0 条、降级到模糊匹配时的原因。
REASON_FUZZY_DEGRADED = "硬过滤命中 0 条，降级模糊匹配"


def _norm_item_code(value: str | None) -> str:
    """项目码归一：去空白 + 转大写。

    项目码不做更激进的处理 —— 不去分隔符、不剥前缀。「IHC-CD20」与「CD20」
    是不是同一项检查，取决于院内用哪套编码，本产品**不替科室猜**，宁可
    判为不同项走挂起。
    """
    if value is None:
        return ""
    return str(value).strip().upper()


@dataclass(frozen=True)
class FilterKey:
    """结构化硬过滤键：(部位序号, 蜡块号, 项目码)。

    蜡块号在构造时即归一化 —— 基线写 ``B-3``、回报写 ``b03`` 是同一块，
    归一化放在键里，比较时只比归一化后的值。

    ``region_desc`` 不参与硬过滤，只在降级到模糊匹配时作为查询文本。
    """

    region_seq: int | None = None
    block_no: str | None = None
    item_code: str = ""
    region_desc: str | None = None

    @property
    def normalized_block_no(self) -> str | None:
        """归一化后的蜡块号；归一化后为空表示这个字段等于没填。"""
        return normalize_block_no(self.block_no)

    @property
    def normalized_item_code(self) -> str:
        """归一化后的项目码。"""
        return _norm_item_code(self.item_code)

    @property
    def usable(self) -> bool:
        """这个键能不能用来做硬过滤。

        两个条件：**项目码要有**（否则这条键不知道在找哪一项检查），
        且**部位序号或蜡块号至少有一个**（否则没有可比的定位字段，
        硬过滤退化成「全都算命中」—— 万能钥匙）。
        """
        if not self.normalized_item_code:
            return False
        return self.region_seq is not None or self.normalized_block_no is not None

    @property
    def text(self) -> str:
        """键的可读形式（写审计与对齐依据时用）。"""
        block = self.normalized_block_no or "-"
        seq = self.region_seq if self.region_seq is not None else "-"
        return f"{seq}|{block}|{self.normalized_item_code or '-'}"

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_entry(cls, entry: "ResultEntry") -> "FilterKey":
        """从一条结果回报构造过滤键。"""
        return cls(
            region_seq=entry.region_seq,
            block_no=entry.block_no,
            item_code=entry.item_code,
            region_desc=entry.region_desc,
        )

    @classmethod
    def from_recall_key(cls, recall_key: "RecallKey") -> "FilterKey":
        """从一把检索键构造过滤键。

        检索键的三种来源里，``block`` 与 ``region`` 带定位字段，可以硬过滤；
        ``order``（只带加做申请号）没有定位字段 → 键不可用 → 走降级或挂起。
        """
        return cls(
            region_seq=recall_key.region_seq,
            block_no=recall_key.block_no,
            item_code=recall_key.item_code,
            region_desc=recall_key.region_desc,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "region_seq": self.region_seq,
            "block_no": self.normalized_block_no,
            "item_code": self.normalized_item_code,
            "region_desc": self.region_desc,
            "text": self.text,
            "usable": self.usable,
        }


@dataclass(frozen=True)
class FilterCandidate:
    """候选条目（被过滤的一侧）。

    前四段与过滤键同构，便于比较。``payload`` 放原对象（结果条目 / 待查项），
    过滤层只搬运不解析，不关心下游怎么用。

    **后三段是业务身份**（病理号 / 观察时间 / 结果状态），过滤本身用不上，
    但下游的**去重与稳定排序**需要它们 —— 那两件事按**业务幂等键**算，
    而业务幂等键不是从过滤键推得出来的（过滤键里没有病理号与时间）。候选
    在这一层被捞出来时把身份一并带上，下游就不必再回头解 ``payload``。
    """

    region_seq: int | None = None
    block_no: str | None = None
    item_code: str = ""
    region_desc: str | None = None
    payload: Any = None
    accession_no: str | None = None
    observed_at: str | None = None
    result_status: str | None = None

    @property
    def normalized_block_no(self) -> str | None:
        return normalize_block_no(self.block_no)

    @property
    def normalized_item_code(self) -> str:
        return _norm_item_code(self.item_code)

    @classmethod
    def from_entry(cls, entry: "ResultEntry") -> "FilterCandidate":
        """从一条结果回报构造候选（含业务身份）。

        这是候选的**唯一构造入口** —— 免得各处手搓候选时漏掉病理号，
        漏了就退化成「无法判别是否重复」，去重段只能保守地全留。
        """
        return cls(
            region_seq=entry.region_seq,
            block_no=entry.block_no,
            item_code=entry.item_code,
            region_desc=entry.region_desc,
            payload=entry,
            accession_no=entry.accession_no,
            observed_at=entry.observed_at,
            result_status=entry.status.value,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "region_seq": self.region_seq,
            "block_no": self.normalized_block_no,
            "item_code": self.normalized_item_code,
            "region_desc": self.region_desc,
            "accession_no": self.accession_no,
            "observed_at": self.observed_at,
            "result_status": self.result_status,
        }


@dataclass(frozen=True)
class FilteredCandidate:
    """过滤结果里的一条。

    ``pending`` 与 ``pending_reason`` 是本层的**核心产出**：降级出来的候选
    一律带 ``pending=True`` 与原因，上层据此把它送进待人工处理清单，
    而不是当成「已对上」写进草稿。
    """

    candidate: FilterCandidate
    outcome: FilterOutcome
    score: float = 0.0
    pending: bool = False
    pending_reason: str | None = None

    @property
    def payload(self) -> Any:
        return self.candidate.payload

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "outcome": self.outcome.value,
            "score": self.score,
            "pending": self.pending,
            "pending_reason": self.pending_reason,
        }


@dataclass(frozen=True)
class FilterResult:
    """一次过滤的结论。

    - ``items``：进入结果集的条目（硬过滤命中，或降级后的模糊候选）；
    - ``degraded``：是否走了模糊降级；
    - ``pending``：**本条**是否需要挂起。降级时为真 —— 即使模糊候选非空，
      也没有一条是「字面对上」的，硬命中仍需人工确认；
    - ``pending_reason``：挂起原因标签。
    """

    key: FilterKey
    policy: FilterPolicy
    items: tuple[FilteredCandidate, ...] = ()
    hard_hits: int = 0
    fuzzy_hits: int = 0
    degraded: bool = False
    pending: bool = False
    pending_reason: str | None = None

    @property
    def count(self) -> int:
        """结果集条数。"""
        return len(self.items)

    @property
    def pending_count(self) -> int:
        """结果集里带 pending 标记的条数。"""
        return sum(1 for item in self.items if item.pending)

    @property
    def hard_items(self) -> tuple[FilteredCandidate, ...]:
        """硬过滤命中的条目。"""
        return tuple(item for item in self.items if item.outcome is FilterOutcome.HARD)

    @property
    def fuzzy_items(self) -> tuple[FilteredCandidate, ...]:
        """模糊降级命中的条目。"""
        return tuple(item for item in self.items if item.outcome is FilterOutcome.FUZZY)

    def as_dict(self) -> dict[str, object]:
        """可打印的摘要（写进运行摘要可查）。"""
        return {
            "policy": self.policy.value,
            "key": self.key.to_dict(),
            "count": self.count,
            "hard_hits": self.hard_hits,
            "fuzzy_hits": self.fuzzy_hits,
            "degraded": self.degraded,
            "pending": self.pending,
            "pending_reason": self.pending_reason,
            "pending_count": self.pending_count,
        }


# ---------------------------------------------------------------- 硬过滤


def build_filter_key(
    region_seq: int | None,
    block_no: str | None,
    item_code: str,
    *,
    region_desc: str | None = None,
) -> FilterKey:
    """构造结构化硬过滤键。

    这是本层唯一的键构造入口 —— 蜡块号在这里归一化，别处不再重复归一化。
    """
    return FilterKey(
        region_seq=region_seq,
        block_no=block_no,
        item_code=item_code,
        region_desc=region_desc,
    )


def _hard_match(key: FilterKey, candidate: FilterCandidate) -> bool:
    """候选是否与硬过滤键字面对上。

    口径：**键里有的每个字段，候选都必须有且相等**。

    - 键里有蜡块号、候选没有 → 不算命中（不能确认就是这块）；
    - 键里有蜡块号、候选是另一块 → 不算命中（冲突）；
    - 键里没有的字段不作约束（键没提部位序号，就不比部位序号）。

    这样「两个都没有」永远不会被当成「两个相同」。
    """
    if key.region_seq is not None:
        if candidate.region_seq is None or candidate.region_seq != key.region_seq:
            return False

    key_block = key.normalized_block_no
    if key_block is not None:
        candidate_block = candidate.normalized_block_no
        if candidate_block is None or candidate_block != key_block:
            return False

    key_item = key.normalized_item_code
    if not key_item:
        return False
    if candidate.normalized_item_code != key_item:
        return False

    return True


def hard_filter(
    key: FilterKey, candidates: Iterable[FilterCandidate]
) -> tuple[FilteredCandidate, ...]:
    """按硬过滤键过滤候选集。

    键不可用（缺项目码，或既无部位序号又无蜡块号）时返回空元组 —— 不做
    「全都算命中」的退化过滤。
    """
    if not key.usable:
        return ()

    hits: list[FilteredCandidate] = []
    for candidate in candidates:
        if _hard_match(key, candidate):
            hits.append(
                FilteredCandidate(
                    candidate=candidate,
                    outcome=FilterOutcome.HARD,
                    score=1.0,
                    pending=False,
                )
            )
    return tuple(hits)


# ---------------------------------------------------------------- 模糊降级


def _item_compatible(key: FilterKey, candidate: FilterCandidate) -> bool:
    """项目码是否一致。

    降级只放宽「位置」，**不放宽「项目」**：CD20 的回报不是 CK20 的回报。
    放宽项目码就是把两项检查混成一项 —— 那是误并入。
    """
    key_item = key.normalized_item_code
    if not key_item:
        return False
    return candidate.normalized_item_code == key_item


def fuzzy_filter(
    key: FilterKey,
    candidates: Iterable[FilterCandidate],
    *,
    scorer: LexicalScorer | None = None,
    limit: int | None = None,
) -> tuple[FilteredCandidate, ...]:
    """硬过滤命中 0 条时的模糊降级。

    步骤：先按项目码筛出「同一项检查」的候选（放宽的只是位置），
    再按部位描述的词法相似度排序。

    **每一条都带 ``pending=True``** —— 模糊匹配给的是「像」，不是「是」。
    相似度 0.87 分既可能是 B3 也可能是 B5，所以这一层的结果只能进待人工
    处理清单，不能直接并入草稿。

    ``limit`` 给定时只保留前 N 条（对应源项目的 ``top_k``）。
    """
    pool = [
        candidate
        for candidate in candidates
        if _item_compatible(key, candidate)
    ]
    if not pool:
        return ()

    if scorer is None:
        scorer = LexicalScorer(
            [candidate.region_desc for candidate in pool if candidate.region_desc]
        )

    # 逐条打分而不是用 ``score_candidates``：后者按分重排后返回，位置就对不回
    # 原候选了（部位描述重复时更会张冠李戴）。这里自己排，同分保持原顺序 ——
    # 排序结果因此与候选传入顺序无关地稳定。
    scored = [
        (scorer.score_detail(key.region_desc, candidate.region_desc).score, index)
        for index, candidate in enumerate(pool)
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))

    hits: list[FilteredCandidate] = []
    for score, index in scored:
        hits.append(
            FilteredCandidate(
                candidate=pool[index],
                outcome=FilterOutcome.FUZZY,
                score=score,
                pending=True,
                pending_reason=REASON_FUZZY_DEGRADED,
            )
        )
        if limit is not None and len(hits) >= limit:
            break
    return tuple(hits)


# ---------------------------------------------------------------- 入口


def apply_filter(
    key: FilterKey,
    candidates: Iterable[FilterCandidate],
    *,
    policy: FilterPolicy | str = FilterPolicy.MERGE,
    scorer: LexicalScorer | None = None,
    limit: int | None = None,
) -> FilterResult:
    """按策略过滤候选集，返回过滤结论。

    ``REPLACE``：

    - 硬过滤命中 → 结果集 = 硬过滤命中项，模糊候选不进入结果集；
    - 硬过滤命中 0 条 → 结果集为空，本条 ``pending=True`` 并带原因
      （**该条不静默丢弃**，上层据此挂起）。

    ``MERGE``：

    - 硬过滤命中 → 同上（模糊候选不混入，否则同一条结果会在硬过滤与模糊
      两路各出现一次，与 id 25 的幂等键去重口径打架）；
    - 硬过滤命中 0 条 → **降级**为模糊匹配，结果集非空时每条带
      ``pending=True`` 与原因；结果集仍为空（连项目码都不兼容）时，
      本条 ``pending=True`` 并带「召回命中 0 条」。
    """
    resolved = FilterPolicy(policy)
    pool = tuple(candidates)
    hard = hard_filter(key, pool)

    if hard:
        return FilterResult(
            key=key,
            policy=resolved,
            items=hard,
            hard_hits=len(hard),
            fuzzy_hits=0,
            degraded=False,
            pending=False,
            pending_reason=None,
        )

    if resolved is FilterPolicy.REPLACE:
        return FilterResult(
            key=key,
            policy=resolved,
            items=(),
            hard_hits=0,
            fuzzy_hits=0,
            degraded=False,
            pending=True,
            pending_reason=REASON_INCOMPLETE_KEY if not key.usable else REASON_NO_HIT,
        )

    fuzzy = fuzzy_filter(key, pool, scorer=scorer, limit=limit)
    # 原因取**根因**：键字段不全时，「硬过滤键字段不全」比「命中 0 条」更可
    # 操作 —— 技术员看到前者知道该去补部位序号或蜡块号，而不是再去查回报。
    if not key.usable:
        reason = REASON_INCOMPLETE_KEY
    elif fuzzy:
        reason = REASON_FUZZY_DEGRADED
    else:
        reason = REASON_NO_HIT

    return FilterResult(
        key=key,
        policy=resolved,
        items=fuzzy,
        hard_hits=0,
        fuzzy_hits=len(fuzzy),
        degraded=True,
        pending=True,
        pending_reason=reason,
    )
