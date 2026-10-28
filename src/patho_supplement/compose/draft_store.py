"""草稿的落库口径。

草稿有两个来源：守护进程按批次成稿、续跑按人工补齐成稿。两条路写的是同一张
``drafts`` 表、同一条版本链，因此**写入口径只写在这里** —— 版本号怎么算、
条目怎么拆、状态怎么推进，各处不再各写一份 SQL。

版本链的规矩：

- **追加不覆盖**：每次成稿追加一版，旧版本仍在库里可回查（医师签发后被追问
  「上一版写了什么」，要答得上来）；
- **版本号从基线返回的基础版本号派生并递增**：基线是 ``base-v3`` → 链的起点
  是第 4 版；基线没给版本号就按本地链从第 1 版起；链已存在就只增不改；
- **状态推进有顺序**：待生成 → 已生成 → 已回写 → 已签发。签发由院内系统回写，
  本系统只读。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..db import Database
from ..models import Alignment, Draft, DraftEntry, DraftState, DraftVersion
from ..timefmt import now_text
from .state_machine import transition_draft_state

__all__ = [
    "DraftVersionConflict",
    "derive_base_version_number",
    "draft_entry_from_row",
    "list_draft_versions",
    "load_draft_body",
    "load_draft_entries",
    "load_draft_version",
    "load_draft_version_entries",
    "load_latest_draft",
    "mark_draft_written_back",
    "next_draft_version",
    "persist_draft",
]


#: 版本号里的数字段。基线返回的基础版本号长这样：「base-v3」「v12」「3」。
_BASE_VERSION_DIGITS = re.compile(r"\d+")


class DraftVersionConflict(RuntimeError):
    """同一份报告的同一版本号已经存在。

    版本链**只增不覆盖** —— 已经发出去的版本号不能被另一份草稿占掉，否则
    医师按旧号回查会查到别人的内容。撞号说明编号口径出了问题，宁可报错，
    也不能悄悄覆盖。
    """

    def __init__(self, *, accession_no: str, version: int) -> None:
        self.accession_no = accession_no
        self.version = version
        super().__init__(
            f"病理号 {accession_no} 的草稿 v{version} 已存在，版本链只追加不覆盖"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession_no": self.accession_no,
            "version": self.version,
            "reason": "版本号已存在，版本链只追加不覆盖",
        }


def derive_base_version_number(base_version: str | None) -> int | None:
    """从基线返回的基础版本号里取出版本序号。

    「base-v3」「v3」「3」「draft-v12」分别取 3、3、3、12 —— 取**最后一串
    数字**：版本号末尾的数字才是序号（前面的可能是年份或批次号）。

    基线没给版本号、或给的版本号里没有数字，就返回 ``None`` —— 这时按本地
    链递增，不猜。
    """
    if not base_version:
        return None
    groups = _BASE_VERSION_DIGITS.findall(base_version)
    if not groups:
        return None
    return int(groups[-1])


def next_draft_version(
    database: Database,
    accession_no: str,
    base_version: str | None = None,
) -> int:
    """这份报告的下一个草稿版本号。

    版本链的规矩：

    - **从基线返回的基础版本号派生并递增**：基线说这份报告现在是第 3 版
      （``base-v3``），补充报告接着它往下写 → 链的起点是第 4 版（3 + 1）。
      医师在院内系统里看到的版本序列因此是连续的，不会出现「报告 v3、草稿
      v1」两头对不上号；
    - **基线版本缺失时按本地链递增**：老系统不返回版本号，链从第 1 版起；
    - **链已存在就只增不改**：库里已有几版就续第几版。发出去的号不能改 ——
      医师那边按旧号回查，改了号就查不到东西了。
    """
    local_max = int(
        database.query_value(
            "SELECT MAX(version) FROM drafts WHERE accession_no = ?",
            (accession_no,),
            default=0,
        )
        or 0
    )
    if local_max >= 1:
        return local_max + 1
    base_number = derive_base_version_number(base_version)
    if base_number is None:
        return 1
    return base_number + 1


def persist_draft(database: Database, draft: Draft) -> int:
    """草稿落库（状态：待生成 → 已生成，等回写），返回草稿行 ID。

    草稿正文存全文（``body``），条目另存结构化行 —— 正文给医师看，条目给
    续跑重建与审计用。

    **版本链只增不覆盖**：同一份报告的同一版本号已经在库里 → 抛
    :class:`DraftVersionConflict`，不覆盖旧版本。

    **状态推进走状态机**（见 :mod:`patho_supplement.compose.state_machine`）：
    先以「待生成」落库，再由状态机推进到「已生成」，这一跳会留下一行状态
    审计流水。直接 INSERT 成「已生成」就绕过了状态机 —— 那样一来，非法迁移
    就再也拦不住了。

    草稿行、条目行与状态迁移在**同一个事务**里落库：中途失败不留半截草稿
    （库里有一份没有条目的草稿，比没有草稿更难查）。
    """
    existing = database.query_one(
        "SELECT id FROM drafts WHERE accession_no = ? AND version = ?",
        (draft.accession_no, draft.version),
    )
    if existing is not None:
        raise DraftVersionConflict(
            accession_no=draft.accession_no,
            version=draft.version,
        )
    content_hash = hashlib.sha256(draft.body.encode("utf-8")).hexdigest()
    with database.transaction():
        cursor = database.execute(
            "INSERT INTO drafts(accession_no, version, base_version, state, entry_count,"
            " content_hash, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.accession_no,
                draft.version,
                draft.base_version,
                DraftState.PENDING.value,
                len(draft.entries),
                content_hash,
                draft.body,
                draft.created_at or now_text(),
            ),
        )
        draft_id = int(cursor.lastrowid)
        for entry in draft.entries:
            database.execute(
                "INSERT INTO draft_entries(draft_id, region_seq, region_desc, block_no,"
                " item_code, item_name, value, source_system, observed_at, placeholder,"
                " rationale_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    entry.region_seq,
                    entry.region_desc,
                    entry.block_no,
                    entry.item_code,
                    entry.item_name,
                    entry.value,
                    entry.source_system,
                    entry.observed_at,
                    1 if entry.placeholder else 0,
                    json.dumps(entry.alignment.to_dict(), ensure_ascii=False)
                    if entry.alignment
                    else None,
                ),
            )
        transition_draft_state(
            database,
            draft_id,
            DraftState.GENERATED,
            accession_no=draft.accession_no,
            note="成稿",
        )
    return draft_id


def mark_draft_written_back(database: Database, draft_id: int, receipt: Any) -> None:
    """回写成功后推进草稿状态（已生成 → 已回写）并记下回执。

    状态推进走状态机；**已经是「已回写」的重复回写**不再迁移（状态没变，
    就不是一次状态变更），只补记回执 —— 退避重试队列重发同一份草稿时走的是
    这条路径，不该被当成非法迁移拒掉。
    """
    row = database.query_one("SELECT state FROM drafts WHERE id = ?", (draft_id,))
    current = DraftState(row["state"]) if row else DraftState.PENDING
    if current is not DraftState.WRITTEN_BACK:
        transition_draft_state(
            database,
            draft_id,
            DraftState.WRITTEN_BACK,
            note="回写成功",
        )
    database.execute(
        "UPDATE drafts SET written_back_at = ?, receipt = ? WHERE id = ?",
        (
            now_text(),
            json.dumps(receipt.to_dict(), ensure_ascii=False),
            draft_id,
        ),
    )


def load_latest_draft(database: Database, accession_no: str) -> Any | None:
    """这份报告的**最新一版**草稿行；没有就给 ``None``。"""
    return database.query_one(
        "SELECT * FROM drafts WHERE accession_no = ? ORDER BY version DESC LIMIT 1",
        (accession_no,),
    )


def list_draft_versions(database: Database, accession_no: str) -> list[DraftVersion]:
    """版本链一览（按版本号升序）。

    每一版都带着条目数、生成时间、基础版本号（这一版是基于病理系统的哪一版
    写的）与状态 —— 医师问「上一版写了什么」，这一串就是答案。
    """
    rows = database.query_all(
        "SELECT * FROM drafts WHERE accession_no = ? ORDER BY version",
        (accession_no,),
    )
    return [DraftVersion.from_row(row) for row in rows]


def load_draft_version(
    database: Database,
    accession_no: str,
    version: int,
) -> DraftVersion | None:
    """按版本号回查某一版草稿；查不到给 ``None``。"""
    row = database.query_one(
        "SELECT * FROM drafts WHERE accession_no = ? AND version = ?",
        (accession_no, version),
    )
    return DraftVersion.from_row(row) if row is not None else None


def load_draft_version_entries(
    database: Database,
    accession_no: str,
    version: int,
) -> list[DraftEntry]:
    """某一版的**来源条目**（按落库顺序回放）。

    这一版收了哪几项、每项的值、来源系统与回报时间、凭什么对上号（依据），
    逐条可查 —— 回查不只看正文文本，还能把条目重新拼回去。
    """
    row = database.query_one(
        "SELECT id FROM drafts WHERE accession_no = ? AND version = ?",
        (accession_no, version),
    )
    if row is None:
        return []
    return [
        draft_entry_from_row(entry_row)
        for entry_row in database.query_all(
            "SELECT * FROM draft_entries WHERE draft_id = ? ORDER BY id",
            (int(row["id"]),),
        )
    ]


def draft_entry_from_row(row: Any) -> DraftEntry:
    """条目行 → :class:`DraftEntry`（对齐依据一并还原）。"""
    rationale = row["rationale_json"]
    alignment = Alignment.from_dict(json.loads(rationale)) if rationale else None
    return DraftEntry(
        item_code=row["item_code"],
        item_name=row["item_name"],
        value=row["value"],
        region_seq=row["region_seq"],
        region_desc=row["region_desc"],
        block_no=row["block_no"],
        source_system=row["source_system"],
        observed_at=row["observed_at"],
        alignment=alignment,
        placeholder=bool(row["placeholder"]),
    )


def load_draft_entries(database: Database, draft_id: int) -> list[Any]:
    """某一版草稿的条目行（按落库顺序）。"""
    return database.query_all(
        "SELECT * FROM draft_entries WHERE draft_id = ? ORDER BY id",
        (draft_id,),
    )


def load_draft_body(database: Database, draft_id: int) -> str:
    """某一版草稿的正文（给技术员看的那份文本）。"""
    return str(database.query_value(
        "SELECT body FROM drafts WHERE id = ?", (draft_id,), default=""
    ) or "")
