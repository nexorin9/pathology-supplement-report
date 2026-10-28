"""对照实验：同一份基准集上，本产品 vs 两种朴素做法。

**为什么要有这一层**：产品跑得好是产品自己的说法，数字得能被别人复算。
本模块把三套归属绑定策略放在**同一份输入**上跑一遍，用**同一套指标**
（:mod:`patho_supplement.align.metrics`）逐条对账，把差异摊成一张表：

======================  ====================================================
``exact_normalized``    基线 A：仅规范化精确匹配（查表，对不上就等人）
``lexical_top_score``   基线 B：仅取词法最高分（同分直接取第一）
（产品）                四级级联 + 唯一领先 + 申请侧状态闸门 + 同分全挂起
======================  ====================================================

**口径（三方共用，差异只在策略）**：

- 输入：同一份去重 / 更正取代后的生效结果条目 + 同一份基线；
- 前置判定：无法获得 / 值为空 / 回报时间倒挂，三方结论一致（与归属无关）；
- 指标：同一套 :class:`AlignmentMetrics`（对齐率 / 错绑定 / 误并入 / 挂起率）。

**「对齐率」的口径要写清楚**：本模块的**对齐率 = 归到正确块的条数 ÷ 样本条数**
（并入但归错块的不计入）；另给一列**并入率**（并入草稿的条数占比，含归错的）。
两个数一起看才说得清：朴素做法往往**并入得更多**（该等的人不等了），但
**并对的没变多**，多出来的是误并入 —— 报告凭空多了一项，或者挂错了块。

**结论不靠嘴说**：跑完自动判定「产品错绑定 = 0」「产品对齐率不低于基线 A」，
不达标即以非 0 退出，可以直接挂到 CI 上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..compose.baseline import load_baseline_file
from .baselines import (
    BASELINE_A_LABEL,
    BASELINE_B_LABEL,
    exact_normalized_decisions,
    lexical_top_score_decisions,
)
from .benchmark import Benchmark, BenchmarkRun, run_benchmark
from .metrics import AlignmentDecision, AlignmentMetrics, evaluate

__all__ = [
    "COMPARISON_JSON_NAME",
    "COMPARISON_TEXT_NAME",
    "ComparisonCheck",
    "ComparisonParty",
    "ComparisonRun",
    "run_comparison",
]

#: 对照结果（JSON）的文件名。
COMPARISON_JSON_NAME = "comparison.json"
#: 对照结果（文本表）的文件名。
COMPARISON_TEXT_NAME = "comparison.txt"

#: 口径说明（写进对照结果文件，数字不得脱离口径单看）。
CALIBER = (
    "三方共用同一份输入：去重与更正取代后的生效结果条目 + 同一份基线快照；"
    "无法获得 / 值为空 / 回报时间倒挂的前置判定三方一致（与归属无关）；"
    "差异只在「结果归到哪一块」这一步。对齐率 = 归到正确块的条数 ÷ 样本条数；"
    "并入率 = 并入草稿的条数 ÷ 样本条数（含归错的）；"
    "错绑定 = 并入但归到了另一块；误并入 = 标准答案要求挂起却被并入；"
    "挂起率 = 进了待人工处理清单的条数 ÷ 样本条数。"
    "「原因不符」不计入对照口径 —— 挂起原因的中文标签是产品词汇，朴素做法没有这套标签。"
)


@dataclass(frozen=True)
class ComparisonParty:
    """对照表里的一行（一方做法）。"""

    key: str
    label: str
    kind: str
    metrics: AlignmentMetrics
    note: str = ""

    @property
    def correct_aligned(self) -> int:
        """归到正确块的条数（并入但归错块的不算）。"""
        return self.metrics.aligned - self.metrics.misbound - self.metrics.false_merge

    @property
    def correct_alignment_rate(self) -> float:
        """对齐率：归到正确块的条数 ÷ 样本条数。"""
        total = self.metrics.total
        return self.correct_aligned / total if total else 0.0

    @property
    def merged_rate(self) -> float:
        """并入率：并入草稿的条数 ÷ 样本条数（含归错的）。"""
        return self.metrics.alignment_rate

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "note": self.note,
            "sample_count": self.metrics.total,
            "merged_count": self.metrics.aligned,
            "merged_rate": round(self.merged_rate, 4),
            "correct_aligned_count": self.correct_aligned,
            "alignment_rate": round(self.correct_alignment_rate, 4),
            "misbound_count": self.metrics.misbound,
            "false_merge_count": self.metrics.false_merge,
            "pending_count": self.metrics.pending,
            "pending_rate": round(self.metrics.pending_rate, 4),
            "missed_match_count": self.metrics.missed_match,
            "ambiguous_pending_rate": round(self.metrics.ambiguous_pending_rate, 4),
            "dangling_count": self.metrics.dangling,
        }


@dataclass(frozen=True)
class ComparisonCheck:
    """对照实验的一条判定。"""

    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ComparisonRun:
    """一次对照实验的完整结果。"""

    dataset: str
    version: str
    parties: tuple[ComparisonParty, ...]
    checks: tuple[ComparisonCheck, ...]
    caliber: str = CALIBER

    @property
    def sample_count(self) -> int:
        return self.parties[0].metrics.total if self.parties else 0

    @property
    def ok(self) -> bool:
        """全部判定是否通过。"""
        return all(check.passed for check in self.checks)

    def party(self, key: str) -> ComparisonParty:
        for party in self.parties:
            if party.key == key:
                return party
        raise KeyError(f"对照表里没有这一方：{key}")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "sample_count": self.sample_count,
            "caliber": self.caliber,
            "parties": [party.as_dict() for party in self.parties],
            "checks": [check.as_dict() for check in self.checks],
            "ok": self.ok,
        }

    def table(self) -> str:
        """三方对照表（终端可读）。"""
        headers = ("做法", "并入", "并入率", "对齐率", "错绑定", "误并入", "挂起率")
        rows: list[tuple[str, ...]] = []
        for party in self.parties:
            rows.append(
                (
                    party.label,
                    f"{party.metrics.aligned}/{party.metrics.total}",
                    f"{party.merged_rate:.1%}",
                    f"{party.correct_alignment_rate:.1%}",
                    str(party.metrics.misbound),
                    str(party.metrics.false_merge),
                    f"{party.metrics.pending_rate:.1%}",
                )
            )
        widths = tuple(
            max(_display_width(header), max(_display_width(row[i]) for row in rows))
            for i, header in enumerate(headers)
        )
        lines = [_row(headers, widths)]
        for row in rows:
            lines.append(_row(row, widths))
        return "\n".join(lines)

    def check_lines(self) -> str:
        """判定清单（逐条说清过没过）。"""
        return "\n".join(
            f"  [{'通过' if check.passed else '不通过'}] {check.name}：{check.detail}"
            for check in self.checks
        )


def _display_width(text: str) -> int:
    """中文按两列宽算（终端对齐用）。"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x2E7F else 1
    return width


