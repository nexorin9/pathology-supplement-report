"""基准集跑批：把脱敏基准集跑过**产品真路径**，产出对齐结论与指标。

**跑的是产品本身，不是另写一套判定**：入站解析 → 幂等去重 → 更正取代 →
待查项派生 → 申请侧状态闸门 → 四级对齐 → 成稿 / 挂起。基准集与线上跑的是同一
段代码，指标才有意义 —— 拿一份「另写的简化判定」去测产品，测的是那份简化
判定。

基准集目录长这样::

    tests/fixtures/benchmark/
    ├── baseline.json       # 病理报告基线（部位与已申请项目）
    ├── *.hl7               # 入站结果 / 申请报文（脱敏样例）
    └── ground_truth.json   # 标准答案：每条结果应该归到哪块，或应该挂起

**标准答案是手写的**，不是跑出来的 —— 它就是「病理科期望的归属」。产品跑出来的
结论与它逐条对账，对不上就是产品错（或标准答案写错），两边都不许糊过去。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..replay import ReplayOutcome, run_replay
from .metrics import (
    AlignmentDecision,
    AlignmentMetrics,
    GroundTruthSet,
    ResultKey,
    evaluate,
    load_ground_truth,
)
from .rationale import rationale_tag

__all__ = [
    "Benchmark",
    "BenchmarkRun",
    "decisions_from_outcome",
    "load_benchmark",
    "run_benchmark",
]

#: 基线快照在基准集目录里的文件名。
BASELINE_NAME = "baseline.json"
#: 标准答案在基准集目录里的文件名。
GROUND_TRUTH_NAME = "ground_truth.json"


@dataclass(frozen=True)
class Benchmark:
    """一份基准集（基线 + 入站报文 + 标准答案）。"""

    root: Path
    inbox: Path
    baseline_path: Path
    ground_truth: GroundTruthSet

    @property
    def version(self) -> str:
        return self.ground_truth.version

    @property
    def case_count(self) -> int:
        return len(self.ground_truth.cases)


@dataclass(frozen=True)
class BenchmarkRun:
    """一次基准集跑批的结果。"""

    benchmark: Benchmark
    outcome: ReplayOutcome
    decisions: tuple[AlignmentDecision, ...]
    metrics: AlignmentMetrics

    def summary(self) -> str:
        """指标摘要（打印用）。"""
        return self.metrics.summary()


def load_benchmark(
    root: Path | str,
    *,
    baseline_path: Path | str | None = None,
    ground_truth_path: Path | str | None = None,
) -> Benchmark:
    """读一份基准集目录。

    缺基线或缺标准答案直接报错 —— 基准集少一半就没有对账的意义了。

    ``baseline_path`` / ``ground_truth_path`` 给了就覆盖目录里的同名文件
    （对照实验常用「结果目录 + 别处的标准答案」这种组合）。
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"基准集目录不存在：{root_path}")

    baseline_file = Path(baseline_path) if baseline_path else root_path / BASELINE_NAME
    ground_truth_file = (
        Path(ground_truth_path) if ground_truth_path else root_path / GROUND_TRUTH_NAME
    )
    if not baseline_file.is_file():
        raise FileNotFoundError(f"基准集缺基线快照：{baseline_file}")
    if not ground_truth_file.is_file():
        raise FileNotFoundError(f"基准集缺标准答案：{ground_truth_file}")

    return Benchmark(
        root=root_path,
        inbox=root_path,
        baseline_path=baseline_file,
        ground_truth=load_ground_truth(ground_truth_file),
    )


def decisions_from_outcome(outcome: ReplayOutcome) -> tuple[AlignmentDecision, ...]:
    """把一轮跑批的产出翻译成「一条结果一个结论」。

    草稿里的条目 → 并入（带依据）；待人工处理清单里的条目 → 挂起（带原因）。
    **两处都没有的，就是消失了** —— 指标里的「无第三去向」就是查这个。
    """
    decisions: list[AlignmentDecision] = []

    for draft in outcome.drafts:
        for entry in draft.entries:
            if entry.placeholder:
                # 「待回报」占位不是一条真实结果，不参与对账。
                continue
            decisions.append(
                AlignmentDecision(
                    key=ResultKey(
                        accession_no=draft.accession_no,
                        item_code=entry.item_code,
                        observed_at=entry.observed_at or "",
                    ),
                    matched=True,
                    block=(entry.region_seq, entry.block_no),
                    rationale=(
                        rationale_tag(entry.alignment) if entry.alignment else None
                    ),
                )
            )

    for record in outcome.pending:
        decisions.append(
            AlignmentDecision(
                key=ResultKey(
                    accession_no=record.accession_no,
                    item_code=record.item_code,
                    observed_at=record.observed_at or "",
                ),
                matched=False,
                reason=record.reason.value,
            )
        )

    return tuple(decisions)


def run_benchmark(
    benchmark: Benchmark,
    *,
    output_dir: Path | str,
    database: object | None = None,
) -> BenchmarkRun:
    """跑一遍基准集，产出结论与指标。

    ``output_dir`` 是这一轮的产出目录（草稿、待处理清单、运行摘要）；
    ``database`` 给了就把审计流水也落库（用于核对「依据一条不落」）。
    """
    outcome = run_replay(
        benchmark.inbox,
        baseline_path=benchmark.baseline_path,
        output_dir=output_dir,
        database=database,
    )
    decisions = decisions_from_outcome(outcome)
    metrics = evaluate(decisions, benchmark.ground_truth.cases)
    return BenchmarkRun(
        benchmark=benchmark,
        outcome=outcome,
        decisions=decisions,
        metrics=metrics,
    )
