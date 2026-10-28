"""部位-蜡块四级匹配（**归属绑定的收口主干**）。

**为什么是四级**：病理回报里能用来指认「这条结果属于哪一块」的字段只有一个 ——
蜡块号。它可能缺（回报没填）、可能写错（基线写 ``B-3``、回报写 ``b03``）、
也可能与部位描述互相矛盾。所以判定**逐级降级**，每一级都比上一级弱：

======================  ==========================================================
``L1_block_exact``      蜡块号归一化后精确命中，且只有一块命中。
``L2_region_normalized``没有可用蜡块号时，部位描述归一化后精确命中（或折算到
                        同一规范词 —— 别名是病理科自己维护的等价关系），
                        且只有一块命中。
``L3_region_fuzzy``     部位描述词法相似度（中文 BM25）达阈值，且**唯一领先**。
``L4_pending``          都不成立 → 挂起，等人补齐，绝不猜。
======================  ==========================================================

**绝不猜**：同级出现多个候选（同分、或者领先幅度不够）一律挂起，不取第一个。
绑定错一块就是一份签出去却写错部位的补充报告 —— 这在病理科是事故，不是瑕疵。

**两个位置字段互相矛盾时也不绑**：回报写「宫颈」而基线那一块登记的是「子宫体」，
说明两个字段里有一个是错的。这种情况**判为冲突并挂起**，而不是「挑一个信」。

**候选块必须申请过这个项目**：结果只可能落在申请过它的块上。这一条把
「同一份报告里的加做项目落到另一块」这类误绑挡在外面 —— 也让「这个项目压根
没在任何块上申请」的结果老老实实挂起等人查登记，而不是硬绑到部位相近的块上。

**阈值外置可配**（见 :class:`MatchThresholds`）：不同科室的部位描述习惯不同，
「词法分多少才算像」「领先多少才算唯一」就该由科室自己调。阈值越界**报错并
说明原因**，不静默改用默认值 —— 「配错了却看不出来」是院内工具最常见的投诉来源。

**与源产品的关系**：源项目的检索后端是 embedding 相似度检索
（``github_ref/haystack/haystack/components/retrievers/text_embedding_retriever.py``、
``.../retrievers/in_memory/embedding_retriever.py``），相似度给不出「这条就是
B3 块」这种确定性绑定 —— 0.87 分既可能是 B3 也可能是 B5。本模块把那个后端换成
**归一化 + 词法打分 + 阈值 + 唯一领先**的确定性判定（打分器见
:mod:`patho_supplement.align.scorer`），并把「同分怎么办」的答案从向量检索的
「取 top-k」改成**挂起等人**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

from ..models import Block, CheckItemState, MatchLevel, ReportBaseline, ResultEntry
from .lexicon import Lexicon, builtin_lexicon
from .normalizer import (
    normalize_block_no,
    normalize_region_desc,
    same_block_no,
    same_region_desc,
)
from .scorer import LexicalScorer

__all__ = [
    "DEFAULT_BM25_THRESHOLD",
    "DEFAULT_LEAD_MARGIN_THRESHOLD",
    "MATCH_LEVEL_LABELS",
    "MatchCandidate",
    "MatchConfigError",
    "MatchOutcome",
    "MatchState",
    "MatchThresholds",
    "align_entries",
    "align_entry",
    "build_filter_key",
    "filter_key_text",
    "group_by_block",
    "summarize_match_levels",
]

#: 申请侧状态闸门要拦的状态（已取消 / 已终止的待查项，其结果不进草稿）。
#: 闸门实现见 :mod:`patho_supplement.align.order_guard`。
BLOCKED_ITEM_STATES: tuple[CheckItemState, ...] = (
    CheckItemState.CANCELLED,
    CheckItemState.TERMINATED,
)

#: L3 词法匹配的最低分（BM25 归一化到 0–1 之后）。
DEFAULT_BM25_THRESHOLD = 0.32
#: L3 「唯一领先」所需的最低分差：首名与次名之差不得小于它。
DEFAULT_LEAD_MARGIN_THRESHOLD = 0.05


class MatchConfigError(ValueError):
    """判定阈值越界。

    消息面向运维：必须一眼看出「哪一项填错了、合法范围是多少」。
    """


def _check_threshold(name: str, value: float) -> None:
    """校验一个阈值：必须是 0–1 之间的有限数。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MatchConfigError(f"{name} 必须是数值，当前值 {value!r}") from exc
    if not math.isfinite(number):
        raise MatchConfigError(f"{name} 必须是有限数值，当前值 {value!r}")
    if not 0.0 <= number <= 1.0:
        raise MatchConfigError(f"{name} 必须在 0 与 1 之间，当前值 {number}")


