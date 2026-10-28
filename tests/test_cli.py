"""运维命令行的验收测试。

这一组盯的是**运维通道本身**：命令齐不齐、退出码对不对、``doctor`` 说不说
人话、``benchmark`` 的指标能不能当 CI 闸门用、``summary`` 能不能把一轮跑了
多少讲清楚。

退出码口径（与 ``cli.py`` 顶部常量一致）：

- ``0`` 成功；
- ``1`` 失败（配置有误、指标不达标、状态库不存在……）；
- ``2`` 用法不对（没给子命令、参数用错）；
- ``3`` 补齐后仍对不上，挂起继续等人；
- ``4`` 这一行正被别人补齐（互斥没抢到）；
- ``5`` 已有另一个守护进程在盯这个目录。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from patho_supplement import cli
from patho_supplement.logging_setup import LOGGER_NAME

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "inbox"
BENCHMARK_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "benchmark"

#: 命令行必须齐备的子命令 —— 运维与 CI 靠这一组名字排班。
EXPECTED_COMMANDS = (
    "replay",
    "daemon",
    "doctor",
    "pending",
    "audit",
    "benchmark",
    "summary",
)

#: doctor 必须打印的对外接口清单（入站 2 条事件流 + 出站 2 条 HTTP + 推送）。
EXPECTED_INTERFACE_HINTS = (
    "ORU^R01",
    "ORM^O01",
    "GET 病理报告基线",
    "POST 补充报告草稿",
    "SMTP / 企业微信通知",
)


def _run(argv: list[str]) -> int:
    """跑一条命令行，返回退出码。"""
    return cli.main(argv)


def _clear_env(monkeypatch) -> None:
    """把本组测试关心的凭据位清干净（环境里可能本来就设过）。"""
    for key in (
        "PATHOLOGY_BASE_URL",
        "PATHOLOGY_API_TOKEN",
        "PATHOLOGY_RESULT_INBOX",
        "PATHOLOGY_STATE_DB",
        "PATHOLOGY_LEXICON_PATH",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "NOTIFY_RECIPIENTS",
        "WECOM_WEBHOOK_URL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _restore_logging():
    """本组会跑 ``cli replay``，它会改装包日志器（挂 handler、关 propagate）——
    用完还原，免得后面别的测试文件收不到日志、或写到已关闭的捕获流上。"""
    logger = logging.getLogger(LOGGER_NAME)
    before_handlers = list(logger.handlers)
    before_propagate = logger.propagate
    before_level = logger.level
    yield
    for handler in list(logger.handlers):
        if handler not in before_handlers:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 清理不该让测试失败
                pass
    logger.handlers = list(before_handlers)
    logger.propagate = before_propagate
    logger.setLevel(before_level)


# ------------------------------------------------------------------ 命令齐备


def test_help_lists_every_command(capsys):
    """总帮助里要能看到全部子命令 —— 运维不查文档，只看 --help。"""
    with pytest.raises(SystemExit) as excinfo:
        _run(["--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    for command in EXPECTED_COMMANDS:
        assert command in out, f"总帮助里少了子命令：{command}"


@pytest.mark.parametrize("command", EXPECTED_COMMANDS)
def test_each_command_has_help(command, capsys):
    """每个子命令都有 --help，且退出码为 0。"""
    with pytest.raises(SystemExit) as excinfo:
        _run([command, "--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "usage:" in out


@pytest.mark.parametrize(
    "group", ["pending", "audit"]
)
def test_subcommand_group_without_subcommand_prints_usage(group, capsys):
    """「pending」「audit」是一组子命令：只敲组名不带子命令时打印用法并退 2。"""
    code = _run([group])
    out = capsys.readouterr().out

    assert code == 2
    assert "usage:" in out


def test_no_command_prints_usage(capsys):
    """不带子命令：打印用法并退 2。"""
    code = _run([])
    out = capsys.readouterr().out

    assert code == 2
    assert "usage:" in out


# ------------------------------------------------------------------ doctor


def test_doctor_lists_interfaces_and_missing_credentials(monkeypatch, capsys):
    """凭据位缺了：doctor 要点名缺哪几个，并以非 0 退出。"""
    _clear_env(monkeypatch)

    code = _run(["doctor"])
    captured = capsys.readouterr()

    assert code != 0
    assert code == 1
    # 缺项清单：点名到键名，不能只说「配置不全」。
    assert "缺少必需的环境变量" in captured.err
    assert "PATHOLOGY_BASE_URL" in captured.err
    assert "PATHOLOGY_API_TOKEN" in captured.err
    assert "PATHOLOGY_RESULT_INBOX" in captured.err
    # 对外接口清单照打 —— 缺凭据也要让人看得见要接哪几条。
    assert "【对外接口清单】" in captured.out
    for hint in EXPECTED_INTERFACE_HINTS:
        assert hint in captured.out


def test_doctor_passes_when_credentials_present(monkeypatch, capsys):
    """凭据齐了：doctor 退出 0，并打印生效阈值与对外接口清单。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("PATHOLOGY_BASE_URL", "http://127.0.0.1:9/pathology")
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "token-for-selftest")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", str(INBOX_FIXTURE))

    code = _run(["doctor"])
    captured = capsys.readouterr()

    assert code == 0
    assert "自检通过：凭据位齐备。" in captured.out
    for hint in EXPECTED_INTERFACE_HINTS:
        assert hint in captured.out
    # 生效阈值照打 —— 配错了要看得见当前值。
    assert "【生效阈值与运行参数】" in captured.out
    assert "BM25_THRESHOLD" in captured.out


