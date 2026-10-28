"""续跑幂等台账：同一组补齐值只生效一次。

**幂等键** = 挂起行 ID + 补齐字段 + 补齐值（见
:func:`patho_supplement.pending.resume.resolve_key_for`）。键一样 ⇒ 是同一次
补齐 ⇒ 只能有一份草稿。

台账按键记两件事：

- **提交次数** —— 同一组补齐值被提交了几次。第一次是「这一次补齐」，第二次
  才是「重复提交」（技术员点两下「补齐并续跑」）。只提交过一次就重跑，那是
  手工重跑 —— 该拦下（报「已了结」），而不是拿旧结论顶上去。
- **生效结论** —— 这一组补齐值并进了哪一版草稿（草稿行 ID 与版本号）。
  重复提交时直接返回这份结论，不重算、不重并。

**为什么要有台账，而不是每次去翻审计流水**：并发补齐时，两次提交可能都发生
在续跑之前，光看「了结时的提交次数」分不清「重复提交」与「手工重跑」——
提交次数那一栏恰好会被并发的另一次提交顶高。台账按键记次数，判据与并发无关。

**台账不是结论的副本**：真正的结论仍以草稿版本链与审计流水为准，台账只存
「哪个键生效过、生效到哪一版」这一层索引。台账坏了（行读不出来）不影响
草稿 —— 删掉重来即可，最坏情况退回到「重新提交一次」的常规路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..timefmt import now_text

__all__ = [
    "RESOLVE_OUTCOME_MERGED",
    "ResolveKeyLedger",
    "ResolveKeyRecord",
]

#: 这一组补齐值已并入草稿。
RESOLVE_OUTCOME_MERGED = "merged"


@dataclass(frozen=True)
class ResolveKeyRecord:
    """幂等台账里的一行。"""

    resolve_key: str
    pending_id: int
    accession_no: str
    item_code: str
    #: 这一组补齐值被提交了几次。
    submissions: int
    #: 生效结论（``merged``）；还没生效时为空串。
    status: str
    draft_id: int | None = None
    draft_version: int | None = None
    detail: dict[str, Any] | None = None
    created_at: str = ""

    @property
    def applied(self) -> bool:
        """这一组补齐值是否已经生效过。"""
        return bool(self.status)

    @property
    def duplicate(self) -> bool:
        """是否是**重复提交**（提交次数 ≥ 2）。

        只提交过一次就重跑属于手工重跑，不是重复提交 —— 两者处置不同。
        """
        return self.submissions >= 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolve_key": self.resolve_key,
            "pending_id": self.pending_id,
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "submissions": self.submissions,
            "status": self.status,
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "created_at": self.created_at,
        }


class ResolveKeyLedger:
    """续跑幂等台账的读写门面。"""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------ 写

    def record_submission(
        self,
        resolve_key: str,
        *,
        pending_id: int,
        accession_no: str,
        item_code: str,
    ) -> ResolveKeyRecord:
        """记一次补齐提交（同一键重复提交则次数 +1）。

        提交次数是「重复提交」的判据 —— 第一次提交是这次补齐，第二次才是
        重复。值没变也算一次提交（技术员点两下按钮就是提交了两次）。
        """
        self._db.execute(
            "INSERT INTO pending_resolve_keys(resolve_key, pending_id, accession_no,"
            " item_code, submissions, status, draft_id, draft_version, detail_json,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 1, '', NULL, NULL, NULL, ?, ?)"
            " ON CONFLICT(resolve_key) DO UPDATE SET"
            " submissions = pending_resolve_keys.submissions + 1,"
            " updated_at = excluded.updated_at",
            (
                resolve_key,
                pending_id,
                accession_no,
                item_code,
                now_text(),
                now_text(),
            ),
        )
        record = self.find(resolve_key)
        if record is None:  # pragma: no cover - 刚写进去的一行读不出来
            return ResolveKeyRecord(
                resolve_key=resolve_key,
                pending_id=pending_id,
                accession_no=accession_no,
                item_code=item_code,
                submissions=1,
                status="",
            )
        return record

    def record_outcome(
        self,
        resolve_key: str,
        *,
        pending_id: int,
        accession_no: str = "",
        item_code: str = "",
        status: str = RESOLVE_OUTCOME_MERGED,
        draft_id: int | None = None,
        draft_version: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """记下这一组补齐值生效到了哪里（并入草稿的第几版）。

        没有提交记录时也会补一行（``submissions`` 记 1）—— 直接调续跑、没走
        补齐入口的情形下，台账里也要有这把键。
        """
        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
        stamp = now_text()
        self._db.execute(
            "INSERT INTO pending_resolve_keys(resolve_key, pending_id, accession_no,"
            " item_code, submissions, status, draft_id, draft_version, detail_json,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(resolve_key) DO UPDATE SET"
            " status = excluded.status, draft_id = excluded.draft_id,"
            " draft_version = excluded.draft_version, detail_json = excluded.detail_json,"
            " updated_at = excluded.updated_at",
            (
                resolve_key,
                pending_id,
                accession_no,
                item_code,
                status,
                draft_id,
                draft_version,
                payload,
                stamp,
                stamp,
            ),
        )

    # ------------------------------------------------------------ 读

    def find(self, resolve_key: str) -> ResolveKeyRecord | None:
        """按键取台账行；没有这一行给 ``None``。"""
        row = self._db.query_one(
            "SELECT resolve_key, pending_id, accession_no, item_code, submissions,"
            " status, draft_id, draft_version, detail_json, created_at"
            " FROM pending_resolve_keys WHERE resolve_key = ?",
            (resolve_key,),
        )
        if row is None:
            return None
        detail: dict[str, Any] | None = None
        raw = row["detail_json"]
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                detail = parsed
        return ResolveKeyRecord(
            resolve_key=row["resolve_key"],
            pending_id=int(row["pending_id"] or 0),
            accession_no=row["accession_no"] or "",
            item_code=row["item_code"] or "",
            submissions=int(row["submissions"] or 0),
            status=row["status"] or "",
            draft_id=int(row["draft_id"]) if row["draft_id"] is not None else None,
            draft_version=(
                int(row["draft_version"]) if row["draft_version"] is not None else None
            ),
            detail=detail,
            created_at=row["created_at"] or "",
        )

    def submissions(self, resolve_key: str) -> int:
        """这一组补齐值被提交了几次；没见过这一键给 0。"""
        record = self.find(resolve_key)
        return record.submissions if record else 0
