"""挂起行的行级互斥锁。

两个人同时补同一条挂起项，不能产出两份草稿，也不能把补齐值互相覆盖。
本模块给挂起行加一个**行级互斥标记**：抢到锁的人才有资格续跑这一行；
没抢到的人拿到一句可读的提示，等对方跑完重试即可。

**为什么落在状态库而不是进程内**：补齐既可能由运维在命令行发起，也可能由
守护进程接着跑 —— 那是两个进程，进程内的字典互斥不了跨进程的行。锁记在
``pending_items`` 行上（``lock_token`` / ``locked_at`` / ``lock_owner`` 三列），
状态库里才有的东西，重启后还在。

**抢锁用一条带条件的 UPDATE**::

    UPDATE pending_items SET lock_token = … WHERE id = ? AND (没有锁 or 锁已过期)

原子性由 SQLite 自己保证：两个进程同时抢，只有一个 UPDATE 会命中（命中与否
看 ``rowcount``），不需要额外的文件锁或进程内互斥量。

**锁会过期**：持锁的进程崩了，锁不能永远挂着 —— 否则这一行谁都动不了。
超过 TTL（``PENDING_LOCK_TTL``，默认 5 分钟）即视为过期锁，可被接管，并留一行
审计说明是谁在什么时候接管的。TTL 归零或负数没有意义（等于永不过期），
配置面直接拒绝。

**锁只护「续跑」这一段**：补齐值的登记（``pending_resolutions``）不在锁内 ——
按字段 upsert，本身就是幂等的，且**先登记再续跑**。这样即使没抢到锁，
技术员补的值也已经落在库里，不会丢 —— 抢锁失败的人拿到的是「稍后重试」，
不是「你补的值没了」。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..db import Database
from ..timefmt import now_text

__all__ = [
    "DEFAULT_LOCK_TTL",
    "LOCK_STATUS_ACQUIRED",
    "LOCK_STATUS_LOCKED",
    "LOCK_STATUS_MISSING",
    "LockHolder",
    "LockResult",
    "PendingLock",
]

#: 锁的默认有效期（秒）：超过这个时长没释放，视为过期锁，可被接管。
DEFAULT_LOCK_TTL = 300.0

#: 抢到锁。
LOCK_STATUS_ACQUIRED = "acquired"
#: 锁被别人拿着，且没过期。
LOCK_STATUS_LOCKED = "locked"
#: 这一行不存在。
LOCK_STATUS_MISSING = "missing"

#: 时间戳格式（与 :func:`patho_supplement.timefmt.now_text` 一致）。
#: 这个格式按字典序比较即时间序 —— 过期判断靠它。
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class LockHolder:
    """当前占着这把锁的人。"""

    #: 锁的令牌（释放时凭它核对，防止误删别人的锁）。
    token: str
    #: 占用时间（``YYYY-MM-DD HH:MM:SS``）。
    locked_at: str
    #: 占用者标识（默认是进程号；调用方可传更贴切的说明）。
    owner: str

    def as_dict(self) -> dict[str, str]:
        return {"owner": self.owner, "locked_at": self.locked_at}


@dataclass(frozen=True)
class LockResult:
    """一次抢锁的结果。"""

    #: ``acquired`` / ``locked`` / ``missing``。
    status: str
    #: 抢到时给出令牌（释放时要用）。
    token: str | None = None
    #: 没抢到时给出占用者。
    holder: LockHolder | None = None
    #: 抢到的是一把**过期锁**（原占用者没释放，已被接管）。
    stale: bool = False

    @property
    def acquired(self) -> bool:
        """是否抢到了。"""
        return self.status == LOCK_STATUS_ACQUIRED

    @property
    def locked(self) -> bool:
        """是否被别人占着。"""
        return self.status == LOCK_STATUS_LOCKED

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"status": self.status, "stale": self.stale}
        if self.token:
            data["token"] = self.token
        if self.holder is not None:
            data["holder"] = self.holder.as_dict()
        return data


def _expired_before(ttl: float) -> str:
    """过期时刻（时间戳文本）。

    TTL 为 0 或负数时返回「当下」—— 即一切都算过期，等于不设防。配置面不
    允许这种取值，这里只是兜底。
    """
    moment = datetime.now() - timedelta(seconds=max(0.0, float(ttl)))
    return moment.strftime(_TIMESTAMP_FORMAT)


class PendingLock:
    """挂起行的行级互斥锁。"""

    def __init__(
        self,
        database: Database,
        *,
        ttl: float = DEFAULT_LOCK_TTL,
        owner: str | None = None,
    ) -> None:
        self._db = database
        self._ttl = float(ttl)
        self._owner = owner or f"pid:{os.getpid()}"

    @property
    def owner(self) -> str:
        """占用者标识。"""
        return self._owner

    @property
    def ttl(self) -> float:
        """锁的有效期（秒）。"""
        return self._ttl

    # ------------------------------------------------------------ 抢 / 放

    def acquire(self, pending_id: int) -> LockResult:
        """抢这一行的锁。

        - 抢到 → ``acquired``（带令牌，释放时用它）；
        - 别人占着且没过期 → ``locked``（带占用者）；
        - 这一行不存在 → ``missing``。
        """
        row = self._db.query_one(
            "SELECT lock_token, locked_at, lock_owner FROM pending_items WHERE id = ?",
            (pending_id,),
        )
        if row is None:
            return LockResult(status=LOCK_STATUS_MISSING)

        previous = self._holder_of(row)
        cutoff = _expired_before(self._ttl)
        token = uuid.uuid4().hex
        cursor = self._db.execute(
            "UPDATE pending_items SET lock_token = ?, locked_at = ?, lock_owner = ?"
            " WHERE id = ? AND (lock_token IS NULL OR lock_token = ''"
            " OR locked_at IS NULL OR locked_at <= ?)",
            (token, now_text(), self._owner, pending_id, cutoff),
        )
        if cursor.rowcount == 0:
            # 没命中：锁被别人占着，且没过期。
            return LockResult(status=LOCK_STATUS_LOCKED, holder=self.holder(pending_id))
        # 抢到的是过期锁 → 标出来，接管时要留审计。
        stale = previous is not None and previous.locked_at <= cutoff
        return LockResult(
            status=LOCK_STATUS_ACQUIRED, token=token, holder=previous, stale=stale
        )

    def release(self, pending_id: int, token: str) -> bool:
        """释放锁。

        **凭令牌释放**：只有持有者本人能放，别人的锁不会被误删。返回是否真的
        释放掉了（令牌对不上说明锁已经易主，返回 ``False``）。
        """
        cursor = self._db.execute(
            "UPDATE pending_items SET lock_token = NULL, locked_at = NULL,"
            " lock_owner = NULL WHERE id = ? AND lock_token = ?",
            (pending_id, token),
        )
        return cursor.rowcount > 0

    def force_release(self, pending_id: int) -> bool:
        """强制解锁（不等 TTL）。

        只给运维手动介入用 —— 确认持锁的进程已经不在了才用。返回是否释放掉。
        """
        cursor = self._db.execute(
            "UPDATE pending_items SET lock_token = NULL, locked_at = NULL,"
            " lock_owner = NULL WHERE id = ? AND lock_token IS NOT NULL",
            (pending_id,),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------ 读

    def holder(self, pending_id: int) -> LockHolder | None:
        """当前占用者；没人占着给 ``None``。"""
        row = self._db.query_one(
            "SELECT lock_token, locked_at, lock_owner FROM pending_items WHERE id = ?",
            (pending_id,),
        )
        if row is None:
            return None
        return self._holder_of(row)

    def is_locked(self, pending_id: int) -> bool:
        """这一行现在是否有人占着。"""
        return self.holder(pending_id) is not None

    @staticmethod
    def _holder_of(row: object) -> LockHolder | None:
        token = row["lock_token"]  # type: ignore[index]
        if not token:
            return None
        return LockHolder(
            token=token,
            locked_at=row["locked_at"] or "",  # type: ignore[index]
            owner=row["lock_owner"] or "",  # type: ignore[index]
        )
