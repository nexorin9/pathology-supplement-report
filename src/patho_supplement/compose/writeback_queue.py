"""回写队列：退避重试 + 超次升级告警。

病理系统不可达时，已经成好的草稿**不能丢** —— 那是医师等着签发的东西。
本模块把这类草稿放进 SQLite 的 ``writeback_queue`` 表：草稿状态不推进，等重试。

队列项三态：

======================  ===================================================
状态                    含义
======================  ===================================================
``pending``             等待重试（到 ``next_attempt_at`` 才取）
``escalated``           重试用尽 / 版本冲突，**升级告警转人工**（行留在库里）
``written``             回写成功（留一行历史，复盘「这份草稿第几次写回去的」）
======================  ===================================================

退避口径：第 n 次写回失败后等 ``base × 2^(n-1)`` 秒（封顶 ``max_delay``），
重试次数上限来自配置面 ``WRITEBACK_MAX_RETRIES``（默认 5 次）。

四条硬规矩：

1. **草稿状态不推进**：回写没成功，草稿就停在「已生成」，本地绝不假装已回写。
2. **超次升级不丢弃**：升级之后这一行**留在队列里**，payload 原样保留，等人处置
   —— 队列行没了，就等于把成好的草稿弄丢了。
3. **失败原因逐次留痕**：每次失败更新 ``last_error`` 与 ``attempts``；升级告警的
   明细带着最后一次错误原文与已试次数。
4. **重试内容以库里那份为准**：重试时条目从 ``draft_entries`` 重新读 —— 免得
   重试发出去的草稿跟库里存的不是同一份。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from ..audit.ledger import (
    CATEGORY_WRITEBACK,
    EVENT_WRITEBACK_ESCALATED,
    EVENT_WRITEBACK_RETRY_FAILED,
    EVENT_WRITEBACK_RETRY_SUCCEEDED,
    record_audit,
)
from ..db import Database
from .conflict import CONFLICT_SOURCE_RETRY, record_writeback_conflict
from .draft_store import draft_entry_from_row, load_draft_entries, mark_draft_written_back
from .pathology_client import PathologyClientError, PathologyConflictError
from .state_machine import StateTransitionError

__all__ = [
    "QUEUE_STATE_PENDING",
    "QUEUE_STATE_ESCALATED",
    "QUEUE_STATE_WRITTEN",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_MAX_DELAY_SECONDS",
    "ESCALATION_RETRY_EXHAUSTED",
    "ESCALATION_CONFLICT",
    "ESCALATION_STATE_CONFLICT",
    "EscalationNotice",
    "WritebackItem",
    "WritebackRetryRound",
    "WritebackRetryWorker",
    "backoff_seconds",
    "count_escalated_writebacks",
    "count_pending_writebacks",
    "count_open_writebacks",
    "due_writebacks",
    "enqueue_pending_writeback",
    "list_escalated_writebacks",
    "list_open_writebacks",
    "list_pending_writebacks",
    "load_writeback_item",
    "record_writeback_failure",
    "record_writeback_success",
]

#: 队列项状态：等待重试。
QUEUE_STATE_PENDING = "pending"
#: 队列项状态：重试用尽或版本冲突，升级告警转人工（行保留在库里）。
QUEUE_STATE_ESCALATED = "escalated"
#: 队列项状态：回写成功。
QUEUE_STATE_WRITTEN = "written"

#: 默认重试上限（配置面 ``WRITEBACK_MAX_RETRIES`` 可覆盖）。
DEFAULT_MAX_RETRIES = 5
#: 默认退避基数（秒）。
DEFAULT_BASE_DELAY_SECONDS = 5.0
#: 默认退避封顶（秒）。
DEFAULT_MAX_DELAY_SECONDS = 900.0

#: 升级原因：重试次数用尽。
ESCALATION_RETRY_EXHAUSTED = "retry_exhausted"
#: 升级原因：版本冲突 —— 重试也没用，得人判。
ESCALATION_CONFLICT = "version_conflict"
#: 升级原因：本地草稿状态推不动（已签发等），得人判。
ESCALATION_STATE_CONFLICT = "state_conflict"

#: 退避指数的上限：防止 ``2 ** n`` 溢出成天文数字。
_MAX_BACKOFF_EXPONENT = 32


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class WritebackItem:
    """队列里的一项（一份待回写的草稿）。"""

    id: int
    accession_no: str
    draft_id: int
    payload: Mapping[str, Any]
    attempts: int
    next_attempt_at: str
    state: str
    last_error: str | None
    created_at: str
    updated_at: str
    escalated_at: str | None = None
    escalation_reason: str | None = None

    @property
    def draft_version(self) -> int | None:
        """这份草稿的版本号（从入队时存的 payload 里取）。"""
        raw = self.payload.get("version")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def open(self) -> bool:
        """还没写回成功（待重试，或已升级待人工）。"""
        return self.state in (QUEUE_STATE_PENDING, QUEUE_STATE_ESCALATED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "accession_no": self.accession_no,
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "state": self.state,
            "last_error": self.last_error,
            "escalated_at": self.escalated_at,
            "escalation_reason": self.escalation_reason,
        }


@dataclass(frozen=True)
class EscalationNotice:
    """升级告警：这份草稿写不回去了，转人工处置。

    告警内容就是通知当班医师的那条消息 —— 病理号、草稿版本、已试几次、
    最后一次错在哪、下一步该谁做什么，一样不缺。
    """

    accession_no: str
    draft_id: int
    draft_version: int | None
    attempts: int
    max_retries: int
    last_error: str
    reason: str
    raised_at: str
    action: str = "草稿已保留在本地状态库，请人工确认后重放，或改由人工签发"

    @property
    def level(self) -> str:
        """告警级别：升级告警一律是 critical（已经等人了）。"""
        return "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "last_error": self.last_error,
            "reason": self.reason,
            "raised_at": self.raised_at,
            "action": self.action,
            "level": self.level,
        }

    def summary(self) -> str:
        """一行可读文本（写进运行日志 / 通知正文）。"""
        version = f"v{self.draft_version}" if self.draft_version else "（版本号未知）"
        return (
            f"病理号 {self.accession_no} 的补充报告草稿 {version} 回写失败 "
            f"{self.attempts}/{self.max_retries} 次：{self.last_error}；{self.action}"
        )


@dataclass
class WritebackRetryRound:
    """一轮重试的结果。"""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    escalated: list[EscalationNotice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "escalated": len(self.escalated),
            "errors": list(self.errors),
        }


# ------------------------------------------------------------------ 时间与退避


def _fmt(moment: datetime) -> str:
    """时间戳统一成 ``YYYY-MM-DD HH:MM:SS``（与库里其他时间戳同一种格式，
    字符串比较即时间比较）。"""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _moment(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now()


def backoff_seconds(
    attempt: int,
    *,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
) -> float:
    """第 ``attempt`` 次失败之后的等待秒数（指数退避，封顶）。

    第 1 次失败等 ``base``、第 2 次等 ``2×base``、第 3 次等 ``4×base``……
    封顶 ``max_delay``：院内机不能因为退避把重试拖到第二天才发生。
    """
    exponent = max(0, min(int(attempt), _MAX_BACKOFF_EXPONENT + 1) - 1)
    delay = float(base_delay) * (2**exponent)
    return min(delay, float(max_delay))


# ------------------------------------------------------------------ 入队与查询


def enqueue_pending_writeback(
    database: Database,
    *,
    accession_no: str,
    draft_id: int,
    payload: Mapping[str, Any],
    error: str,
    delay_seconds: float = 0.0,
    now: datetime | None = None,
) -> int:
    """把回写失败的草稿放进队列，返回队列行 ID。

    入队即持久化：守护进程重启后队列还在，不会丢已经成好的草稿。
    ``delay_seconds`` 是**第一次重试**要等的时间（刚失败就立刻再打一次接口，
    对正在恢复中的病理系统是压力）。
    """
    moment = _moment(now)
    stamp = _fmt(moment)
    next_at = _fmt(moment + timedelta(seconds=max(0.0, float(delay_seconds))))
    cursor = database.execute(
        "INSERT INTO writeback_queue"
        "(accession_no, draft_id, payload_json, attempts, next_attempt_at, state,"
        " last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            accession_no,
            draft_id,
            json.dumps(dict(payload), ensure_ascii=False),
            0,
            next_at,
            QUEUE_STATE_PENDING,
            error,
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid)


def _item_from_row(row: Any) -> WritebackItem:
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    keys = set(row.keys())
    return WritebackItem(
        id=int(row["id"]),
        accession_no=str(row["accession_no"]),
        draft_id=int(row["draft_id"]),
        payload=payload if isinstance(payload, dict) else {},
        attempts=int(row["attempts"] or 0),
        next_attempt_at=str(row["next_attempt_at"] or ""),
        state=str(row["state"] or QUEUE_STATE_PENDING),
        last_error=row["last_error"],
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        escalated_at=(row["escalated_at"] if "escalated_at" in keys else None),
        escalation_reason=(
            row["escalation_reason"] if "escalation_reason" in keys else None
        ),
    )


def load_writeback_item(database: Database, item_id: int) -> WritebackItem | None:
    """按行 ID 取队列项；查不到给 ``None``。"""
    row = database.query_one("SELECT * FROM writeback_queue WHERE id = ?", (item_id,))
    return _item_from_row(row) if row is not None else None


def due_writebacks(
    database: Database,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[WritebackItem]:
    """到期该重试的队列项（到 ``next_attempt_at`` 的，按到期时间排）。

    没到期的**不取** —— 这是退避的意义：接口不可达时别把病理系统打爆。
    """
    stamp = _fmt(_moment(now))
    rows = database.query_all(
        "SELECT * FROM writeback_queue WHERE state = ? AND next_attempt_at <= ?"
        " ORDER BY next_attempt_at, id",
        (QUEUE_STATE_PENDING, stamp),
    )
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return [_item_from_row(row) for row in rows]


def list_pending_writebacks(database: Database) -> list[Mapping[str, Any]]:
    """列出队列里的全部待回写草稿（按入队顺序）。"""
    rows = database.query_all(
        "SELECT id, accession_no, draft_id, attempts, next_attempt_at, state,"
        " last_error, created_at FROM writeback_queue WHERE state = ? ORDER BY id",
        (QUEUE_STATE_PENDING,),
    )
    return [dict(row) for row in rows]


def list_escalated_writebacks(database: Database) -> list[Mapping[str, Any]]:
    """已升级告警、等人处置的队列项。"""
    rows = database.query_all(
        "SELECT id, accession_no, draft_id, attempts, next_attempt_at, state,"
        " last_error, created_at, escalated_at, escalation_reason"
        " FROM writeback_queue WHERE state = ? ORDER BY id",
        (QUEUE_STATE_ESCALATED,),
    )
    return [dict(row) for row in rows]


def list_open_writebacks(database: Database) -> list[Mapping[str, Any]]:
    """还没写回成功的全部队列项（待重试 + 已升级）。"""
    rows = database.query_all(
        "SELECT id, accession_no, draft_id, attempts, next_attempt_at, state,"
        " last_error, created_at FROM writeback_queue WHERE state IN (?, ?)"
        " ORDER BY id",
        (QUEUE_STATE_PENDING, QUEUE_STATE_ESCALATED),
    )
    return [dict(row) for row in rows]


def count_pending_writebacks(database: Database) -> int:
    """队列里待重试的条数。"""
    return int(
        database.query_value(
            "SELECT COUNT(*) FROM writeback_queue WHERE state = ?",
            (QUEUE_STATE_PENDING,),
            default=0,
        )
        or 0
    )


def count_escalated_writebacks(database: Database) -> int:
    """已升级告警、等人处置的条数。"""
    return int(
        database.query_value(
            "SELECT COUNT(*) FROM writeback_queue WHERE state = ?",
            (QUEUE_STATE_ESCALATED,),
            default=0,
        )
        or 0
    )


def count_open_writebacks(database: Database) -> int:
    """还没写回成功的条数（待重试 + 已升级）。"""
    return int(
        database.query_value(
            "SELECT COUNT(*) FROM writeback_queue WHERE state IN (?, ?)",
            (QUEUE_STATE_PENDING, QUEUE_STATE_ESCALATED),
            default=0,
        )
        or 0
    )


# ------------------------------------------------------------------ 成功与失败


def record_writeback_success(
    database: Database,
    item_id: int,
    *,
    receipt: Mapping[str, Any] | None = None,
    attempts: int | None = None,
    now: datetime | None = None,
) -> None:
    """记一次成功：队列项转 ``written``，回执留档。

    行**不删** —— 复盘「这份草稿是第几次才写回去的」要查得到。
    ``attempts`` 记的是**这一路总共写了几次**（含前面失败的那几次）。
    """
    stamp = _fmt(_moment(now))
    receipt_json = (
        json.dumps(dict(receipt), ensure_ascii=False) if receipt is not None else None
    )
    if attempts is None:
        row = database.query_one(
            "SELECT attempts FROM writeback_queue WHERE id = ?", (item_id,)
        )
        attempts = int(row["attempts"] or 0) if row is not None else 0
    database.execute(
        "UPDATE writeback_queue SET state = ?, attempts = ?, last_attempt_at = ?,"
        " next_attempt_at = ?, last_error = NULL, receipt_json = ?, updated_at = ?"
        " WHERE id = ?",
        (QUEUE_STATE_WRITTEN, int(attempts), stamp, stamp, receipt_json, stamp, item_id),
    )


def record_writeback_failure(
    database: Database,
    item_id: int,
    *,
    error: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    now: datetime | None = None,
) -> EscalationNotice | None:
    """记一次失败。

    - 还没用完重试次数 → 按指数退避推 ``next_attempt_at``，留在队列里等下轮；
    - 次数用尽 → 队列项转 ``escalated``（**行保留**），返回升级告警内容。

    返回 ``None`` 表示只是普通失败、还在队列里等重试。
    """
    moment = _moment(now)
    stamp = _fmt(moment)
    row = database.query_one("SELECT * FROM writeback_queue WHERE id = ?", (item_id,))
    if row is None:
        return None
    item = _item_from_row(row)
    attempts = item.attempts + 1

    if attempts >= int(max_retries):
        notice = _escalate(
            database,
            item,
            attempts=attempts,
            error=error,
            reason=ESCALATION_RETRY_EXHAUSTED,
            max_retries=int(max_retries),
            moment=moment,
        )
        return notice

    delay = backoff_seconds(attempts, base_delay=base_delay, max_delay=max_delay)
    next_at = _fmt(moment + timedelta(seconds=delay))
    database.execute(
        "UPDATE writeback_queue SET state = ?, attempts = ?, last_attempt_at = ?,"
        " next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (QUEUE_STATE_PENDING, attempts, stamp, next_at, error, stamp, item_id),
    )
    return None


def _escalate(
    database: Database,
    item: WritebackItem,
    *,
    attempts: int,
    error: str,
    reason: str,
    max_retries: int,
    moment: datetime,
) -> EscalationNotice:
    """把队列项转成「升级告警、等人处置」，并落一行审计流水。

    **草稿状态不动**（还停在「已生成」），**队列行不删** —— 这一行就是「这份
    草稿还没写回去」的凭证。
    """
    stamp = _fmt(moment)
    database.execute(
        "UPDATE writeback_queue SET state = ?, attempts = ?, last_attempt_at = ?,"
        " last_error = ?, escalated_at = ?, escalation_reason = ?, updated_at = ?"
        " WHERE id = ?",
        (
            QUEUE_STATE_ESCALATED,
            attempts,
            stamp,
            error,
            stamp,
            reason,
            stamp,
            item.id,
        ),
    )
    notice = EscalationNotice(
        accession_no=item.accession_no,
        draft_id=item.draft_id,
        draft_version=item.draft_version,
        attempts=attempts,
        max_retries=max_retries,
        last_error=error,
        reason=reason,
        raised_at=stamp,
    )
    record_audit(
        database,
        category=CATEGORY_WRITEBACK,
        event=EVENT_WRITEBACK_ESCALATED,
        accession_no=item.accession_no,
        detail=notice.to_dict(),
    )
    return notice


# ------------------------------------------------------------------ 重试执行


class WritebackRetryWorker:
    """回写队列的执行者：一轮一轮把到期的草稿写回病理系统。

    依赖都是注入进来的（状态库 + 出站客户端），测试可以喂本地协议替身，
    生产走的是同一段代码。
    """

    def __init__(
        self,
        database: Database,
        client: Any,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        clock: Callable[[], datetime] | None = None,
        echo: Callable[[str], None] | None = None,
        on_escalate: Callable[[EscalationNotice], None] | None = None,
    ) -> None:
        self._database = database
        self._client = client
        self._max_retries = max(0, int(max_retries))
        self._base_delay = float(base_delay)
        self._max_delay = float(max_delay)
        self._clock = clock or datetime.now
        self._echo = echo
        self._on_escalate = on_escalate

    # ------------------------------------------------------------ 属性

    @property
    def max_retries(self) -> int:
        return self._max_retries

    # ------------------------------------------------------------ 主循环

    def run_once(self, *, limit: int | None = None) -> WritebackRetryRound:
        """重试一轮：取到期项 → 逐个写回 → 记成功 / 失败 / 升级。"""
        moment = self._clock()
        outcome = WritebackRetryRound()

        if self._max_retries <= 0:
            # 不允许重试：到期项直接升级告警，不再往接口上打。
            for item in due_writebacks(self._database, now=moment, limit=limit):
                outcome.attempted += 1
                notice = _escalate(
                    self._database,
                    item,
                    attempts=item.attempts + 1,
                    error="回写重试上限为 0，不再重试",
                    reason=ESCALATION_RETRY_EXHAUSTED,
                    max_retries=self._max_retries,
                    moment=moment,
                )
                self._deliver_escalation(notice, outcome)
            return outcome

        for item in due_writebacks(self._database, now=moment, limit=limit):
            outcome.attempted += 1
            try:
                receipt, state_error = self._write(item)
            except PathologyConflictError as exc:
                # 版本冲突：重试也没用（对面版本已经变了），不重试，转人工。
                # 冲突同时登记一条 —— 待人工处理清单上要能看见「哪一版写不回去」。
                outcome.failed += 1
                outcome.errors.append(f"{item.accession_no} 回写冲突：{exc.detail}")
                entry_count = self._database.query_value(
                    "SELECT entry_count FROM drafts WHERE id = ?",
                    (item.draft_id,),
                    default=None,
                )
                record_writeback_conflict(
                    self._database,
                    accession_no=item.accession_no,
                    draft_id=item.draft_id,
                    draft_version=item.draft_version,
                    base_version=item.payload.get("base_version"),
                    entry_count=(int(entry_count) if entry_count is not None else None),
                    reason=exc.detail,
                    detail=exc.to_dict(),
                    source=CONFLICT_SOURCE_RETRY,
                    now=moment,
                )
                notice = _escalate(
                    self._database,
                    item,
                    attempts=item.attempts + 1,
                    error=exc.detail,
                    reason=ESCALATION_CONFLICT,
                    max_retries=self._max_retries,
                    moment=moment,
                )
                self._deliver_escalation(notice, outcome)
                continue
            except PathologyClientError as exc:
                outcome.failed += 1
                outcome.errors.append(f"{item.accession_no} 回写失败：{exc.detail}")
                notice = record_writeback_failure(
                    self._database,
                    item.id,
                    error=exc.detail,
                    max_retries=self._max_retries,
                    base_delay=self._base_delay,
                    max_delay=self._max_delay,
                    now=moment,
                )
                record_audit(
                    self._database,
                    category=CATEGORY_WRITEBACK,
                    event=EVENT_WRITEBACK_RETRY_FAILED,
                    accession_no=item.accession_no,
                    detail={
                        "queue_id": item.id,
                        "draft_id": item.draft_id,
                        "attempts": item.attempts + 1,
                        "max_retries": self._max_retries,
                        "error": exc.detail,
                        "escalated": notice is not None,
                    },
                )
                if notice is not None:
                    self._deliver_escalation(notice, outcome)
                continue

            if state_error is not None:
                # 写回去了，但本地草稿状态推不动（已签发等）：状态机自己会写
                # 一行拒绝审计，这里只把话说清楚，不当成回写失败。
                outcome.errors.append(f"{item.accession_no} 草稿状态未推进：{state_error}")
            record_writeback_success(
                self._database,
                item.id,
                receipt=receipt.to_dict() if receipt is not None else None,
                attempts=item.attempts + 1,
                now=moment,
            )
            record_audit(
                self._database,
                category=CATEGORY_WRITEBACK,
                event=EVENT_WRITEBACK_RETRY_SUCCEEDED,
                accession_no=item.accession_no,
                detail={
                    "queue_id": item.id,
                    "draft_id": item.draft_id,
                    "attempts": item.attempts + 1,
                    "receipt": receipt.to_dict() if receipt is not None else None,
                },
            )
            outcome.succeeded += 1
        return outcome

    # ------------------------------------------------------------ 内部

    def _write(self, item: WritebackItem) -> tuple[Any, str | None]:
        """把队列里的草稿写回病理系统。

        条目从 ``draft_entries`` 重新读 —— 重试发出去的必须是库里存的那一份，
        不能拿内存里的旧对象。

        返回 ``(回执, 状态未推进的原因)``：写成功但本地状态推不动时，
        第二段给出原因（回写本身算成功）。
        """
        entries = [
            draft_entry_from_row(row)
            for row in load_draft_entries(self._database, item.draft_id)
        ]
        receipt = self._client.post_supplemental_draft(
            item.accession_no,
            body=str(item.payload.get("body") or ""),
            base_version=item.payload.get("base_version"),
            entries=entries,
        )
        state_error: str | None = None
        try:
            mark_draft_written_back(self._database, item.draft_id, receipt)
        except StateTransitionError as exc:
            state_error = str(exc)
        return receipt, state_error

    def _deliver_escalation(
        self, notice: EscalationNotice, outcome: WritebackRetryRound
    ) -> None:
        """升级告警的三处去处：运行摘要、审计流水（已落）、通知当班医师。

        通知通道由外部注入（``on_escalate``）—— SMTP / 企业微信那套通道落地
        后接在这里；没接的时候，告警**照样**进审计流水与每轮摘要，不会丢。
        """
        outcome.escalated.append(notice)
        if self._echo is not None:
            self._echo(f"[{notice.raised_at}] 升级告警｜{notice.summary()}")
        if self._on_escalate is not None:
            try:
                self._on_escalate(notice)
            except Exception:  # pragma: no cover - 通知失败不吞掉升级事实
                record_audit(
                    self._database,
                    category=CATEGORY_WRITEBACK,
                    event=EVENT_WRITEBACK_ESCALATED,
                    accession_no=notice.accession_no,
                    detail={
                        **notice.to_dict(),
                        "notify_error": "通知当班医师失败，告警仍以审计流水为准",
                    },
                )
