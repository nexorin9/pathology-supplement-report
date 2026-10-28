"""朴素基线：院内两种常见做法，作为对照实验的**对照组**。

**这两个实现不是产品的一部分**，它们存在的原因只有一个：让「本产品比朴素做法
好在哪」变成**能复算的数字**，而不是一句宣传话。

两种做法都是真实存在于病理科的：

===========================  ====================================================
``exact_normalized``        **基线 A：仅规范化精确匹配**。把蜡块号 / 部位描述
                            归一化后按相等去查表，对得上就归那块，对不上就
                            挂起等人。多个命中取第一个（查表的默认行为）。
``lexical_top_score``       **基线 B：仅取词法最高分**。对所有候选块算词法
                            相似度，取分最高的那块；**同分直接取第一个**。
===========================  ====================================================

**它们故意不做的事**（正是这些「不做」构成对照组的失败形态）：

- 不查「这块有没有申请过这个项目」—— 所以会把没登记的结果并进报告；
- 不看申请侧状态闸门（已取消 / 已终止）—— 所以被取消的项目结果照样进报告；
- 不做「同分多候选一律挂起」—— 基线 B 遇到并列直接取第一，猜错就是错绑；
- 不做同义折算（基线 A）—— 「宫体」和「子宫体」在它眼里是两个部位。

**三方共用同一份前置判定**：入站去重、更正取代、结果质量（无法获得 / 值为空）
与回报时间倒挂在三方之间是**同一份结论**，差异只在「结果归到哪一块」这一步。
把口径写清楚，对照实验才站得住 —— 否则测的就是别的差异。

**打分器是共用的**：基线 B 用的是产品那套中文 BM25 与同一部同义词典
（见 :mod:`patho_supplement.align.scorer`）。共用词汇、只换策略，差异才归得到
策略头上；否则测的是「谁的词表大」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import Block, ReportBaseline, ResultEntry
from ..pending.reasons import PendingReason, reason_for_entry
from .metrics import AlignmentDecision, ResultKey
from .normalizer import normalize_block_no, normalize_region_desc, same_block_no, same_region_desc
from .scorer import LexicalScorer

__all__ = [
    "BASELINE_A_LABEL",
    "BASELINE_B_LABEL",
    "BaselineResult",
    "exact_normalized_decisions",
    "lexical_top_score_decisions",
    "quality_gate_decisions",
    "run_baseline",
]

#: 基线 A 的中文标签（写进对照表）。
BASELINE_A_LABEL = "基线 A｜仅规范化精确匹配"
#: 基线 B 的中文标签（写进对照表）。
BASELINE_B_LABEL = "基线 B｜仅取词法最高分"

#: 朴素做法没有「同分多候选」这套标签，一律归到「未命中部位」或具体缺项。
REASON_NO_MATCH = PendingReason.NO_MATCH.value
REASON_NO_BASELINE = PendingReason.NO_BASELINE.value


@dataclass(frozen=True)
class BaselineResult:
    """朴素做法对一条结果给出的结论。"""

    key: ResultKey
    matched: bool
    block: tuple[int | None, str | None] | None = None
    reason: str | None = None
    note: str = ""

    def as_decision(self) -> AlignmentDecision:
        """翻译成指标层认识的结论。"""
        return AlignmentDecision(
            key=self.key,
            matched=self.matched,
            block=self.block,
            reason=self.reason,
            rationale=self.note or ("挂起" if not self.matched else None),
        )


def _key_of(entry: ResultEntry) -> ResultKey:
    return ResultKey.from_entry(entry)


def _block_key(block: Block) -> tuple[int | None, str | None]:
    return (block.region_seq, block.block_no)


def quality_gate_decisions(
    withheld: Sequence[ResultEntry], inverted: Sequence[ResultEntry]
) -> tuple[AlignmentDecision, ...]:
    """结果质量前置判定（**三方共用**）。

    - ``withheld``：已回报但拿不到结果 / 值为空 —— 一律不并入报告；
    - ``inverted``：回报时间倒挂、没生效的更正 —— 等人确认。

    这两类与「归到哪一块」无关，三方结论必须一致，否则对照的不是策略。
    """
    decisions: list[AlignmentDecision] = []
    for entry in withheld:
        reason = reason_for_entry(entry) or PendingReason.RESULT_NOT_OBTAINED
        decisions.append(
            AlignmentDecision(
                key=_key_of(entry),
                matched=False,
                reason=reason.value,
                rationale="结果质量前置判定（无法获得 / 值为空）",
            )
        )
    for entry in inverted:
        decisions.append(
            AlignmentDecision(
                key=_key_of(entry),
                matched=False,
                reason=PendingReason.TIME_INVERSION.value,
                rationale="结果质量前置判定（回报时间倒挂）",
            )
        )
    return tuple(decisions)


def _exact_normalized(
    entry: ResultEntry, baseline: ReportBaseline | None
) -> BaselineResult:
    """基线 A：**仅规范化精确匹配**。

    蜡块号归一化后相等就算命中；没有蜡块号就按部位描述归一化后相等算命中；
    多个命中取第一个；都不命中就挂起。

    不查已申请项目、不看取消状态、不做同义折算 —— 朴素做法就这么多。
    """
    key = _key_of(entry)
    if baseline is None:
        return BaselineResult(key, False, reason=REASON_NO_BASELINE, note="基线里没有这份报告")

    block_key = normalize_block_no(entry.block_no)
    if block_key:
        hits = [
            block for block in baseline.blocks if same_block_no(block.block_no, block_key)
        ]
        if hits:
            return BaselineResult(
                key,
                True,
                block=_block_key(hits[0]),
                note=f"蜡块号规范化后精确命中 {hits[0].block_no}（命中 {len(hits)} 块，取第一）",
            )
        return BaselineResult(
            key, False, reason=REASON_NO_MATCH, note=f"基线里没有蜡块号 {entry.block_no}"
        )

    region_key = normalize_region_desc(entry.region_desc)
    if region_key:
        hits = [
            block
            for block in baseline.blocks
            if same_region_desc(block.region_desc, region_key)
        ]
        if hits:
            return BaselineResult(
                key,
                True,
                block=_block_key(hits[0]),
                note=f"部位描述规范化后精确命中 {hits[0].region_desc}（命中 {len(hits)} 块，取第一）",
            )
    return BaselineResult(
        key, False, reason=REASON_NO_MATCH, note="规范化后没有精确相等的部位"
    )


def _lexical_top_score(
    entry: ResultEntry, baseline: ReportBaseline | None
) -> BaselineResult:
    """基线 B：**仅取词法最高分，遇歧义直接取第一**。

    对报告里的每一块算词法相似度，取最高分那块；并列时按候选顺序取第一。
    最高分为 0（完全不相关）就挂起。

    不查已申请项目、不看取消状态、不要求唯一领先 —— 同分就是猜，猜错就是错绑。
    """
    key = _key_of(entry)
    if baseline is None:
        return BaselineResult(key, False, reason=REASON_NO_BASELINE, note="基线里没有这份报告")

    corpus = [block.region_desc or "" for block in baseline.blocks]
    scorer = LexicalScorer(corpus)
    best_score = -1.0
    best_block: Block | None = None
    tied = 0
    for block in baseline.blocks:
        score = scorer.score(entry.region_desc, block.region_desc)
        if score > best_score:
            best_score = score
            best_block = block
            tied = 1
        elif score == best_score:
            tied += 1

    if best_block is None or best_score <= 0.0:
        return BaselineResult(
            key, False, reason=REASON_NO_MATCH, note="没有词法相关的候选部位"
        )
    return BaselineResult(
        key,
        True,
        block=_block_key(best_block),
        note=(
            f"词法最高分 {best_score:.4f} → {best_block.region_desc}"
            f"（并列 {tied} 块，取第一）"
        ),
    )


def run_baseline(
    strategy: str,
    entries: Sequence[ResultEntry],
    baselines: dict[str, ReportBaseline],
    *,
    withheld: Sequence[ResultEntry] = (),
    inverted: Sequence[ResultEntry] = (),
) -> tuple[AlignmentDecision, ...]:
    """跑一个朴素做法，产出与产品同格式的结论序列。

    ``strategy`` 取 ``exact_normalized``（基线 A）或 ``lexical_top_score``（基线 B）。
    """
    if strategy == "exact_normalized":
        matcher = _exact_normalized
    elif strategy == "lexical_top_score":
        matcher = _lexical_top_score
    else:
        raise ValueError(f"未知的朴素做法：{strategy!r}")

    decisions = [
        matcher(entry, baselines.get(entry.accession_no)).as_decision() for entry in entries
    ]
    decisions.extend(quality_gate_decisions(withheld, inverted))
    return tuple(decisions)


def exact_normalized_decisions(
    entries: Sequence[ResultEntry],
    baselines: dict[str, ReportBaseline],
    *,
    withheld: Sequence[ResultEntry] = (),
    inverted: Sequence[ResultEntry] = (),
) -> tuple[AlignmentDecision, ...]:
    """基线 A 的结论序列（仅规范化精确匹配）。"""
    return run_baseline(
        "exact_normalized", entries, baselines, withheld=withheld, inverted=inverted
    )


def lexical_top_score_decisions(
    entries: Sequence[ResultEntry],
    baselines: dict[str, ReportBaseline],
    *,
    withheld: Sequence[ResultEntry] = (),
    inverted: Sequence[ResultEntry] = (),
) -> tuple[AlignmentDecision, ...]:
    """基线 B 的结论序列（仅取词法最高分）。"""
    return run_baseline(
        "lexical_top_score", entries, baselines, withheld=withheld, inverted=inverted
    )