@dataclass(frozen=True)
class MatchThresholds:
    """判定阈值（外置可配）。

    - ``bm25_threshold``：L3 词法匹配的最低分，低于它不成立；
    - ``lead_margin_threshold``：L3 首名与次名的最小分差，不够即视为
      「不唯一领先」→ 挂起。

    越界一律 **报错并说明原因**，不静默改用默认值。
    """

    bm25_threshold: float = DEFAULT_BM25_THRESHOLD
    lead_margin_threshold: float = DEFAULT_LEAD_MARGIN_THRESHOLD

    def __post_init__(self) -> None:
        _check_threshold("BM25 阈值", self.bm25_threshold)
        _check_threshold("领先幅度阈值", self.lead_margin_threshold)

    @classmethod
    def from_config(cls, config: Any) -> "MatchThresholds":
        """从配置面取阈值（缺项用内置默认值）。"""
        return cls(
            bm25_threshold=getattr(config, "bm25_threshold", DEFAULT_BM25_THRESHOLD),
            lead_margin_threshold=getattr(
                config, "lead_margin_threshold", DEFAULT_LEAD_MARGIN_THRESHOLD
            ),
        )

    def as_dict(self) -> dict[str, float]:
        """可打印的阈值视图（写进运行摘要可查）。"""
        return {
            "bm25_threshold": self.bm25_threshold,
            "lead_margin_threshold": self.lead_margin_threshold,
        }


class MatchState:
    """对齐结论的几种状态。"""

    MATCHED = "matched"                    # 对上了
    MISSING_BLOCK_NO = "missing_block_no"  # 回报没带蜡块号，且部位描述也没能定到块
    NO_BLOCK = "no_block"                  # 基线里没有对应部位
    ITEM_NOT_ORDERED = "item_not_ordered"  # 部位对上了，但这个项目没在基线里申请
    NO_BASELINE = "no_baseline"            # 病理号在基线里找不到
    AMBIGUOUS = "ambiguous"                # 多个候选同分 / 领先不够，不猜
    CONFLICT = "conflict"                  # 蜡块号与部位描述互相矛盾
    FIELDS_MISSING = "fields_missing"      # 蜡块号与部位描述都缺，没有定位依据
    ITEM_CANCELLED = "item_cancelled"      # 待查项已被取消，结果不并入草稿
    ITEM_TERMINATED = "item_terminated"    # 待查项已被终止，结果不并入草稿


#: 挂起原因标签（写进对齐依据与待人工处理清单）。
REASON_MISSING_BLOCK_NO = "缺蜡块号"
REASON_NO_BASELINE = "病理号无基线"
REASON_NO_BLOCK = "未命中部位"
REASON_ITEM_NOT_ORDERED = "待查项未登记"
REASON_AMBIGUOUS = "同分多候选"
REASON_CONFLICT = "部位与蜡块号冲突"
REASON_FUZZY_BELOW_THRESHOLD = "词法匹配未达阈值"
#: 蜡块号与部位描述都缺 —— 没有定位依据，只能等人补。
REASON_FIELDS_MISSING = "字段缺失"
#: 待查项被取消 / 终止 —— 结果不并入草稿，等人处置。
REASON_ITEM_CANCELLED = "待查项已取消"
REASON_ITEM_TERMINATED = "待查项已终止"

