"""规模与性能验收的测试。

功能测试证明「算得对」，这一组证明「**扛得住、涨不凶**」：

- 四项指标（端到端耗时 / 单轮对齐率 / 峰值内存占用 / 状态库体积）都真的
  量得出来，且口径写在报告里，不是拍脑袋填的数；
- 阈值超了就是不合格，判定要**点名是哪一项超了、超了多少** —— 只写一个
  「不通过」，运维还得自己翻 JSON；
- 同一批数据连跑两次，样本量与比率必须一致（只有耗时会变）；
- 命令行能出一条命令跑完并落一份可复核的报告。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from patho_supplement import cli
from patho_supplement.config import ConfigError
from patho_supplement.scale import ScaleThresholds, measure_scale

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATOR_PATH = PROJECT_ROOT / "tests" / "fixtures" / "generate_samples.py"


def _load_generator():
    """按路径加载生成器脚本（它是可执行脚本，不是包里的模块）。"""
    spec = importlib.util.spec_from_file_location("generate_samples", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


@pytest.fixture(scope="module")
def scale_dataset(tmp_path_factory) -> Path:
    """一小份规模集（9 份报告 / 60 条 ORU）—— 够跑通全链，又不至于拖慢测试。"""
    root = tmp_path_factory.mktemp("scale-set")
    dataset_dir, _ = generator.generate(out=root, flat=True, seed=42, scale=60)
    return dataset_dir


@pytest.fixture()
def work_dir(tmp_path) -> Path:
    return tmp_path / "work"


# --------------------------------------------------------------------------
# 阈值配置面
# --------------------------------------------------------------------------


def test_thresholds_default_to_built_in_values():
    """缺项用缺省值，不静默改用别的。"""
    thresholds = ScaleThresholds.from_env({})
    assert thresholds.max_elapsed_seconds == 60.0
    assert thresholds.min_alignment_rate == 0.60
    assert thresholds.max_pending_rate == 0.40
    assert thresholds.max_peak_memory_mb == 256.0
    assert thresholds.max_state_db_mb == 64.0


def test_thresholds_read_from_environment():
    """环境变量逐项生效。"""
    thresholds = ScaleThresholds.from_env(
        {
            "SCALE_MAX_ELAPSED_SECONDS": "12.5",
            "SCALE_MIN_ALIGNMENT_RATE": "0.8",
            "SCALE_MAX_PENDING_RATE": "0.2",
            "SCALE_MAX_PEAK_MEMORY_MB": "128",
            "SCALE_MAX_STATE_DB_MB": "32",
        }
    )
    assert thresholds.max_elapsed_seconds == 12.5
    assert thresholds.min_alignment_rate == 0.8
    assert thresholds.max_pending_rate == 0.2
    assert thresholds.max_peak_memory_mb == 128.0
    assert thresholds.max_state_db_mb == 32.0


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"SCALE_MAX_ELAPSED_SECONDS": "abc"}, "SCALE_MAX_ELAPSED_SECONDS"),
        ({"SCALE_MAX_ELAPSED_SECONDS": "-1"}, "SCALE_MAX_ELAPSED_SECONDS"),
        ({"SCALE_MIN_ALIGNMENT_RATE": "1.5"}, "SCALE_MIN_ALIGNMENT_RATE"),
        ({"SCALE_MAX_PENDING_RATE": "-0.1"}, "SCALE_MAX_PENDING_RATE"),
        ({"SCALE_MAX_PEAK_MEMORY_MB": "0"}, "SCALE_MAX_PEAK_MEMORY_MB"),
        ({"SCALE_MAX_STATE_DB_MB": "not-a-number"}, "SCALE_MAX_STATE_DB_MB"),
    ],
)
def test_out_of_range_thresholds_are_rejected(env, key):
    """阈值配错要报错并点名是哪一项 —— 不静默退回缺省值。"""
    with pytest.raises(ConfigError) as excinfo:
        ScaleThresholds.from_env(env)
    assert key in str(excinfo.value)


# --------------------------------------------------------------------------
# 四项指标
# --------------------------------------------------------------------------


def test_measurement_reports_four_metrics(scale_dataset, work_dir):
    """四项指标都量得出来，且口径自带样本量。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir)

    assert measurement.elapsed_seconds > 0
    assert 0.0 < measurement.alignment_rate <= 1.0
    assert 0.0 <= measurement.pending_rate < 1.0
    assert measurement.peak_memory_mb >= 0.0
    assert measurement.state_db_bytes > 0

    data = measurement.as_dict()
    assert set(data["metrics"]) >= {
        "elapsed_seconds",
        "alignment_rate",
        "pending_rate",
        "peak_memory_mb",
        "state_db_bytes",
    }


