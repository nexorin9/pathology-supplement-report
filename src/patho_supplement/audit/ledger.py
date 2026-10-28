"""审计流水写入。

审计行不是可选项：一条结果为什么进草稿、为什么被挂起、哪条报文被拒收，
都得留下可查的痕迹。因此写入口径统一在这里，别处不再各写一份 SQL。

口径：

- **一行 = 一次动作**：时间、分类、事件、病理号、明细 JSON；
- 明细序列化失败用 ``str`` 兜底 —— 审计行绝不能因为明细里混进了奇怪对象就写不进去；
- 明细默认**不含**患者可识别字段（姓名 / 身份证 / 联系方式），只留病理号、项目码、
  部位与蜡块号这些业务标识；
- 审计**只记不判**：写一行不代表这条结果已对齐，只代表这一步发生过。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..timefmt import now_text

__all__ = [
    "AuditCounts",
    "AuditRow",
    "CATEGORY_ALIGN",
    "CATEGORY_INGEST",
    "CATEGORY_NOTIFY",
    "CATEGORY_PENDING",
    "CATEGORY_RUNTIME",
    "CATEGORY_STATE",
    "CATEGORY_WRITEBACK",
    "EVENT_ALIGNMENT_PENDING",
    "EVENT_ALIGNMENT_RATIONALE",
    "EVENT_CORRECTION_APPLIED",
    "EVENT_CORRECTION_INVERTED",
    "EVENT_DAEMON_LOCK_CONFLICT",
    "EVENT_DAEMON_RECOVERED",
    "EVENT_DEFERRED",
    "EVENT_DUPLICATE",
    "EVENT_ENCODING_DETECTED",
    "EVENT_LATE_ARRIVAL_AMBIGUOUS",
    "EVENT_LATE_ARRIVAL_MERGED",
    "EVENT_LATE_ARRIVAL_REQUEUED",
    "EVENT_MALFORMED",
    "EVENT_NOTIFY_DEGRADED",
    "EVENT_NOTIFY_FAILED",
    "EVENT_NOTIFY_ROUTED",
    "EVENT_NOTIFY_SENT",
    "EVENT_NOTIFY_SKIPPED",
    "EVENT_ORDER_ACCEPTED",
    "EVENT_ORDER_REJECTED",
    "EVENT_PENDING_FILLED",
    "EVENT_PENDING_INTEGRITY_REJECTED",
    "EVENT_PENDING_LOCK_CONFLICT",
    "EVENT_PENDING_LOCK_TAKEN_OVER",
    "EVENT_PENDING_RESOLVED",
    "EVENT_PENDING_RESUME_FAILED",
    "EVENT_REJECTED",
    "EVENT_STATE_TRANSITION",
    "EVENT_STATE_TRANSITION_REJECTED",
    "EVENT_WRITEBACK_ESCALATED",
    "EVENT_WRITEBACK_ACCEPTED",
    "EVENT_WRITEBACK_CONFLICT",
    "EVENT_WRITEBACK_CONFLICT_RESOLVED",
    "EVENT_WRITEBACK_QUEUED",
    "EVENT_WRITEBACK_RETRY_FAILED",
    "EVENT_WRITEBACK_RETRY_SUCCEEDED",
    "audit_counts",
    "count_audit_rows",
    "read_audit_rows",
    "record_audit",
    "record_encoding_audit",
]

#: 审计分类：按「哪一步」分。
CATEGORY_INGEST = "ingest"
CATEGORY_ALIGN = "align"
CATEGORY_STATE = "state"
CATEGORY_WRITEBACK = "writeback"
CATEGORY_NOTIFY = "notify"
#: 挂起项这条线（补齐、续跑、了结）。
CATEGORY_PENDING = "pending"
#: 守护进程自身的运行事件（启动恢复、单实例锁占用）。
CATEGORY_RUNTIME = "runtime"

#: 审计事件：入站侧用到的事件名。
EVENT_REJECTED = "rejected"
EVENT_DEFERRED = "deferred"
EVENT_ORDER_REJECTED = "order_rejected"
EVENT_ORDER_ACCEPTED = "order_accepted"
#: 结果侧报文畸形（缺 MSH-9 / MSH-12、段结构不可解析）—— 与「不认识的报文类型」分开计。
EVENT_MALFORMED = "malformed"
#: 更正取代了旧值（明细里记旧值 → 新值）。
EVENT_CORRECTION_APPLIED = "correction_applied"
#: 更正没生效：回报时间比已有结果还早，倒挂，等人确认。
EVENT_CORRECTION_INVERTED = "correction_inverted"
#: 入站文件用了哪种解码方式（utf-8 / gbk / mixed）—— 同一批文件编码不一致是
#: 常事，事后复盘「这份报文当初是怎么读进来的」要查这一行。
EVENT_ENCODING_DETECTED = "encoding_detected"
#: 重复投递：同一条结果再投了一次（幂等键命中），**不重复入账**。
#: 这一类**单独计数**：它既不是对齐依据，也不是失败 —— 只是「又来了一遍」。
#: 混进对齐依据行数会让「对上了几条」的口径失真，所以另记一行、另计一个数。
EVENT_DUPLICATE = "duplicate"
#: 一条结果的对齐依据（已并入草稿）：命中方式 / 得分 / 召回键 / 来源系统 /
#: 回报时间 / 置信等级。内审核对与飞检语境下，这一行就是「凭什么」。
EVENT_ALIGNMENT_RATIONALE = "alignment_rationale"
#: 一条结果没对上（挂起）：依据同样留一行，但**没有置信等级**。
EVENT_ALIGNMENT_PENDING = "alignment_pending"
#: 续跑前的完整性校验没过：快照缺节点或内容不可解析，这一轮没续跑。
#: 挂起行原样留在库里 —— 记这一行是为了让「为什么没续跑」可查。
EVENT_PENDING_INTEGRITY_REJECTED = "pending_integrity_rejected"
#: 人工补齐了一个字段（明细里记补了哪个字段、补成什么值）。
EVENT_PENDING_FILLED = "pending_filled"
#: 补齐后已并入草稿，这条挂起项了结（明细里记新草稿的版本号与行 ID）。
EVENT_PENDING_RESOLVED = "pending_resolved"
#: 补齐后**仍然对不上**：挂起行原地重拍，尝试次数 +1，继续等人。
EVENT_PENDING_RESUME_FAILED = "pending_resume_failed"
#: 抢行级互斥锁没抢到：这一行正在被别的人补齐续跑（明细里记占用者与占用时间）。
#: 记这一行是为了事后能答出「当时是不是有人同时在处理这一行」。
EVENT_PENDING_LOCK_CONFLICT = "pending_lock_conflict"
#: 接管了一把过期锁：上一个持锁的进程没释放（崩了），锁超过有效期后被接管。
EVENT_PENDING_LOCK_TAKEN_OVER = "pending_lock_taken_over"
#: 迟到结果自动接回挂起项：这条挂起行被重新入队（尝试次数 +1、快照时间前移）。
EVENT_LATE_ARRIVAL_REQUEUED = "late_arrival_requeued"
#: 迟到结果接回后对上了：这条挂起行了结，结果已并入草稿。
EVENT_LATE_ARRIVAL_MERGED = "late_arrival_merged"
#: 有多条挂起行都说得通 —— 不猜，退回常规路径等人判。
EVENT_LATE_ARRIVAL_AMBIGUOUS = "late_arrival_ambiguous"
#: 一次状态迁移被接受（草稿四态 / 检查项八态）。明细里记「从哪一态到哪一态」。
#: 审计行数 = 迁移尝试次数，这条口径靠它与下面的拒绝事件配平。
EVENT_STATE_TRANSITION = "state_transition"
#: 一次状态迁移被拒：走的不是合法路径。状态原样不动，只多这一行痕迹，
#: 用来回答「当时谁想把这个已签发的草稿改回已生成」。
EVENT_STATE_TRANSITION_REJECTED = "state_transition_rejected"
#: 回写失败，草稿进了回写队列（明细里记队列行 ID 与最后一次错误）。
EVENT_WRITEBACK_QUEUED = "queued"
#: 写回回执：草稿成功写回病理系统（明细里带回执：状态、接受版本号、草稿 ID）。
#: 一份草稿写回成功 = 一行 —— 口径上「草稿份数」就取这一类行的条数。
EVENT_WRITEBACK_ACCEPTED = "accepted"
#: 回写队列的一次重试失败（明细里记已试次数、下次重试时间、是否升级）。
EVENT_WRITEBACK_RETRY_FAILED = "writeback_retry_failed"
#: 回写队列的一次重试成功（明细里记回执）。
EVENT_WRITEBACK_RETRY_SUCCEEDED = "writeback_retry_succeeded"
#: 回写重试用尽 / 版本冲突：升级告警转人工处置。**草稿状态不推进**，
#: 队列行也不删 —— 这一行就是「这份草稿还没写回去」的凭证。
EVENT_WRITEBACK_ESCALATED = "writeback_escalated"
#: 版本冲突：本地草稿按老基础版本回写被对面拒收。**拒绝写回**（不覆盖对方
#: 版本），本地草稿保留（状态停在「已生成」），转人工处置。明细里带本地草稿
#: 版本号与基础版本号 —— 事后能答出「当时为什么没写回去」。
EVENT_WRITEBACK_CONFLICT = "writeback_conflict"
#: 冲突了结：这份报告的后续版本已写回成功，先前的冲突不再成立。
EVENT_WRITEBACK_CONFLICT_RESOLVED = "writeback_conflict_resolved"
# --- 通知这条线 ---------------------------------------------------------

#: 一条通知发出去了（明细里带通道名、收件人与业务事实）。
EVENT_NOTIFY_SENT = "notify_sent"
#: 一条通知没发出去（明细里带通道名、机器可读错误码与已试次数）。
#: **通知失败不牵连成稿** —— 草稿已经写回，这一行只是「没通知到」的痕迹。
EVENT_NOTIFY_FAILED = "notify_failed"
#: 通知降级：少了一条通道（如这家医院没开通企业微信），或通道发不出去。
#: 降级必须写进运行摘要 —— 「没发出去而没人知道」比「不发通知」更危险。
EVENT_NOTIFY_DEGRADED = "notify_degraded"
#: 一个通知通道都没配，本轮没发。**没发通知**这件事本身也要可查。
EVENT_NOTIFY_SKIPPED = "notify_skipped"
#: 收件人是怎么来的（明细里带收件人与路由依据：排班命中 / 科室默认收件人）。
#: 这一行是「通知发给了谁」的凭证 —— 事后复盘「这份报告该谁签」靠它。
EVENT_NOTIFY_ROUTED = "notify_routed"
# --- 守护进程运行这条线 ---------------------------------------------------

#: 启动恢复：进程重启后把上一轮留下的状态接回来（明细里带恢复出来的条数）。
#: 这一行回答「重启之后手上还剩多少活」—— 断电、进程被杀之后最该问的一句。
EVENT_DAEMON_RECOVERED = "daemon_recovered"
#: 单实例锁没抢到：已经有另一个守护进程在盯这个目录了，本进程不启动。
EVENT_DAEMON_LOCK_CONFLICT = "daemon_lock_conflict"


def record_encoding_audit(
    database: Database,
    *,
    file_name: str,
    encoding: str,
    fallback: bool = False,
    byte_length: int = 0,
) -> None:
    """把一份入站文件的解码方式写进审计流水。

    只记「怎么读进来的」这一件事，不含任何报文内容 —— 审计行本身不该变成
    另一份病历副本。
    """
    record_audit(
        database,
        category=CATEGORY_INGEST,
        event=EVENT_ENCODING_DETECTED,
        accession_no=None,
        detail={
            "file": file_name,
            "encoding": encoding,
            "fallback": fallback,
            "bytes": byte_length,
        },
    )


def record_audit(
    database: Database,
    *,
    category: str,
    event: str,
    accession_no: str | None = None,
    detail: Any = None,
) -> None:
    """写一行审计流水。

    ``detail`` 会被序列化成 JSON 存进 ``detail_json``；传 ``None`` 时存 ``null``，
    不影响其他字段落库。
    """
    database.execute(
        "INSERT INTO audit_log(ts, category, event, accession_no, detail_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            now_text(),
            category,
            event,
            accession_no,
            json.dumps(detail, ensure_ascii=False, default=str),
        ),
    )


# ------------------------------------------------------------------ 读取侧
#
# 写进去是为了事后能查；查出来是为了能带走 —— 医师复核、内审、飞检语境下，
# 「按某个病理号把这一份报告的来龙去脉导出来」是硬需求。


@dataclass(frozen=True)
class AuditRow:
    """审计流水的一行（明细已解析成字典）。

    ``detail`` 解析失败时给空字典 —— 一行读不出来的审计行不该让整份导出
    失败；那一行照样在导出里，只是说明列写「明细读不出来」。
    """

    id: int
    ts: str
    category: str
    event: str
    accession_no: str | None
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "category": self.category,
            "event": self.event,
            "accession_no": self.accession_no,
            "detail": self.detail,
        }


def _parse_detail(text: str | None) -> dict[str, Any]:
    """把明细 JSON 解析成字典；解析不出来给空字典。"""
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {"value": payload}


def read_audit_rows(
    database: Database,
    *,
    accession_no: str | None = None,
    category: str | None = None,
    event: str | None = None,
    limit: int | None = None,
) -> tuple[AuditRow, ...]:
    """读审计流水（按行号升序 —— 导出件的顺序就是事情发生的顺序）。

    ``accession_no`` 给空则读全部。畸形报文那一类行的 ``accession_no`` 是空的
    （报文根本没解析出来，不知道属于哪份报告），所以按病理号导出时天然不会
    把它们带进来 —— 这是对的：那类行属于「批次级」而不是「报告级」。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if accession_no is not None:
        clauses.append("accession_no = ?")
        params.append(accession_no)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if event is not None:
        clauses.append("event = ?")
        params.append(event)
    sql = "SELECT id, ts, category, event, accession_no, detail_json FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    return tuple(
        AuditRow(
            id=int(row["id"]),
            ts=row["ts"],
            category=row["category"],
            event=row["event"],
            accession_no=row["accession_no"],
            detail=_parse_detail(row["detail_json"]),
        )
        for row in database.query_all(sql, params)
    )