#: 四级的中文标签（写进运行摘要，运维一眼看出今天有几条是「靠文字猜」的）。
MATCH_LEVEL_LABELS: dict[str, str] = {
    MatchLevel.L1_BLOCK_EXACT.value: "L1 蜡块号精确",
    MatchLevel.L2_REGION_NORMALIZED.value: "L2 部位描述精确",
    MatchLevel.L3_REGION_FUZZY.value: "L3 词法模糊",
    MatchLevel.L4_PENDING.value: "L4 挂起",
}


@dataclass(frozen=True)
class MatchCandidate:
    """一个候选部位及其命中依据。"""

    block: Block
    match_level: MatchLevel
    score: float
    recall_key: str
    #: L3 命中的词（写进对齐依据，医师能看懂凭什么）。
    matched_terms: tuple[str, ...] = ()

    @property
    def confidence(self) -> str:
        """置信等级：L1 高 / L2 中 / L3 低。"""
        return self.match_level.confidence


@dataclass(frozen=True)
class MatchOutcome:
    """一条结果的对齐结论。

    - ``state``：对上 / 对不上的原因；
    - ``candidate``：对上时的那一块（带命中方式与分数）；
    - ``match_level``：落在哪一级（``L4_pending`` = 挂起）；
    - ``missing_fields``：需要人工补齐的字段；
    - ``candidates``：候选部位（挂起时给技术人员去查登记）；
    - ``pending_reason``：挂起原因标签。
    """

    state: str
    candidate: MatchCandidate | None = None
    missing_fields: tuple[str, ...] = ()
    candidates: tuple[Block, ...] = ()
    match_level: MatchLevel | None = None
    pending_reason: str | None = None

    @property
    def matched(self) -> bool:
        return self.state == MatchState.MATCHED and self.candidate is not None

    @property
    def level(self) -> MatchLevel:
        """落到的级别；未命中一律是 ``L4_pending``。"""
        return self.match_level or MatchLevel.L4_PENDING

    def as_dict(self) -> dict[str, Any]:
        """可打印的摘要视图（写进运行摘要与审计可查）。"""
        return {
            "state": self.state,
            "match_level": self.level.value,
            "confidence": self.level.confidence,
            "score": self.candidate.score if self.candidate else 0.0,
            "missing_fields": list(self.missing_fields),
            "pending_reason": self.pending_reason,
            "candidates": len(self.candidates),
        }


def build_filter_key(
    region_seq: int | None, block_no: str | None, item_code: str
) -> tuple[int | None, str | None, str]:
    """构造结构化硬过滤键：(部位序号, 蜡块号, 项目码)。"""
    return (region_seq, block_no, item_code)


def filter_key_text(region_seq: int | None, block_no: str | None, item_code: str) -> str:
    """硬过滤键的可读形式（用于对齐依据里的召回键）。"""
    return f"{region_seq if region_seq is not None else '-'}|{block_no or '-'}|{item_code}"


# ---------------------------------------------------------------- 判定辅助


def _norm_item_code(value: str | None) -> str:
    """项目码归一：去空白 + 转大写。"""
    if value is None:
        return ""
    return str(value).strip().upper()


def _item_compatible(block: Block, item_code: str) -> bool:
    """这块能不能承载这个项目。

    基线没登记已申请项目时**不拦**（信息不全不等于不成立）；登记了的话，
    项目码必须对得上 —— 结果只可能落在申请过它的块上。
    """
    if not block.ordered_items:
        return True
    if not item_code:
        return False
    return item_code in {_norm_item_code(code) for code in block.ordered_items}


def _seq_compatible(block: Block, entry: ResultEntry) -> bool:
    """部位序号是否冲突。

    任一侧缺部位序号 → **不算冲突**（缺字段不是矛盾）；两侧都有且不等 → 冲突。
    """
    if entry.region_seq is None or block.region_seq is None:
        return True
    return entry.region_seq == block.region_seq


