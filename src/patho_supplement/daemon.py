"""守护主循环（最小版）。

这是本产品的**常驻形态**：一个进程盯着结果投递目录，回报一落地就处理，处理完
把补充报告草稿回写到病理系统的草稿区，并把对不上的结果单独挂起等人补齐。

一轮的完整路径::

    目录监听（落稳判定）
      → 解析 ORU^R01（拒收畸形报文，不静默塞进草稿）
      → 按病理号分组
      → GET 病理系统取基线（真实出站调用，凭据取自环境变量）
      → 部位-蜡块对齐
      → 成稿（按取材部位分组，逐项标来源与回报时间）
      → POST 回写草稿区（真实出站调用）
      → 落 SQLite（草稿、条目、回执、挂起项、审计）
      → 打印回执与本轮摘要

两条硬规矩：

1. **没处理完就不归档。** 报文文件只有在这一份报告真正落定（草稿回写成功，或
   结果已记入挂起清单）之后才移进 ``processed/``。取基线失败时文件留在原地，
   下一轮重来 —— 绝不因为一次网络抖动把结果弄丢。
2. **状态只落 SQLite。** 每轮结束释放内存里的中间对象，进程常驻也不涨内存。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .align.filtering import FilterCandidate
from .align.matcher import (
    MATCH_LEVEL_LABELS,
    MatchCandidate,
    MatchThresholds,
    align_entries,
    summarize_match_levels,
)
from .align.merger import business_key_of, merge_fanout
from .align.order_guard import OrderStateGuard
from .align.query_derivation import PendingQueryStore, derive_pending_queries
from .align.rationale import (
    build_alignment,
    build_pending_alignment,
    record_alignment_audit,
)
from .align.retriever import fanout_params_from_config, fanout_retrieve
from .audit.ledger import (
    CATEGORY_INGEST,
    CATEGORY_NOTIFY,
    CATEGORY_RUNTIME,
    CATEGORY_WRITEBACK,
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
    EVENT_DAEMON_RECOVERED,
    EVENT_DEFERRED,
    EVENT_DUPLICATE,
    EVENT_MALFORMED,
    EVENT_NOTIFY_ROUTED,
    EVENT_REJECTED,
    EVENT_WRITEBACK_ACCEPTED,
    EVENT_WRITEBACK_QUEUED,
    record_audit,
    record_encoding_audit,
)
from .compose.draft_renderer import derive_awaiting, render_draft
from .compose.draft_store import (
    mark_draft_written_back,
    next_draft_version,
    persist_draft,
)
from .compose.conflict import (
    CONFLICT_SOURCE_DIRECT,
    close_conflicts_for_accession,
    list_open_conflicts,
    record_writeback_conflict,
)
from .compose.pathology_client import (
    PathologyClient,
    PathologyClientError,
    PathologyConflictError,
)
from .compose.writeback_queue import (
    EscalationNotice,
    WritebackRetryWorker,
    enqueue_pending_writeback,
)
from .config import Config, ConfigError
from .db import Database
from .ingest.dirty import decode_payload
from .ingest.er7 import Er7ParseError, parse_message
from .ingest.idempotency import build_idempotency_key, resolve_corrections
from .ingest.inbox_watcher import InboxWatcher
from .ingest.orm_parser import OrderIngestor
from .ingest.oru_parser import parse_oru
from .logging_setup import StructuredLogger, build_round_summary
from .models import Draft, OrderRequest, ResultEntry
from .notify.dispatcher import DispatchResult, NotifyDispatcher
from .notify.messages import build_draft_notice, build_escalation_notice
from .notify.router import DutyRouter, RouteResult
from .pending.late_arrival import (
    LateArrivalOutcome,
    handle_late_arrival,
    resolve_after_merge,
)
from .pending.reasons import (
    PendingReason,
    classify_reason,
    missing_fields_for_entry,
    missing_fields_for_reason,
    missing_fields_for_state,
    reason_for_entry,
)
from .pending.records import (
    PendingRecord,
    render_pending_text,
)
from .pending.store import PENDING_STATES, PendingStore
from .pipeline import (
    NODE_ALIGN,
    NODE_COMPOSE,
    NODE_DERIVE_ORDERS,
    NODE_FETCH_BASELINE,
    NODE_FANOUT_RETRIEVE,
    NODE_INGEST,
    NODE_NOTIFY,
    NODE_WRITEBACK,
    Pipeline,
    PipelineState,
)
from .recovery import RecoveryReport, recover_state
from .timefmt import now_text

__all__ = [
    "Daemon",
    "DaemonRound",
    "SingleInstanceError",
    "SingleInstanceLock",
    "REJECTED_DIR_NAME",
]

#: 被拒收报文的归档目录名。
REJECTED_DIR_NAME = "rejected"

#: 单实例锁文件默认名。
LOCK_FILE_NAME = "patho_supplement.lock"


class SingleInstanceError(RuntimeError):
    """已有另一个守护进程在跑。"""


class SingleInstanceLock:
    """单实例文件锁。

    院内机资源紧张，同一个目录跑两个守护进程会互相抢文件、抢回写。因此第二个
    实例启动即退出，并把占锁的进程号报出来。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._fd: int | None = None

    def acquire(self) -> bool:
        """抢锁；抢不到返回 ``False``。"""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        except ImportError:  # pragma: no cover - 非 POSIX 平台
            os.close(fd)
            return False
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd
        return True

    def holder_pid(self) -> str:
        """占锁的进程号（读不出来就说明锁文件还没写内容）。"""
        try:
            return self.path.read_text(encoding="ascii").strip() or "未知"
        except OSError:
            return "未知"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except (OSError, ImportError):  # pragma: no cover - 释放失败不应影响退出
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise SingleInstanceError(
                f"已有一个守护进程在运行（锁文件 {self.path}，进程号 {self.holder_pid()}）"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass
class DaemonRound:
    """一轮守护的产出与计数。"""

    started_at: str = ""
    elapsed_seconds: float = 0.0
    files_seen: int = 0
    files_archived: int = 0
    parsed_results: int = 0
    parsed_orders: int = 0
    order_malformed: int = 0
    malformed: int = 0
    accepted: int = 0
    duplicates: int = 0
    superseded: int = 0
    corrections_applied: int = 0
    inverted_corrections: int = 0
    withheld: int = 0
    drafts_written: int = 0
    drafts_failed: int = 0
    conflicts: int = 0
    #: 本轮因后续版本写回成功而了结的版本冲突条数。
    conflicts_closed: int = 0
    #: 本轮回写队列的重试情况（到期的项试了几次、成功几份、失败几份）。
    writeback_retried: int = 0
    writeback_retry_succeeded: int = 0
    writeback_retry_failed: int = 0
    #: 本轮升级告警（重试用尽 / 版本冲突，转人工处置）的条数。
    writeback_escalated: int = 0
    pending_count: int = 0
    #: 本轮因超过挂起尝试上限而转人工处置的条数。
    pending_escalated: int = 0
    #: 本轮自动接回挂起项的**迟到结果**条数（重新入队，仍等人）。
    late_requeued: int = 0
    #: 本轮自动接回后对上了的迟到结果条数（挂起行了结，结果并入草稿）。
    late_resolved: int = 0
    derived_queries: int = 0
    awaiting_queries: int = 0
    cancelled_queries: int = 0
    held_queries: int = 0
    terminated_queries: int = 0
    deferred: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    order_rejected: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 本轮读到的报文文件按解码方式计数（utf-8 / gbk / mixed）。
    decode_counts: dict[str, int] = field(default_factory=dict)
    #: 四级判定分布（L1 蜡块号精确 / L2 部位描述精确 / L3 词法模糊 / L4 挂起）。
    match_levels: dict[str, int] = field(default_factory=dict)
    #: 本轮写入审计流水里的对齐依据条数（已并入草稿 / 挂起）。
    rationale_count: int = 0
    pending_rationale_count: int = 0
    #: 草稿里的「待回报」占位条数（已申请但这一版还没结果）。
    placeholder_count: int = 0
    #: 本轮发了多少条通知（成稿待签发 + 升级告警）。
    notify_sent: int = 0
    #: 本轮有几条通知没发出去。
    notify_failed: int = 0
    #: 本轮通知是否发生降级（少了一条通道，或某条通道发不出去）。
    notify_degraded: bool = False
    #: 通知通道是否一个都没配（本轮没发通知）。
    notify_skipped: bool = False
    #: 本轮通知的可读说明（降级原因、失败原因）。
    notify_notes: list[str] = field(default_factory=list)
    #: 本轮的收件人路由结果（排班命中 / 降级到科室默认收件人）。
    notify_route: dict[str, Any] = field(default_factory=dict)
    #: 本轮按路由结果收件的通知条数。
    notify_routed: int = 0
    #: 本轮多路召回的路况（各路成败、合并去重条数）。
    fanout: dict[str, Any] = field(default_factory=dict)
    #: 本轮节点表的跳过流水（哪个节点因为缺什么 / 没有触发源而没往下走）。
    pipeline_skips: list[dict[str, Any]] = field(default_factory=list)
    #: 本轮各节点的推进次数（节点名 → 次数）。同一事件只推进一次。
    pipeline_visits: dict[str, int] = field(default_factory=dict)
    #: 本轮推进过的节点（按推进顺序，含重复 —— 一份报告走一遍链）。
    pipeline_executed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "files_seen": self.files_seen,
            "files_archived": self.files_archived,
            "parsed_results": self.parsed_results,
            "parsed_orders": self.parsed_orders,
            "order_malformed": self.order_malformed,
            "malformed": self.malformed,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "superseded": self.superseded,
            "corrections_applied": self.corrections_applied,
            "inverted_corrections": self.inverted_corrections,
            "withheld": self.withheld,
            "drafts_written": self.drafts_written,
            "drafts_failed": self.drafts_failed,
            "conflicts": self.conflicts,
            "conflicts_closed": self.conflicts_closed,
            "writeback_retried": self.writeback_retried,
            "writeback_retry_succeeded": self.writeback_retry_succeeded,
            "writeback_retry_failed": self.writeback_retry_failed,
            "writeback_escalated": self.writeback_escalated,
            "pending_count": self.pending_count,
            "pending_escalated": self.pending_escalated,
            "late_requeued": self.late_requeued,
            "late_resolved": self.late_resolved,
            "derived_queries": self.derived_queries,
            "awaiting_queries": self.awaiting_queries,
            "cancelled_queries": self.cancelled_queries,
            "held_queries": self.held_queries,
            "terminated_queries": self.terminated_queries,
            "deferred": list(self.deferred),
            "rejected": [{"file": name, "reason": reason} for name, reason in self.rejected],
            "order_rejected": [
                {"file": name, "reason": reason} for name, reason in self.order_rejected
            ],
            "decode_counts": dict(self.decode_counts),
            "match_levels": dict(self.match_levels),
            "rationale_count": self.rationale_count,
            "pending_rationale_count": self.pending_rationale_count,
            "placeholder_count": self.placeholder_count,
            "notify_sent": self.notify_sent,
            "notify_failed": self.notify_failed,
            "notify_degraded": self.notify_degraded,
            "notify_skipped": self.notify_skipped,
            "notify_notes": list(self.notify_notes),
            "notify_route": dict(self.notify_route),
            "notify_routed": self.notify_routed,
            "fanout": dict(self.fanout),
            "pipeline_skips": list(self.pipeline_skips),
            "pipeline_visits": dict(self.pipeline_visits),
            "pipeline_executed": list(self.pipeline_executed),
            "errors": list(self.errors),
        }


