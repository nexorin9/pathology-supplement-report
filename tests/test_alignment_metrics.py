"""基准集三项指标验收：错绑定 = 0 / 同分多候选挂起率 = 100% / 无第三去向。

这一组用例把「本产品敢承诺错绑定数 = 0」这句话**变成可复核的数字**：

- **错绑定数 = 0** —— 基准集里每一条都写清了应该归到哪一块（或应该挂起）；
  产品跑完逐条对账，绑错的、该挂起却并入的，一条都不许有；
- **同分多候选挂起率 = 100%** —— 同分时取第一个就是在猜，猜错一块就是一份
  签出去却写错部位的补充报告；
- **无第三去向** —— 每条结果要么进草稿（带对齐依据），要么进待人工处理清单
  （带原因），**没有第三个去处**。悄悄丢掉的结果，就是医师看不到的检查项。

**基准集跑的是产品真路径**（入站解析 → 幂等去重 → 更正取代 → 待查项派生 →
申请侧状态闸门 → 四级对齐 → 成稿 / 挂起），不是另写一套简化判定 —— 拿简化
判定去测产品，测的是那份简化判定。

**指标本身也要能抓错**：末尾有一组「故意造错」的用例 —— 造一个绑错块、造一个
没有依据的结论，看指标是不是真的报出来。指标要是永远报 0，上面那三条断言
就是空话。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patho_supplement.align.benchmark import (
    BenchmarkRun,
    decisions_from_outcome,
    load_benchmark,
    run_benchmark,
)
from patho_supplement.align.metrics import (
    AMBIGUOUS_REASON,
    AlignmentDecision,
    GroundTruthCase,
    ResultKey,
    evaluate,
)
from patho_supplement.audit.ledger import (
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
)
from patho_supplement.db import Database
from patho_supplement.pending.reasons import PendingReason

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BENCHMARK_DIR = FIXTURES / "benchmark"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 基准集应有的样本数（手写标准答案的条数 —— 少一条就是基准集本身出了问题）。
EXPECTED_CASE_COUNT = 21

#: 基准集必须覆盖到的挂起原因（这些是病理科在清单上天天见到的几类）。
REQUIRED_REASONS = {
    PendingReason.MISSING_BLOCK_NO,
    PendingReason.NO_MATCH,
    PendingReason.ITEM_NOT_ORDERED,
    PendingReason.AMBIGUOUS,
    PendingReason.CONFLICT,
    PendingReason.FIELDS_MISSING,
    PendingReason.RESULT_NOT_OBTAINED,
    PendingReason.EMPTY_VALUE,
    PendingReason.ITEM_CANCELLED,
}


@pytest.fixture(scope="module")
def bench():
    return load_benchmark(BENCHMARK_DIR)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("bench")


@pytest.fixture(scope="module")
def run(workdir, bench) -> BenchmarkRun:
    """跑一遍基准集（带状态库，便于核对审计流水一条不落）。"""
    database = Database(workdir / "state.db")
    with database:
        return run_benchmark(
            bench,
            output_dir=workdir / "out",
            database=database,
        )


@pytest.fixture(scope="module")
def state_db(workdir):
    """重开状态库读审计流水（跑批时已关闭连接，这里只读）。"""
    database = Database(workdir / "state.db")
    database.init_schema()
    yield database
    database.close()


# ---------------------------------------------------------------- 三项硬指标


def test_基准集跑批_错绑定数为零(run: BenchmarkRun) -> None:
    """错绑定数 = 0：任何一条结果都不得并入错误的部位。"""
    metrics = run.metrics
    assert metrics.total == EXPECTED_CASE_COUNT, (
        f"基准集样本数应为 {EXPECTED_CASE_COUNT}，实际 {metrics.total}"
    )
    assert metrics.misbound == 0, (
        "出现错绑定：\n"
        + "\n".join(
            f"  {verdict.key}｜{verdict.detail}"
            for verdict in metrics.verdicts
            if verdict.verdict == "错绑定"
        )
    )
    assert metrics.false_merge == 0, (
        "出现误并入：\n"
        + "\n".join(
            f"  {verdict.key}｜{verdict.detail}"
            for verdict in metrics.verdicts
            if verdict.verdict == "误并入"
        )
    )
    assert metrics.misbinding_rate == 0.0
    assert metrics.false_merge_rate == 0.0


def test_基准集跑批_同分多候选挂起率百分之百(run: BenchmarkRun) -> None:
    """同分多候选挂起率 = 100%：同分时一律挂起，不取第一个。"""
    metrics = run.metrics
    assert metrics.ambiguous_cases >= 2, (
        f"基准集里的同分样本太少（{metrics.ambiguous_cases} 条），测不出挂起率"
    )
    assert metrics.ambiguous_pending == metrics.ambiguous_cases
    assert metrics.ambiguous_pending_rate == 1.0


def test_基准集跑批_无第三去向(run: BenchmarkRun) -> None:
    """无第三去向：每条结果要么进草稿（带依据），要么进挂起清单（带原因）。"""
    metrics = run.metrics
    assert metrics.dangling == 0, "有结果既没进草稿也没进清单 —— 它消失了"
    assert metrics.missing_decisions == 0, "基准集里的样本在结论里找不到"
    assert metrics.extra_decisions == 0, "结论里多出基准集没有的条数"
    assert metrics.duplicate_decisions == 0, "同一条结果出了两个结论"
    assert metrics.reason_mismatch == 0, "挂起原因与标准答案写的不是同一类"
    assert metrics.missed_match == 0, "标准答案要求对齐的样本被挂了起"


def test_基准集跑批_条数守恒(run: BenchmarkRun) -> None:
    """条数守恒：入账 − 被取代 = 草稿条目 + 挂起条目。

    这条是「无第三去向」的**计数版**证明：既没有多出来的去向，也没有少了的
    去向（更正取代掉的那条不另算一条结果）。
    """
    outcome = run.outcome
    assert outcome.accepted - outcome.superseded == (
        outcome.draft_entry_count + len(outcome.pending)
    )
    # 重复投递只入账一条；更正取代旧值一条。
    assert outcome.duplicates == 1
    assert outcome.superseded == 1
    assert outcome.withheld == 2
    assert outcome.malformed == 0


def test_基准集跑批_每条结论都带依据或原因(run: BenchmarkRun) -> None:
    """逐条查：并入草稿的有对齐依据，挂起的有原因与缺失字段。"""
    for draft in run.outcome.drafts:
        for entry in draft.entries:
            if entry.placeholder:
                continue
            assert entry.alignment is not None, f"草稿条目缺对齐依据：{entry}"
            assert entry.alignment.recall_key
            assert entry.alignment.source_system
            assert entry.alignment.observed_at
            assert entry.alignment.confidence in {"高", "中", "低"}
    for record in run.outcome.pending:
        assert record.reason is not None
        assert record.reason.value, "挂起项缺原因"
        assert record.missing_fields is not None


def test_基准集跑批_待处理清单条目齐全(run: BenchmarkRun) -> None:
    """挂起清单文件里，一条挂起项都不能少（清单是人拿去查登记的唯一凭据）。"""
    pending_path = run.outcome.pending_path
    assert pending_path is not None and pending_path.is_file()
    text = pending_path.read_text(encoding="utf-8")
    assert f"条目数：{len(run.outcome.pending)} 条" in text
    for record in run.outcome.pending:
        assert record.accession_no in text
        assert record.item_code in text


def test_基准集跑批_审计流水一条不落(run: BenchmarkRun, state_db: Database) -> None:
    """审计流水与产出对得上：对上了几条、挂起了几条，一条都不能少。"""
    rows = state_db.execute(
        "SELECT event, COUNT(*) AS n FROM audit_log WHERE event IN (?, ?) GROUP BY event",
        (EVENT_ALIGNMENT_RATIONALE, EVENT_ALIGNMENT_PENDING),
    ).fetchall()
    counts = {row["event"]: row["n"] for row in rows}
    assert counts.get(EVENT_ALIGNMENT_RATIONALE, 0) == run.outcome.draft_entry_count
    assert counts.get(EVENT_ALIGNMENT_PENDING, 0) == run.outcome.pending_rationale_count


def test_基准集跑批_指标摘要与落盘(run: BenchmarkRun) -> None:
    """指标摘要要能打印、能落盘 —— 评审时拿得出数字。"""
    summary = run.metrics.summary()
    assert "错绑定数：0" in summary
    assert "无第三去向：0" in summary
    assert "同分多候选挂起率：2/2 = 100.0%" in summary

    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "benchmark_version": run.benchmark.version,
        "case_count": run.benchmark.case_count,
        **run.metrics.as_dict(),
    }
    target = reports_dir / "alignment-metrics.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert target.is_file()
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["misbound"] == 0
    assert reloaded["ambiguous_pending_rate"] == 1.0
    print(run.metrics.summary())


# ---------------------------------------------------------------- 基准集自身


def test_基准集标准答案_覆盖各级判定与挂起原因(run: BenchmarkRun) -> None:
    """基准集要覆盖到四级判定与主要挂起原因 —— 覆盖面不够就测不出东西。"""
    levels = run.outcome.match_levels
    # 结果无法获得 / 值为空 / 回报时间倒挂这几类在进入对齐之前就被扣下等人，
    # 不参与四级判定 —— 四级分布的条数因此比基准集条数少这几条。
    assert levels and sum(levels.values()) == (
        run.metrics.total - run.outcome.withheld - run.outcome.inverted_corrections
    )
    for level in (
        "L1_block_exact",
        "L2_region_normalized",
        "L3_region_fuzzy",
        "L4_pending",
    ):
        assert levels.get(level, 0) > 0, f"基准集缺少 {level} 的样本"

    reasons = {record.reason for record in run.outcome.pending}
    missing = REQUIRED_REASONS - reasons
    assert not missing, (
        f"基准集没覆盖这些挂起原因：{sorted(item.value for item in missing)}"
    )


def test_同分多候选标签与挂起原因枚举一致() -> None:
    """指标层认的「同分多候选」标签，必须与挂起原因枚举对得上。"""
    assert AMBIGUOUS_REASON == PendingReason.AMBIGUOUS.value


# ---------------------------------------------------------------- 指标自身


def test_指标能抓出错绑定与误并入() -> None:
    """指标必须真的能抓错 —— 否则「错绑定数 = 0」是句空话。"""
    cases = (
        GroundTruthCase(
            key=ResultKey("ACC-X", "IHC-CD3", "20260101130000"),
            expect_matched=True,
            expected_block=(1, "B1"),
        ),
        GroundTruthCase(
            key=ResultKey("ACC-X", "IHC-CD20", "20260101131000"),
            expect_matched=False,
            expected_reason=PendingReason.AMBIGUOUS.value,
        ),
    )
    decisions = (
        # 归到了另一块 → 错绑定。
        AlignmentDecision(
            key=cases[0].key,
            matched=True,
            block=(2, "B2"),
            rationale="L1 蜡块号精确（置信 高）",
        ),
        # 该挂起却并入了 → 误并入。
        AlignmentDecision(
            key=cases[1].key,
            matched=True,
            block=(1, "B1"),
            rationale="L1 蜡块号精确（置信 高）",
        ),
    )
    metrics = evaluate(decisions, cases)
    assert metrics.misbound == 1
    assert metrics.false_merge == 1
    assert metrics.ambiguous_cases == 1
    assert metrics.ambiguous_pending == 0
    assert metrics.ambiguous_pending_rate == 0.0
    assert metrics.ok is False


def test_指标能抓出无第三去向() -> None:
    """既没依据也没原因的结论，必须被算成「消失了」。"""
    cases = (
        GroundTruthCase(
            key=ResultKey("ACC-Y", "IHC-CD3", "20260101130000"),
            expect_matched=True,
            expected_block=(1, "B1"),
        ),
    )
    decisions = (
        # 并入了却没写依据。
        AlignmentDecision(key=cases[0].key, matched=True, block=(1, "B1")),
    )
    metrics = evaluate(decisions, cases)
    assert metrics.dangling == 1
    assert metrics.ok is False


def test_结论翻译自跑批产出(run: BenchmarkRun) -> None:
    """结论翻译只认「草稿条目」与「挂起记录」两处 —— 没有第三个来源。"""
    decisions = decisions_from_outcome(run.outcome)
    assert len(decisions) == run.metrics.total
    assert all(decision.has_trace for decision in decisions)
    assert {decision.matched for decision in decisions} == {True, False}