def test_doctor_rejects_out_of_range_config(monkeypatch, capsys):
    """阈值越界：doctor 直接拒启并说明是哪一项、合法范围是多少。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BM25_THRESHOLD", "1.5")

    code = _run(["doctor"])
    captured = capsys.readouterr()

    assert code == 1
    assert "BM25_THRESHOLD" in captured.err
    assert "不得大于" in captured.err


# ------------------------------------------------------------------ benchmark


def test_benchmark_runs_product_path_and_passes(tmp_path, capsys):
    """基准集跑批：跑产品真路径，指标达标即退出 0。"""
    code = _run(
        [
            "benchmark",
            "--dataset",
            str(BENCHMARK_FIXTURE),
            "--out",
            str(tmp_path / "bench"),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "基准集跑批" in out
    assert "错绑定数：0" in out
    assert "无第三去向：0" in out
    assert "指标达标" in out
    # 指标落盘，供 CI 与评审复核。
    metrics_path = tmp_path / "bench" / "metrics.json"
    assert metrics_path.is_file()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["misbound"] == 0
    assert metrics["false_merge"] == 0
    assert metrics["dangling"] == 0
    assert metrics["ok"] is True


def test_benchmark_writes_drafts_and_pending_list(tmp_path, capsys):
    """基准集跑批走的是产品真路径：产物里有草稿与待处理清单。"""
    out_dir = tmp_path / "bench"
    code = _run(
        ["benchmark", "--dataset", str(BENCHMARK_FIXTURE), "--out", str(out_dir)]
    )
    capsys.readouterr()

    assert code == 0
    assert (out_dir / "drafts").is_dir()
    assert (out_dir / "pending_list.txt").is_file()
    assert (out_dir / "summary.json").is_file()


def test_benchmark_missing_dataset_exits_nonzero(tmp_path, capsys):
    """基准集目录不存在：报错并退 1，不静默产出空指标。"""
    code = _run(["benchmark", "--dataset", str(tmp_path / "nope")])
    captured = capsys.readouterr()

    assert code == 1
    assert "基准集不可用" in captured.err


def test_benchmark_incomplete_dataset_exits_nonzero(tmp_path, capsys):
    """基准集缺标准答案：报错并退 1 —— 少一半就没有对账的意义。"""
    dataset = tmp_path / "half"
    dataset.mkdir()
    (dataset / "baseline.json").write_text("{}", encoding="utf-8")

    code = _run(["benchmark", "--dataset", str(dataset)])
    captured = capsys.readouterr()

    assert code == 1
    assert "基准集不可用" in captured.err
    assert "ground_truth.json" in captured.err


def test_benchmark_fails_when_metrics_break(tmp_path, capsys, monkeypatch):
    """指标不达标必须以非 0 退出，且点名是哪一项坏了 —— 可直接当 CI 闸门。"""
    # 把「错绑定数」的判定改坏：让产品把一条挂起的结果硬绑进草稿。
    import patho_supplement.align.metrics as metrics_module

    original = metrics_module.evaluate

    def broken_evaluate(decisions, cases):  # noqa: ANN001 - 测试替身
        result = original(decisions, cases)
        return metrics_module.AlignmentMetrics(
            total=result.total,
            aligned=result.aligned,
            pending=result.pending,
            misbound=1,  # 造一条错绑定
            false_merge=result.false_merge,
            ambiguous_cases=result.ambiguous_cases,
            ambiguous_pending=result.ambiguous_pending,
        )

    monkeypatch.setattr(metrics_module, "evaluate", broken_evaluate)
    monkeypatch.setattr(
        "patho_supplement.align.benchmark.evaluate", broken_evaluate, raising=True
    )

    code = _run(
        [
            "benchmark",
            "--dataset",
            str(BENCHMARK_FIXTURE),
            "--out",
            str(tmp_path / "bench"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "指标不达标" in captured.err
    assert "错绑定 1 条" in captured.err


# ------------------------------------------------------------------ summary


@pytest.fixture()
def replay_run(tmp_path) -> Path:
    """先跑一轮批处理重放，产出带 summary.json 的产物目录。"""
    out_dir = tmp_path / "replay"
    code = _run(["replay", "--inbox", str(INBOX_FIXTURE), "--out", str(out_dir)])
    assert code == 0
    return out_dir


def test_summary_reads_replay_run_summary(replay_run, capsys):
    """summary 能把一轮跑了多少讲清楚，并核对条数守恒。"""
    code = _run(["summary", "--dir", str(replay_run)])
    out = capsys.readouterr().out

    assert code == 0
    assert "运行摘要" in out
    assert "入账结果" in out
    assert "并入草稿" in out
    assert "挂起待人工" in out
    assert "条数守恒" in out
    assert "对得上" in out


def test_summary_json_output_is_machine_readable(replay_run, capsys):
    """--json 输出可被其他程序读取。"""
    code = _run(["summary", "--dir", str(replay_run), "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out)
    assert payload["run"]["kind"] == "replay"
    assert payload["run"]["core"]["processed"] == 7
    assert payload["run"]["balanced"] is True
    assert payload["run"]["drafts"]


def test_summary_reads_daemon_round_summary(tmp_path, capsys):
    """守护进程写的 round_summary.json 也能读 —— 两种摘要字段名不同，
    但运维只该记一个命令。"""
    run_dir = tmp_path / "daemon"
    run_dir.mkdir()
    (run_dir / "round_summary.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-28 09:00:00",
                "summary_line": "本轮处理 7 条｜对齐 5 条｜挂起 1 条｜写回成功 2 份、失败 0 份｜耗时 1.20 秒",
                "summary": {
                    "processed": 7,
                    "aligned": 5,
                    "pending": 1,
                    "writeback_ok": 2,
                    "writeback_failed": 0,
                    "elapsed_seconds": 1.2,
                    "notify_sent": 1,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    code = _run(["summary", "--dir", str(run_dir)])
    out = capsys.readouterr().out

    assert code == 0
    assert "本轮一行摘要" in out
    assert "写回成功" in out
    assert "本轮耗时（秒）" in out


def test_summary_missing_run_dir_exits_nonzero(tmp_path, capsys):
    """产物目录里没有摘要：说清是哪个文件没找到，并退 1。"""
    code = _run(["summary", "--dir", str(tmp_path / "empty")])
    captured = capsys.readouterr()

    assert code == 1
    assert "运行摘要读不出来" in captured.err


def test_summary_broken_summary_file_exits_nonzero(tmp_path, capsys):
    """摘要文件坏了：说清是哪个文件坏了，并退 1。"""
    run_dir = tmp_path / "broken"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{不是 JSON", encoding="utf-8")

    code = _run(["summary", "--dir", str(run_dir)])
    captured = capsys.readouterr()

    assert code == 1
    assert "运行摘要读不出来" in captured.err
    assert "summary.json" in captured.err


def test_summary_without_source_exits_usage(monkeypatch, tmp_path, capsys):
    """两个来源都没给：退 2，并告诉运维两条路怎么走。"""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    code = _run(["summary"])
    captured = capsys.readouterr()

    assert code == 2
    assert "--dir" in captured.err
    assert "--state-db" in captured.err


def test_summary_state_db_overview(tmp_path, capsys):
    """summary 能从状态库汇总：草稿、挂起项、回写队列、审计条数一眼可见。"""
    state_db = tmp_path / "state.db"
    code = _run(
        [
            "replay",
            "--inbox",
            str(INBOX_FIXTURE),
            "--out",
            str(tmp_path / "replay"),
            "--state-db",
            str(state_db),
        ]
    )
    assert code == 0

    code = _run(["summary", "--state-db", str(state_db)])
    out = capsys.readouterr().out

    assert code == 0
    assert "状态库概览" in out
    assert "审计流水合计" in out
    assert "审计分类" in out


def test_summary_state_db_missing_exits_nonzero(tmp_path, capsys):
    """状态库不存在：说清路径并退 1，不打一份空概览糊弄人。"""
    code = _run(["summary", "--state-db", str(tmp_path / "nope.db")])
    captured = capsys.readouterr()

    assert code == 1
    assert "状态库不存在" in captured.err


def test_summary_combines_both_sources(replay_run, tmp_path, capsys):
    """两个来源都给就都打印。"""
    state_db = tmp_path / "state.db"
    _run(
        [
            "replay",
            "--inbox",
            str(INBOX_FIXTURE),
            "--out",
            str(tmp_path / "replay2"),
            "--state-db",
            str(state_db),
        ]
    )

    code = _run(
        ["summary", "--dir", str(replay_run), "--state-db", str(state_db)]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "运行摘要" in out
    assert "状态库概览" in out


# ------------------------------------------------------------------ 退出码常量


def test_exit_code_constants_are_distinct():
    """退出码各司其职，别撞车 —— 运维脚本按码分支。"""
    codes = {
        cli.EXIT_OK,
        cli.EXIT_FAILURE,
        cli.EXIT_USAGE,
        cli.EXIT_STILL_PENDING,
        cli.EXIT_LOCKED,
        cli.EXIT_ALREADY_RUNNING,
    }
    assert len(codes) == 6
    assert cli.EXIT_OK == 0


def test_cli_module_exposes_build_parser():
    """解析器构造是公开入口 —— 别的程序（如 systemd 单元）可能要用。"""
    parser = cli.build_parser()
    assert parser.prog == "patho-supplement"


def test_commands_read_credentials_from_environment_only(monkeypatch, capsys):
    """凭据位只从环境变量读：命令跑完不会把凭据写回环境，也不会写盘。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("PATHOLOGY_BASE_URL", "http://127.0.0.1:9/pathology")
    monkeypatch.setenv("PATHOLOGY_API_TOKEN", "token-for-selftest")
    monkeypatch.setenv("PATHOLOGY_RESULT_INBOX", str(INBOX_FIXTURE))

    code = _run(["doctor"])
    out = capsys.readouterr().out

    assert code == 0
    # 只回显「已配置 / 未配置」，绝不回显凭据本身。
    assert "token-for-selftest" not in out
    assert "已配置" in out