def _same_region(
    entry_desc: str | None, block_desc: str | None, lexicon: Lexicon
) -> bool:
    """两个部位描述是否指同一部位（**确定性等价**，不是「像」）。

    两种情形算同一部位：

    1. 归一化后完全相同（「子宫体（内侧）」≡「子宫体内侧」）；
    2. 折算到**同一个规范词**（「宫体」≡「子宫体」）—— 别名是病理科自己维护的
       等价关系，属于确定性判定，因此算 L2 而不是 L3。

    其余情形（哪怕词法很像）都不算等价，只能走 L3 的阈值判定。
    """
    if same_region_desc(entry_desc, block_desc):
        return True
    left = lexicon.canonicalize(entry_desc)
    right = lexicon.canonicalize(block_desc)
    return left is not None and left == right


def _desc_conflict(entry_desc: str | None, block_desc: str | None, lexicon: Lexicon) -> bool:
    """两个部位描述是否互相矛盾。

    判「不冲突」的三种情形：

    1. 任一侧为空 —— 缺字段不是矛盾；
    2. 归一化后完全相同；
    3. 一个是另一个的子串（「子宫体」vs「子宫体内侧」）或折算到同一个规范词
       （「宫体」vs「子宫体」）—— 写法不同，部位相同。

    其余情形判为**冲突**：两个字段里必有一个是错的，此时不猜，交给人。
    """
    left = normalize_region_desc(entry_desc)
    right = normalize_region_desc(block_desc)
    if not left or not right:
        return False
    if left == right:
        return False
    if left in right or right in left:
        return False

    left_canonical = lexicon.canonicalize(left) or left
    right_canonical = lexicon.canonicalize(right) or right
    if left_canonical == right_canonical:
        return False
    if left_canonical in right_canonical or right_canonical in left_canonical:
        return False
    return True


@lru_cache(maxsize=1)
def _default_lexicon() -> Lexicon:
    """内置最小词表（进程内缓存一份，避免每次判定都重建索引）。"""
    return builtin_lexicon()


def _missing_fields(entry: ResultEntry, *fields: str) -> tuple[str, ...]:
    """需补齐字段：去重且保持给定顺序。"""
    result: list[str] = []
    for field_name in fields:
        if field_name not in result:
            result.append(field_name)
    return tuple(result)


def _blocked_outcome(
    entry: ResultEntry,
    state: CheckItemState,
    guard: Any,
    block: Block | None = None,
) -> MatchOutcome:
    """申请侧状态闸门拦下的结果 → 挂起（不并入草稿）。

    ``candidates`` 里带上那块「本来会归到」的位置，技术人员一眼看得出
    是哪一块上的哪一项被取消了。
    """
    query = guard.match_query(
        entry,
        region_seq=block.region_seq if block is not None else None,
        block_no=block.block_no if block is not None else None,
    )
    candidates: list[Block] = []
    if block is not None:
        candidates.append(block)
    if query is not None:
        query_block = guard.query_block(query)
        if not any(
            item.block_no == query_block.block_no
            and item.region_seq == query_block.region_seq
            for item in candidates
        ):
            candidates.append(query_block)

    if state is CheckItemState.CANCELLED:
        match_state = MatchState.ITEM_CANCELLED
        reason = REASON_ITEM_CANCELLED
    else:
        match_state = MatchState.ITEM_TERMINATED
        reason = REASON_ITEM_TERMINATED

    return MatchOutcome(
        state=match_state,
        candidates=tuple(candidates),
        missing_fields=(),
        match_level=MatchLevel.L4_PENDING,
        pending_reason=reason,
    )


def _matched_outcome(
    entry: ResultEntry,
    block: Block,
    level: MatchLevel,
    score: float,
    guard: Any = None,
    matched_terms: tuple[str, ...] = (),
) -> MatchOutcome:
    """命中之后的收口：**再过一遍申请侧状态闸门**。

    对齐之前那一遍闸门用的是回报自带的定位；回报没带蜡块号时，那一遍可能
    定位不到。这遍用命中块的位置复核 —— 这一条结果如果落在了被取消的块上，
    就更不能并进草稿。
    """
    if guard is not None:
        blocked = guard.blocked_state(
            entry, region_seq=block.region_seq, block_no=block.block_no
        )
        if blocked is not None:
            return _blocked_outcome(entry, blocked, guard, block)

    return MatchOutcome(
        state=MatchState.MATCHED,
        candidate=MatchCandidate(
            block=block,
            match_level=level,
            score=score,
            recall_key=filter_key_text(block.region_seq, block.block_no, entry.item_code),
            matched_terms=matched_terms,
        ),
        candidates=(block,),
        match_level=level,
    )


