"""对齐依据（**可解释层**）。

每条并入草稿的结果都要能回答一句话：**凭什么算这条 B3 块**。

一条依据带六样东西（顺序即展示顺序）：

====================  ==========================================================
``match_level``      命中方式：L1 蜡块号精确 / L2 部位描述精确 / L3 词法模糊 / L4 挂起
``score``            得分：L1 / L2 恒为 1.0（确定性命中），L3 是词法分
``recall_key``       召回键：``部位序号|蜡块号|项目码``
``source_system``    来源系统：这条结果是从哪个系统回来的
``observed_at``      回报时间：这条结果是几点回来的
``confidence``       置信等级：L1 高 / L2 中 / L3 低；**挂起没有等级**
====================  ==========================================================

**为什么挂起没有置信等级**：置信回答的是「这条有多可信」，而挂起的结论是
「我们不敢下结论」—— 那不是「可信度低」，是「没有结论」。挂起挂一个「低」
会让统计口径失真：L3 已经有「低」了，值班的人会把两类混在一起看。

**依据落到两处**：

- **审计流水**：一条结果一条依据行（:func:`record_alignment_audit`）。医师复核
  时能顺着这条依据走一遍，内审 / 飞检语境下也拿得出「凭什么」；
- **草稿条目标注**：草稿正文逐项标出来源系统与回报时间与命中方式
  （见 :mod:`patho_supplement.compose.draft_renderer`），签发时不必回头翻
  原始报文。

**缺失值不装成有值**：回报时间缺失就写「时间未知」，并在
:func:`missing_value_fields` 里点名 —— 空栏会被读成漏填，猜一个时间更糟。
"""

from __future__ import annotations

from typing import Any

from ..audit.ledger import (
    CATEGORY_ALIGN,
    EVENT_ALIGNMENT_RATIONALE,
    record_audit,
)
from ..db import Database
from ..models import Alignment, MatchLevel, ResultEntry
from ..timefmt import format_observed_at
from .matcher import MATCH_LEVEL_LABELS, MatchCandidate, MatchOutcome, filter_key_text

__all__ = [
    "CONFIDENCE_BY_LEVEL",
    "RATIONALE_FIELDS",
    "build_alignment",
    "build_pending_alignment",
    "confidence_of",
    "is_complete",
    "missing_value_fields",
    "rationale_detail",
    "rationale_fields",
    "rationale_summary",
    "rationale_tag",
    "record_alignment_audit",
]

#: 对齐依据的六个必备字段（顺序即展示顺序）。
RATIONALE_FIELDS: tuple[str, ...] = (
    "match_level",
    "score",
    "recall_key",
    "source_system",
    "observed_at",
    "confidence",
)

#: 置信等级映射：L1 高 / L2 中 / L3 低；**挂起无等级**。
#: 与 :attr:`patho_supplement.models.MatchLevel.confidence` 是同一套口径
#: （有用例锁住两者不漂移）。
CONFIDENCE_BY_LEVEL: dict[MatchLevel, str] = {
    MatchLevel.L1_BLOCK_EXACT: "高",
    MatchLevel.L2_REGION_NORMALIZED: "中",
    MatchLevel.L3_REGION_FUZZY: "低",
    MatchLevel.L4_PENDING: "无",
}

#: 底层值缺失时的展示占位 —— 不写空栏，空栏会被读成漏填。
UNKNOWN = "未标注"


def confidence_of(level: MatchLevel) -> str:
    """取某个命中方式对应的置信等级。"""
    return CONFIDENCE_BY_LEVEL.get(level, level.confidence)


def build_alignment(entry: ResultEntry, candidate: MatchCandidate) -> Alignment:
    """根据命中结果生成对齐依据（已并入草稿的那一类）。

    依据里的部位与蜡块号取**命中块**的登记值（不是回报自带的）—— 对上的
    那一刻，报关的定位信息与基线登记就是同一个块了。
    """
    block = candidate.block
    return Alignment(
        accession_no=entry.accession_no,
        item_code=entry.item_code,
        match_level=candidate.match_level,
        score=candidate.score,
        recall_key=candidate.recall_key,
        block_no=block.block_no,
        region_seq=block.region_seq,
        region_desc=block.region_desc,
        source_system=entry.source_system,
        observed_at=entry.observed_at,
        matched_terms=tuple(candidate.matched_terms),
    )


