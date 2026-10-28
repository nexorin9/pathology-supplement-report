"""对照基准实验：本产品 vs 两种院内常见做法（同一份输入、同一套指标）。

**这一组用例回答一个问题**：产品多出来的那些机制，到底值不值。

对照的两个做法都是病理科真在用的：

- **基线 A：仅规范化精确匹配** —— 蜡块号 / 部位描述归一化后按相等查表，
  对得上就归那块，对不上就挂起等人；多个命中取第一个。
- **基线 B：仅取词法最高分** —— 对所有候选块算词法相似度，取分最高那块；
  **同分直接取第一**。

**实验口径**（写死在这组用例里，改口径就得改用例，不能悄悄换）：

- 三方拿的是**同一份输入**（去重与更正取代后的生效条目 + 同一份基线）；
- 无法获得 / 值为空 / 回报时间倒挂的前置判定**三方一致**（与归属无关）；
- 差异只在「结果归到哪一块」这一步；
- 指标用同一套 :mod:`patho_supplement.align.metrics`。

**判定**：本产品错绑定 = 0、误并入 = 0，对齐率不低于两个基线。

最后有一组「**数字不得凭空出现**」的用例：对照表里的样本量必须等于手写标准
答案的条数，产品那一行的并入条数必须等于产品真路径产出的草稿条目数 ——
数字要有出处，不能是表格里手填的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from patho_supplement.align.baselines import (
    BASELINE_A_LABEL,
    BASELINE_B_LABEL,
    exact_normalized_decisions,
    lexical_top_score_decisions,
    quality_gate_decisions,
)
from patho_supplement.align.benchmark import load_benchmark, run_benchmark
from patho_supplement.align.comparison import (
    COMPARISON_JSON_NAME,
    COMPARISON_TEXT_NAME,
    ComparisonRun,
    run_comparison,
)
from patho_supplement.models import Block, ReportBaseline, ResultEntry, ResultStatus
from patho_supplement.pending.reasons import PendingReason

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BENCHMARK_DIR = FIXTURES / "benchmark"
INBOX_DIR = FIXTURES / "inbox"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 手写标准答案的条数（少一条就是标准答案本身出了问题，数字不许凭空出现）。
EXPECTED_BENCHMARK_CASES = 21
EXPECTED_INBOX_CASES = 6


# ------------------------------------------------------------------ 小工具


def _baseline(
    blocks: list[Block], accession_no: str = "ACC-TEST-0001"
) -> dict[str, ReportBaseline]:
    return {
        accession_no: ReportBaseline(
            accession_no=accession_no,
            blocks=tuple(blocks),
            base_version="base-v1",
        )
    }


def _entry(**kwargs) -> ResultEntry:
    base = {
        "accession_no": "ACC-TEST-0001",
        "item_code": "IHC-CD20",
        "value": "阳性",
        "status": ResultStatus.FINAL,
        "observed_at": "20260101130000",
    }
    base.update(kwargs)
    return ResultEntry(**base)


# -------------------------------------------------- 基线 A：仅规范化精确匹配


def test_基线A_蜡块号归一化后精确命中():
    baselines = _baseline([Block(region_seq=2, region_desc="宫颈", block_no="B2")])
    decisions = exact_normalized_decisions([_entry(block_no="b-02")], baselines)
    assert decisions[0].matched is True
    assert decisions[0].block == (2, "B2")


def test_基线A_部位描述精确命中():
    baselines = _baseline([Block(region_seq=1, region_desc="子宫体", block_no="B1")])
    decisions = exact_normalized_decisions(
        [_entry(block_no=None, region_desc="子宫体")], baselines
    )
    assert decisions[0].matched is True
    assert decisions[0].block == (1, "B1")


def test_基线A_别名不折算_宫体不等于子宫体():
    """基线 A 只做规范化，**不做同义折算** —— 这是它漏对齐的来源。"""
    baselines = _baseline([Block(region_seq=1, region_desc="子宫体", block_no="B1")])
    decisions = exact_normalized_decisions(
        [_entry(block_no=None, region_desc="宫体")], baselines
    )
    assert decisions[0].matched is False
    assert decisions[0].reason == PendingReason.NO_MATCH.value


def test_基线A_多个命中取第一个():
    baselines = _baseline(
        [
            Block(region_seq=1, region_desc="淋巴结", block_no="B1"),
            Block(region_seq=2, region_desc="淋巴结", block_no="B2"),
        ]
    )
    decisions = exact_normalized_decisions(
        [_entry(block_no=None, region_desc="淋巴结")], baselines
    )
    assert decisions[0].matched is True
    assert decisions[0].block == (1, "B1")


def test_基线A_蜡块号不在基线则挂起():
    baselines = _baseline([Block(region_seq=1, region_desc="宫颈", block_no="B2")])
    decisions = exact_normalized_decisions([_entry(block_no="B9")], baselines)
    assert decisions[0].matched is False
    assert decisions[0].reason == PendingReason.NO_MATCH.value


def test_基线A_不查已申请项目_未登记的结果照样并入():
    """基线 A 不看「这块申请过这个项目没有」 —— 未登记的结果被写进报告。"""
    baselines = _baseline(
        [Block(region_seq=2, region_desc="宫颈", block_no="B2", ordered_items=("IHC-CD20",))]
    )
    decisions = exact_normalized_decisions(
        [_entry(item_code="IHC-P53", block_no="B2", region_desc="宫颈")], baselines
    )
    assert decisions[0].matched is True
    assert decisions[0].block == (2, "B2")


# -------------------------------------------------- 基线 B：仅取词法最高分


def test_基线B_取词法最高分那块():
    baselines = _baseline(
        [
            Block(region_seq=1, region_desc="子宫体", block_no="B1"),
            Block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ]
    )
    decisions = lexical_top_score_decisions(
        [_entry(block_no=None, region_desc="宫颈组织")], baselines
    )
    assert decisions[0].matched is True
    assert decisions[0].block == (2, "B2")


def test_基线B_同分直接取第一():
    """基线 B 遇同分取第一 —— 产品在这一条上是「全部挂起，不猜」。"""
    baselines = _baseline(
        [
            Block(region_seq=1, region_desc="子宫体", block_no="B1"),
            Block(region_seq=11, region_desc="输卵管壁", block_no="B11"),
            Block(region_seq=12, region_desc="胆囊管壁", block_no="B12"),
        ]
    )
    decisions = lexical_top_score_decisions(
        [_entry(block_no=None, region_desc="管壁")], baselines
    )
    assert decisions[0].matched is True
    assert decisions[0].block == (11, "B11")


def test_基线B_毫无词法关系则挂起():
    baselines = _baseline(
        [
            Block(region_seq=1, region_desc="子宫体", block_no="B1"),
            Block(region_seq=2, region_desc="宫颈", block_no="B2"),
        ]
    )
    decisions = lexical_top_score_decisions(
        [_entry(block_no=None, region_desc="胃窦")], baselines
    )
    assert decisions[0].matched is False
    assert decisions[0].reason == PendingReason.NO_MATCH.value


def test_基线B_不查已申请项目也照样并入():
    baselines = _baseline(
        [Block(region_seq=2, region_desc="宫颈", block_no="B2", ordered_items=("IHC-CD20",))]
    )
    decisions = lexical_top_score_decisions(
        [_entry(item_code="IHC-P53", block_no=None, region_desc="宫颈")], baselines
    )
    assert decisions[0].matched is True


# -------------------------------------------------- 三方共用的结果质量前置判定


def test_结果质量前置判定_无法获得与空值一律挂起():
    withheld = [
        _entry(item_code="IHC-CD20", status=ResultStatus.NOT_OBTAINED, value=None),
        _entry(item_code="IHC-CD3", value=""),
    ]
    decisions = quality_gate_decisions(withheld, [])
    assert [d.matched for d in decisions] == [False, False]
    assert decisions[0].reason == PendingReason.RESULT_NOT_OBTAINED.value
    assert decisions[1].reason == PendingReason.EMPTY_VALUE.value


def test_结果质量前置判定_回报时间倒挂也挂起():
    decisions = quality_gate_decisions([], [_entry(item_code="IHC-CD3")])
    assert decisions[0].matched is False
    assert decisions[0].reason == PendingReason.TIME_INVERSION.value


# -------------------------------------------------- 三方对照实验


@pytest.fixture(scope="module")
def comparison(tmp_path_factory) -> ComparisonRun:
    out_dir = tmp_path_factory.mktemp("comparison")
    benchmark = load_benchmark(BENCHMARK_DIR)
    return run_comparison(benchmark, output_dir=out_dir)


def test_对照跑通_本产品错绑定与误并入均为零(comparison):
    product = comparison.party("product")
    assert product.metrics.misbound == 0
    assert product.metrics.false_merge == 0
    assert product.metrics.dangling == 0


def test_对照跑通_本产品对齐率不低于两个基线(comparison):
    product = comparison.party("product")
    assert product.correct_alignment_rate >= comparison.party("baseline_a").correct_alignment_rate
    assert product.correct_alignment_rate >= comparison.party("baseline_b").correct_alignment_rate


def test_对照跑通_朴素做法会误并入(comparison):
    """对照要有说服力：基线确实会把不该进报告的结果并进去。"""
    assert comparison.party("baseline_a").metrics.false_merge > 0
    assert comparison.party("baseline_b").metrics.false_merge > 0


def test_对照跑通_朴素做法并入更多不等于并得更对(comparison):
    """基线 B 并入的条数比产品多，多出来的是误并入，不是正确对齐。"""
    product = comparison.party("product")
    baseline_b = comparison.party("baseline_b")
    assert baseline_b.metrics.aligned > product.metrics.aligned
    assert baseline_b.correct_aligned == product.correct_aligned


def test_对照跑通_全部判定通过(comparison):
    assert comparison.ok is True
    assert all(check.passed for check in comparison.checks)
    assert [check.name for check in comparison.checks]


def test_对照跑通_样本量与手写标准答案条数一致(comparison):
    """数字不得凭空出现：样本量 = 标准答案条数。"""
    benchmark = load_benchmark(BENCHMARK_DIR)
    assert comparison.sample_count == EXPECTED_BENCHMARK_CASES
    assert comparison.sample_count == benchmark.case_count


def test_对照跑通_产品那一行的并入条数等于草稿条目数(comparison, tmp_path):
    """产品那行的数字来自产品真路径，不是表格里手填的。"""
    benchmark = load_benchmark(BENCHMARK_DIR)
    run = run_benchmark(benchmark, output_dir=tmp_path / "recheck")
    product = comparison.party("product")
    assert product.metrics.aligned == run.metrics.aligned
    assert product.metrics.aligned == run.outcome.draft_entry_count - run.outcome.placeholder_count


def test_对照跑通_可复现_两次跑结果一致(tmp_path):
    """同一份基准集跑两次，数字逐项一致。"""
    benchmark = load_benchmark(BENCHMARK_DIR)
    first = run_comparison(benchmark, output_dir=tmp_path / "a")
    second = run_comparison(benchmark, output_dir=tmp_path / "b")
    assert [p.as_dict() for p in first.parties] == [p.as_dict() for p in second.parties]
    assert [c.as_dict() for c in first.checks] == [c.as_dict() for c in second.checks]


def test_对照跑通_结果落盘且带口径与样本量(tmp_path):
    benchmark = load_benchmark(BENCHMARK_DIR)
    run_comparison(benchmark, output_dir=tmp_path)
    payload = json.loads((tmp_path / COMPARISON_JSON_NAME).read_text(encoding="utf-8"))
    assert payload["sample_count"] == EXPECTED_BENCHMARK_CASES
    assert payload["caliber"]
    assert len(payload["parties"]) == 3
    assert payload["ok"] is True
    text = (tmp_path / COMPARISON_TEXT_NAME).read_text(encoding="utf-8")
    assert "三方对照实验" in text
    assert f"样本量：{EXPECTED_BENCHMARK_CASES} 条" in text
    assert BASELINE_A_LABEL in text
    assert BASELINE_B_LABEL in text


def test_对照跑通_结果目录样例集也能跑三方对照(tmp_path):
    """``--inbox`` 模式：结果目录 + 同目录标准答案，也能出三方对照表。"""
    benchmark = load_benchmark(INBOX_DIR)
    result = run_comparison(benchmark, output_dir=tmp_path)
    assert result.sample_count == EXPECTED_INBOX_CASES
    assert result.ok is True
    assert result.party("product").metrics.misbound == 0


# --------------------------------------------------------------- 命令行


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    return subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=environment,
    )


def test_cli_benchmark_结果目录模式打印三方对照表(tmp_path):
    result = _run_cli(
        "benchmark", "--inbox", "tests/fixtures/inbox/", "--out", str(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert "三方对照" in result.stdout
    assert BASELINE_A_LABEL in result.stdout
    assert BASELINE_B_LABEL in result.stdout
    assert f"样本量：{EXPECTED_INBOX_CASES} 条" in result.stdout
    assert (tmp_path / COMPARISON_JSON_NAME).is_file()


def test_cli_benchmark_默认基准集也出三方对照表(tmp_path):
    result = _run_cli("benchmark", "--out", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "三方对照" in result.stdout
    assert f"样本量：{EXPECTED_BENCHMARK_CASES} 条" in result.stdout


def test_cli_benchmark_缺标准答案时报错并说明怎么补(tmp_path):
    """缺标准答案就说清楚缺什么、怎么补，不给一句「失败」。"""
    result = _run_cli(
        "benchmark", "--inbox", "tests/fixtures/edge/", "--out", str(tmp_path)
    )
    assert result.returncode == 1
    assert "标准答案" in result.stderr


def test_cli_benchmark_基线与标准答案可分别指定(tmp_path):
    result = _run_cli(
        "benchmark",
        "--inbox",
        "tests/fixtures/inbox/",
        "--baseline",
        "tests/fixtures/inbox/baseline.json",
        "--ground-truth",
        "tests/fixtures/inbox/ground_truth.json",
        "--out",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "三方对照" in result.stdout
