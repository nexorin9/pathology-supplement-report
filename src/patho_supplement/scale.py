"""规模与性能验收：在一批真实体量的结果回报上量四个数。

院内的日常体量是「一天几十到上百份报告、每份 6–10 项加做结果」。守护进程
能不能扛住这个量、状态库会不会越滚越大、内存会不会跟着运行时长一路涨 ——
这三件事在功能测试里看不出来，只能**拿真实体量的输入跑一遍**才有数。

因此本模块只做一件事：把一批报文跑过**产品真路径**（入站解析 → 幂等去重 →
更正取代 → 待查项派生 → 四级对齐 → 成稿 / 挂起），量出四项可核对的指标：

============================  ==========================================
指标                          口径
============================  ==========================================
端到端耗时                    从读第一个文件到跑完落盘的墙钟秒数
单轮对齐率                    并入草稿条目 ÷ 生效结果数（一条结果只有
                              「进草稿」或「进挂起」两个去向）
峰值内存占用                  本轮跑完后的进程峰值 RSS 相对进场时的增量
状态库体积                    SQLite 主库 + WAL + SHM 的字节数合计
============================  ==========================================

**阈值超了就是不合格**，报告里逐条点名是哪一项超了、超了多少 —— 运维看到
「耗时 12.4 秒（上限 60 秒）」才知道该查什么，而不是只看到一个「不通过」。

数据来自脚本化合成生成器（``tests/fixtures/generate_samples.py --scale 2000
--flat``）：固定种子可重跑、断网不失败、标识一律类目化占位。它是**脱敏样例**，
不是院内真实数据 —— 但体量、形态分布与真实批次一致，量出来的数才有参考意义。
"""

from __future__ import annotations

import json
import os
import platform
import resource
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import (
    DEFAULT_SCALE_MAX_ELAPSED_SECONDS,
    DEFAULT_SCALE_MAX_PEAK_MEMORY_MB,
    DEFAULT_SCALE_MAX_PENDING_RATE,
    DEFAULT_SCALE_MAX_STATE_DB_MB,
    DEFAULT_SCALE_MIN_ALIGNMENT_RATE,
    Config,
)
from .db import Database
from .replay import ReplayOutcome, run_replay

__all__ = [
    "ScaleThresholds",
    "ScaleCheck",
    "ScaleMeasurement",
    "measure_scale",
    "DEFAULT_MAX_ELAPSED_SECONDS",
    "DEFAULT_MIN_ALIGNMENT_RATE",
    "DEFAULT_MAX_PENDING_RATE",
    "DEFAULT_MAX_PEAK_MEMORY_MB",
    "DEFAULT_MAX_STATE_DB_MB",
]

#: 端到端耗时上限（秒）。实测 2200 份报文约 1.5 秒（2026 款笔记本 CPU）；
#: 院内机再慢十倍也留得下余量，但慢到分钟级就说明某处退化了。
DEFAULT_MAX_ELAPSED_SECONDS = DEFAULT_SCALE_MAX_ELAPSED_SECONDS
#: 单轮对齐率下限。并入草稿的条目占比不得低于这个数 —— 低于它说明大批结果
#: 进了挂起清单，等于白跑一轮。
DEFAULT_MIN_ALIGNMENT_RATE = DEFAULT_SCALE_MIN_ALIGNMENT_RATE
#: 挂起率上限（分母同为生效结果数，与对齐率互补）。留一点余量：无法获得 /
#: 值为空 / 回报时间倒挂这几类本来就该扣下等人。
DEFAULT_MAX_PENDING_RATE = DEFAULT_SCALE_MAX_PENDING_RATE
#: 峰值内存增量上限（MB）。状态只落 SQLite，中间对象一轮结束就释放 ——
#: 增量涨过这个数，说明有东西被留住了。
DEFAULT_MAX_PEAK_MEMORY_MB = DEFAULT_SCALE_MAX_PEAK_MEMORY_MB
#: 状态库体积上限（MB）。实测约 2.9 MB；院内机磁盘不宽裕，别让它无界涨。
DEFAULT_MAX_STATE_DB_MB = DEFAULT_SCALE_MAX_STATE_DB_MB