def build_pending_alignment(entry: ResultEntry, outcome: MatchOutcome) -> Alignment:
    """没对上的一条结果也留一条依据 —— 但**没有置信等级**。

    依据里带挂起原因与需补齐字段：技术人员拿着这一行就知道该去补哪张单。
    召回键用**回报自带的**定位信息拼 —— 此刻还没有命中块，只能用它。
    """
    return Alignment(
        accession_no=entry.accession_no,
        item_code=entry.item_code,
        match_level=MatchLevel.L4_PENDING,
        score=0.0,
        recall_key=filter_key_text(entry.region_seq, entry.block_no, entry.item_code),
        block_no=entry.block_no,
        region_seq=entry.region_seq,
        region_desc=entry.region_desc,
        source_system=entry.source_system,
        observed_at=entry.observed_at,
        pending_reason=outcome.pending_reason,
        missing_fields=tuple(outcome.missing_fields),
    )


def rationale_fields(alignment: Alignment) -> dict[str, str]:
    """依据的六个字段（展示用，**不留空**）。

    缺的值写成「未标注」/「时间未知」—— 展示层不留空栏，空栏会被读成漏填。
    """
    return {
        "match_level": MATCH_LEVEL_LABELS.get(
            alignment.match_level.value, alignment.match_level.value
        ),
        "score": f"{alignment.score:.3f}",
        "recall_key": alignment.recall_key or UNKNOWN,
        "source_system": alignment.source_system or UNKNOWN,
        "observed_at": format_observed_at(alignment.observed_at),
        "confidence": alignment.confidence,
    }


def missing_value_fields(alignment: Alignment) -> tuple[str, ...]:
    """底层值实际缺失的字段（展示层补了占位，这里点名）。

    空回报时间不算致命 —— 报告照样能写；但内审问「这条什么时候回来的」，
    答不上来就是缺口，要能查得出来。
    """
    missing: list[str] = []
    if not (alignment.recall_key or "").strip():
        missing.append("recall_key")
    if not (alignment.source_system or "").strip():
        missing.append("source_system")
    if not (alignment.observed_at or "").strip():
        missing.append("observed_at")
    return tuple(missing)


def is_complete(alignment: Alignment) -> bool:
    """这条依据的六个字段是不是都齐。

    齐 = 展示值非空 **且** 底层值不缺（不靠占位蒙混）。
    """
    fields = rationale_fields(alignment)
    if any(not str(value).strip() for value in fields.values()):
        return False
    return not missing_value_fields(alignment)


def rationale_summary(alignment: Alignment) -> str:
    """依据的一行可读摘要（进审计明细，也进草稿正文标注）。"""
    fields = rationale_fields(alignment)
    parts = [
        fields["match_level"],
        f"置信 {fields['confidence']}",
        f"得分 {fields['score']}",
        f"召回键 {fields['recall_key']}",
        f"来源 {fields['source_system']}",
        f"回报时间 {fields['observed_at']}",
    ]
    if alignment.pending_reason:
        parts.append(f"挂起原因 {alignment.pending_reason}")
    if alignment.matched_terms:
        parts.append(f"命中词 {'/'.join(alignment.matched_terms)}")
    return "｜".join(parts)


def rationale_tag(alignment: Alignment) -> str:
    """依据的短标注（写进草稿正文每一条的末尾）。

    医师在草稿上看到的是「命中方式 + 置信等级」两个词：L3 的那几条一眼就
    能挑出来重点复核 —— 置信「低」意味着这条是**靠文字相似**对上的。
    """
    fields = rationale_fields(alignment)
    return f"{fields['match_level']}（置信 {fields['confidence']}）"


def rationale_detail(alignment: Alignment) -> dict[str, Any]:
    """依据的审计明细（机器可读）。

    只含业务标识（病理号 / 项目码 / 部位 / 蜡块号），**不含**姓名、身份证、
    联系方式等患者可识别明细 —— 审计行会被人带走。
    """
    detail = alignment.to_dict()
    detail["match_level_label"] = rationale_fields(alignment)["match_level"]
    detail["summary"] = rationale_summary(alignment)
    detail["missing_value_fields"] = list(missing_value_fields(alignment))
    detail["complete"] = is_complete(alignment)
    return detail


def record_alignment_audit(
    database: Database,
    alignment: Alignment,
    *,
    event: str = EVENT_ALIGNMENT_RATIONALE,
) -> None:
    """把一条对齐依据写进审计流水。

    ``event`` 默认是「已并入草稿」；挂起的结果用
    :data:`patho_supplement.audit.ledger.EVENT_ALIGNMENT_PENDING` 另记一类，
    这样按事件统计时，「对上了几条」与「挂起了几条」口径清楚。
    """
    record_audit(
        database,
        category=CATEGORY_ALIGN,
        event=event,
        accession_no=alignment.accession_no,
        detail=rationale_detail(alignment),
    )