class Daemon:
    """守护进程。

    依赖都是注入进来的：客户端（出站）、状态库、目录监听器。这样测试可以喂
    本地协议替身，而生产走的是同一段代码。
    """

    def __init__(
        self,
        config: Config,
        *,
        client: PathologyClient | None = None,
        database: Database | None = None,
        watcher: InboxWatcher | None = None,
        output_dir: Path | str | None = None,
        echo: Callable[[str], None] = print,
        escalation_notifier: Callable[[EscalationNotice], None] | None = None,
        notifier: NotifyDispatcher | None = None,
        router: DutyRouter | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._config = config
        self._echo = echo
        # 结构化行日志：运维看日志就知道这一轮处理了多少、对齐了多少、挂了多少、
        # 写回成功几份。结果值默认不入日志（见 logging_setup 模块开头的规矩）。
        self._logger = logger or StructuredLogger(
            logging.getLogger("patho_supplement.daemon"),
            include_result_values=config.log_result_values,
        )
        # 归属绑定的判定阈值走配置面（BM25 阈值 / 领先幅度阈值）。
        self._thresholds = MatchThresholds.from_config(config)
        self._database = database if database is not None else Database(
            config.pathology_state_db or "data/patho_state.db"
        )
        self._database.init_schema()
        self._client = client if client is not None else PathologyClient.from_config(config)
        # 申请侧去重账本：进程内跨批次有效（重复投递要挡住）。
        # 跨重启的持久化去重靠待查项表（pending_queries）的唯一标识。
        self._order_ingestor = OrderIngestor(self._database)
        self._query_store = PendingQueryStore(self._database)
        # 挂起项快照的读写门面：落库与恢复的口径只有这一处。
        self._pending_store = PendingStore(self._database)
        # 通知调度器（SMTP / 企业微信）：成稿之后通知当班的人。
        # **通知失败不牵连成稿** —— 调度器自己吞掉异常并留审计。
        self._notifier = (
            notifier
            if notifier is not None
            else NotifyDispatcher.from_config(config, database=self._database, echo=echo)
        )
        # 外部注入的升级告警回调（没有也不影响：告警照样进审计与摘要）。
        self._escalation_notifier = escalation_notifier
        # 收件人路由：按排班表找当班医师；排班缺失时降级到科室默认收件人，
        # 降级在运行摘要里看得见。
        self._router = router if router is not None else DutyRouter.from_config(config)
        # 回写队列的执行者：上一轮没写回去的草稿，这一轮按退避接着写。
        # 升级告警除了进审计流水与每轮摘要，还会走通知通道通知当班医师。
        self._writeback_worker = WritebackRetryWorker(
            self._database,
            self._client,
            max_retries=config.writeback_max_retries,
            base_delay=config.writeback_retry_base_seconds,
            max_delay=config.writeback_retry_max_seconds,
            echo=echo,
            on_escalate=self._handle_escalation,
        )
        # 本轮读到的申请侧事件，按病理号归拢 —— 派生待查项时与基线合并。
        self._round_orders: dict[str, list[OrderRequest]] = {}
        # 本轮的产出对象：升级告警回调发生在轮次内部，靠它把通知结果并进摘要。
        self._round_outcome: DaemonRound | None = None
        # 启动恢复的结果（进程重启后接回来的东西）：留一份给运维查。
        self._recovery: RecoveryReport | None = None
        # 节点表：一条报告的处理链（入站解析 → 取基线 → 派生待查项 → 多路召回
        # → 部位-蜡块对齐 → 成稿 → 回写 → 通知）。两道闸门在
        # :mod:`patho_supplement.pipeline` 里，实现各节点的执行体在下面。
        self._pipeline = Pipeline()
        if watcher is not None:
            self._watcher = watcher
        else:
            if config.pathology_result_inbox is None:
                raise ConfigError(
                    "缺少环境变量 PATHOLOGY_RESULT_INBOX（结果投递目录）"
                )
            self._watcher = InboxWatcher(
                config.pathology_result_inbox,
                poll_interval=config.watch_poll_interval,
                settle_seconds=config.watch_settle_seconds,
            )
        self._output_dir = Path(output_dir or "data/daemon")
        self._closed = False

    # ------------------------------------------------------------ 属性

    @property
    def watcher(self) -> InboxWatcher:
        return self._watcher

    @property
    def database(self) -> Database:
        return self._database

    @property
    def client(self) -> PathologyClient:
        return self._client

    @property
    def recovery(self) -> RecoveryReport | None:
        """本次进程的启动恢复结果（还没恢复过则给 ``None``）。"""
        return self._recovery

    # ------------------------------------------------------------ 主循环

    def run_once(self, *, force_scan: bool = False) -> DaemonRound:
        """跑一轮：监听 → 解析 → 取基线 → 对齐 → 成稿 → 回写 → 落库。

        轮次开头先接住**上一轮没写回去的草稿**：回写队列里到期的项按指数退避
        重试，重试用尽就升级告警转人工 —— 成好的草稿不会因为病理系统一时
        不可达就丢掉。
        """
        outcome = DaemonRound(started_at=now_text())
        started = time.monotonic()
        self._round_orders = {}
        self._round_outcome = outcome
        self._logger.event("round_start", started_at=outcome.started_at)

        retry = self._writeback_worker.run_once()
        outcome.writeback_retried = retry.attempted
        outcome.writeback_retry_succeeded = retry.succeeded
        outcome.writeback_retry_failed = retry.failed
        outcome.writeback_escalated = len(retry.escalated)

        ready = self._watcher.scan(force=force_scan)
        outcome.files_seen = len(ready)

        file_accessions: dict[Path, set[str]] = {}
        # 报文里「还没落定」的病理号：这些结果这一轮没能进草稿也没进挂起，
        # 报文要留在原地，下一轮重来。
        unsettled: dict[Path, set[str]] = {}
        accession_entries: dict[str, list[ResultEntry]] = {}
        resolved: set[str] = set()
        seen_keys: set[str] = set()

        for path in ready:
            entries, disposition = self._read_file(path, outcome)
            if disposition == "deferred":
                self._watcher.mark_deferred(path)
                continue
            if disposition == "rejected":
                self._quarantine(path)
                continue

            if not entries:
                # 报文里没有结果条目（空回报）：归档，不占着目录。
                self._watcher.mark_processed(path)
                outcome.files_archived += 1
                continue

            for entry in entries:
                # 报文涉及哪些病理号（无论这条结果此前是否已落定）——
                # 归档看的是「这份报文里的结果是不是都有归宿」。
                file_accessions.setdefault(path, set()).add(entry.accession_no)
                key = build_idempotency_key(entry)
                if key in seen_keys:
                    outcome.duplicates += 1
                    self._audit_duplicate(entry, "本批重复投递")
                    continue
                seen_keys.add(key)
                outcome.parsed_results += 1
                if self._already_recorded(entry):
                    # 上一轮已经落定过这条结果 —— 不再重复进草稿。
                    # 这一条已有归宿，不算「还没落定」，不该挡住归档。
                    outcome.duplicates += 1
                    self._audit_duplicate(entry, "此前已落定")
                    continue
                outcome.accepted += 1
                unsettled.setdefault(path, set()).add(entry.accession_no)
                accession_entries.setdefault(entry.accession_no, []).append(entry)
                file_accessions.setdefault(path, set()).add(entry.accession_no)

        for accession_no in sorted(accession_entries):
            if self._process_accession(accession_no, accession_entries[accession_no], outcome):
                resolved.add(accession_no)

        # 归档：一份报文里的结果**全部有归宿**后才移走 —— 进了草稿、进了挂起
        # 清单、进了回写队列，或此前就已落定过。仍有没落定结果的报文留在原地，
        # 下一轮重来（绝不因为一次网络抖动把结果弄丢）。
        for path in file_accessions:
            if unsettled.get(path, set()) - resolved:
                continue
            self._watcher.mark_processed(path)
            outcome.files_archived += 1

        self._write_pending_list()
        outcome.elapsed_seconds = time.monotonic() - started
        # 每轮一行摘要：处理 / 对齐 / 挂起 / 写回成功 / 写回失败 / 耗时。
        # 机器读的走日志，人读的走上面的中文摘要行 —— 两份口径同源。
        summary = build_round_summary(outcome)
        self._write_round_summary(summary, outcome.started_at)
        self._logger.round_summary(summary)
        self._echo(self.render_round(outcome))
        # 本轮结束就撒手：中间对象（这一轮的申请条目、这一轮的产出）不再留在
        # 守护进程里。**状态只落 SQLite** —— 常驻几天的进程，内存不能跟着
        # 处理的份数一起涨。
        self._round_orders = {}
        self._round_outcome = None
        return outcome

    # ------------------------------------------------------------ 中断恢复

    def recover(self) -> RecoveryReport:
        """进程重启后把上一轮留下的东西接回来。

        接回四样：挂起项、幂等键（已落定的结果）、回写队列、结果目录里还没
        归档的报文。这一步**只读不写业务状态** —— 挂起项不会自动并进草稿，
        回写也不会在这里发出去（回写由每轮开头的队列重试按退避接手）。

        唯一的例外是**释放过期的挂起行锁**：崩掉的进程可能正拿着某一行挂起项
        的互斥锁，锁过了有效期就该被接管，重启后主动放掉，这一行立刻能补齐。
        """
        report = recover_state(
            self._database,
            watcher=self._watcher,
            max_attempts=self._config.pending_max_attempts,
            lock_ttl=self._config.pending_lock_ttl,
        )
        self._recovery = report
        self._audit(
            CATEGORY_RUNTIME,
            EVENT_DAEMON_RECOVERED,
            None,
            report.as_dict(),
        )
        self._logger.event(
            "daemon_recovered",
            pending_items=report.pending_items,
            writeback_open=report.open_writebacks,
            idempotency_keys=report.idempotency_keys,
            leftover_files=report.leftover_files,
            stale_locks_released=report.stale_locks_released,
        )
        self._echo(f"[{now_text()}] 启动恢复｜{report.summary_line()}")
        if report.broken_rows:
            self._echo(
                f"[{now_text()}] 告警｜状态库有 {len(report.broken_rows)} 行挂起项"
                "读不出来，已跳过（行号 "
                + "、".join(str(row_id) for row_id in report.broken_rows)
                + "）"
            )
        return report

    def run_forever(
        self,
        *,
        max_cycles: int | None = None,
        fallback_interval: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
        keep_rounds: int = 1,
    ) -> list[DaemonRound]:
        """常驻主循环。

        ``max_cycles`` 给了就跑这么多轮（测试与演练用）；否则一直跑到收到停止
        请求。``fallback_interval`` 是**定时兜底扫描**的间隔：目录长时间没动静
        时，按周期强制扫一遍，接住守护进程停机期间投进来的文件；不给就取配置面
        的 ``WATCH_FALLBACK_INTERVAL``。

        ``keep_rounds`` 是**保留几轮的产出**（默认只留最近一轮）。守护进程要
        连着跑上几天，每轮的产出对象（结果条目、审计明细、拒收清单）加起来
        不小 —— 全攒在内存里就是慢性泄漏。这里明确只留最近几轮，其余的
        落进 SQLite 与日志，内存不随运行时长涨。
        """
        interval = (
            float(fallback_interval)
            if fallback_interval is not None
            else float(self._config.watch_fallback_interval)
        )
        keep = max(1, int(keep_rounds))
        rounds: list[DaemonRound] = []
        cycle = 0
        last_fallback = time.monotonic()
        while True:
            if stop_requested is not None and stop_requested():
                break
            now = time.monotonic()
            force = (now - last_fallback) >= interval
            rounds.append(self.run_once(force_scan=force))
            if force:
                last_fallback = now
            # 只保留最近几轮：常驻进程不攒历史产出对象。
            if len(rounds) > keep:
                del rounds[: len(rounds) - keep]
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            time.sleep(self._watcher.poll_interval)
        return rounds

    # ------------------------------------------------------------ 单份报告

    def _process_accession(
        self,
        accession_no: str,
        entries: Sequence[ResultEntry],
        outcome: DaemonRound,
    ) -> bool:
        """处理一份病理报告（走节点表）。

        返回 ``True`` 表示这份报告已经**落定**（草稿回写成功，或结果全部记入
        挂起清单）；``False`` 表示没落定，文件留在原地，下一轮重来。

        推进顺序由节点表决定（见 :mod:`patho_supplement.pipeline`）：入站解析
        → 取基线 → 派生待查项 → 多路召回 → 部位-蜡块对齐 → 成稿 → 回写 →
        通知。两道闸门在图里：**基线没取回就不许往下对齐**，**没有结果到达就
        不许往下对齐**。停在哪、缺什么，都记进本轮摘要与日志。
        """
        state = PipelineState(event_id=f"accession:{accession_no}")
        state.set("raw_entries", tuple(entries))
        state.set("order_requests", tuple(self._round_orders.get(accession_no, [])))

        run = self._pipeline.run(state, handlers=self._node_handlers(outcome))

        # 节点推进计数：这一轮走过哪几个节点、各走了几次，运维在摘要里能查。
        outcome.pipeline_executed.extend(name for name, _ in run.executed)
        for node_name, count in state.visits.items():
            outcome.pipeline_visits[node_name] = (
                outcome.pipeline_visits.get(node_name, 0) + count
            )

        # 跳过流水：停在哪、缺什么，运维看得见 —— 默默停住和不停住一样危险。
        for record in run.skipped:
            self._logger.event(
                "pipeline_skipped",
                level=logging.WARNING,
                node=record.node,
                reason=record.reason,
                detail=record.detail,
            )
        if run.skipped:
            outcome.pipeline_skips.extend(record.to_dict() for record in run.skipped)
        for message in run.errors:
            outcome.errors.append(message)

        # 基线没取回 → 这份报告没落定：文件留在原地，下一轮重来，结果不丢。
        if state.notes.get("baseline_error"):
            return False
        return True

    # ------------------------------------------------------------ 节点执行体

    def _node_handlers(self, outcome: DaemonRound) -> dict:
        """本轮的节点执行体（每个节点一个）。

        闭包里带着 ``outcome`` —— 节点产出与本轮计数是同一份口径。
        """
        return {
            NODE_INGEST: lambda state: self._node_ingest(state, outcome),
            NODE_FETCH_BASELINE: lambda state: self._node_fetch_baseline(state, outcome),
            NODE_DERIVE_ORDERS: lambda state: self._node_derive_orders(state, outcome),
            NODE_FANOUT_RETRIEVE: lambda state: self._node_fanout(state, outcome),
            NODE_ALIGN: lambda state: self._node_align(state, outcome),
            NODE_COMPOSE: lambda state: self._node_compose(state, outcome),
            NODE_WRITEBACK: lambda state: self._node_writeback(state, outcome),
            NODE_NOTIFY: lambda state: self._node_notify(state, outcome),
        }

    def _node_ingest(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """入站解析：更正取代、回报时间倒挂、无法获得与空值扣下等人。"""
        raw: tuple[ResultEntry, ...] = state.get("raw_entries") or ()
        effective = resolve_corrections(raw, database=self._database)
        outcome.superseded += effective.superseded
        outcome.corrections_applied += len(effective.applied)
        outcome.inverted_corrections += len(effective.inverted)
        outcome.withheld += len(effective.withheld)
        return {
            "accession_no": raw[0].accession_no if raw else "",
            "result_entries": tuple(effective.kept),
            "withheld_entries": tuple(effective.withheld),
            "inverted_entries": tuple(
                inversion.entry for inversion in effective.inverted
            ),
            # 这一轮**到了**的结果全量（能并入的 + 扣下等人的）：对齐节点按它
            # 判断「有没有结果到达」—— 一条都没有就不往下走。
            "all_entries": (
                *tuple(effective.kept),
                *tuple(effective.withheld),
                *tuple(inversion.entry for inversion in effective.inverted),
            ),
            "order_requests": tuple(state.get("order_requests") or ()),
        }

    def _node_fetch_baseline(
        self, state: PipelineState, outcome: DaemonRound
    ) -> dict:
        """取报告基线（真实出站调用）。

        取不到就**不产出 baseline** —— 下游节点的必需输入因此到不齐，被闸门一
        挡住：宁可这一轮不成稿，也不拿没有基线的报告去对齐。
        """
        accession_no = state.get("accession_no") or ""
        try:
            baseline = self._client.fetch_baseline(accession_no)
        except PathologyClientError as exc:
            # 取不到基线就不成稿：文件留在原地，下一轮重来，结果不丢。
            outcome.errors.append(f"{accession_no} 取基线失败：{exc.detail}")
            outcome.drafts_failed += 1
            self._audit("baseline", "fetch_failed", accession_no, exc.to_dict())
            self._logger.event(
                "baseline_failed",
                level=logging.WARNING,
                accession_no=accession_no,
                error_code=exc.to_dict().get("error_code", "pathology_error"),
                detail=exc.detail,
            )
            self._echo(f"[{now_text()}] 取基线失败｜病理号 {accession_no}｜{exc.detail}")
            state.notes["baseline_error"] = exc.detail
            return {}
        return {"baseline": baseline}

    def _node_derive_orders(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """派生待查项：基线已申请项 + 申请侧事件合并，剔除已取消/暂停/终止项。"""
        baseline = state.get("baseline")
        orders = state.get("order_requests") or ()
        # 落库后同一项重复投递只更新原行；基线重新派生也不会复活已取消的项。
        query_set = derive_pending_queries(baseline, list(orders))
        self._query_store.upsert(query_set.items)
        outcome.derived_queries += query_set.derived_count
        outcome.awaiting_queries += len(query_set.awaiting)
        outcome.cancelled_queries += len(query_set.cancelled)
        outcome.held_queries += len(query_set.held)
        outcome.terminated_queries += len(query_set.terminated)
        # 申请侧状态闸门：已取消 / 已终止的待查项，其结果不并入草稿。
        guard = OrderStateGuard.from_queries(query_set.items)
        return {
            "pending_queries": tuple(query_set.items),
            "recall_keys": tuple(query_set.recall_keys),
            "order_guard": guard,
        }

    def _node_fanout(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """多路召回：多把检索键并发捞候选，每路失败独立隔离。

        **召回层不决定归属** —— 归属由对齐节点的四级判定说了算。召回层管的是
        「哪几把钥匙捞到了什么」与「哪几把钥匙坏了」：坏了的钥匙要让运维看得见，
        但**不能因此把一份已经到手的报告卡住** —— 所以召回失败不拦住对齐，
        只在本轮摘要里标出来。
        """
        keys = state.get("recall_keys") or ()
        entries = state.get("result_entries") or ()
        candidates = tuple(FilterCandidate.from_entry(entry) for entry in entries)
        workers, route_timeout = fanout_params_from_config(self._config)
        result = fanout_retrieve(
            keys,
            candidates,
            policy=self._config.filter_policy,
            max_workers=workers,
            route_timeout=route_timeout,
        )
        merged = merge_fanout(result.routes)

        # 召回索引：业务键 → 捞到它的那几把钥匙（供对齐节点查「这把结果是被
        # 哪几把钥匙捞出来的」）。
        recall_index: dict[str, tuple[str, ...]] = {}
        for item in merged.items:
            key = business_key_of(item.candidate)
            if key is None:
                continue
            recall_index[key] = recall_index.get(key, ()) + (item.recall_key,)

        stats = {
            "routes_total": len(result.routes),
            "routes_ok": result.ok_routes,
            "routes_failed": result.failed_routes,
            "routes_timed_out": result.timed_out_routes,
            "merged": merged.merged_count,
            "kept": merged.count,
            "duplicates": merged.duplicates,
            "unkeyed": merged.unkeyed,
            "max_workers": workers,
            "route_timeout": route_timeout,
            "policy": self._config.filter_policy,
            "failures": [route.to_dict() for route in result.failures],
        }
        outcome.fanout = stats
        self._logger.event(
            "fanout_done",
            routes_total=stats["routes_total"],
            routes_ok=stats["routes_ok"],
            routes_failed=stats["routes_failed"],
            routes_timed_out=stats["routes_timed_out"],
            kept=stats["kept"],
        )
        return {
            "retrieved_candidates": tuple(merged.items),
            "recall_index": recall_index,
            "fanout_stats": stats,
        }

    def _node_align(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """部位-蜡块对齐：命中 → 往后走；未命中 / 歧义 → 挂起落库。

        **未命中出口接的是挂起落库**（快照持久化，见
        :mod:`patho_supplement.pending.store`）—— 对不上的结果一条都不丢，
        落进待人工处理清单等人补齐后从挂起点续跑。
        """
        accession_no = state.get("accession_no") or ""
        entries: tuple[ResultEntry, ...] = state.get("result_entries") or ()
        baseline = state.get("baseline")
        guard = state.get("order_guard")
        withheld: tuple[ResultEntry, ...] = state.get("withheld_entries") or ()
        inverted = state.get("inverted_entries") or ()

        # 迟到结果自动续挂：状态库里若已有同一条结果的挂起行，自动接回去 ——
        # 对上了就并入本轮草稿（挂起行随后了结），还是对不上就把那一行重拍
        # （尝试次数 +1、快照时间前移），人不用再翻清单。
        late_outcomes: list[LateArrivalOutcome] = []
        late_matched: list[tuple[ResultEntry, MatchCandidate]] = []
        normal_entries: list[ResultEntry] = []
        for entry in entries:
            late = handle_late_arrival(
                self._database,
                entry=entry,
                baseline=baseline,
                thresholds=self._thresholds,
                guard=guard,
                max_attempts=self._config.pending_max_attempts,
            )
            if late.merged and late.candidate is not None:
                late_matched.append((entry, late.candidate))
                late_outcomes.append(late)
                outcome.late_resolved += 1
            elif late.still_pending:
                late_outcomes.append(late)
                outcome.late_requeued += 1
            else:
                normal_entries.append(entry)

        matched, unmatched = align_entries(
            normal_entries,
            {accession_no: baseline},
            thresholds=self._thresholds,
            guard=guard,
        )
        # 迟到结果接回来的那些排在前面：本轮草稿按部位分组渲染，顺序只影响
        # 同一部位内条目的先后，与归属判定无关。
        matched = late_matched + matched
        # 四级判定分布按轮累计：L3（靠文字相似猜上的）置信低，值班的人要重点复核。
        for level_name, level_count in summarize_match_levels(matched, unmatched).items():
            outcome.match_levels[level_name] = (
                outcome.match_levels.get(level_name, 0) + level_count
            )
        # 对齐依据：一条结果一条依据，写进审计流水。
        #   - 已并入草稿的：含命中方式 / 得分 / 召回键 / 来源系统 / 回报时间 / 置信等级；
        #   - 挂起的：同样留一条，但**没有置信等级**（不敢下结论 ≠ 可信度低）。
        for entry, candidate in matched:
            record_alignment_audit(
                self._database,
                build_alignment(entry, candidate),
                event=EVENT_ALIGNMENT_RATIONALE,
            )
            outcome.rationale_count += 1
        for entry, item in unmatched:
            record_alignment_audit(
                self._database,
                build_pending_alignment(entry, item),
                event=EVENT_ALIGNMENT_PENDING,
            )
            outcome.pending_rationale_count += 1
        pending: list[PendingRecord] = []
        for entry, item in unmatched:
            reason = classify_reason(entry, item)
            pending.append(
                PendingRecord.from_entry(
                    entry,
                    reason,
                    missing_fields=(
                        item.missing_fields
                        or missing_fields_for_reason(reason)
                        or missing_fields_for_state(item.state)
                    ),
                    candidates=item.candidates,
                )
            )
        # 无法获得 / 空值：已回报但写不进报告 —— 进待人工处理清单，不进草稿。
        pending.extend(
            PendingRecord.from_entry(
                entry,
                reason_for_entry(entry) or PendingReason.RESULT_NOT_OBTAINED,
                missing_fields=missing_fields_for_entry(entry),
            )
            for entry in withheld
        )
        # 回报时间倒挂：更正没生效，较新的原值留着，更正等人确认。
        pending.extend(
            PendingRecord.from_entry(
                entry,
                PendingReason.TIME_INVERSION,
                missing_fields=("observed_at",),
            )
            for entry in inverted
        )
        if pending:
            escalated = self._persist_pending(pending)
            outcome.pending_count += len(pending)
            outcome.pending_escalated += len(escalated)
            if escalated:
                self._logger.event(
                    "pending_escalated",
                    level=logging.WARNING,
                    accession_no=accession_no,
                    escalated=len(escalated),
                    max_attempts=self._config.pending_max_attempts,
                )
                self._echo(
                    f"[{now_text()}] 挂起转人工｜病理号 {accession_no}｜"
                    f"{len(escalated)} 条已超挂起尝试上限"
                )
        # 迟到结果接回来但仍对不上的：那一行已经重拍过（在库里），不再新建行，
        # 但本轮仍算「待人工处理」—— 清单上它还在。
        outcome.pending_count += outcome.late_requeued

        if not matched:
            # 全都挂起：没有草稿可成，但结果已经记进挂起清单，不丢。
            # 成稿节点因「没有命中」被闸门一挡住，天然不会往下走。
            self._record_keys(state.get("raw_entries") or ())
            self._logger.event(
                "accession_pending",
                accession_no=accession_no,
                pending=len(pending),
                matched=0,
            )
            self._echo(
                f"[{now_text()}] 挂起｜病理号 {accession_no}｜{len(pending)} 条待人工处理"
            )

        return {
            "matched": tuple(matched),
            "unmatched": tuple(unmatched),
            "pending_records": tuple(pending),
            "late_outcomes": tuple(late_outcomes),
        }

    def _node_compose(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """成稿：按取材部位分组渲染，逐项标明来源系统与回报时间。"""
        accession_no = state.get("accession_no") or ""
        matched = state.get("matched") or ()
        baseline = state.get("baseline")
        queries = state.get("pending_queries") or ()
        if not matched:
            # 没有可并入的结果 → 本节点不产出草稿，下游自然停住。
            return {}

        version = self._next_version(accession_no, baseline.base_version if baseline else None)
        # 未到项占位：已申请但这一版**一次都没回来**的检查项，以「待回报」
        # 出现在草稿里 —— 少写一项，医师就会以为这项没做。
        # 「回来过但没进报告」的（无法获得 / 空值 / 对不上）不算未回报，
        # 它们在待人工处理清单里。
        kept = state.get("result_entries") or ()
        withheld = state.get("withheld_entries") or ()
        inverted = state.get("inverted_entries") or ()
        reported = [*kept, *withheld, *inverted]
        awaiting = derive_awaiting(
            baseline,
            queries,
            reported=reported,
        )
        draft = render_draft(
            accession_no,
            matched,
            baseline=baseline,
            awaiting=awaiting,
            version=version,
        )
        outcome.placeholder_count += draft.placeholder_count
        draft_id = self._persist_draft(draft)

        # 迟到结果已并入本轮草稿 → 对应的挂起行了结（草稿先落库，了结再记，
        # 顺序反过来的话会出现「行已了结但草稿没落库」的中间态）。
        for late in state.get("late_outcomes") or ():
            if late.merged:
                resolve_after_merge(self._database, outcome=late)

        return {"draft": draft, "draft_id": draft_id}

    def _node_writeback(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """回写草稿区（真实出站调用）。

        回写成功才产出回执 —— 通知节点以回执为必需输入，所以**回写没成功就不
        会去喊人来签发**（喊了也签不了）。
        """
        accession_no = state.get("accession_no") or ""
        draft = state.get("draft")
        draft_id = state.get("draft_id")
        version = draft.version
        try:
            receipt = self._client.post_supplemental_draft(
                accession_no,
                body=draft.body,
                base_version=draft.base_version,
                entries=draft.entries,
            )
        except PathologyConflictError as exc:
            # 版本冲突：**拒绝写回**（不覆盖对方版本）、**保留本地草稿**、
            # **转人工处置**。三条硬规矩：
            #   1. 不猜一个新版本号硬塞过去 —— 猜错就是把别人的修改抹掉；
            #   2. 本地草稿原样留着（状态停在「已生成」），正文另落一份文本；
            #   3. 不进回写队列的常规退避路径 —— 对面版本不会因为重试而变回
            #      本地基础版本，反复重试只会把版本链越堆越乱。
            outcome.conflicts += 1
            outcome.errors.append(f"{accession_no} 回写冲突：{exc.detail}")
            conflict_id = record_writeback_conflict(
                self._database,
                accession_no=accession_no,
                draft_id=draft_id,
                draft_version=version,
                base_version=draft.base_version,
                entry_count=draft.merged_count,
                reason=exc.detail,
                detail=exc.to_dict(),
                source=CONFLICT_SOURCE_DIRECT,
            )
            # 本地草稿留底：正文落盘，库里那一版原样保留（状态仍是「已生成」）。
            self._write_draft_file(draft)
            # 结果已有归宿（本地草稿 + 冲突待处置），记幂等键 —— 报文随即归档，
            # 下一轮不会拿同一批结果再撞一次冲突、也不会把版本链越堆越乱。
            self._record_keys(state.get("raw_entries") or ())
            self._logger.event(
                "writeback_conflict",
                level=logging.WARNING,
                accession_no=accession_no,
                draft_version=version,
                base_version=draft.base_version,
                conflict_id=conflict_id,
                detail=exc.detail,
            )
            self._echo(
                f"[{now_text()}] 冲突｜病理号 {accession_no}｜"
                f"本地草稿 v{version} 已保留（条目 {draft.merged_count} 条）｜"
                f"已转人工处置（冲突 #{conflict_id}）：{exc.detail}"
            )
            return {}
        except PathologyClientError as exc:
            outcome.drafts_failed += 1
            outcome.errors.append(f"{accession_no} 回写失败：{exc.detail}")
            queue_id = enqueue_pending_writeback(
                self._database,
                accession_no=accession_no,
                draft_id=draft_id,
                payload={
                    "accession_no": accession_no,
                    "base_version": draft.base_version,
                    "body": draft.body,
                    "version": version,
                },
                error=exc.detail,
                delay_seconds=self._config.writeback_retry_base_seconds,
            )
            self._audit(
                CATEGORY_WRITEBACK,
                EVENT_WRITEBACK_QUEUED,
                accession_no,
                {
                    **exc.to_dict(),
                    "queue_id": queue_id,
                    "draft_id": draft_id,
                    "draft_version": version,
                    "retry_base_seconds": self._config.writeback_retry_base_seconds,
                    "max_retries": self._config.writeback_max_retries,
                },
            )
            self._echo(
                f"[{now_text()}] 回写失败｜病理号 {accession_no}｜"
                f"草稿已入回写队列 #{queue_id}，等待重试"
            )
            self._logger.event(
                "writeback_failed",
                level=logging.WARNING,
                accession_no=accession_no,
                draft_version=version,
                queue_id=queue_id,
                error_code=exc.to_dict().get("error_code", "pathology_error"),
                detail=exc.detail,
            )
            # 结果已经落在库里（草稿 + 条目 + 队列行），报文可以归档 ——
            # 下一轮由回写队列接着写回去，不会再起草一版。草稿状态停在
            # 「已生成」，回写成功前绝不推进。
            self._record_keys(state.get("raw_entries") or ())
            return {}

        self._mark_written_back(draft_id, receipt)
        self._record_keys(state.get("raw_entries") or ())
        outcome.drafts_written += 1
        self._write_draft_file(draft)
        # 这一版写回去了，先前的版本冲突随之了结（冲突行不删，转为「已了结」
        # 并留一行审计：复盘时答得出「当时冲突过、后来是哪一版写回去的」）。
        closed_conflicts = close_conflicts_for_accession(
            self._database,
            accession_no,
            resolution=f"已由 v{version} 回写成功，版本冲突随之了结",
        )
        if closed_conflicts:
            outcome.conflicts_closed += closed_conflicts
        self._audit("writeback", EVENT_WRITEBACK_ACCEPTED, accession_no, receipt.to_dict())
        self._logger.event(
            "draft_written",
            accession_no=accession_no,
            draft_version=version,
            accepted_version=receipt.version,
            entries=draft.merged_count,
            pending=len(state.get("pending_records") or ()),
        )
        self._echo(
            f"[{now_text()}] 回写成功｜病理号 {accession_no}｜草稿 v{version}"
            f"｜接受版本 {receipt.version}｜条目 {draft.merged_count} 条"
        )
        return {"writeback_receipt": receipt}

    def _node_notify(self, state: PipelineState, outcome: DaemonRound) -> dict:
        """通知当班的人：这份补充报告草稿已回写，等待签发。

        通知发不出去**不影响这一轮**：草稿已经写回病理系统了，通知只是多喊
        一嗓子。失败只落审计与本轮摘要（降级 / 失败都看得见）。
        """
        accession_no = state.get("accession_no") or ""
        draft = state.get("draft")
        receipt = state.get("writeback_receipt")
        pending_count = len(state.get("pending_records") or ())
        result = self._notify_draft_ready(
            accession_no,
            draft.version,
            draft.merged_count,
            outcome,
            accepted_version=receipt.version,
            pending_count=pending_count,
        )
        return {"notify_result": result}

    # ------------------------------------------------------------ 读文件

    def _read_file(
        self, path: Path, outcome: DaemonRound
    ) -> tuple[list[ResultEntry], str]:
        """读一个报文文件，返回 ``(结果条目, 处置方式)``。

        处置方式：``result``（带回结果）/ ``deferred``（申请侧，暂不处理）/
        ``rejected``（畸形或未知类型，拒收）。
        """
        try:
            raw = path.read_bytes()
            # 逐行 UTF-8 → GBK 回退：同一批文件里两种编码都有是常事。
            payload = decode_payload(raw)
            outcome.decode_counts[payload.encoding] = (
                outcome.decode_counts.get(payload.encoding, 0) + 1
            )
            record_encoding_audit(
                self._database,
                file_name=path.name,
                encoding=payload.encoding,
                fallback=payload.fallback,
                byte_length=payload.byte_length,
            )
            message = parse_message(
                payload.text, source=str(path), encoding=payload.encoding
            )
        except (OSError, Er7ParseError) as exc:
            outcome.malformed += 1
            outcome.rejected.append((path.name, str(exc)))
            self._audit(
                CATEGORY_INGEST,
                EVENT_MALFORMED,
                None,
                {"file": path.name, **self._error_detail(exc)},
            )
            self._logger.event(
                "message_rejected",
                level=logging.WARNING,
                file=path.name,
                reason="报文结构不可解析",
            )
            return [], "rejected"

        code = message.message_code.upper()
        structure = message.message_structure.upper()

        if code == "ORU" and structure == "R01":
            try:
                entries = parse_oru(message, source_file=path.name)
            except Er7ParseError as exc:
                outcome.malformed += 1
                outcome.rejected.append((path.name, str(exc)))
                self._audit(
                    CATEGORY_INGEST,
                    EVENT_MALFORMED,
                    None,
                    {"file": path.name, **self._error_detail(exc)},
                )
                self._logger.event(
                    "message_rejected",
                    level=logging.WARNING,
                    file=path.name,
                    reason="结果报文解析不通过",
                )
                return [], "rejected"
            return entries, "result"

        if code == "ORM":
            # 申请侧事件：解析出「哪些检查项在等结果、哪些被取消/暂停/终止」。
            # 写错的一条拒收并记审计，其余照常。解析出来的申请条目按病理号收拢，
            # 等基线取回后与基线合并成一份待查项。
            outcome.deferred.append(path.name)
            ingested = self._order_ingestor.ingest(message, source_file=path.name)
            outcome.parsed_orders += ingested.accepted
            outcome.order_malformed += ingested.malformed
            for order in ingested.orders:
                self._round_orders.setdefault(order.accession_no, []).append(order)
            for rejected in ingested.rejected:
                # 拒收原因已由 OrderIngestor 写进审计流水（ingest / order_rejected）
                outcome.order_rejected.append((path.name, rejected.detail))
            self._audit(
                CATEGORY_INGEST,
                EVENT_DEFERRED,
                None,
                {
                    "file": path.name,
                    "orders": ingested.accepted,
                    "malformed": ingested.malformed,
                },
            )
            return [], "deferred"

        reason = f"未知报文类型 MSH-9={message.message_type!r}，不属入站事件流"
        outcome.rejected.append((path.name, reason))
        self._audit(
            CATEGORY_INGEST, EVENT_REJECTED, None, {"file": path.name, "reason": reason}
        )
        self._logger.event(
            "message_rejected",
            level=logging.WARNING,
            file=path.name,
            reason="未知报文类型，不属入站事件流",
        )
        return [], "rejected"

    # ------------------------------------------------------------ 状态落库

    def _already_recorded(self, entry: ResultEntry) -> bool:
        """这条结果是否在之前的轮次里已经落定过。"""
        row = self._database.query_one(
            "SELECT 1 FROM idempotency_keys WHERE key = ?",
            (build_idempotency_key(entry),),
        )
        return row is not None

    def _audit_duplicate(self, entry: ResultEntry, reason: str) -> None:
        """重复投递单独记一行。

        「又投了一遍」有自己的计数器 —— 混进对齐依据行数会让「这一份报告
        对上了几条」失真。守护这层的重复有两个来路（本批重复 / 此前已落定），
        都记同一类事件，来路写进明细，事后能分清是哪一种。
        """
        self._audit(
            CATEGORY_INGEST,
            EVENT_DUPLICATE,
            entry.accession_no,
            {
                "item_code": entry.item_code,
                "block_no": entry.block_no,
                "observed_at": entry.observed_at,
                "result_status": entry.status.value,
                "source_file": entry.source_file,
                "reason": reason,
            },
        )

    def _record_keys(self, entries: Sequence[ResultEntry]) -> None:
        """把落定过的结果记进幂等键表（跨批次去重）。"""
        stamp = now_text()
        for entry in entries:
            self._database.execute(
                "INSERT OR IGNORE INTO idempotency_keys"
                "(key, accession_no, block_no, item_code, observed_at, result_status,"
                " source_file, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    build_idempotency_key(entry),
                    entry.accession_no,
                    entry.block_no,
                    entry.item_code,
                    entry.observed_at or "",
                    entry.status.value,
                    entry.source_file,
                    stamp,
                ),
            )

    def _next_version(self, accession_no: str, base_version: str | None = None) -> int:
        """这份报告的下一个草稿版本号（口径见 :mod:`compose.draft_store`）。

        基线给了基础版本号就**从它派生并递增**（base-v3 → 首版 v4），基线没给
        就按本地链递增（v1、v2……）。
        """
        return next_draft_version(self._database, accession_no, base_version)

    def _persist_draft(self, draft: Draft) -> int:
        """草稿落库（状态：已生成，等回写）。"""
        return persist_draft(self._database, draft)

    def _mark_written_back(self, draft_id: int, receipt) -> None:
        mark_draft_written_back(self._database, draft_id, receipt)

    def _persist_pending(self, records: Sequence[PendingRecord]) -> list[PendingRecord]:
        """挂起项落库（快照持久化）。

        快照记的是结果**进对齐之前**的样子：未对齐结果 + 缺失字段 + 候选列表
        + 尝试次数 + 快照时间 + 来源系统。全部落 SQLite，进程重启后按库恢复，
        不丢结果。

        尝试次数按「病理号 + 项目码」累计：同一条结果反复对不上时每挂起一次
        多试一次，超过配置面给的挂起尝试上限（``PENDING_MAX_ATTEMPTS``）就转
        人工处置，不再自动续跑。返回本轮转人工处置的挂起记录。
        """
        snapshots = self._pending_store.save_many(
            records, max_attempts=self._config.pending_max_attempts
        )
        escalated: list[PendingRecord] = []
        for snapshot in snapshots:
            if not snapshot.escalated:
                continue
            escalated.append(snapshot.to_record())
            self._audit(
                "pending",
                "pending_escalated",
                snapshot.accession_no,
                {
                    "item_code": snapshot.item_code,
                    "attempts": snapshot.attempts,
                    "max_attempts": self._config.pending_max_attempts,
                    "reason": snapshot.reason.value,
                    "note": snapshot.note,
                },
            )
        return escalated

    def _previous_attempts(self, accession_no: str, item_code: str) -> int:
        """同一「病理号 + 项目码」此前已挂起过几次。"""
        return self._pending_store.previous_attempts(accession_no, item_code)

    @staticmethod
    def _error_detail(exc: Exception) -> dict[str, Any]:
        """把一条入站错误整理成审计明细（带机器可读的错误码）。"""
        if isinstance(exc, Er7ParseError):
            return exc.to_dict()
        return {
            "code": "unreadable",
            "reason": "报文文件读不出来",
            "detail": str(exc),
        }

    # ------------------------------------------------------------ 通知

    def _notify_draft_ready(
        self,
        accession_no: str,
        version: int,
        entry_count: int,
        outcome: DaemonRound,
        *,
        accepted_version: str | None = None,
        pending_count: int = 0,
    ) -> DispatchResult | None:
        """通知当班的人：这份补充报告草稿已回写，等待签发。

        通知发不出去**不影响这一轮**：草稿已经写回病理系统了，通知只是多喊
        一嗓子。失败只落审计与本轮摘要（降级 / 失败都看得见）。
        """
        message = build_draft_notice(
            accession_no=accession_no,
            version=version,
            entry_count=entry_count,
            pending_count=pending_count,
            accepted_version=accepted_version,
        )
        message = self._route_message(message, outcome)
        return self._dispatch(message, outcome, reason="草稿待签发")

    def _handle_escalation(self, notice: EscalationNotice) -> None:
        """回写升级告警：通知当班医师（外加外部注入的回调）。

        告警本身已经进了审计流水与每轮摘要；通知只是多一条路。
        """
        message = build_escalation_notice(
            accession_no=notice.accession_no,
            draft_version=notice.draft_version,
            attempts=notice.attempts,
            max_retries=notice.max_retries,
            last_error=notice.last_error,
            action=notice.action,
        )
        message = self._route_message(message, self._round_outcome)
        self._dispatch(message, self._round_outcome, reason="回写升级告警")
        if self._escalation_notifier is not None:
            self._escalation_notifier(notice)

    def _route_message(self, message, outcome: DaemonRound | None):
        """按排班表取当班收件人，并把路由结果写进审计与本轮摘要。

        降级**不是静默的**：排班没命中而走了科室默认收件人时，运行摘要里
        会显式写明「降级了、为什么降级」—— 排班表过期却没人知道，比
        排班表过期本身更麻烦。
        """
        try:
            route: RouteResult = self._router.route()
        except Exception as exc:  # noqa: BLE001 - 路由异常不许拖垮成稿
            if outcome is not None:
                outcome.notify_notes.append(f"收件人路由异常：{exc}")
            return message

        if outcome is not None:
            outcome.notify_route = route.to_dict()
            outcome.notify_routed += 1
            summary_line = route.summary_line()
            if summary_line not in outcome.notify_notes:
                outcome.notify_notes.append(summary_line)

        self._audit(
            CATEGORY_NOTIFY,
            EVENT_NOTIFY_ROUTED,
            message.accession_no,
            route.to_dict(),
        )

        if not route.recipients:
            # 没有收件人就让通道按自己的默认收件人走；通道也没有 → 由通道
            # 报「没有收件人」的配置错误，不在这里猜收件人。
            return message
        return replace(message, recipients=route.recipients)

    def _dispatch(
        self,
        message,
        outcome: DaemonRound | None,
        *,
        reason: str,
    ) -> DispatchResult | None:
        """派发一条通知，并把结果并进本轮摘要。

        调度器本身不抛异常；这里再兜一层，保证「通知出问题」永远变不成
        「成稿出问题」。
        """
        try:
            result = self._notifier.dispatch(message, reason=reason)
        except Exception as exc:  # noqa: BLE001 - 通知异常不许拖垮成稿
            if outcome is not None:
                outcome.notify_failed += 1
                outcome.notify_degraded = True
                outcome.notify_notes.append(f"通知异常：{exc}")
            return None
        if outcome is not None:
            outcome.notify_sent += len(result.sent)
            outcome.notify_failed += len(result.failed)
            if result.degraded:
                outcome.notify_degraded = True
            if result.status == "skipped":
                outcome.notify_skipped = True
            for note in result.notes:
                if note not in outcome.notify_notes:
                    outcome.notify_notes.append(note)
        return result

    def _audit(
        self, category: str, event: str, accession_no: str | None, detail: Any
    ) -> None:
        record_audit(
            self._database,
            category=category,
            event=event,
            accession_no=accession_no,
            detail=detail,
        )

    # ------------------------------------------------------------ 产物

    def _write_draft_file(self, draft: Draft) -> Path:
        directory = self._output_dir / "drafts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{draft.accession_no}-v{draft.version}.txt"
        path.write_text(draft.body, encoding="utf-8")
        return path

    def _write_round_summary(self, summary, started_at: str) -> Path:
        """把本轮运行摘要落盘（``round_summary.json``）。

        摘要与日志里的那行同源：运维看日志能答的问题，看这个文件也能答 ——
        进程重启后回头查上一轮处理了多少条，答案在这里。
        """
        directory = self._output_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "round_summary.json"
        path.write_text(
            json.dumps(
                {
                    "started_at": started_at,
                    "summary_line": summary.summary_line(),
                    "summary": summary.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def _write_pending_list(self) -> Path:
        """把挂起清单导出成人类可读文本（技术员直接拿去查登记）。"""
        directory = self._output_dir
        directory.mkdir(parents=True, exist_ok=True)
        loaded = self._pending_store.load_all(
            states=PENDING_STATES,
            strict=False,
            max_attempts=self._config.pending_max_attempts,
        )
        if loaded.broken_rows:
            # 读不出来的行不静默丢弃：报出来，让人知道状态库里少了东西。
            self._echo(
                f"[{now_text()}] 告警｜状态库有 {len(loaded.broken_rows)} 行挂起项"
                "读不出来，已跳过（行号 "
                + "、".join(str(row_id) for row_id in loaded.broken_rows)
                + "）"
            )
        records = [snapshot.to_record() for snapshot in loaded.snapshots]
        # 版本冲突待处置的条目也进这张清单 —— 它们同样要人处理，
        # 技术员不该为了找一份冲突清单再翻另一个地方。
        conflicts = list_open_conflicts(self._database)
        path = directory / "pending_list.txt"
        path.write_text(
            render_pending_text(records, conflicts=conflicts), encoding="utf-8"
        )
        return path

    # ------------------------------------------------------------ 杂项

    def _quarantine(self, path: Path) -> None:
        """把坏报文移进 ``rejected/``，不丢也不混进草稿。"""
        target_dir = self._watcher.inbox / REJECTED_DIR_NAME
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        index = 1
        while target.exists():
            target = target_dir / f"{path.stem}-{index}{path.suffix}"
            index += 1
        try:
            path.replace(target)
        except OSError:
            pass

    @staticmethod
    def render_round(outcome: DaemonRound) -> str:
        """本轮摘要（运维看日志就知道这一轮干了什么）。"""
        parts = [
            f"本轮：文件 {outcome.files_seen} 个 → 入账 {outcome.accepted} 条"
            f"（重复 {outcome.duplicates}、更正取代 {outcome.superseded}）",
            f"草稿回写成功 {outcome.drafts_written} 份、失败 {outcome.drafts_failed} 份、"
            f"冲突 {outcome.conflicts} 份；挂起 {outcome.pending_count} 条",
            f"待查项 {outcome.derived_queries} 项（在等结果 {outcome.awaiting_queries}、"
            f"取消 {outcome.cancelled_queries}、暂停 {outcome.held_queries}、"
            f"终止 {outcome.terminated_queries}）",
        ]
        if outcome.deferred:
            parts.append(
                f"申请侧事件 {len(outcome.deferred)} 个（解析出申请 "
                f"{outcome.parsed_orders} 条，拒收 {outcome.order_malformed} 条）"
            )
        if outcome.match_levels:
            parts.append(
                "对齐判定 "
                + "、".join(
                    f"{MATCH_LEVEL_LABELS.get(name, name)} {count} 条"
                    for name, count in outcome.match_levels.items()
                    if count
                )
            )
        if outcome.fanout:
            fanout = outcome.fanout
            parts.append(
                f"多路召回 {fanout.get('routes_total', 0)} 路"
                f"（成功 {fanout.get('routes_ok', 0)}、"
                f"失败 {fanout.get('routes_failed', 0)}、"
                f"超时 {fanout.get('routes_timed_out', 0)}），"
                f"命中 {fanout.get('kept', 0)} 条"
            )
        if outcome.rationale_count or outcome.pending_rationale_count:
            parts.append(
                f"对齐依据 {outcome.rationale_count} 条"
                f"（挂起 {outcome.pending_rationale_count} 条）"
            )
        if outcome.corrections_applied or outcome.inverted_corrections:
            parts.append(
                f"更正生效 {outcome.corrections_applied} 条"
                f"、倒挂未生效 {outcome.inverted_corrections} 条"
            )
        if outcome.withheld:
            parts.append(f"无法写入报告 {outcome.withheld} 条（已进待人工处理清单）")
        if outcome.conflicts:
            parts.append(
                f"版本冲突 {outcome.conflicts} 份（已保留本地草稿，转人工处置，不自动重试）"
            )
        if outcome.conflicts_closed:
            parts.append(f"版本冲突了结 {outcome.conflicts_closed} 份（已由新版本写回）")
        if outcome.placeholder_count:
            parts.append(
                f"待回报占位 {outcome.placeholder_count} 项（已申请、这一版还没回来）"
            )
        if outcome.late_requeued or outcome.late_resolved:
            parts.append(
                f"迟到结果接回 {outcome.late_requeued + outcome.late_resolved} 条"
                f"（并入草稿 {outcome.late_resolved} 条、重新入队 {outcome.late_requeued} 条）"
            )
        if outcome.rejected:
            parts.append(
                f"拒收 {len(outcome.rejected)} 个（畸形 {outcome.malformed} 个）"
            )
        if outcome.pipeline_skips:
            parts.append(
                "节点停步 "
                + "、".join(
                    f"{skip.get('node', '?')}（{skip.get('reason', '?')}）"
                    for skip in outcome.pipeline_skips
                )
            )
        if outcome.writeback_retried or outcome.writeback_escalated:
            parts.append(
                f"回写队列重试 {outcome.writeback_retried} 次"
                f"（成功 {outcome.writeback_retry_succeeded}、"
                f"失败 {outcome.writeback_retry_failed}）、"
                f"升级告警 {outcome.writeback_escalated} 条"
            )
        if outcome.notify_route:
            route_summary = str(outcome.notify_route.get("summary") or "").strip()
            if route_summary:
                parts.append(route_summary)
        if outcome.notify_sent or outcome.notify_failed or outcome.notify_skipped:
            note = ""
            if outcome.notify_notes:
                note = f"（{'；'.join(outcome.notify_notes)}）"
            parts.append(
                f"通知 发出 {outcome.notify_sent} 条、失败 {outcome.notify_failed} 条"
                f"{note}"
            )
        parts.append(f"耗时 {outcome.elapsed_seconds:.2f}s")
        return "｜".join(parts)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._database.close()