def test_sample_size_matches_the_data_actually_processed(scale_dataset, work_dir):
    """样本量要跟真跑过的报文对得上，数字不得凭空出现。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir)
    sample = measurement.sample

    assert sample["result_files"] == 60
    assert sample["order_files"] == 6
    assert sample["files_scanned"] == 66
    assert sample["reports"] == 9
    # 一条结果只有两个去向：进草稿或进挂起 —— 两者之和就是生效结果数。
    assert sample["draft_entries"] + sample["pending_count"] == sample["effective_results"]
    assert sample["effective_results"] > 0


def test_alignment_rate_and_pending_rate_are_complementary(scale_dataset, work_dir):
    """对齐率与挂起率互补（分母同为生效结果数）—— 不是两个独立凑出来的数。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir)
    assert measurement.alignment_rate + measurement.pending_rate == pytest.approx(
        1.0, abs=1e-9
    )


def test_measurement_records_environment(scale_dataset, work_dir):
    """报告里写明测量环境与样本量，换台机器复现才知道差在哪。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir)
    env = measurement.environment

    assert env["python_version"]
    assert env["platform"]
    assert env["cpu_count"]
    assert env["measured_at"]
    assert env["dataset_sha256"]
    assert measurement.sample["dataset_seed"] == 42


def test_state_db_size_is_measured_from_the_real_file(scale_dataset, work_dir):
    """状态库体积量的是真文件（主库 + WAL + SHM），不是估的。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir, state_db=work_dir / "s.db")
    db_path = work_dir / "s.db"

    assert db_path.is_file()
    assert measurement.state_db_bytes >= db_path.stat().st_size
    assert measurement.state_db_mb == pytest.approx(
        measurement.state_db_bytes / (1024 * 1024), abs=1e-6
    )


def test_measurement_is_reproducible(scale_dataset, tmp_path):
    """同一批数据连跑两次：样本量与比率一致（只有耗时会变）。"""
    first = measure_scale(scale_dataset, work_dir=tmp_path / "a")
    second = measure_scale(scale_dataset, work_dir=tmp_path / "b")

    assert first.sample["effective_results"] == second.sample["effective_results"]
    assert first.sample["draft_entries"] == second.sample["draft_entries"]
    assert first.alignment_rate == second.alignment_rate
    assert first.pending_rate == second.pending_rate


# --------------------------------------------------------------------------
# 阈值判定
# --------------------------------------------------------------------------


def test_all_checks_pass_on_a_healthy_run(scale_dataset, work_dir):
    """正常规模集跑完，五项判定都过。"""
    measurement = measure_scale(scale_dataset, work_dir=work_dir)

    assert measurement.ok
    assert measurement.failed_checks == []
    assert len(measurement.checks) == 5


def test_threshold_violation_fails_and_names_the_metric(scale_dataset, work_dir):
    """超阈即判定不合格，并且要点名是哪一项、超了多少。"""
    strict = ScaleThresholds(min_alignment_rate=1.0)
    measurement = measure_scale(scale_dataset, work_dir=work_dir, thresholds=strict)

    assert not measurement.ok
    failed = measurement.failed_checks
    assert [check.name for check in failed] == ["单轮对齐率"]
    assert "下限" in failed[0].detail
    assert "92" in failed[0].detail or "%" in failed[0].detail