def _row(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = []
    for cell, width in zip(cells, widths):
        padding = " " * max(0, width - _display_width(cell))
        padded.append(cell + padding)
    return "  " + "  ".join(padded).rstrip()


def _build_checks(parties: dict[str, ComparisonParty]) -> tuple[ComparisonCheck, ...]:
    product = parties["product"]
    baseline_a = parties["baseline_a"]
    baseline_b = parties["baseline_b"]
    checks: list[ComparisonCheck] = [
        ComparisonCheck(
            name="本产品错绑定数 = 0",
            passed=product.metrics.misbound == 0,
            detail=(
                f"错绑定 {product.metrics.misbound} 条"
                f"（基线 A {baseline_a.metrics.misbound} 条、"
                f"基线 B {baseline_b.metrics.misbound} 条）"
            ),
        ),
        ComparisonCheck(
            name="本产品误并入数 = 0",
            passed=product.metrics.false_merge == 0,
            detail=(
                f"误并入 {product.metrics.false_merge} 条"
                f"（基线 A {baseline_a.metrics.false_merge} 条、"
                f"基线 B {baseline_b.metrics.false_merge} 条）"
            ),
        ),
        ComparisonCheck(
            name="本产品对齐率不低于基线 A",
            passed=product.correct_alignment_rate >= baseline_a.correct_alignment_rate,
            detail=(
                f"本产品 {product.correct_alignment_rate:.1%}"
                f"（{product.correct_aligned}/{product.metrics.total}）"
                f" vs 基线 A {baseline_a.correct_alignment_rate:.1%}"
                f"（{baseline_a.correct_aligned}/{baseline_a.metrics.total}）"
            ),
        ),
        ComparisonCheck(
            name="本产品对齐率不低于基线 B",
            passed=product.correct_alignment_rate >= baseline_b.correct_alignment_rate,
            detail=(
                f"本产品 {product.correct_alignment_rate:.1%}"
                f"（{product.correct_aligned}/{product.metrics.total}）"
                f" vs 基线 B {baseline_b.correct_alignment_rate:.1%}"
                f"（{baseline_b.correct_aligned}/{baseline_b.metrics.total}）"
            ),
        ),
        ComparisonCheck(
            name="本产品无第三去向 = 0",
            passed=product.metrics.dangling == 0,
            detail=f"无第三去向 {product.metrics.dangling} 条（结果既没进草稿也没进清单）",
        ),
    ]
    return tuple(checks)


def run_comparison(
    benchmark: Benchmark,
    *,
    output_dir: Path | str,
    database: object | None = None,
    run: BenchmarkRun | None = None,
) -> ComparisonRun:
    """在基准集上跑三方对照，产出可复算的结果表。

    产品走的是**产品真路径**（入站解析 → 去重 → 更正取代 → 四级对齐 → 成稿），
    与线上跑的是同一段代码；两个基线拿**同一份生效条目**与**同一份基线**，
    只换归属绑定策略。

    ``run`` 给了就直接用这次跑批的结果（不重跑一遍）；不给就自己跑一次。
    """
    active_run: BenchmarkRun = run or run_benchmark(
        benchmark, output_dir=output_dir, database=database
    )
    run = active_run
    outcome = run.outcome
    cases = benchmark.ground_truth.cases
    baselines = load_baseline_file(benchmark.baseline_path)

    entries = list(outcome.effective_entries)
    withheld = list(outcome.withheld_entries)
    inverted = list(outcome.inverted_entries)

    decisions_a: tuple[AlignmentDecision, ...] = exact_normalized_decisions(
        entries, baselines, withheld=withheld, inverted=inverted
    )
    decisions_b: tuple[AlignmentDecision, ...] = lexical_top_score_decisions(
        entries, baselines, withheld=withheld, inverted=inverted
    )

    parties = {
        "product": ComparisonParty(
            key="product",
            label="本产品｜四级级联 + 唯一领先 + 状态闸门",
            kind="本产品",
            metrics=run.metrics,
            note="跑产品真路径，与线上同一段代码",
        ),
        "baseline_a": ComparisonParty(
            key="baseline_a",
            label=BASELINE_A_LABEL,
            kind="朴素基线",
            metrics=evaluate(decisions_a, cases),
            note="归一化后相等即命中；多个命中取第一；不查已申请项目与取消状态",
        ),
        "baseline_b": ComparisonParty(
            key="baseline_b",
            label=BASELINE_B_LABEL,
            kind="朴素基线",
            metrics=evaluate(decisions_b, cases),
            note="取词法最高分那块；同分直接取第一；不查已申请项目与取消状态",
        ),
    }

    comparison = ComparisonRun(
        dataset=str(benchmark.root),
        version=benchmark.version,
        parties=(parties["product"], parties["baseline_a"], parties["baseline_b"]),
        checks=_build_checks(parties),
    )

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / COMPARISON_JSON_NAME).write_text(
        json.dumps(comparison.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_path / COMPARISON_TEXT_NAME).write_text(
        _render_text(comparison), encoding="utf-8"
    )
    return comparison


def _render_text(comparison: ComparisonRun) -> str:
    """对照结果的可读文本（写进文件，终端打印用同一份）。"""
    lines = [
        "病理补充报告 · 三方对照实验",
        f"基准集：{comparison.dataset}（版本 {comparison.version or '-'}）",
        f"样本量：{comparison.sample_count} 条",
        "",
        comparison.table(),
        "",
        "口径：" + comparison.caliber,
        "",
        "判定：",
        comparison.check_lines(),
        "",
        "结论："
        + ("全部判定通过" if comparison.ok else "存在不通过的判定（详见上面逐条）"),
    ]
    return "\n".join(lines)