#: 规模集清单文件名（生成器写的）。
MANIFEST_NAME = "manifest.json"


def _peak_rss_mb() -> float:
    """当前进程的峰值 RSS（MB）。

    ``ru_maxrss`` 的单位在 macOS 是字节、在 Linux 是 KB —— 按平台换算，
    别把 Linux 上的数当成字节用（会差一千倍）。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024.0 * 1024.0)
    return raw / 1024.0


@dataclass(frozen=True)
class ScaleThresholds:
    """规模验收的阈值（可配；越界即拒绝启动）。"""

    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS
    min_alignment_rate: float = DEFAULT_MIN_ALIGNMENT_RATE
    max_pending_rate: float = DEFAULT_MAX_PENDING_RATE
    max_peak_memory_mb: float = DEFAULT_MAX_PEAK_MEMORY_MB
    max_state_db_mb: float = DEFAULT_MAX_STATE_DB_MB

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ScaleThresholds":
        """从环境变量读阈值（``SCALE_*``）；缺项用缺省值，越界报错。

        走的是 :class:`~patho_supplement.config.Config` 那一套解析 ——
        阈值只有一份真源，``doctor`` 打印的与这里生效的是同一批值。
        """
        config = Config.from_env(env)
        return cls(
            max_elapsed_seconds=config.scale_max_elapsed_seconds,
            min_alignment_rate=config.scale_min_alignment_rate,
            max_pending_rate=config.scale_max_pending_rate,
            max_peak_memory_mb=config.scale_max_peak_memory_mb,
            max_state_db_mb=config.scale_max_state_db_mb,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "min_alignment_rate": self.min_alignment_rate,
            "max_pending_rate": self.max_pending_rate,
            "max_peak_memory_mb": self.max_peak_memory_mb,
            "max_state_db_mb": self.max_state_db_mb,
        }


@dataclass(frozen=True)
class ScaleCheck:
    """一项阈值判定。"""

    name: str
    passed: bool
    actual: float
    limit: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": self.actual,
            "limit": self.limit,
            "detail": self.detail,
        }


@dataclass
class ScaleMeasurement:
    """一次规模验收的测量结果。"""

    inbox: str
    sample: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    alignment_rate: float = 0.0
    pending_rate: float = 0.0
    peak_memory_mb: float = 0.0
    process_peak_rss_mb: float = 0.0
    entry_peak_rss_mb: float = 0.0
    state_db_bytes: int = 0
    state_db_mb: float = 0.0
    environment: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    checks: list[ScaleCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全部阈值都过了才算合格。"""
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[ScaleCheck]:
        return [check for check in self.checks if not check.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "inbox": self.inbox,
            "sample": dict(self.sample),
            "metrics": {
                "elapsed_seconds": round(self.elapsed_seconds, 3),
                "alignment_rate": round(self.alignment_rate, 4),
                "pending_rate": round(self.pending_rate, 4),
                "peak_memory_mb": round(self.peak_memory_mb, 2),
                "process_peak_rss_mb": round(self.process_peak_rss_mb, 2),
                "state_db_bytes": self.state_db_bytes,
                "state_db_mb": round(self.state_db_mb, 3),
            },
            "environment": dict(self.environment),
            "thresholds": dict(self.thresholds),
            "checks": [check.as_dict() for check in self.checks],
            "ok": self.ok,
        }

    def summary(self) -> str:
        """给人看的一行式摘要（打印用）。"""
        return (
            f"样本量 {self.sample.get('effective_results', 0)} 条生效结果"
            f"（{self.sample.get('result_files', 0)} 份结果报文 / "
            f"{self.sample.get('order_files', 0)} 份申请报文 / "
            f"{self.sample.get('reports', 0)} 份报告）｜"
            f"端到端耗时 {self.elapsed_seconds:.2f} 秒｜"
            f"单轮对齐率 {self.alignment_rate:.1%}｜"
            f"挂起率 {self.pending_rate:.1%}｜"
            f"峰值内存增量 {self.peak_memory_mb:.1f} MB｜"
            f"状态库体积 {self.state_db_mb:.2f} MB"
        )

    def check_lines(self) -> list[str]:
        """逐条阈值判定（打印用）。"""
        lines: list[str] = []
        for check in self.checks:
            mark = "过" if check.passed else "不过"
            lines.append(f"[{mark}] {check.name}：{check.detail}")
        return lines


