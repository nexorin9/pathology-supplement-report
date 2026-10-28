"""判定阈值与降级策略的配置面（任务 id 29）。

阈值必须由科室自己配 —— 不同医院的部位描述习惯不同，词法命中线就该不同；
「配错了却看不出来」则是院内工具最常见的投诉来源。所以这份配置面只有两条验收：

1. 缺项能用默认值跑起来（不逼着科室先配一堆参数才让用）；
2. 越界立刻报错并说清**是哪一项、合法范围是多少**，且**不静默改用默认值** ——
   悄悄退回默认值会让人以为「按配置跑过了」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from patho_supplement.align.matcher import MatchLevel, MatchThresholds, align_entries
from patho_supplement.compose.baseline import baseline_from_payload
from patho_supplement.config import (
    DEFAULT_BM25_THRESHOLD,
    DEFAULT_FANOUT_MAX_WORKERS,
    DEFAULT_FILTER_POLICY,
    DEFAULT_LEAD_MARGIN_THRESHOLD,
    DEFAULT_PENDING_MAX_ATTEMPTS,
    Config,
    ConfigError,
)
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.models import Block, ReportBaseline, ResultEntry
from patho_supplement.pending.records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    decide_attempt,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_INBOX = PROJECT_ROOT / "tests" / "fixtures" / "inbox"
ACCESSION = "ACC-EXAMPLE-0001"


# ---------------------------------------------------------------- 缺省与生效


def test_missing_items_fall_back_to_defaults():
    """一项都不配时用缺省值，不逼着科室先配参数才让用。"""
    config = Config.from_env({})

    assert config.bm25_threshold == DEFAULT_BM25_THRESHOLD
    assert config.lead_margin_threshold == DEFAULT_LEAD_MARGIN_THRESHOLD
    assert config.filter_policy == DEFAULT_FILTER_POLICY
    assert config.fanout_max_workers == DEFAULT_FANOUT_MAX_WORKERS
    assert config.pending_max_attempts == DEFAULT_PENDING_MAX_ATTEMPTS


def test_env_items_take_effect():
    """配置项走环境变量（.env 走同一条路），逐项生效。"""
    config = Config.from_env(
        {
            "BM25_THRESHOLD": "0.51",
            "LEAD_MARGIN_THRESHOLD": "0.11",
            "FILTER_POLICY": "REPLACE",
            "FANOUT_MAX_WORKERS": "8",
            "FANOUT_ROUTE_TIMEOUT": "2.5",
            "PENDING_MAX_ATTEMPTS": "5",
        }
    )

    assert config.bm25_threshold == 0.51
    assert config.lead_margin_threshold == 0.11
    assert config.filter_policy == "REPLACE"
    assert config.fanout_max_workers == 8
    assert config.fanout_route_timeout == 2.5
    assert config.pending_max_attempts == 5


def test_filter_policy_value_is_case_insensitive():
    """过滤策略大小写不敏感，但取值只有 REPLACE / MERGE 两种。"""
    assert Config.from_env({"FILTER_POLICY": "replace"}).filter_policy == "REPLACE"
    assert Config.from_env({"FILTER_POLICY": " merge "}).filter_policy == "MERGE"


# ---------------------------------------------------------------- 越界即拒


@pytest.mark.parametrize(
    ("env_key", "value"),
    [
        ("BM25_THRESHOLD", "1.5"),
        ("BM25_THRESHOLD", "-0.1"),
        ("BM25_THRESHOLD", "abc"),
        ("LEAD_MARGIN_THRESHOLD", "1.5"),
        ("LEAD_MARGIN_THRESHOLD", "-0.5"),
        ("FANOUT_MAX_WORKERS", "0"),
        ("FANOUT_MAX_WORKERS", "33"),
        ("FANOUT_MAX_WORKERS", "many"),
        ("FANOUT_ROUTE_TIMEOUT", "0"),
        ("PENDING_MAX_ATTEMPTS", "0"),
        ("PENDING_MAX_ATTEMPTS", "51"),
    ],
)
def test_out_of_range_values_are_rejected(env_key, value):
    """负数、非数值、超范围一律报错，并点名是哪一项。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({env_key: value})

    message = str(excinfo.value)
    assert env_key in message
    assert any(token in message for token in ("不得大于", "不得小于", "必须是"))


def test_invalid_filter_policy_is_rejected_with_allowed_values():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"FILTER_POLICY": "STRICT"})

    message = str(excinfo.value)
    assert "FILTER_POLICY" in message
    assert "REPLACE" in message and "MERGE" in message


