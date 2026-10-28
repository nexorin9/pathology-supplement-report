"""对齐指标：对齐率 / 错绑定数 / 挂起率 / 误并入率。

**这一层只做对账，不做判定。** 判定在 :mod:`patho_supplement.align.matcher`
里完成；本模块拿一批「对齐结论」与一份「基准集标准答案」逐条对账，算出四个数：

=========================  ==================================================
``alignment_rate``         对齐率 = 并入草稿的条数 ÷ 基准集条数
``misbound``               **错绑定数** = 并入草稿但归到的块不对的条数
``pending_rate``           挂起率 = 进了待人工处理清单的条数 ÷ 基准集条数
``false_merge_rate``       **误并入率** = 标准答案要求挂起却被并入的条数
=========================  ==================================================

**错绑定与误并入不是一回事**：错绑定是「归到了另一块」（报告上写着 B3，实际
是 B5）—— 报告内容错了；误并入是「根本不该进报告却进了」（这一项已取消 /
无法获得，却被写进草稿）—— 报告凭空多了一项。两条都要求为 0。

外加两道护栏：

- ``ambiguous_pending_rate``：**同分多候选挂起率**。基准集里「同分多候选」的
  样本有多少真的挂起了 —— 要求 100%。同分时取第一个就是在猜，猜错一块就是
  一份签出去却写错部位的补充报告。
- ``dangling``：**无第三去向**。既没有对齐依据（没进草稿）也没有挂起原因
  （没进清单）的条数 —— 要求 0。一条结果如果两边都没落，它就是**消失了**：
  医师在报告里看不到它，人工清单里也找不到它。

**指标与判定解耦**：本模块只认「结论」，不关心结论怎么来的。因此同一套指标
既能评本产品的四级匹配，也能评院内常见做法（只按规范化精确匹配、只取词法
最高分）—— 对照实验（见基准对照任务）用的就是这一套口径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..models import ResultEntry

__all__ = [
    "AMBIGUOUS_REASON",
    "AlignmentDecision",
    "AlignmentMetrics",
    "CaseVerdict",
    "GroundTruthCase",
    "GroundTruthSet",
    "ResultKey",
    "evaluate",
    "load_ground_truth",
]

#: 「同分多候选」的原因标签。与
#: :class:`patho_supplement.pending.reasons.PendingReason` 的 ``AMBIGUOUS``
#: 取值一致（有用例锁住两者不漂移）。
AMBIGUOUS_REASON = "同分多候选"

#: 结论与标准答案一致的判定标签。
VERDICT_OK = "一致"
#: 并入草稿但归到了另一块。
VERDICT_MISBOUND = "错绑定"
#: 标准答案要求挂起，却被并入草稿。
VERDICT_FALSE_MERGE = "误并入"
#: 标准答案要求对齐，却挂了起。
VERDICT_MISSED = "漏对齐"
#: 挂起了，但原因与标准答案写的不是同一类。
VERDICT_REASON_MISMATCH = "原因不符"
#: 既没进草稿也没进清单（结果消失了）。
VERDICT_DANGLING = "无去向"
#: 基准集里有这一条，结论里却没有。
VERDICT_MISSING = "缺样本"
#: 结论里多出基准集没有的条数。
VERDICT_EXTRA = "多出样本"


@dataclass(frozen=True, order=True)
class ResultKey:
    """一条结果的标识：**病理号 + 项目码 + 观察时间**。

    基准集里这三项合起来唯一标识一条结果 —— 同一份报告里同一个项目在不同观察
    时间上是两条不同的回报（初步 / 更正 / 换时间重做）。
    """

    accession_no: str
    item_code: str
    observed_at: str = ""

    @classmethod
    def from_entry(cls, entry: ResultEntry) -> "ResultKey":
        """从一条结果条目取标识。"""
        return cls(
            accession_no=(entry.accession_no or "").strip(),
            item_code=(entry.item_code or "").strip().upper(),
            observed_at=(entry.observed_at or "").strip(),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "observed_at": self.observed_at,
        }

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志与展示
        return f"{self.accession_no}/{self.item_code}/{self.observed_at or '-'}"


@dataclass(frozen=True)
class GroundTruthCase:
    """基准集里的一条标准答案。

    - ``expect_matched``：这一条**应该**并入草稿（``True``）还是挂起（``False``）；
    - ``expected_block``：应该归到哪一块（``(部位序号, 蜡块号)``）；只有
      ``expect_matched=True`` 时才填；
    - ``expected_reason``：挂起时的原因标签（人工看得懂的那种）。
    """

    key: ResultKey
    expect_matched: bool
    expected_block: tuple[int | None, str | None] | None = None
    expected_reason: str | None = None
    note: str = ""

    @property
    def is_ambiguous_case(self) -> bool:
        """这条是不是「同分多候选」样本（挂起率 100% 要求的统计口径）。"""
        return self.expected_reason == AMBIGUOUS_REASON


@dataclass(frozen=True)
class GroundTruthSet:
    """一份基准集标准答案。"""

    version: str = ""
    note: str = ""
    cases: tuple[GroundTruthCase, ...] = ()

    def __len__(self) -> int:
        return len(self.cases)


@dataclass(frozen=True)
class AlignmentDecision:
    """某个对齐实现对一条结果给出的结论。

    - ``matched``：并入草稿（``True``）还是挂起（``False``）；
    - ``block``：并入时归到的块（``(部位序号, 蜡块号)``）；
    - ``reason``：挂起时的原因标签；
    - ``rationale``：并入时的对齐依据摘要（草稿里那句「凭什么」）。
    """

    key: ResultKey
    matched: bool
    block: tuple[int | None, str | None] | None = None
    reason: str | None = None
    rationale: str | None = None

    @property
    def has_trace(self) -> bool:
        """这条结论有没有留下痕迹。

        并入草稿要有依据，挂起要有原因 —— 两边都没有就是「消失了」。
        """
        return bool(self.rationale) if self.matched else bool(self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "matched": self.matched,
            "block": list(self.block) if self.block else None,
            "reason": self.reason,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CaseVerdict:
    """一条样本的对账结论（写进指标明细，出了问题能直接定位到是哪条）。"""

    key: ResultKey
    verdict: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key.as_dict(), "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True)
class AlignmentMetrics:
    """一批结论对完账之后的指标。"""

    total: int = 0
    aligned: int = 0
    pending: int = 0
    misbound: int = 0
    false_merge: int = 0
    missed_match: int = 0
    dangling: int = 0
    ambiguous_cases: int = 0
    ambiguous_pending: int = 0
    reason_mismatch: int = 0
    duplicate_decisions: int = 0
    missing_decisions: int = 0
    extra_decisions: int = 0
    verdicts: tuple[CaseVerdict, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------ 比率

    @property
    def alignment_rate(self) -> float:
        """对齐率：并入草稿的条数 ÷ 基准集条数。"""
        return self.aligned / self.total if self.total else 0.0

    @property
    def pending_rate(self) -> float:
        """挂起率：进了待人工处理清单的条数 ÷ 基准集条数。"""
        return self.pending / self.total if self.total else 0.0

    @property
    def misbinding_rate(self) -> float:
        """错绑定率：错绑定条数 ÷ 基准集条数。"""
        return self.misbound / self.total if self.total else 0.0

    @property
    def false_merge_rate(self) -> float:
        """误并入率：误并入条数 ÷ 基准集条数。"""
        return self.false_merge / self.total if self.total else 0.0

    @property
    def ambiguous_pending_rate(self) -> float:
        """同分多候选挂起率：同分样本里真的挂起的比例。

        基准集里没有同分样本时返回 1.0（无从违反）；同时 ``ambiguous_cases``
        会显式记着样本数 —— 「没有样本」不等于「通过了」。
        """
        if not self.ambiguous_cases:
            return 1.0
        return self.ambiguous_pending / self.ambiguous_cases

    # ------------------------------------------------------------ 视图

    @property
    def ok(self) -> bool:
        """三项硬指标是否全部达标（错绑定 0 / 误并入 0 / 无第三去向 0）。"""
        return (
            self.misbound == 0
            and self.false_merge == 0
            and self.dangling == 0
            and self.missing_decisions == 0
            and self.extra_decisions == 0
        )

    def as_dict(self) -> dict[str, Any]:
        """可打印 / 可落盘的指标视图。"""
        return {
            "total": self.total,
            "aligned": self.aligned,
            "pending": self.pending,
            "alignment_rate": round(self.alignment_rate, 4),
            "pending_rate": round(self.pending_rate, 4),
            "misbound": self.misbound,
            "misbinding_rate": round(self.misbinding_rate, 4),
            "false_merge": self.false_merge,
            "false_merge_rate": round(self.false_merge_rate, 4),
            "missed_match": self.missed_match,
            "dangling": self.dangling,
            "ambiguous_cases": self.ambiguous_cases,
            "ambiguous_pending": self.ambiguous_pending,
            "ambiguous_pending_rate": round(self.ambiguous_pending_rate, 4),
            "reason_mismatch": self.reason_mismatch,
            "duplicate_decisions": self.duplicate_decisions,
            "missing_decisions": self.missing_decisions,
            "extra_decisions": self.extra_decisions,
            "ok": self.ok,
            "verdicts": [verdict.as_dict() for verdict in self.verdicts],
        }

    def summary(self) -> str:
        """一行一项的指标摘要（打印用）。"""
        lines = [
            f"基准集样本：{self.total} 条",
            f"对齐率：{self.aligned}/{self.total} = {self.alignment_rate:.1%}",
            f"错绑定数：{self.misbound}",
            f"误并入数：{self.false_merge}",
            f"挂起率：{self.pending}/{self.total} = {self.pending_rate:.1%}",
            f"同分多候选挂起率：{self.ambiguous_pending}/{self.ambiguous_cases} = "
            f"{self.ambiguous_pending_rate:.1%}",
            f"无第三去向：{self.dangling}",
            f"漏对齐：{self.missed_match}｜原因不符：{self.reason_mismatch}",
            f"缺样本：{self.missing_decisions}｜多出样本：{self.extra_decisions}",
        ]
        return "\n".join(lines)


def evaluate(
    decisions: Iterable[AlignmentDecision],
    cases: Iterable[GroundTruthCase],
) -> AlignmentMetrics:
    """把一批对齐结论与一份标准答案对账，算出指标。

    口径：

    - **错绑定**：并入了草稿，但归到的块与标准答案不是同一块；
    - **误并入**：标准答案要求挂起，却被并入了草稿；
    - **漏对齐**：标准答案要求并入，却挂起了（保守，不算错，但要看得见）；
    - **无第三去向**：结论既没有依据（并入）也没有原因（挂起）—— 结果消失了。
    """
    decision_by_key: dict[ResultKey, AlignmentDecision] = {}
    duplicates = 0
    for decision in decisions:
        if decision.key in decision_by_key:
            duplicates += 1
            continue
        decision_by_key[decision.key] = decision

    case_list = list(cases)
    aligned = pending = misbound = false_merge = missed = 0
    ambiguous_cases = ambiguous_pending = reason_mismatch = 0
    missing = 0
    verdicts: list[CaseVerdict] = []

    for case in case_list:
        decision = decision_by_key.get(case.key)
        if decision is None:
            missing += 1
            verdicts.append(CaseVerdict(case.key, VERDICT_MISSING, "基准集有这一条，结论里没有"))
            continue

        if case.is_ambiguous_case:
            ambiguous_cases += 1
            if not decision.matched:
                ambiguous_pending += 1

        if decision.matched:
            aligned += 1
            if not case.expect_matched:
                false_merge += 1
                verdicts.append(
                    CaseVerdict(
                        case.key,
                        VERDICT_FALSE_MERGE,
                        f"标准答案要求挂起（{case.expected_reason or '未写原因'}），"
                        f"实际并入 {decision.block}",
                    )
                )
            elif (
                case.expected_block is not None
                and decision.block != case.expected_block
            ):
                misbound += 1
                verdicts.append(
                    CaseVerdict(
                        case.key,
                        VERDICT_MISBOUND,
                        f"标准答案 {case.expected_block}，实际并入 {decision.block}",
                    )
                )
            else:
                verdicts.append(CaseVerdict(case.key, VERDICT_OK, case.note))
            continue

        pending += 1
        if case.expect_matched:
            missed += 1
            verdicts.append(
                CaseVerdict(
                    case.key,
                    VERDICT_MISSED,
                    f"标准答案 {case.expected_block}，实际挂起（{decision.reason or '无原因'}）",
                )
            )
        elif (
            case.expected_reason
            and decision.reason
            and decision.reason != case.expected_reason
        ):
            reason_mismatch += 1
            verdicts.append(
                CaseVerdict(
                    case.key,
                    VERDICT_REASON_MISMATCH,
                    f"标准答案原因 {case.expected_reason}，实际 {decision.reason}",
                )
            )
        else:
            verdicts.append(CaseVerdict(case.key, VERDICT_OK, case.note))

    case_keys = {case.key for case in case_list}
    extra = [key for key in decision_by_key if key not in case_keys]
    dangling = sum(1 for decision in decision_by_key.values() if not decision.has_trace)
    for key in extra:
        verdicts.append(CaseVerdict(key, VERDICT_EXTRA, "结论里多出基准集没有的条数"))

    return AlignmentMetrics(
        total=len(case_list),
        aligned=aligned,
        pending=pending,
        misbound=misbound,
        false_merge=false_merge,
        missed_match=missed,
        dangling=dangling,
        ambiguous_cases=ambiguous_cases,
        ambiguous_pending=ambiguous_pending,
        reason_mismatch=reason_mismatch,
        duplicate_decisions=duplicates,
        missing_decisions=missing,
        extra_decisions=len(extra),
        verdicts=tuple(verdicts),
    )


def load_ground_truth(path: Path | str) -> GroundTruthSet:
    """读一份基准集标准答案（JSON）。

    文件形状::

        {
          "version": "bench-2026.10",
          "note": "口径说明",
          "cases": [
            {
              "accession_no": "ACC-BENCH-0001",
              "item_code": "IHC-CD3",
              "observed_at": "20260101130000",
              "expect": "matched",
              "block": {"region_seq": 1, "block_no": "B1"},
              "reason": null,
              "note": "蜡块号精确命中"
            }
          ]
        }

    ``expect`` 取 ``matched``（应并入草稿）或 ``pending``（应挂起）；
    ``block`` 只在 ``matched`` 时填；``reason`` 只在 ``pending`` 时填。
    """
    raw = Path(path).expanduser()
    data = json.loads(raw.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"基准集标准答案格式不对（应为 JSON 对象）：{raw}")

    cases: list[GroundTruthCase] = []
    for item in data.get("cases", []):
        expect = str(item.get("expect") or "pending").strip().lower()
        block_raw = item.get("block") or {}
        expected_block: tuple[int | None, str | None] | None = None
        if block_raw:
            expected_block = (
                block_raw.get("region_seq"),
                block_raw.get("block_no"),
            )
        reason = item.get("reason")
        cases.append(
            GroundTruthCase(
                key=ResultKey(
                    accession_no=str(item.get("accession_no") or "").strip(),
                    item_code=str(item.get("item_code") or "").strip().upper(),
                    observed_at=str(item.get("observed_at") or "").strip(),
                ),
                expect_matched=expect == "matched",
                expected_block=expected_block,
                expected_reason=str(reason).strip() if reason else None,
                note=str(item.get("note") or ""),
            )
        )

    return GroundTruthSet(
        version=str(data.get("version") or ""),
        note=str(data.get("note") or ""),
        cases=tuple(cases),
    )
