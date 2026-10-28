"""挂起项快照的持久化与恢复。

对不上的结果既不能塞进草稿，也不能丢掉。系统把「这条结果当时长什么样」拍成
一张**快照**落进状态库，等人补齐字段后再从挂起的那一步接着跑。

快照语义沿用三条：

- **拍在消费之前** —— 快照记的是结果**进入对齐环节之前**的样子（原始回报：
  项目码、结果值、观察时间、部位序号、蜡块号、来源），不是对齐算完的结论。
  续跑要重算，不能拿结论当输入。
- **落库不落文件** —— 快照进 SQLite，进程重启后按库恢复。写成文件的话，
  文件被清掉就等于丢了一条结果，而丢一条结果等于报告少一项。
- **缺项即报错，不静默跳过** —— 读不出来的行不悄悄扔掉，行号一并返回，
  由调用方报出来（续跑前的完整性校验见任务 id 32）。

写入与读取的口径只有这一处，别处不再各写一份 SQL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Sequence

from ..db import Database
from ..models import Block, ResultEntry, ResultStatus
from ..timefmt import now_text
from .fills import apply_fills
from .reasons import PendingReason, ReasonHistoryEntry, parse_reason
from .records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    PENDING_STATE_RESOLVED,
    PendingRecord,
    decide_attempt,
)

__all__ = [
    "PENDING_STATES",
    "PendingLoadResult",
    "PendingSnapshot",
    "PendingSnapshotError",
    "PendingStore",
    "SNAPSHOT_FORMAT_VERSION",
    "entry_from_payload",
    "snapshot_payload",
]

#: 快照格式版本。写进快照内容，续跑时用来判断手里的快照是不是当前格式。
SNAPSHOT_FORMAT_VERSION = 1

#: 挂起项在库里的两种状态：等人补齐 / 已超尝试上限转人工。
PENDING_STATES = (PENDING_STATE_PENDING, PENDING_STATE_ESCALATED)

#: 结果条目的字段名（写快照与读快照共用一份，避免两处各列一遍）。
_ENTRY_FIELDS = (
    "accession_no",
    "item_code",
    "item_name",
    "value",
    "status",
    "observed_at",
    "order_no",
    "region_seq",
    "block_no",
    "region_desc",
    "source_system",
    "source_file",
)

#: 读挂起行时取的列。
_SELECT_COLUMNS = (
    "id, accession_no, item_code, result_json, missing_fields, candidates_json,"
    " reason, attempts, state, snapshot_at, block_no, region_seq, note,"
    " resolved_at, resolve_key, fills_revision, resolved_revision"
)


class PendingSnapshotError(RuntimeError):
    """挂起项快照读不出来（行损坏或格式不对）。

    带上行号，便于直接定位到状态库里的哪一行。
    """

    def __init__(self, row_id: int, reason: str) -> None:
        super().__init__(f"挂起项快照第 {row_id} 行读不出来：{reason}")
        self.row_id = row_id
        self.reason = reason


@dataclass(frozen=True)
class PendingSnapshot:
    """一条挂起项在库里的样子。

    六个必备内容：**未对齐结果**（``entry``）、**缺失字段**、**候选列表**、
    **尝试次数**、**快照时间**、**来源系统**（在 ``entry`` 里）。
    """

    snapshot_id: int
    accession_no: str
    item_code: str
    reason: PendingReason
    missing_fields: tuple[str, ...]
    candidates: tuple[Block, ...]
    entry: ResultEntry | None
    attempts: int
    state: str
    snapshot_at: str
    #: 是否已超尝试次数上限（转人工处置，不再自动续跑）。
    escalated: bool = False
    #: 转人工处置的说明（快照自足，恢复时不依赖配置面也读得懂）。
    note: str | None = None
    #: 原因历史（带时间戳）：当前原因取最新一条，早先的原因不被覆盖。
    reason_history: tuple[ReasonHistoryEntry, ...] = ()
    #: 人工补齐值（字段名 → 值）：**不覆盖原始快照**，续跑时叠加在原始回报上。
    fills: tuple[tuple[str, str], ...] = ()
    #: 了结时间（未了结时为 ``None``）。
    resolved_at: str | None = None
    #: 了结时生效的补齐幂等键：同一组补齐值重复提交时靠它认出「这是同一次」。
    resolve_key: str | None = None
    #: 补齐提交次数：每次提交补齐值 +1（值没变也算一次提交）。
    fills_revision: int = 0
    #: 了结当时的补齐提交次数：比它大说明「了结之后又提交过一次」。
    resolved_revision: int | None = None

    @property
    def pending(self) -> bool:
        """还在等人补齐（尚未被转人工处置）。"""
        return self.state == PENDING_STATE_PENDING

    @property
    def resolved(self) -> bool:
        """这条挂起项已了结（补齐后并入草稿）。"""
        return self.state == PENDING_STATE_RESOLVED

    @property
    def fill_map(self) -> dict[str, str]:
        """补齐值（字段名 → 值）。"""
        return dict(self.fills)

    @property
    def effective_entry(self) -> ResultEntry | None:
        """原始回报叠加补齐值之后的结果条目。

        ``entry`` 是**原始快照**（进对齐之前的样子，不动）；这里是叠加了人工
        补齐值之后的版本 —— 续跑重算用的是它。
        """
        if self.entry is None:
            return None
        if not self.fills:
            return self.entry
        return apply_fills(self.entry, self.fill_map)

    @property
    def block_no(self) -> str | None:
        """蜡块号（从原始结果取）。"""
        return self.entry.block_no if self.entry else None

    @property
    def source_system(self) -> str | None:
        """来源系统（从原始结果取）。"""
        return self.entry.source_system if self.entry else None

    def to_record(self) -> PendingRecord:
        """转成给人看的挂起记录（清单渲染与导出用）。

        用的是**叠加补齐值之后**的条目 —— 清单要给技术员看「现在还缺什么」，
        不是「当初回报里缺什么」。
        """
        entry = self.effective_entry
        return PendingRecord(
            accession_no=self.accession_no,
            item_code=self.item_code,
            reason=self.reason,
            missing_fields=self.missing_fields,
            candidates=self.candidates,
            source_file=entry.source_file if entry else None,
            source_system=entry.source_system if entry else None,
            observed_at=entry.observed_at if entry else None,
            value=entry.value if entry else None,
            attempt=self.attempts,
            snapshot_at=self.snapshot_at,
            escalated=self.escalated,
            note=self.note,
            previous_reasons=tuple(
                entry.reason.label for entry in self.reason_history[:-1]
            ),
            entry=entry,
            pending_id=self.snapshot_id,
            filled_fields=tuple(self.fill_map.keys()),
        )


@dataclass(frozen=True)
class PendingLoadResult:
    """一次读取的结果。

    ``broken_rows`` 是**读不出来**的行号 —— 不静默丢弃，交给调用方报出来。
    """

    snapshots: tuple[PendingSnapshot, ...] = ()
    broken_rows: tuple[int, ...] = ()

    @property
    def count(self) -> int:
        return len(self.snapshots)


def snapshot_payload(record: PendingRecord) -> str:
    """把一条挂起记录拍成快照内容（JSON 文本）。

    快照记的是结果**进对齐之前**的样子：原始回报的字段全留（含蜡块号、
    部位序号、结果状态），这样续跑时不必再回头翻原始报文。
    """
    payload: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_FORMAT_VERSION,
        "accession_no": record.accession_no,
        "item_code": record.item_code,
        "value": record.value,
        "source_system": record.source_system,
        "source_file": record.source_file,
        "observed_at": record.observed_at,
    }
    entry = record.entry
    if entry is not None:
        payload.update(entry.to_dict())
    return json.dumps(payload, ensure_ascii=False)


def entry_from_payload(payload: dict[str, Any]) -> ResultEntry | None:
    """从快照内容还原原始结果条目；认不出来给 ``None``。

    认不出来就明说认不出来 —— 宁可没有原始条目，也不要拼一个半截的出来。
    """
    accession_no = payload.get("accession_no")
    item_code = payload.get("item_code")
    if not accession_no or not item_code:
        return None
    try:
        status = ResultStatus(payload.get("status") or "F")
    except ValueError:
        return None
    data = {key: payload.get(key) for key in _ENTRY_FIELDS}
    data["accession_no"] = accession_no
    data["item_code"] = item_code
    data["status"] = status
    return ResultEntry(**data)


def _candidates_json(candidates: Sequence[Block]) -> str:
    """候选部位列表落库文本。"""
    return json.dumps(
        [
            {
                "region_seq": block.region_seq,
                "region_desc": block.region_desc,
                "block_no": block.block_no,
                "order_no": block.order_no,
            }
            for block in candidates
        ],
        ensure_ascii=False,
    )


def _candidates_from_json(text: str) -> tuple[Block, ...]:
    """候选部位列表从库读回来。"""
    items = json.loads(text or "[]")
    return tuple(
        Block(
            region_seq=item.get("region_seq"),
            region_desc=item.get("region_desc"),
            block_no=item.get("block_no"),
            order_no=item.get("order_no"),
        )
        for item in items
    )


class PendingStore:
    """挂起项快照的读写门面。

    挂起项的口径只有这一处：写尝试次数、写快照、读回来、重启后恢复。
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        """状态库句柄（需要往库里写审计行等场景用）。"""
        return self._db

    # ------------------------------------------------------------ 写

    def save(self, record: PendingRecord, *, max_attempts: int) -> PendingSnapshot:
        """把一条挂起记录拍成快照落库，返回落库后的快照。

        尝试次数按「病理号 + 项目码」累计：同一条结果反复对不上时每挂起一次
        多试一次；超过上限（``PENDING_MAX_ATTEMPTS``）就不再自动续跑，转人工
        处置 —— 否则一条永远对不上的结果会在库里无限累积，把真正要看的异常淹掉。
        """
        decision = decide_attempt(
            self.previous_attempts(record.accession_no, record.item_code),
            max_attempts,
        )
        # 原因历史先记：原因变了要留痕，当前原因取最新一条。
        # （先记后写挂起行 —— 写完再记会被自己刚写的那行挡住，误判成「没变」。）
        self.record_reason(
            record.accession_no,
            record.item_code,
            record.reason,
            detail=record.note,
        )
        entry = record.entry
        snapshot = PendingSnapshot(
            snapshot_id=0,
            accession_no=record.accession_no,
            item_code=record.item_code,
            reason=record.reason,
            missing_fields=tuple(record.missing_fields),
            candidates=tuple(record.candidates),
            entry=entry,
            attempts=decision.attempts,
            state=decision.state,
            snapshot_at=record.snapshot_at or now_text(),
            escalated=decision.escalated,
            note=decision.note,
        )
        cursor = self._db.execute(
            "INSERT INTO pending_items(accession_no, item_code, result_json, missing_fields,"
            " candidates_json, reason, attempts, state, snapshot_at, block_no, region_seq, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.accession_no,
                snapshot.item_code,
                snapshot_payload(record),
                ",".join(snapshot.missing_fields),
                _candidates_json(snapshot.candidates),
                snapshot.reason.value,
                snapshot.attempts,
                snapshot.state,
                snapshot.snapshot_at,
                entry.block_no if entry else None,
                entry.region_seq if entry else None,
                snapshot.note,
            ),
        )
        return replace(snapshot, snapshot_id=int(cursor.lastrowid))

    def save_many(
        self, records: Sequence[PendingRecord], *, max_attempts: int
    ) -> list[PendingSnapshot]:
        """批量落库，按写入顺序返回快照。"""
        return [self.save(record, max_attempts=max_attempts) for record in records]

    def previous_attempts(self, accession_no: str, item_code: str) -> int:
        """同一「病理号 + 项目码」此前已挂起过几次。"""
        row = self._db.query_one(
            "SELECT MAX(attempts) AS attempts FROM pending_items"
            " WHERE accession_no = ? AND item_code = ?",
            (accession_no, item_code),
        )
        if row is None or row["attempts"] is None:
            return 0
        return int(row["attempts"])

    # ------------------------------------------------------------ 补齐值

    def record_fill(
        self,
        pending_id: int,
        field_name: str,
        field_value: str,
        *,
        recorded_at: str | None = None,
    ) -> None:
        """记一个补齐值（字段名 → 值）。

        同一个字段再补一次就是**改值**（原值被替换）—— 技术员查登记后发现
        第一次填错了是常事，补齐入口不能把路堵死。

        补齐值**不碰原始快照**：``result_json`` 里存的仍是回报原样，事后才
        说得清「当时回报里到底有没有蜡块号」。

        每次提交都把 ``fills_revision`` 加一（**值没变也算一次提交**）：续跑
        靠它分辨「这是同一次补齐的重放」还是「又补了一次」—— 前者应当幂等
        返回上一次结论，后者才该报错。
        """
        self._db.execute(
            "INSERT INTO pending_resolutions(pending_id, field_name, field_value, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(pending_id, field_name) DO UPDATE SET"
            " field_value = excluded.field_value, created_at = excluded.created_at",
            (pending_id, field_name, field_value, recorded_at or now_text()),
        )
        self._db.execute(
            "UPDATE pending_items SET fills_revision = COALESCE(fills_revision, 0) + 1"
            " WHERE id = ?",
            (pending_id,),
        )

    def load_fills(self, pending_id: int) -> dict[str, str]:
        """读回一条挂起项的补齐值（字段名 → 值）。"""
        rows = self._db.query_all(
            "SELECT field_name, field_value FROM pending_resolutions"
            " WHERE pending_id = ? ORDER BY id",
            (pending_id,),
        )
        return {row["field_name"]: row["field_value"] for row in rows}

    # ------------------------------------------------------------ 续跑

    def restamp(
        self,
        snapshot_id: int,
        *,
        record: PendingRecord,
        max_attempts: int,
        entry: ResultEntry | None = None,
    ) -> PendingSnapshot:
        """续跑后**仍然对不上**：把这一行重新拍一次。

        「重新拍」指的就地更新这一行（不新增行）：尝试次数 +1、原因与候选更新
        成最新的、快照时间前移。**不新增行**是为了让「待人工处理清单」里一条
        结果只占一行 —— 同一个问题挂两次，技术人员就得去分哪条是哪条。

        原始快照内容（``result_json``）不动：补齐值另落一张表，续跑时叠加。

        ``entry`` 给了才换快照内容 —— 那是**迟到结果**接回来的场景：这一行
        记的应当是最新一次回报。补齐值仍然不写进 ``result_json``（它另有落点）。
        """
        snapshot = self.load_by_id(snapshot_id)
        if snapshot is None:
            raise PendingSnapshotError(snapshot_id, "状态库里没有这一行挂起项")
        decision = decide_attempt(snapshot.attempts, max_attempts)
        # 原因历史先记（先记后写，免得被自己刚写的行挡住，误判成「没变」）。
        self.record_reason(
            record.accession_no,
            record.item_code,
            record.reason,
            detail=record.note,
        )
        self._db.execute(
            "UPDATE pending_items SET missing_fields = ?, candidates_json = ?, reason = ?,"
            " attempts = ?, state = ?, snapshot_at = ?, note = ? WHERE id = ?",
            (
                ",".join(record.missing_fields),
                _candidates_json(record.candidates),
                record.reason.value,
                decision.attempts,
                decision.state,
                record.snapshot_at or now_text(),
                decision.note,
                snapshot_id,
            ),
        )
        if entry is not None:
            # 迟到结果接回：快照内容换成新回报（补齐值不写进来 —— 它在另一张表）。
            self._db.execute(
                "UPDATE pending_items SET result_json = ?, block_no = ?, region_seq = ?"
                " WHERE id = ?",
                (
                    snapshot_payload(replace(record, entry=entry)),
                    entry.block_no,
                    entry.region_seq,
                    snapshot_id,
                ),
            )
        updated = self.load_by_id(snapshot_id)
        if updated is None:  # pragma: no cover - 行刚更新过，不该消失
            raise PendingSnapshotError(snapshot_id, "状态库里没有这一行挂起项")
        return updated

    def mark_resolved(
        self,
        snapshot_id: int,
        *,
        resolve_key: str | None = None,
        resolved_at: str | None = None,
    ) -> None:
        """把挂起项标成**已了结**（补齐后已并入草稿）。

        了结的行不再出现在待人工处理清单里，但行本身留在库中 —— 审计与复盘
        还要查它。

        了结时记下当时的补齐提交次数（``resolved_revision``）：之后若又提交过
        同样的补齐值，续跑据此判定「这是同一次提交的重放」，幂等返回上一次
        结论，而不是再并一份草稿。
        """
        row = self._db.query_one(
            "SELECT fills_revision FROM pending_items WHERE id = ?", (snapshot_id,)
        )
        revision = int(row["fills_revision"] or 0) if row is not None else 0
        self._db.execute(
            "UPDATE pending_items SET state = ?, resolved_at = ?, resolve_key = ?,"
            " resolved_revision = ? WHERE id = ?",
            (
                PENDING_STATE_RESOLVED,
                resolved_at or now_text(),
                resolve_key,
                revision,
                snapshot_id,
            ),
        )

    # ------------------------------------------------------------ 原因历史

    def record_reason(
        self,
        accession_no: str,
        item_code: str,
        reason: PendingReason,
        *,
        detail: str | None = None,
        recorded_at: str | None = None,
    ) -> ReasonHistoryEntry | None:
        """记一条挂起原因（带时间戳）。

        **只在原因变化时追加**：同一原因反复挂起不重复记 —— 原因历史记的是
        「为什么变了」，不是「试了几次」（试了几次由 ``attempts`` 记）。
        原因变了，历史保留旧原因，**当前原因取最新一条**。
        """
        current = self.current_reason(accession_no, item_code)
        if current is not None and current is reason:
            return None
        stamp = recorded_at or now_text()
        self._db.execute(
            "INSERT INTO pending_reason_history(accession_no, item_code, reason, detail,"
            " recorded_at) VALUES (?, ?, ?, ?, ?)",
            (accession_no, item_code, reason.value, detail, stamp),
        )
        return ReasonHistoryEntry(reason=reason, recorded_at=stamp, detail=detail)

    def reason_history(
        self, accession_no: str, item_code: str
    ) -> tuple[ReasonHistoryEntry, ...]:
        """读回原因历史（按记录顺序）。"""
        rows = self._db.query_all(
            "SELECT reason, detail, recorded_at FROM pending_reason_history"
            " WHERE accession_no = ? AND item_code = ? ORDER BY id",
            (accession_no, item_code),
        )
        entries: list[ReasonHistoryEntry] = []
        for row in rows:
            try:
                reason = parse_reason(row["reason"])
            except ValueError:
                # 原因不认识就不当没看见 —— 记成坏行，由调用方决定怎么办。
                continue
            entries.append(
                ReasonHistoryEntry(
                    reason=reason,
                    recorded_at=row["recorded_at"],
                    detail=row["detail"],
                )
            )
        return tuple(entries)

    def current_reason(
        self, accession_no: str, item_code: str
    ) -> PendingReason | None:
        """当前原因（最新一条）；没有历史时取挂起行上的原因。"""
        history = self.reason_history(accession_no, item_code)
        if history:
            return history[-1].reason
        row = self._db.query_one(
            "SELECT reason FROM pending_items WHERE accession_no = ? AND item_code = ?"
            " ORDER BY id DESC LIMIT 1",
            (accession_no, item_code),
        )
        if row is None:
            return None
        try:
            return parse_reason(row["reason"])
        except ValueError:
            return None

    # ------------------------------------------------------------ 读

    def load_all(
        self,
        *,
        states: Sequence[str] | None = None,
        strict: bool = True,
        max_attempts: int | None = None,
    ) -> PendingLoadResult:
        """读回挂起项快照。

        ``strict=True``（默认）：碰到读不出来的行直接报错 —— 缺项即报错，
        不静默跳过。``strict=False``：读不出来的行进 ``broken_rows``，
        由调用方报出来，不悄悄扔掉。

        ``max_attempts`` 只在补写转人工说明时用得上（老库里没存说明的行，
        按上限补一句），不影响快照内容本身。
        """
        sql = f"SELECT {_SELECT_COLUMNS} FROM pending_items"
        params: list[Any] = []
        if states:
            placeholders = ", ".join("?" for _ in states)
            sql += f" WHERE state IN ({placeholders})"
            params.extend(states)
        sql += " ORDER BY id"

        snapshots: list[PendingSnapshot] = []
        broken: list[int] = []
        for row in self._db.query_all(sql, params):
            try:
                snapshots.append(self._row_to_snapshot(row, max_attempts=max_attempts))
            except PendingSnapshotError as exc:
                if strict:
                    raise
                broken.append(exc.row_id)
        return PendingLoadResult(snapshots=tuple(snapshots), broken_rows=tuple(broken))

    def recover(self, *, max_attempts: int | None = None) -> PendingLoadResult:
        """进程重启后从库里恢复全部挂起项。

        恢复的是**快照**（未对齐结果 + 缺失字段 + 候选列表 + 尝试次数 +
        快照时间 + 来源系统），不是最终结论 —— 续跑要拿快照重算。
        """
        return self.load_all(states=PENDING_STATES, max_attempts=max_attempts)

    def load_by_id(
        self, snapshot_id: int, *, max_attempts: int | None = None
    ) -> PendingSnapshot | None:
        """按行号读一条挂起项快照；没有这一行给 ``None``。"""
        row = self._db.query_one(
            f"SELECT {_SELECT_COLUMNS} FROM pending_items WHERE id = ?",
            (snapshot_id,),
        )
        if row is None:
            return None
        return self._row_to_snapshot(row, max_attempts=max_attempts)

    def find_open(
        self, *, accession_no: str, item_code: str
    ) -> tuple[PendingSnapshot, ...]:
        """按「病理号 + 项目码」找出**未了结**的挂起行（按行号升序）。

        迟到结果接回来时要找的就是这些行（见
        :mod:`patho_supplement.pending.late_arrival`）：蜡块号对不对得上由调用
        方判断，这里只负责把候选行取出来。

        读不出来的行不静默丢弃，但也不在这里报错 —— 它已经坏了，接不回任何
        结果；每一轮清单导出时会把坏行号报出来（守护进程的告警行）。
        """
        rows = self._db.query_all(
            f"SELECT {_SELECT_COLUMNS} FROM pending_items"
            " WHERE state IN (?, ?) AND accession_no = ? AND UPPER(item_code) = UPPER(?)"
            " ORDER BY id",
            (
                PENDING_STATE_PENDING,
                PENDING_STATE_ESCALATED,
                accession_no,
                item_code,
            ),
        )
        snapshots: list[PendingSnapshot] = []
        for row in rows:
            try:
                snapshots.append(self._row_to_snapshot(row))
            except PendingSnapshotError:
                continue
        return tuple(snapshots)

    def count(self, *, states: Sequence[str] | None = None) -> int:
        """挂起项条数。"""
        sql = "SELECT COUNT(*) FROM pending_items"
        params: list[Any] = []
        if states:
            placeholders = ", ".join("?" for _ in states)
            sql += f" WHERE state IN ({placeholders})"
            params.extend(states)
        return int(self._db.query_value(sql, params, default=0))

    # ------------------------------------------------------------ 行 → 快照

    def _row_to_snapshot(
        self, row: Any, *, max_attempts: int | None = None
    ) -> PendingSnapshot:
        row_id = int(row["id"])
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, ValueError) as exc:
            raise PendingSnapshotError(row_id, f"快照内容不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise PendingSnapshotError(row_id, "快照内容不是对象")
        try:
            reason = parse_reason(row["reason"])
        except ValueError as exc:
            raise PendingSnapshotError(
                row_id, f"挂起原因不认识：{row['reason']!r}"
            ) from exc
        try:
            candidates = _candidates_from_json(row["candidates_json"])
        except (TypeError, ValueError) as exc:
            raise PendingSnapshotError(
                row_id, f"候选列表不是合法 JSON：{exc}"
            ) from exc
        missing_fields = tuple(
            part for part in (row["missing_fields"] or "").split(",") if part
        )
        attempts = int(row["attempts"] or 0)
        state = row["state"]
        escalated = state == PENDING_STATE_ESCALATED
        note = row["note"]
        if escalated and not note and max_attempts is not None:
            note = (
                f"已尝试 {attempts} 次，超过挂起尝试上限 {max_attempts} 次，转人工处置"
            )
        return PendingSnapshot(
            snapshot_id=row_id,
            accession_no=row["accession_no"],
            item_code=row["item_code"] or "",
            reason=reason,
            missing_fields=missing_fields,
            candidates=candidates,
            entry=entry_from_payload(payload),
            attempts=attempts,
            state=state,
            snapshot_at=row["snapshot_at"],
            escalated=escalated,
            note=note,
            reason_history=self.reason_history(
                row["accession_no"], row["item_code"] or ""
            ),
            fills=tuple(self.load_fills(row_id).items()),
            resolved_at=row["resolved_at"],
            resolve_key=row["resolve_key"],
            fills_revision=int(row["fills_revision"] or 0),
            resolved_revision=(
                int(row["resolved_revision"])
                if row["resolved_revision"] is not None
                else None
            ),
        )
