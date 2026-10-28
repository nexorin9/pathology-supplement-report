"""中断恢复：进程重启后把上一轮留下的东西接回来。

守护进程是常驻的 —— 院内机断电、进程被运维杀掉、机器重启，都会让进程在
「一半」的状态下消失。重启之后必须答得出四个问题：

1. **哪些结果对不上、等人补齐？** —— 落在 ``pending_items`` 表（快照）。
2. **哪些结果已经落定过、不该再进一次草稿？** —— 落在 ``idempotency_keys`` 表。
3. **哪些草稿成好了却还没写回病理系统？** —— 落在 ``writeback_queue`` 表。
4. **结果目录里还有没有没处理的报文？** —— 落在目录本身（**文件就是权威
   清单**，不必另建一套状态）。

这四样都是**已经写进 SQLite 或磁盘**的东西，恢复这一步只把它们读回来数一遍，
让人和运维看得见「重启后手上还剩多少活」。恢复本身**不改业务状态**：

- 挂起项不会在恢复时被自动并进草稿 —— 那是「补齐 + 续跑」那条路的事；
- 回写队列不会在恢复时被直接发出去 —— 每轮开头按退避到期才试（见
  :mod:`patho_supplement.compose.writeback_queue`）。

唯一一处状态改动是**释放过期的挂起行锁**：崩掉的进程可能正拿着某一行挂起项的
互斥锁（见 :mod:`patho_supplement.pending.lock`）。锁的有效期一过就本该被接管，
重启后主动把过期的释放掉，这一行立刻就能继续补齐 —— 不用干等一个 TTL。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from .compose.writeback_queue import (
    count_escalated_writebacks,
    count_pending_writebacks,
)
from .db import Database
from .pending.lock import PendingLock
from .pending.store import PENDING_STATES, PendingStore

__all__ = [
    "RecoveryReport",
    "expired_lock_rows",
    "recover_state",
]

#: 锁时间戳的格式（与 :func:`patho_supplement.timefmt.now_text` 一致）。
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class RecoveryReport:
    """一次恢复的结果（重启后手上还剩多少活）。

    每个字段都是**可核对的条数**，不是估计值。
    """

    #: 恢复出来的挂起项条数（等人补齐 + 已转人工处置）。
    pending_items: int = 0
    #: 其中已超尝试上限、转人工处置的条数。
    escalated_items: int = 0
    #: 读不出来的挂起行号（不丢弃，报出来 —— 状态库里少了东西要有人知道）。
    broken_rows: tuple[int, ...] = ()
    #: 已落定过的结果条数（跨批次去重账本）。
    idempotency_keys: int = 0
    #: 回写队列里还没写回去的条数（待重试）。
    writeback_pending: int = 0
    #: 回写队列里已升级告警、等人处置的条数。
    writeback_escalated: int = 0
    #: 崩掉的进程留下的过期锁，本次恢复释放了几把。
    stale_locks_released: int = 0
    #: 结果目录里还没归档的报文数（**事件没丢**的直接证据）。
    leftover_files: int = 0
    #: 状态库路径。
    state_db: str = ""

    @property
    def open_writebacks(self) -> int:
        """还没写回成功的回写条数（待重试 + 已升级）。"""
        return self.writeback_pending + self.writeback_escalated

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_items": self.pending_items,
            "escalated_items": self.escalated_items,
            "broken_rows": list(self.broken_rows),
            "idempotency_keys": self.idempotency_keys,
            "writeback_pending": self.writeback_pending,
            "writeback_escalated": self.writeback_escalated,
            "open_writebacks": self.open_writebacks,
            "stale_locks_released": self.stale_locks_released,
            "leftover_files": self.leftover_files,
            "state_db": self.state_db,
        }

    def summary_line(self) -> str:
        """一行可读摘要（运维看启动日志的第一眼）。"""
        parts = [
            f"挂起项 {self.pending_items} 条（转人工 {self.escalated_items} 条）",
            f"回写队列 待重试 {self.writeback_pending} 条、"
            f"已升级 {self.writeback_escalated} 条",
            f"已落定结果 {self.idempotency_keys} 条",
            f"结果目录待处理 {self.leftover_files} 个文件",
        ]
        if self.stale_locks_released:
            parts.append(f"释放过期锁 {self.stale_locks_released} 把")
        if self.broken_rows:
            parts.append(
                f"坏行 {len(self.broken_rows)} 行（"
                + "、".join(str(row_id) for row_id in self.broken_rows)
                + "）"
            )
        return "｜".join(parts)


def expired_lock_rows(
    database: Database,
    *,
    ttl: float,
    now: datetime | None = None,
) -> list[int]:
    """找出**过期**的挂起行锁（持锁的进程多半已经不在了）。

    锁的有效期是 ``ttl`` 秒：占用时间超过有效期还没释放，就当它失效。
    时间戳读不出来的行也算过期 —— 宁可让它能被接管，也不能让一行挂起项
    因为一把读不出时间的锁永远卡住。
    """
    moment = now or datetime.now()
    cutoff = moment - timedelta(seconds=max(0.0, float(ttl)))
    rows = database.query_all(
        "SELECT id, locked_at FROM pending_items"
        " WHERE lock_token IS NOT NULL AND lock_token != '' ORDER BY id"
    )
    expired: list[int] = []
    for row in rows:
        locked_at = (row["locked_at"] or "").strip()
        if not locked_at:
            expired.append(int(row["id"]))
            continue
        try:
            stamp = datetime.strptime(locked_at, _TIMESTAMP_FORMAT)
        except ValueError:
            expired.append(int(row["id"]))
            continue
        if stamp <= cutoff:
            expired.append(int(row["id"]))
    return expired


def recover_state(
    database: Database,
    *,
    watcher: Any | None = None,
    max_attempts: int | None = None,
    lock_ttl: float | None = None,
    release_stale_locks: bool = True,
) -> RecoveryReport:
    """把重启后该接回来的东西接回来，返回一份可核对的清单。

    参数：

    - ``watcher``：结果目录监听器。给了就把目录里还没归档的报文数报出来
      （**事件没丢**的直接证据）；不给就不数这一项。
    - ``max_attempts``：挂起尝试上限，用于给老库里的行补写转人工说明。
    - ``lock_ttl``：挂起行锁的有效期（秒）。给了才会释放过期锁。
    - ``release_stale_locks``：是否释放过期锁。默认释放；只读排查时关掉。

    这一步**不产生任何对外调用**：不取基线、不回写、不发通知。
    """
    store = PendingStore(database)
    # 宽松读：读不出来的挂起行进 ``broken_rows``，报出来但不抛 —— 状态库里
    # 有一行坏了，不该让整个守护进程起不来（起不来的话那一行更没人管了）。
    loaded = store.load_all(
        states=PENDING_STATES, strict=False, max_attempts=max_attempts
    )
    # 条数只数**读得出来**的挂起项：坏行进 ``broken_rows`` 单独报，不混进
    # 挂起项条数 —— 「读不出来」与「等人补齐」是两件事。
    pending_count = sum(1 for snapshot in loaded.snapshots if snapshot.pending)
    escalated_count = sum(1 for snapshot in loaded.snapshots if snapshot.escalated)

    idempotency_keys = int(
        database.query_value("SELECT COUNT(*) FROM idempotency_keys", default=0) or 0
    )

    released = 0
    if release_stale_locks and lock_ttl is not None:
        expired = expired_lock_rows(database, ttl=lock_ttl)
        if expired:
            lock = PendingLock(database, ttl=lock_ttl)
            for pending_id in expired:
                if lock.force_release(pending_id):
                    released += 1

    leftover = 0
    if watcher is not None:
        try:
            leftover = len(watcher.candidate_files())
        except OSError:
            leftover = 0

    return RecoveryReport(
        pending_items=pending_count + escalated_count,
        escalated_items=escalated_count,
        broken_rows=loaded.broken_rows,
        idempotency_keys=idempotency_keys,
        writeback_pending=count_pending_writebacks(database),
        writeback_escalated=count_escalated_writebacks(database),
        stale_locks_released=released,
        leftover_files=leftover,
        state_db=str(getattr(database, "path", "") or ""),
    )


def summarize_reports(reports: Sequence[RecoveryReport]) -> str:
    """把多次恢复的摘要合成一行（运维批量核对用）。"""
    return "｜".join(report.summary_line() for report in reports)
