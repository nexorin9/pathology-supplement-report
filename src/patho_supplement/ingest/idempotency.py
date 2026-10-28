"""幂等键、去重账本与结果状态取舍。

同一条结果被投两次（补投、网络重发、目录重扫）不能进两份草稿。幂等键 =
**病理号 + 蜡块号 + 项目码 + 观察时间 + 结果状态**，五项拼起来才能唯一标识
「某一块上某个检查项在某次观察时间上的一次回报状态」。

状态相关三条规则都在这里落：

1. **更正取代**（``OBX-11=C``）：更正结果取代同（病理号, 蜡块号, 项目码）的旧值，
   并写一行审计（记旧值 → 新值），**不追加成第二条**。
2. **回报时间倒挂**：更正的时间比已有结果还早 —— 多半是回报延迟或时钟不一致。
   这时**不取代**，较新的原值留着，更正本身扣下来等人确认。悄悄用旧值盖掉新值，
   是这份产品最不能犯的错。
3. **无法获得 / 空值**（``OBX-11=X`` 或值为空）：已回报但没有可写进报告的值，
   一律**不并入草稿**，进待人工处理清单。报告里写一行空项，医师会以为这项没做；
   空值更危险 —— 它跟「无表达」长得一样，一个空值可能被读成阴性结论。

「有值的结果之间比新旧，没值的结果一律扣下来等人处理」—— 这条口径让任何一条
结果都只有一个去向：进草稿、进待处理清单，或被更新的同项结果取代。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..audit.ledger import (
    CATEGORY_INGEST,
    EVENT_CORRECTION_APPLIED,
    EVENT_CORRECTION_INVERTED,
    EVENT_DUPLICATE,
    record_audit,
)
from ..db import Database
from ..models import ResultEntry, ResultStatus
from ..timefmt import now_text

__all__ = [
    "CorrectionEvent",
    "CorrectionOutcome",
    "IDEMPOTENCY_KEY_PARTS",
    "IdempotencyLedger",
    "IngestCounters",
    "InvertedCorrection",
    "apply_corrections",
    "build_idempotency_key",
    "build_idempotency_key_from_parts",
    "idempotency_key_parts",
    "resolve_corrections",
    "unique_entries",
]

#: 键各部分之间的分隔符。用 ASCII 单元分隔符，避免值里含 ``|`` 时撞键。
_KEY_SEP = "\x1f"

#: 幂等键的字段顺序。这五项缺一不可：少一项，两条不同的回报就会撞成同一条。
IDEMPOTENCY_KEY_PARTS: tuple[str, ...] = (
    "accession_no",
    "block_no",
    "item_code",
    "observed_at",
    "result_status",
)


def idempotency_key_parts(entry: ResultEntry) -> tuple[str, ...]:
    """取出幂等键的五段原值（未拼键时用，便于核对与排查）。"""
    return (
        entry.accession_no,
        entry.block_no or "",
        entry.item_code,
        entry.observed_at or "",
        entry.status.value,
    )


def build_idempotency_key_from_parts(
    accession_no: str | None,
    block_no: str | None,
    item_code: str | None,
    observed_at: str | None,
    result_status: str | None,
) -> str:
    """按五段原值拼出幂等键。

    **这是幂等键的唯一拼装入口**：入站层用 :func:`build_idempotency_key`（喂
    :class:`ResultEntry`），召回层的去重段用本函数（喂候选上的五段原值）。
    两层共用一把钥匙 —— 同一条结果在入库层算一次，在召回层也只算一次；
    两层若各拼各的，口径一旦漂移，同一条结果就会在一层被去重、在另一层被
    重复计数，核对时对不上。

    五段取**原值**，不做归一：蜡块号写法归一（``B-3`` 与 ``B03`` 归成一块）
    属于匹配层的职责。在这里归一会把两条来源不同的回报判成同一条，把「重复
    投递」这条计数吃掉 —— 宁可少合并，也不要盖掉一次真实的重复投递。
    """
    return _KEY_SEP.join(
        (
            accession_no or "",
            block_no or "",
            item_code or "",
            observed_at or "",
            result_status or "",
        )
    )


def build_idempotency_key(entry: ResultEntry) -> str:
    """算出这条结果的幂等键。"""
    return build_idempotency_key_from_parts(*idempotency_key_parts(entry))


def _time_rank(value: str | None) -> tuple[int, str]:
    """把观察时间转成可比较的排序键。

    HL7 时间戳（``YYYYMMDDHHMMSS``）按数值比较；认不出来的格式退回字符串比较，
    绝不猜 —— 猜错时间先后，更正就会取代掉更新的原值。
    """
    raw = (value or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        head = raw[:14].ljust(14, "0")
        if head.isdigit():
            return (int(head), raw)
    return (-1, raw)


def _target_key(entry: ResultEntry) -> tuple[str, str, str]:
    """同一条检查项的归属：病理号 + 蜡块号 + 项目码。

    蜡块号为空时也照常成键 —— 空蜡块号的结果与空蜡块号的结果同属一项，
    但它不会去认领有蜡块号的块。
    """
    return (entry.accession_no, entry.block_no or "", entry.item_code)


@dataclass
class IngestCounters:
    """入站计数。分清「首次接收」「重复投递」「被取代」「扣下等人」,四类不能混。"""

    accepted: int = 0
    duplicate: int = 0
    superseded: int = 0
    rejected: int = 0
    deferred: int = 0
    not_obtained: int = 0
    empty_value: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    @property
    def withheld(self) -> int:
        """已回报但并不进草稿的条数（无法获得 + 空值）。"""
        return self.not_obtained + self.empty_value

    def as_dict(self) -> dict[str, object]:
        data = {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "superseded": self.superseded,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "not_obtained": self.not_obtained,
            "empty_value": self.empty_value,
            "withheld": self.withheld,
        }
        data.update(self.extra)
        return data


class IdempotencyLedger:
    """去重账本。

    两种用法：

    - **不给 database**：账本只在本次进程内有效（批处理重放默认）。同一批结果
      重跑产出一致的草稿，方便排查与复现。
    - **给 database**：账本落 SQLite，跨进程、跨批次有效（守护进程用）。
      重复投递只落一条并计 ``duplicate``。
    """

    def __init__(self, database: Database | None = None) -> None:
        self._db = database
        self._seen: set[str] = set()
        self.counters = IngestCounters()

    @property
    def durable(self) -> bool:
        """账本是否跨进程有效。"""
        return self._db is not None

    def accept(self, entry: ResultEntry, *, source_file: str | None = None) -> bool:
        """接收一条结果。

        返回 ``True`` 表示首次接收，``False`` 表示重复投递（不落第二条）。
        """
        key = build_idempotency_key(entry)
        fresh = self._record(key, entry, source_file)
        if fresh:
            self.counters.accepted += 1
        else:
            self.counters.duplicate += 1
            self._audit_duplicate(key, entry, source_file)
        return fresh

    def _audit_duplicate(
        self, key: str, entry: ResultEntry, source_file: str | None
    ) -> None:
        """重复投递单独记一行。

        「又投了一遍」既不是「对上了一条」，也不是失败 —— 它有自己的计数器。
        混进对齐依据行数会让「这一份报告对上了几条」失真，所以另记一行。
        """
        if self._db is None:
            return
        record_audit(
            self._db,
            category=CATEGORY_INGEST,
            event=EVENT_DUPLICATE,
            accession_no=entry.accession_no,
            detail={
                "idempotency_key": key,
                "item_code": entry.item_code,
                "block_no": entry.block_no,
                "observed_at": entry.observed_at,
                "result_status": getattr(entry.status, "value", entry.status),
                "source_file": source_file,
            },
        )

    def seen(self, key: str) -> bool:
        return key in self._seen

    def _record(self, key: str, entry: ResultEntry, source_file: str | None) -> bool:
        if self._db is not None:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO idempotency_keys"
                "(key, accession_no, block_no, item_code, observed_at, result_status,"
                " source_file, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    entry.accession_no,
                    entry.block_no,
                    entry.item_code,
                    entry.observed_at or "",
                    entry.status.value,
                    source_file or entry.source_file,
                    now_text(),
                ),
            )
            fresh = cursor.rowcount > 0
        else:
            fresh = key not in self._seen
        self._seen.add(key)
        return fresh


@dataclass(frozen=True)
class CorrectionEvent:
    """一次更正取代的留痕：旧值 → 新值。"""

    accession_no: str
    item_code: str
    block_no: str | None
    old_value: str | None
    new_value: str | None
    old_observed_at: str | None
    new_observed_at: str | None
    old_status: str
    new_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "block_no": self.block_no,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_observed_at": self.old_observed_at,
            "new_observed_at": self.new_observed_at,
            "old_status": self.old_status,
            "new_status": self.new_status,
        }


@dataclass(frozen=True)
class InvertedCorrection:
    """一条**没生效**的更正：它的回报时间比已有结果还早。

    不取代 —— 较新的原值留着，更正本身扣下来等人确认。
    """

    entry: ResultEntry
    kept: ResultEntry

    def to_dict(self) -> dict[str, object]:
        return {
            "accession_no": self.entry.accession_no,
            "item_code": self.entry.item_code,
            "block_no": self.entry.block_no,
            "correction_value": self.entry.value,
            "correction_observed_at": self.entry.observed_at,
            "kept_value": self.kept.value,
            "kept_observed_at": self.kept.observed_at,
        }


@dataclass
class CorrectionOutcome:
    """一轮结果状态取舍的产出。

    - ``kept``：能进草稿的结果（同块同项只留一条）；
    - ``withheld``：已回报但没有可写进报告的值（无法获得 / 空值），进待人工处理清单；
    - ``inverted``：没生效的更正（回报时间倒挂），同样进待人工处理清单；
    - ``superseded``：被更新的同项结果取代掉的条数；
    - ``applied``：更正取代的留痕，逐条记旧值 → 新值。
    """

    kept: list[ResultEntry] = field(default_factory=list)
    withheld: list[ResultEntry] = field(default_factory=list)
    inverted: list[InvertedCorrection] = field(default_factory=list)
    applied: list[CorrectionEvent] = field(default_factory=list)
    superseded: int = 0

    @property
    def total(self) -> int:
        """本轮收到的条数 = 留下的 + 扣下的 + 倒挂的 + 被取代的。"""
        return len(self.kept) + len(self.withheld) + len(self.inverted) + self.superseded


def _pick_winner(
    entries: Sequence[ResultEntry], *, prefer_authority: bool
) -> ResultEntry | None:
    """在一组同项结果里挑出留用的那一条。

    先比观察时间（新的留用）；时间一样时：

    - 普通结果比**权威度**（终报压过初步）—— ``prefer_authority=True``；
    - 更正之间比**到达顺序**（后到的更正覆盖先到的更正）—— ``prefer_authority=False``。
    """
    if not entries:
        return None
    indexed = list(enumerate(entries))
    if prefer_authority:
        key = lambda pair: (  # noqa: E731 - 取值键，写成 lambda 更直白
            _time_rank(pair[1].observed_at),
            pair[1].status.authority,
            -pair[0],
        )
    else:
        key = lambda pair: (  # noqa: E731
            _time_rank(pair[1].observed_at),
            pair[0],
        )
    return max(indexed, key=key)[1]


def _audit_correction(
    database: Database | None, event: str, accession_no: str, detail: dict[str, object]
) -> None:
    """把一条更正留痕写进审计流水（给了状态库才写）。"""
    if database is None:
        return
    record_audit(
        database,
        category=CATEGORY_INGEST,
        event=event,
        accession_no=accession_no,
        detail=detail,
    )


def resolve_corrections(
    entries: Iterable[ResultEntry], *, database: Database | None = None
) -> CorrectionOutcome:
    """按结果状态取舍：谁留下、谁被取代、谁扣下来等人。

    规则（同一组「病理号 + 蜡块号 + 项目码」内）：

    1. **有值的结果**（初步 / 终报 / 更正，且值不为空）之间比新旧：留最新的那条，
       其余被取代；观察时间相同时，终报压过初步。
    2. **更正**（``OBX-11=C``）压过同项的普通结果，并留一行审计（旧值 → 新值）。
    3. **回报时间倒挂**：更正的时间比同项最新的普通结果还早 → 不生效，
       较新的原值留着，更正本身进 ``inverted``（调用方转待人工处理清单）。
    4. **没有值的结果**（无法获得 ``X`` / 空值）不参与取舍，一律进 ``withheld``
       （调用方转待人工处理清单）。空值永远不盖掉一个有值的真结果。
    """
    items = list(entries)
    outcome = CorrectionOutcome()

    groups: dict[tuple[str, str, str], list[ResultEntry]] = {}
    for entry in items:
        groups.setdefault(_target_key(entry), []).append(entry)

    for group in groups.values():
        valued = [entry for entry in group if entry.mergeable]
        valueless = [entry for entry in group if not entry.mergeable]
        outcome.withheld.extend(valueless)

        if not valued:
            continue

        corrections = [entry for entry in valued if entry.status is ResultStatus.CORRECTION]
        others = [entry for entry in valued if entry.status is not ResultStatus.CORRECTION]

        best_other = _pick_winner(others, prefer_authority=True)
        best_correction = _pick_winner(corrections, prefer_authority=False)

        if best_correction is not None and best_other is not None:
            if _time_rank(best_correction.observed_at) < _time_rank(best_other.observed_at):
                # 回报时间倒挂：更正比已有结果还早，不让它取代。
                # 同项的更正**一条都不生效**，全部扣下来等人确认。
                outcome.kept.append(best_other)
                outcome.superseded += len(others) - 1
                for correction in corrections:
                    outcome.inverted.append(
                        InvertedCorrection(entry=correction, kept=best_other)
                    )
                    _audit_correction(
                        database,
                        EVENT_CORRECTION_INVERTED,
                        correction.accession_no,
                        {
                            "accession_no": correction.accession_no,
                            "item_code": correction.item_code,
                            "block_no": correction.block_no,
                            "correction_value": correction.value,
                            "correction_observed_at": correction.observed_at,
                            "kept_value": best_other.value,
                            "kept_observed_at": best_other.observed_at,
                            "kept_status": best_other.status.value,
                        },
                    )
                continue

        if best_correction is not None:
            outcome.kept.append(best_correction)
            outcome.superseded += len(valued) - 1
            previous = best_other
            event = CorrectionEvent(
                accession_no=best_correction.accession_no,
                item_code=best_correction.item_code,
                block_no=best_correction.block_no,
                old_value=previous.value if previous is not None else None,
                new_value=best_correction.value,
                old_observed_at=previous.observed_at if previous is not None else None,
                new_observed_at=best_correction.observed_at,
                old_status=previous.status.value if previous is not None else "",
                new_status=best_correction.status.value,
            )
            outcome.applied.append(event)
            _audit_correction(
                database,
                EVENT_CORRECTION_APPLIED,
                event.accession_no,
                event.to_dict(),
            )
            continue

        if best_other is not None:
            outcome.kept.append(best_other)
            outcome.superseded += len(valued) - 1

    # 保持原有顺序：结果条目按收到顺序进草稿，核对时不用再排序。
    order = {id(entry): index for index, entry in enumerate(items)}
    outcome.kept.sort(key=lambda entry: order[id(entry)])
    outcome.withheld.sort(key=lambda entry: order[id(entry)])
    return outcome


def apply_corrections(entries: Iterable[ResultEntry]) -> tuple[list[ResultEntry], int]:
    """更正取代（简化口径）：返回 ``(留下的结果, 被取代条数)``。

    需要知道「扣下了哪些」「倒挂了哪些」时用 :func:`resolve_corrections`。
    """
    outcome = resolve_corrections(entries)
    return outcome.kept, outcome.superseded


def unique_entries(entries: Sequence[ResultEntry]) -> list[ResultEntry]:
    """按幂等键去重，保留第一条（顺序稳定）。"""
    seen: set[str] = set()
    result: list[ResultEntry] = []
    for entry in entries:
        key = build_idempotency_key(entry)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result