# ---------------------------------------------------------------- 主判定


def align_entry(
    entry: ResultEntry,
    baseline: ReportBaseline | None,
    *,
    thresholds: MatchThresholds | None = None,
    scorer: LexicalScorer | None = None,
    lexicon: Lexicon | None = None,
    guard: Any = None,
) -> MatchOutcome:
    """把一条结果对齐到基线里的某一块（**四级级联**）。

    判定顺序：

    0. **申请侧状态闸门**：待查项已被取消 / 终止 → 直接挂起，不进草稿。
    1. **L1 蜡块号精确**：归一化后命中且唯一 → 命中（置信「高」）。
       命中多块 → 同分歧义，挂起；蜡块号给了但基线里没有 → 对不上，挂起
       （不拿部位描述去「救」一个可能写错的蜡块号 —— 宁可等人补齐）。
    2. **L2 部位描述精确**：没有可用蜡块号时，部位描述归一化后唯一命中
       （置信「中」）。
    3. **L3 词法模糊**：部位描述词法相似度达阈值且**唯一领先**（置信「低」）。
    4. **L4 挂起**：以上都不成立。

    每一级命中后都要过「项目申请」与「部位描述不矛盾」两道关；过不去就挂起，
    不换一块继续试 —— 换一块试就是误绑的起点。

    ``guard`` 是申请侧状态闸门（见
    :mod:`patho_supplement.align.order_guard`）；不给就跳过这道闸门。
    """
    active_thresholds = thresholds if thresholds is not None else MatchThresholds()
    active_lexicon = lexicon if lexicon is not None else _default_lexicon()

    # 申请侧状态闸门：待查项已被取消 / 终止的，结果不进草稿。
    if guard is not None:
        blocked = guard.blocked_state(entry)
        if blocked is not None:
            return _blocked_outcome(entry, blocked, guard)

    if baseline is None:
        return MatchOutcome(
            state=MatchState.NO_BASELINE,
            missing_fields=("baseline",),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_NO_BASELINE,
        )

    blocks = tuple(baseline.blocks)
    item_code = _norm_item_code(entry.item_code)
    block_key = normalize_block_no(entry.block_no)

    # ---------------------------------------------------------- L1 蜡块号精确
    if block_key is not None:
        l1_hits = tuple(
            block
            for block in blocks
            if same_block_no(block.block_no, block_key) and _seq_compatible(block, entry)
        )
        if len(l1_hits) == 1:
            block = l1_hits[0]
            if _desc_conflict(entry.region_desc, block.region_desc, active_lexicon):
                return MatchOutcome(
                    state=MatchState.CONFLICT,
                    candidates=(block,),
                    missing_fields=_missing_fields(entry, "block_no", "region_desc"),
                    match_level=MatchLevel.L4_PENDING,
                    pending_reason=REASON_CONFLICT,
                )
            if not _item_compatible(block, item_code):
                return MatchOutcome(
                    state=MatchState.ITEM_NOT_ORDERED,
                    candidates=(block,),
                    missing_fields=("item_code",),
                    match_level=MatchLevel.L4_PENDING,
                    pending_reason=REASON_ITEM_NOT_ORDERED,
                )
            return _matched_outcome(
                entry, block, MatchLevel.L1_BLOCK_EXACT, 1.0, guard
            )
        if len(l1_hits) > 1:
            # 同一个蜡块号登记在两块上是登记笔误（切错蜡块、抄错号都可能）。
            # 但「这个项目只在其中一块上申请过」是硬事实 —— 在**申请过该项目的
            # 块**里再数一次命中：收窄候选集只会少绑（转挂起），不会错绑。
            eligible_hits = tuple(
                block for block in l1_hits if _item_compatible(block, item_code)
            )
            if len(eligible_hits) == 1:
                block = eligible_hits[0]
                if _desc_conflict(entry.region_desc, block.region_desc, active_lexicon):
                    return MatchOutcome(
                        state=MatchState.CONFLICT,
                        candidates=(block,),
                        missing_fields=_missing_fields(entry, "block_no", "region_desc"),
                        match_level=MatchLevel.L4_PENDING,
                        pending_reason=REASON_CONFLICT,
                    )
                return MatchOutcome(
                    state=MatchState.MATCHED,
                    candidate=MatchCandidate(
                        block=block,
                        match_level=MatchLevel.L1_BLOCK_EXACT,
                        score=1.0,
                        recall_key=filter_key_text(
                            block.region_seq, block.block_no, entry.item_code
                        ),
                    ),
                    candidates=(block,),
                    match_level=MatchLevel.L1_BLOCK_EXACT,
                )
            return MatchOutcome(
                state=MatchState.AMBIGUOUS,
                candidates=l1_hits,
                missing_fields=("block_no",),
                match_level=MatchLevel.L4_PENDING,
                pending_reason=REASON_AMBIGUOUS,
            )
        # 蜡块号给了但基线里没有这块 → 对不上。不拿部位描述去救一个写错的
        # 蜡块号：报「B9」的回报可能真的是另一块，也可能只是笔误，猜不得。
        return MatchOutcome(
            state=MatchState.NO_BLOCK,
            candidates=blocks,
            missing_fields=_missing_fields(entry, "region_seq", "block_no"),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_NO_BLOCK,
        )

    # -------------------------------------------------- 没有可用蜡块号 → 部位层
    entry_region_key = normalize_region_desc(entry.region_desc)
    if not entry_region_key:
        # 蜡块号与部位描述都没有 —— 没有任何定位依据，直接 L4 挂起，
        # 标「字段缺失」：缺的是两个定位字段，不是「缺蜡块号」这一项。
        return MatchOutcome(
            state=MatchState.MISSING_BLOCK_NO,
            candidates=blocks,
            missing_fields=_missing_fields(entry, "block_no", "region_desc"),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_FIELDS_MISSING,
        )

    # 候选块必须申请过这个项目 —— 否则就是「把结果绑到没申请过它的块上」。
    eligible = tuple(block for block in blocks if _item_compatible(block, item_code))

    # ---------------------------------------------------------- L2 部位描述精确
    l2_hits = tuple(
        block
        for block in eligible
        if _same_region(block.region_desc, entry.region_desc, active_lexicon)
        and _seq_compatible(block, entry)
    )
    if len(l2_hits) == 1:
        block = l2_hits[0]
        return _matched_outcome(
            entry, block, MatchLevel.L2_REGION_NORMALIZED, 1.0, guard
        )
    if len(l2_hits) > 1:
        return MatchOutcome(
            state=MatchState.AMBIGUOUS,
            candidates=l2_hits,
            missing_fields=_missing_fields(entry, "block_no", "region_desc"),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_AMBIGUOUS,
        )

    # ---------------------------------------------------------- L3 词法模糊
    fuzzy_pool = tuple(
        block
        for block in eligible
        if normalize_region_desc(block.region_desc) and _seq_compatible(block, entry)
    )
    if not fuzzy_pool:
        return MatchOutcome(
            state=MatchState.MISSING_BLOCK_NO,
            candidates=blocks,
            missing_fields=("block_no",),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_MISSING_BLOCK_NO,
        )

    # 语料用**本报告全部带部位描述的块**算 IDF：词越罕见越关键，而这个词表
    # 的规模取决于报告有几个部位，不取决于这次能匹配上几个 —— 否则同一个
    # 部位在不同报告里会打出不同的分。
    corpus = [
        block.region_desc or ""
        for block in blocks
        if normalize_region_desc(block.region_desc)
    ]
    active_scorer = scorer or LexicalScorer(corpus, lexicon=active_lexicon)
    scored: list[tuple[float, tuple[str, ...], Block, int]] = []
    for index, block in enumerate(fuzzy_pool):
        detail = active_scorer.score_detail(entry.region_desc, block.region_desc)
        scored.append((detail.score, detail.matched_terms, block, index))
    # 按分降序；同分保持候选原序（结果与候选传入顺序无关）。
    scored.sort(key=lambda row: (-row[0], row[3]))

    top_score, top_terms, top_block, _ = scored[0]
    if top_score < active_thresholds.bm25_threshold:
        return MatchOutcome(
            state=MatchState.MISSING_BLOCK_NO,
            candidates=blocks,
            missing_fields=("block_no",),
            match_level=MatchLevel.L4_PENDING,
            pending_reason=REASON_FUZZY_BELOW_THRESHOLD,
        )

    if len(scored) > 1:
        second_score = scored[1][0]
        tied = second_score >= top_score
        if tied or top_score - second_score < active_thresholds.lead_margin_threshold:
            # 不唯一领先 → 全部并列候选挂起，不猜。
            contenders = tuple(
                row[2] for row in scored
                if row[0] >= top_score - active_thresholds.lead_margin_threshold
            )
            return MatchOutcome(
                state=MatchState.AMBIGUOUS,
                candidates=contenders,
                missing_fields=_missing_fields(entry, "block_no", "region_desc"),
                match_level=MatchLevel.L4_PENDING,
                pending_reason=REASON_AMBIGUOUS,
            )

    return _matched_outcome(
        entry,
        top_block,
        MatchLevel.L3_REGION_FUZZY,
        top_score,
        guard,
        matched_terms=top_terms,
    )