def count_audit_rows(
    database: Database,
    *,
    accession_no: str | None = None,
    category: str | None = None,
    event: str | None = None,
) -> int:
    """数一数有多少行审计流水（口径与 :func:`read_audit_rows` 一致）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if accession_no is not None:
        clauses.append("accession_no = ?")
        params.append(accession_no)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if event is not None:
        clauses.append("event = ?")
        params.append(event)
    sql = "SELECT COUNT(*) FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return int(database.query_value(sql, params, 0) or 0)


@dataclass(frozen=True)
class AuditCounts:
    """审计行数口径。

    口径说明（这是「导出行数对不对」的判据）：

    - **对齐依据行数** = 并入草稿的结果条目数（每条结果一行依据）；
    - **草稿份数** = 写回回执行数（一份草稿写回成功 = 一行）；
    - **状态迁移次数** = 被接受的状态迁移行数（被拒的另计，不混进来）；
    - **重复投递**与**畸形拒收**各自单独计数，**不混进对齐依据行数** ——
      「又投了一遍」和「这份报文看不懂」都不是「对上了一条」。
    """

    total_rows: int = 0
    rationale_rows: int = 0
    pending_rows: int = 0
    draft_rows: int = 0
    state_transition_rows: int = 0
    state_transition_rejected_rows: int = 0
    notify_rows: int = 0
    duplicate_deliveries: int = 0
    malformed_rejected: int = 0
    order_rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_rows": self.total_rows,
            "rationale_rows": self.rationale_rows,
            "pending_rows": self.pending_rows,
            "draft_rows": self.draft_rows,
            "state_transition_rows": self.state_transition_rows,
            "state_transition_rejected_rows": self.state_transition_rejected_rows,
            "notify_rows": self.notify_rows,
            "duplicate_deliveries": self.duplicate_deliveries,
            "malformed_rejected": self.malformed_rejected,
            "order_rejected": self.order_rejected,
        }

    def summary(self) -> str:
        """一行口径摘要（进导出件与运行摘要）。"""
        return (
            f"审计行 {self.total_rows} 行"
            f"（对齐依据 {self.rationale_rows}、挂起依据 {self.pending_rows}、"
            f"草稿份数 {self.draft_rows}、状态迁移 {self.state_transition_rows}、"
            f"通知 {self.notify_rows}）"
            f"｜单独计数：重复投递 {self.duplicate_deliveries}、"
            f"畸形拒收 {self.malformed_rejected}"
        )


def audit_counts(
    database: Database, *, accession_no: str | None = None
) -> AuditCounts:
    """按事件统计审计行数（不区分范围时读全库）。

    「重复投递」与「畸形拒收」在这里**只进各自的计数器**，不混进对齐依据
    行数 —— 对齐依据行数只认「已并入草稿」的那一类事件。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if accession_no is not None:
        clauses.append("accession_no = ?")
        params.append(accession_no)
    sql = "SELECT event, COUNT(*) AS n FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY event"

    by_event = {row["event"]: int(row["n"]) for row in database.query_all(sql, params)}
    total = sum(by_event.values())
    return AuditCounts(
        total_rows=total,
        rationale_rows=by_event.get(EVENT_ALIGNMENT_RATIONALE, 0),
        pending_rows=by_event.get(EVENT_ALIGNMENT_PENDING, 0),
        draft_rows=by_event.get(EVENT_WRITEBACK_ACCEPTED, 0),
        state_transition_rows=by_event.get(EVENT_STATE_TRANSITION, 0),
        state_transition_rejected_rows=by_event.get(EVENT_STATE_TRANSITION_REJECTED, 0),
        notify_rows=by_event.get(EVENT_NOTIFY_SENT, 0),
        duplicate_deliveries=by_event.get(EVENT_DUPLICATE, 0),
        malformed_rejected=by_event.get(EVENT_MALFORMED, 0),
        order_rejected=by_event.get(EVENT_ORDER_REJECTED, 0),
    )