def test_error_message_gives_the_legal_range():
    """报错要说清合法范围，运维照着就能改对。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"BM25_THRESHOLD": "1.5"})

    message = str(excinfo.value)
    assert "BM25_THRESHOLD" in message
    assert "1" in message  # 上界写出来


# ---------------------------------------------------------------- 阈值真的起作用


def _entry(region_desc: str, **kwargs) -> ResultEntry:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-ER",
        value="阴性",
        region_seq=None,
        block_no=None,
        region_desc=region_desc,
    )
    base.update(kwargs)
    return ResultEntry(**base)


def _block(**kwargs) -> Block:
    base = dict(
        region_seq=1,
        region_desc="子宫体",
        block_no="B1",
        order_no="ORD-EXAMPLE-0001",
        ordered_items=("IHC-ER",),
    )
    base.update(kwargs)
    return Block(**base)


def _baseline() -> ReportBaseline:
    """两个部位的基线：词法候选里有一个明显不相关的，分差才有意义。"""
    return ReportBaseline(
        accession_no=ACCESSION,
        blocks=(
            _block(),
            _block(
                region_seq=2,
                region_desc="乳腺组织",
                block_no="B2",
                order_no="ORD-EXAMPLE-0002",
            ),
        ),
        base_version="base-v1",
    )


def _l3_hits(threshold: str) -> int:
    """按给定阈值跑一批结果，数落在 L3 词法模糊的有几条。"""
    config = Config.from_env({"BM25_THRESHOLD": threshold})
    entries = [_entry("子宫体内膜"), _entry("子宫体下段"), _entry("子宫内膜")]
    matched, pending = align_entries(
        entries,
        {ACCESSION: _baseline()},
        thresholds=MatchThresholds.from_config(config),
    )
    assert len(matched) + len(pending) == len(entries)
    return sum(
        1 for _, candidate in matched if candidate.match_level is MatchLevel.L3_REGION_FUZZY
    )


def test_raising_bm25_threshold_reduces_l3_hits():
    """阈值调高 → 靠文字猜的命中变少 → 阈值可配不是摆设。"""
    assert _l3_hits("0.2") == 3
    assert _l3_hits("0.9") == 0


def test_raising_lead_margin_threshold_turns_a_fuzzy_hit_into_pending():
    """领先幅度阈值调高 → 唯一领先不够 → 改挂起，不硬绑。"""
    entries = [_entry("子宫体内膜")]

    loose = Config.from_env({"LEAD_MARGIN_THRESHOLD": "0.05"})
    matched, _ = align_entries(
        entries,
        {ACCESSION: _baseline()},
        thresholds=MatchThresholds.from_config(loose),
    )
    assert len(matched) == 1

    strict = Config.from_env({"LEAD_MARGIN_THRESHOLD": "0.9"})
    matched, pending = align_entries(
        entries,
        {ACCESSION: _baseline()},
        thresholds=MatchThresholds.from_config(strict),
    )
    assert len(matched) == 0
    assert len(pending) == 1
    assert pending[0][1].pending_reason == "同分多候选"


# ---------------------------------------------------------------- 挂起尝试次数上限


def test_attempt_decision_counts_and_escalates():
    """尝试次数逐次累加；超过上限就转人工处置，不再自动续跑。"""
    first = decide_attempt(0, 2)
    assert first.attempts == 1
    assert first.state == PENDING_STATE_PENDING
    assert first.escalated is False

    second = decide_attempt(first.attempts, 2)
    assert second.attempts == 2
    assert second.state == PENDING_STATE_PENDING

    third = decide_attempt(second.attempts, 2)
    assert third.attempts == 3
    assert third.state == PENDING_STATE_ESCALATED
    assert third.escalated is True
    assert "3" in (third.note or "") and "2" in (third.note or "")


class _BaselineOnlyClient:
    """只回基线的本地替身：本用例只关心挂起计数，不触发回写。"""

    def __init__(self, baseline: ReportBaseline) -> None:
        self._baseline = baseline

    def fetch_baseline(self, accession_no: str) -> ReportBaseline:
        return self._baseline

    def post_supplemental_draft(self, accession_no: str, **kwargs):  # pragma: no cover
        raise AssertionError("这条用例不该走到回写")


def _fixture_baseline() -> ReportBaseline:
    payload = json.loads((FIXTURE_INBOX / "baseline.json").read_text(encoding="utf-8"))
    report = next(
        item for item in payload["reports"] if item["accession_no"] == ACCESSION
    )
    return baseline_from_payload(report, expected_accession_no=ACCESSION)


def test_daemon_escalates_pending_item_after_attempt_limit(tmp_path):
    """同一条结果反复对不上 → 超过尝试上限 → 转人工处置并写审计。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    db_path = tmp_path / "state.db"
    messages: list[str] = []
    config = Config.from_env(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(db_path),
            "PENDING_MAX_ATTEMPTS": "1",
        }
    )
    daemon = Daemon(
        config,
        client=_BaselineOnlyClient(_fixture_baseline()),
        database=Database(db_path),
        watcher=InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0),
        output_dir=tmp_path / "out",
        echo=messages.append,
    )

    # 第一轮：这条回报没带蜡块号，对不上 → 挂起（第 1 次尝试）
    source = FIXTURE_INBOX / "acc-example-0001-03-ki67-no-block.hl7"
    shutil.copy(source, inbox / source.name)
    first = daemon.run_once()
    assert first.pending_count == 1
    assert first.pending_escalated == 0

    # 第二轮：同一项目的又一条回报（观察时间不同，不是重复投递）→ 又挂起一次
    # 超过上限（1 次）→ 转人工处置
    again = source.read_text(encoding="utf-8").replace(
        "20260101132000", "20260101133000"
    ).replace("MSG-EXAMPLE-0003", "MSG-EXAMPLE-0007")
    (inbox / "acc-example-0001-07-ki67-again.hl7").write_text(again, encoding="utf-8")
    second = daemon.run_once()
    daemon.close()

    assert second.pending_count == 1
    assert second.pending_escalated == 1

    with Database(db_path) as db:
        rows = db.query_all(
            "SELECT attempts, state FROM pending_items WHERE accession_no = ?"
            " ORDER BY id",
            (ACCESSION,),
        )
        assert [(row["attempts"], row["state"]) for row in rows] == [
            (1, PENDING_STATE_PENDING),
            (2, PENDING_STATE_ESCALATED),
        ]
        audit = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'pending_escalated'"
        )
        assert len(audit) == 1
        assert "IHC-KI67" in audit[0]["detail_json"]

    # 转人工的挂起项要出现在待处理清单里，并写明已超上限
    listing = (tmp_path / "out" / "pending_list.txt").read_text(encoding="utf-8")
    assert "转人工处置" in listing
    assert "IHC-KI67" in listing