def align_entries(
    entries: Sequence[ResultEntry],
    baselines: dict[str, ReportBaseline],
    *,
    thresholds: MatchThresholds | None = None,
    scorer: LexicalScorer | None = None,
    lexicon: Lexicon | None = None,
    guard: Any = None,
) -> tuple[list[tuple[ResultEntry, MatchCandidate]], list[tuple[ResultEntry, MatchOutcome]]]:
    """批量对齐。

    返回 ``(已对上, 未对上)``；未对上的一条都不丢，交给上层挂起。

    ``guard`` 是申请侧状态闸门（已取消 / 已终止的待查项，其结果不进草稿）。
    """
    matched: list[tuple[ResultEntry, MatchCandidate]] = []
    unmatched: list[tuple[ResultEntry, MatchOutcome]] = []
    for entry in entries:
        outcome = align_entry(
            entry,
            baselines.get(entry.accession_no),
            thresholds=thresholds,
            scorer=scorer,
            lexicon=lexicon,
            guard=guard,
        )
        if outcome.matched and outcome.candidate is not None:
            matched.append((entry, outcome.candidate))
        else:
            unmatched.append((entry, outcome))
    return matched, unmatched


def summarize_match_levels(
    matched: Sequence[tuple[ResultEntry, MatchCandidate]],
    unmatched: Sequence[tuple[ResultEntry, MatchOutcome]],
) -> dict[str, int]:
    """四级判定分布（写进运行摘要）。

    病理科最关心的一件事是「今天这批结果里，有几条是**靠蜡块号**对上的、
    有几条是**靠部位描述**对上的、有几条是**靠文字相似**猜上的」——
    L3 的置信是「低」，这几条要请医师重点复核；L4 是挂起，等人补齐。

    四个级别恒定出现（没有的计 0），这样摘要里一眼能对上账。
    """
    counts = {level.value: 0 for level in MatchLevel}
    for _, candidate in matched:
        counts[candidate.match_level.value] += 1
    for _, outcome in unmatched:
        counts[MatchLevel.L4_PENDING.value] += 1
    return counts


def group_by_block(
    items: Iterable[tuple[ResultEntry, MatchCandidate]],
) -> dict[tuple[int | None, str | None], list[tuple[ResultEntry, MatchCandidate]]]:
    """按 (部位序号, 蜡块号) 分组，用于草稿按部位渲染。"""
    groups: dict[tuple[int | None, str | None], list[tuple[ResultEntry, MatchCandidate]]] = {}
    for entry, candidate in items:
        key = (candidate.block.region_seq, candidate.block.block_no)
        groups.setdefault(key, []).append((entry, candidate))
    return groups
