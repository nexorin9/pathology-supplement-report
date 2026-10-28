"""SQLite 状态库：建表与幂等迁移。

状态全部落 SQLite（挂起项、草稿版本链、回写队列、审计流水、幂等键、检查项状态），
进程重启后从库里恢复，不丢事件、不丢已生成但尚未回写的草稿。

迁移策略：每条建表语句都用 ``IF NOT EXISTS``，重复执行不会破坏既有数据；
``schema_meta`` 记录当前版本号，便于以后追加迁移。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

__all__ = ["Database", "apply_migrations", "connect", "SCHEMA_VERSION"]

SCHEMA_VERSION = 10

#: 历史库补列（老库升级用）。每项是 (表名, 列名, 列定义)。
#: 新建库直接带上这些列，老库走 ``ALTER TABLE`` 补齐，两条路径结果一致。
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # 挂起项的项目码：同一条结果反复挂起时按「病理号 + 项目码」累计尝试次数
    ("pending_items", "item_code", "TEXT"),
    # 挂起快照的定位字段：迟到结果按「病理号 + 蜡块号 + 项目码」回找挂起行
    ("pending_items", "block_no", "TEXT"),
    ("pending_items", "region_seq", "INTEGER"),
    # 转人工处置的说明：快照自足 —— 恢复时不依赖配置面也读得懂这条为什么被转人工
    ("pending_items", "note", "TEXT"),
    # 补齐值的幂等依据：了结时记下当时生效的补齐键，重复提交同一组值时靠它判定
    # 「这是同一次提交的重放」而不是「又补了一次」
    ("pending_items", "resolve_key", "TEXT"),
    # 补齐提交次数：了结之后再提交一次（值没变）也能分辨出来 —— 计数比了结时多，
    # 说明是重复提交，续跑应当幂等返回而不是报错
    ("pending_items", "fills_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_items", "resolved_revision", "INTEGER"),
    # 行级互斥锁：同一挂起行被并发补齐时，只有一个能续跑，另一个拿到
    # 「行已被处理」的提示。锁落在行上，跨进程也互斥（守护进程 + 运维命令行）
    ("pending_items", "lock_token", "TEXT"),
    ("pending_items", "locked_at", "TEXT"),
    ("pending_items", "lock_owner", "TEXT"),
    # 检查项的归属键：部位序号与蜡块号。与全系统的归属键（病理号 + 部位序号 +
    # 蜡块号）对齐，状态迁移才能按块定位到具体那项检查。
    ("check_items", "block_no", "TEXT"),
    ("check_items", "region_seq", "INTEGER"),
    # 回写队列的重试轨迹：最后一次尝试的时间、写回成功时的回执。
    # 退避重试要按「上次什么时候试的」推下一次的时间，复盘要看出这份草稿
    # 是第几次才写回去的 —— 都要靠这两列。
    ("writeback_queue", "last_attempt_at", "TEXT"),
    ("writeback_queue", "receipt_json", "TEXT"),
    # 升级告警：什么时候升级的、因为什么升级的。超次之后这一行留在库里等人
    # 处置，这两列就是「为什么等人」的凭据。
    ("writeback_queue", "escalated_at", "TEXT"),
    ("writeback_queue", "escalation_reason", "TEXT"),
)

#: 建表语句。列名一律用小写下划线，注释说明业务含义。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    # 元信息：记录库结构版本
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # 幂等键表：病理号 + 蜡块号 + 项目码 + 观察时间 + 结果状态
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key           TEXT PRIMARY KEY,
        accession_no  TEXT NOT NULL,
        block_no      TEXT,
        item_code     TEXT NOT NULL,
        observed_at   TEXT NOT NULL,
        result_status TEXT NOT NULL,
        source_file   TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    # 待查项表：哪些检查项在等结果
    """
    CREATE TABLE IF NOT EXISTS pending_queries (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no TEXT NOT NULL,
        order_no     TEXT,
        item_code    TEXT NOT NULL,
        item_name    TEXT,
        region_seq   INTEGER,
        block_no     TEXT,
        region_desc  TEXT,
        state        TEXT NOT NULL,
        source       TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE (accession_no, order_no, item_code)
    )
    """,
    # 草稿版本链：每次成稿追加一版，旧版本不被覆盖
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no   TEXT NOT NULL,
        version        INTEGER NOT NULL,
        base_version   TEXT,
        state          TEXT NOT NULL,
        entry_count    INTEGER NOT NULL,
        content_hash   TEXT NOT NULL,
        body           TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        written_back_at TEXT,
        receipt        TEXT,
        UNIQUE (accession_no, version)
    )
    """,
    # 草稿条目：按部位分组后的每一行
    """
    CREATE TABLE IF NOT EXISTS draft_entries (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id      INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
        region_seq    INTEGER,
        region_desc   TEXT,
        block_no      TEXT,
        item_code     TEXT NOT NULL,
        item_name     TEXT,
        value         TEXT,
        source_system TEXT,
        observed_at   TEXT,
        placeholder   INTEGER NOT NULL DEFAULT 0,
        rationale_json TEXT
    )
    """,
    # 挂起项表：对不上、等人补齐的快照
    # 未对齐结果 + 缺失字段 + 候选列表 + 尝试次数 + 快照时间 + 来源系统，
    # 全部在一行里 —— 重启后按这一行恢复，不回头翻原始报文。
    """
    CREATE TABLE IF NOT EXISTS pending_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no   TEXT NOT NULL,
        item_code      TEXT,
        result_json    TEXT NOT NULL,
        missing_fields TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        reason         TEXT NOT NULL,
        attempts       INTEGER NOT NULL DEFAULT 0,
        state          TEXT NOT NULL,
        snapshot_at    TEXT NOT NULL,
        resolved_at    TEXT,
        resolve_key    TEXT,
        block_no       TEXT,
        region_seq     INTEGER,
        note           TEXT,
        fills_revision INTEGER NOT NULL DEFAULT 0,
        resolved_revision INTEGER,
        lock_token     TEXT,
        locked_at      TEXT,
        lock_owner     TEXT
    )
    """,
    # 挂起原因历史：同一条挂起项的原因变化留痕（带时间戳，当前原因取最新一条）
    """
    CREATE TABLE IF NOT EXISTS pending_reason_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no TEXT NOT NULL,
        item_code    TEXT NOT NULL,
        reason       TEXT NOT NULL,
        detail       TEXT,
        recorded_at  TEXT NOT NULL
    )
    """,
    # 人工补齐值：技术员补的字段值，**不覆盖原始快照**。
    # 原始回报留在 pending_items.result_json 里（那是「进对齐之前」的样子），
    # 补齐值另落一张表 —— 续跑时两者叠加，事后也说得清「当时回报里有没有蜡块号」。
    """
    CREATE TABLE IF NOT EXISTS pending_resolutions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pending_id  INTEGER NOT NULL,
        field_name  TEXT NOT NULL,
        field_value TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (pending_id, field_name)
    )
    """,
    # 续跑幂等台账：同一组补齐值（挂起行 ID + 字段 + 值）只生效一次。
    # submissions 记提交了几次（≥2 即「重复提交」，续跑幂等返回）；
    # status / draft_id 记这一组值生效到了哪一版草稿。
    """
    CREATE TABLE IF NOT EXISTS pending_resolve_keys (
        resolve_key   TEXT PRIMARY KEY,
        pending_id    INTEGER NOT NULL,
        accession_no  TEXT NOT NULL,
        item_code     TEXT,
        submissions   INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT '',
        draft_id      INTEGER,
        draft_version INTEGER,
        detail_json   TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    # 回写队列：病理系统不可达时的重试队列（指数退避）
    """
    CREATE TABLE IF NOT EXISTS writeback_queue (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no    TEXT NOT NULL,
        draft_id        INTEGER NOT NULL,
        payload_json    TEXT NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        state           TEXT NOT NULL,
        last_error      TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    # 版本冲突登记：本地草稿按老基础版本回写时被对面拒收。
    # 一行一条冲突，带本地草稿版本号与基础版本号 —— 人拿到清单就知道是哪一版
    # 卡住了。冲突行不删：后续版本写回成功后转为「已了结」，复盘时答得出
    # 「当时冲突过、后来是哪一版写回去的」。
    """
    CREATE TABLE IF NOT EXISTS writeback_conflicts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no  TEXT NOT NULL,
        draft_id      INTEGER,
        draft_version INTEGER,
        base_version  TEXT,
        entry_count   INTEGER,
        reason        TEXT NOT NULL,
        detail        TEXT,
        source        TEXT NOT NULL DEFAULT 'direct',
        state         TEXT NOT NULL DEFAULT 'open',
        detected_at   TEXT NOT NULL,
        resolved_at   TEXT,
        resolution    TEXT
    )
    """,
    # 审计流水：对齐依据、状态迁移、写回回执、通知发送
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        category     TEXT NOT NULL,
        event        TEXT NOT NULL,
        accession_no TEXT,
        detail_json  TEXT
    )
    """,
    # 检查项状态表：检查项的生命周期（待回报 → 已回报 → 已对齐 → 已并入，
    # 旁路待人工；订单控制侧另有已取消 / 已暂停 / 已终止）。
    # 归属键与全系统一致：病理号 + 部位序号 + 蜡块号。
    """
    CREATE TABLE IF NOT EXISTS check_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_no TEXT NOT NULL,
        order_no     TEXT,
        item_code    TEXT NOT NULL,
        state        TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        block_no     TEXT,
        region_seq   INTEGER,
        UNIQUE (accession_no, order_no, item_code)
    )
    """,
)

_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_idem_accession ON idempotency_keys(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_queries_accession ON pending_queries(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_pending_accession ON pending_items(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_pending_state ON pending_items(state)",
    "CREATE INDEX IF NOT EXISTS idx_pending_item_key ON pending_items(accession_no, item_code, block_no)",
    "CREATE INDEX IF NOT EXISTS idx_pending_reason_key ON pending_reason_history(accession_no, item_code)",
    "CREATE INDEX IF NOT EXISTS idx_resolutions_pending ON pending_resolutions(pending_id)",
    "CREATE INDEX IF NOT EXISTS idx_resolve_keys_pending ON pending_resolve_keys(pending_id)",
    "CREATE INDEX IF NOT EXISTS idx_entries_draft ON draft_entries(draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_queue_state ON writeback_queue(state)",
    "CREATE INDEX IF NOT EXISTS idx_conflict_state ON writeback_conflicts(state)",
    "CREATE INDEX IF NOT EXISTS idx_conflict_accession ON writeback_conflicts(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_audit_accession ON audit_log(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_log(category)",
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开（必要时创建）状态库连接。

    开启 WAL 与 busy_timeout：挂起项补齐存在并发写（WAL 下读写不互斥），
    busy_timeout 让并发写排队而不是直接抛「database is locked」。
    """
    path = Path(db_path).expanduser()
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> int:
    """建表 / 补索引 / 记录版本号。重复执行安全。

    返回当前库结构版本号。
    """
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    for statement in _INDEX_STATEMENTS:
        conn.execute(statement)
    for table, column, definition in _COLUMN_MIGRATIONS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return SCHEMA_VERSION


class Database:
    """状态库句柄。

    用法::

        with Database(path) as db:
            db.init_schema()
            db.execute("SELECT ...")
    """

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path).expanduser()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ 连接

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(self.path)
        return self._conn

    def init_schema(self) -> None:
        """建表并记录版本号（幂等）。"""
        apply_migrations(self.connection)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        self.init_schema()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------ 执行

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connection.executemany(sql, [tuple(p) for p in seq])

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, tuple(params)).fetchall()

    def query_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务；异常时回滚，不留下半截写入。

        已经在事务里时**沿用外层事务**（不再 ``BEGIN``）—— sqlite 不允许嵌套
        事务，硬开第二个会报「cannot start a transaction within a
        transaction」。沿用外层事务的语义是一样的：异常照样整体回滚。
        """
        conn = self.connection
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def schema_version(self) -> int:
        row = self.query_one("SELECT value FROM schema_meta WHERE key='schema_version'")
        return int(row[0]) if row else 0