# ---------------------------------------------------------------- 命令行行为


def _run_cli(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "PATHOLOGY_BASE_URL": "http://127.0.0.1:9",
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(FIXTURE_INBOX),
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "patho_supplement.cli", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_replay_refuses_to_start_on_invalid_config(tmp_path):
    """配置越界 → 本轮不启动，且说清是哪一项配错了。"""
    result = _run_cli(
        ["replay", "--inbox", str(FIXTURE_INBOX), "--out", str(tmp_path / "out")],
        {"BM25_THRESHOLD": "1.5"},
    )

    assert result.returncode != 0
    assert "BM25_THRESHOLD" in result.stderr
    assert "不启动" in result.stderr


def test_doctor_refuses_on_invalid_config():
    result = _run_cli(["doctor"], {"PENDING_MAX_ATTEMPTS": "0"})

    assert result.returncode != 0
    assert "PENDING_MAX_ATTEMPTS" in result.stderr


def test_doctor_prints_all_effective_thresholds():
    """doctor 把全部生效阈值打出来，运维一眼能看到当前配置。"""
    result = _run_cli(
        ["doctor"],
        {"BM25_THRESHOLD": "0.51", "PENDING_MAX_ATTEMPTS": "5", "FANOUT_MAX_WORKERS": "8"},
    )

    assert result.returncode == 0, result.stderr
    for key in (
        "BM25_THRESHOLD",
        "LEAD_MARGIN_THRESHOLD",
        "FILTER_POLICY",
        "FANOUT_MAX_WORKERS",
        "FANOUT_ROUTE_TIMEOUT",
        "PENDING_MAX_ATTEMPTS",
        "WRITEBACK_MAX_RETRIES",
        "HTTP_TIMEOUT",
        "WATCH_POLL_INTERVAL",
        "WATCH_SETTLE_SECONDS",
    ):
        assert key in result.stdout
    assert "0.51" in result.stdout
    assert "5" in result.stdout