def test_elapsed_threshold_violation_is_detected(scale_dataset, work_dir):
    """耗时上限调到 0 秒必然超 —— 判定得能抓到。"""
    strict = ScaleThresholds(max_elapsed_seconds=0.0)
    measurement = measure_scale(scale_dataset, work_dir=work_dir, thresholds=strict)

    assert not measurement.ok
    assert "端到端耗时" in [check.name for check in measurement.failed_checks]


def test_pending_rate_threshold_is_checked(scale_dataset, work_dir):
    """挂起率也单独判 —— 不能只对齐率。"""
    strict = ScaleThresholds(max_pending_rate=0.0)
    measurement = measure_scale(scale_dataset, work_dir=work_dir, thresholds=strict)

    assert not measurement.ok
    assert "挂起率" in [check.name for check in measurement.failed_checks]


def test_missing_dataset_directory_is_reported(tmp_path):
    """规模集目录不存在要说人话，不是抛栈。"""
    with pytest.raises(FileNotFoundError):
        measure_scale(tmp_path / "not-there", work_dir=tmp_path / "w")


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def test_cli_scale_report_writes_a_reviewable_report(scale_dataset, tmp_path):
    """一条命令跑完，报告落盘，含四项指标、阈值判定与测量环境。"""
    report_path = tmp_path / "reports" / "scale.json"
    exit_code = cli.main(
        [
            "benchmark",
            "--inbox",
            str(scale_dataset),
            "--scale-report",
            str(report_path),
            "--scale-work-dir",
            str(tmp_path / "work"),
        ]
    )

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["metrics"]["elapsed_seconds"] > 0
    assert report["metrics"]["alignment_rate"] > 0
    assert report["metrics"]["peak_memory_mb"] >= 0
    assert report["metrics"]["state_db_bytes"] > 0
    assert report["sample"]["effective_results"] > 0
    assert report["environment"]["python_version"]
    assert len(report["checks"]) == 5


def test_cli_scale_report_fails_when_threshold_exceeded(
    scale_dataset, tmp_path, monkeypatch
):
    """阈值超了 → 非 0 退出，并把超了哪一项写到标准错误。"""
    monkeypatch.setenv("SCALE_MIN_ALIGNMENT_RATE", "1.0")
    report_path = tmp_path / "scale.json"
    exit_code = cli.main(
        [
            "benchmark",
            "--inbox",
            str(scale_dataset),
            "--scale-report",
            str(report_path),
            "--scale-work-dir",
            str(tmp_path / "work"),
        ]
    )

    assert exit_code == 1
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False


def test_cli_scale_report_rejects_broken_threshold_config(
    scale_dataset, tmp_path, monkeypatch
):
    """阈值配错要拒绝启动，并点名是哪一项。"""
    monkeypatch.setenv("SCALE_MAX_ELAPSED_SECONDS", "abc")
    exit_code = cli.main(
        [
            "benchmark",
            "--inbox",
            str(scale_dataset),
            "--scale-report",
            str(tmp_path / "scale.json"),
            "--scale-work-dir",
            str(tmp_path / "work"),
        ]
    )

    assert exit_code == 1


def test_cli_scale_report_reports_missing_dataset(tmp_path):
    """规模集目录不存在 → 非 0 退出，不抛栈。"""
    exit_code = cli.main(
        [
            "benchmark",
            "--inbox",
            str(tmp_path / "nope"),
            "--scale-report",
            str(tmp_path / "scale.json"),
            "--scale-work-dir",
            str(tmp_path / "work"),
        ]
    )

    assert exit_code == 1


def test_cli_benchmark_help_mentions_scale_report(capsys):
    """运维要能从 --help 里找到这条命令。"""
    with pytest.raises(SystemExit):
        cli.main(["benchmark", "--help"])
    out = capsys.readouterr().out
    assert "--scale-report" in out