def _state_db_size(path: Path) -> int:
    """状态库体积：主库 + WAL + SHM 一起算。

    WAL 模式下刚写完的数据可能还在 ``-wal`` 里，只看主库会低估。
    """
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _read_manifest(inbox: Path) -> dict[str, Any]:
    """读规模集清单（生成器写的）；没有就返回空字典。"""
    manifest_path = inbox / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sample_of(outcome: ReplayOutcome, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """样本量口径（写进报告，数字不得凭空出现）。"""
    accessions = {draft.accession_no for draft in outcome.drafts}
    accessions.update(record.accession_no for record in outcome.pending)
    effective = outcome.effective_result_count
    return {
        "files_scanned": outcome.files_scanned,
        "result_files": outcome.result_files,
        "order_files": outcome.order_files,
        "parsed_results": outcome.parsed_results,
        "parsed_orders": outcome.parsed_orders,
        "reports": len(accessions),
        "accepted": outcome.accepted,
        "duplicates": outcome.duplicates,
        "superseded": outcome.superseded,
        "corrections_applied": outcome.corrections_applied,
        "inverted_corrections": outcome.inverted_corrections,
        "withheld": outcome.withheld,
        "malformed": outcome.malformed,
        "derived_queries": outcome.derived_queries,
        "recall_keys": outcome.recall_key_count,
        "drafts": len(outcome.drafts),
        "draft_entries": outcome.draft_entry_count,
        "placeholder_count": outcome.placeholder_count,
        "pending_count": len(outcome.pending),
        "effective_results": effective,
        "match_levels": dict(outcome.match_levels),
        "dataset_seed": manifest.get("seed"),
        "dataset_sha256": manifest.get("dataset_sha256"),
    }


def _environment(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """测量环境（写进报告，数字不得凭空出现）。"""
    return {
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "未知",
        "cpu_count": os.cpu_count(),
        "dataset_generator": manifest.get("generator"),
        "dataset_seed": manifest.get("seed"),
        "dataset_sha256": manifest.get("dataset_sha256"),
    }


def measure_scale(
    inbox: Path | str,
    *,
    work_dir: Path | str = "data/scale",
    state_db: Path | str | None = None,
    thresholds: ScaleThresholds | None = None,
    lexicon_path: Path | str | None = None,
) -> ScaleMeasurement:
    """把 ``inbox`` 里的全部报文跑过产品真路径，量出四项指标并按阈值判定。

    ``work_dir`` 是本轮的产物目录（草稿、待处理清单、运行摘要）；
    ``state_db`` 是状态库路径（不给则放在 ``work_dir/state.db``）。

    跑的是**产品本身**，不是另写一套简化判定 —— 基准集那套（见
    :mod:`patho_supplement.align.benchmark`）量的是「对不对」，这里量的是
    「扛不扛得住、涨不涨得凶」。
    """
    inbox_path = Path(inbox).expanduser().resolve()
    if not inbox_path.is_dir():
        raise FileNotFoundError(f"规模集目录不存在：{inbox_path}")

    work_path = Path(work_dir).expanduser().resolve()
    work_path.mkdir(parents=True, exist_ok=True)
    db_path = (
        Path(state_db).expanduser().resolve()
        if state_db
        else work_path / "state.db"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次量都从空库开始：否则量到的是「上一轮留下的库」，不是这一轮的。
    for stale in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            stale.unlink()
        except OSError:
            pass

    limits = thresholds or ScaleThresholds()
    entry_peak = _peak_rss_mb()

    database = Database(db_path)
    database.init_schema()
    started = time.perf_counter()
    try:
        outcome = run_replay(
            inbox_path,
            output_dir=work_path,
            database=database,
            lexicon_path=lexicon_path,
        )
    finally:
        elapsed = time.perf_counter() - started
        database.close()

    process_peak = _peak_rss_mb()
    db_bytes = _state_db_size(db_path)

    effective = outcome.effective_result_count
    alignment_rate = (outcome.draft_entry_count / effective) if effective else 0.0
    pending_rate = (len(outcome.pending) / effective) if effective else 0.0
    peak_growth = max(0.0, process_peak - entry_peak)
    db_mb = db_bytes / (1024.0 * 1024.0)

    manifest = _read_manifest(inbox_path)
    measurement = ScaleMeasurement(
        inbox=str(inbox_path),
        sample=_sample_of(outcome, manifest),
        elapsed_seconds=elapsed,
        alignment_rate=alignment_rate,
        pending_rate=pending_rate,
        peak_memory_mb=peak_growth,
        process_peak_rss_mb=process_peak,
        entry_peak_rss_mb=entry_peak,
        state_db_bytes=db_bytes,
        state_db_mb=db_mb,
        environment=_environment(manifest),
        thresholds=limits.as_dict(),
    )
    measurement.checks = _build_checks(measurement, limits)
    return measurement


def _build_checks(
    measurement: ScaleMeasurement, limits: ScaleThresholds
) -> list[ScaleCheck]:
    """逐项阈值判定。超阈即不合格，detail 里点名超了多少。"""
    return [
        ScaleCheck(
            name="端到端耗时",
            passed=measurement.elapsed_seconds <= limits.max_elapsed_seconds,
            actual=measurement.elapsed_seconds,
            limit=limits.max_elapsed_seconds,
            detail=(
                f"{measurement.elapsed_seconds:.2f} 秒（上限 "
                f"{limits.max_elapsed_seconds:.0f} 秒）"
            ),
        ),
        ScaleCheck(
            name="单轮对齐率",
            passed=measurement.alignment_rate >= limits.min_alignment_rate,
            actual=measurement.alignment_rate,
            limit=limits.min_alignment_rate,
            detail=(
                f"{measurement.alignment_rate:.1%}（下限 "
                f"{limits.min_alignment_rate:.0%}）"
            ),
        ),
        ScaleCheck(
            name="挂起率",
            passed=measurement.pending_rate <= limits.max_pending_rate,
            actual=measurement.pending_rate,
            limit=limits.max_pending_rate,
            detail=(
                f"{measurement.pending_rate:.1%}（上限 "
                f"{limits.max_pending_rate:.0%}；与对齐率互补，"
                f"分母同为生效结果数）"
            ),
        ),
        ScaleCheck(
            name="峰值内存增量",
            passed=measurement.peak_memory_mb <= limits.max_peak_memory_mb,
            actual=measurement.peak_memory_mb,
            limit=limits.max_peak_memory_mb,
            detail=(
                f"{measurement.peak_memory_mb:.1f} MB（上限 "
                f"{limits.max_peak_memory_mb:.0f} MB；进程峰值 RSS "
                f"{measurement.process_peak_rss_mb:.1f} MB）"
            ),
        ),
        ScaleCheck(
            name="状态库体积",
            passed=measurement.state_db_mb <= limits.max_state_db_mb,
            actual=measurement.state_db_mb,
            limit=limits.max_state_db_mb,
            detail=(
                f"{measurement.state_db_mb:.2f} MB（上限 "
                f"{limits.max_state_db_mb:.0f} MB）"
            ),
        ),
    ]
