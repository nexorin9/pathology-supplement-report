"""续跑前的完整性校验：快照必须齐全才准续跑。

对齐没对上的结果被拍成快照落库，等人补齐字段后再从挂起的那一步接着跑。
**续跑之前必须先验一遍快照** —— 缺了节点就硬报错，不静默跳过，也不当成
「已对齐」放过去。这条线保的是两件事：

- **不丢结果**：缺了未对齐结果却照跑，等于把这条结果从报告里抹掉；
- **不猜**：缺了缺失字段或候选部位照跑，补齐流程就没有落点，只能靠猜 ——
  猜错就是错绑定，而错绑定比挂起难发现得多。

「缺一即报错」的语义沿用源产品的快照完整性校验：快照必须完整描述它要续跑
的那一段流程，缺了任何一环就直接报错，绝不静默跳过（源实现见
``github_ref/haystack/haystack/core/pipeline/breakpoint.py``
``_validate_pipeline_snapshot_against_pipeline``，缺组件时抛
``PipelineInvalidPipelineSnapshotError`` 并列出缺了哪些）。

**三个必要节点**：

============ ============================ ==================================
节点          快照里的对应内容              缺了会怎样
============ ============================ ==================================
未对齐结果    原始回报条目（``entry``）    没有它就无从重算，结果等于丢
缺失字段      要人工补齐的字段名            没有它补齐流程没有落点
候选列表      候选部位（``Block`` 列表）    没有它歧义无法消解，只能靠猜
============ ============================ ==================================

**校验只读不写**：报错时挂起行原样留在库里（不删、不改状态、不改尝试次数），
只多一行审计流水说明「为什么没续跑」—— 让拒绝这件事本身是可查的。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from ..audit.ledger import (
    CATEGORY_STATE,
    EVENT_PENDING_INTEGRITY_REJECTED,
    record_audit,
)
from .reasons import PendingReason
from .store import PENDING_STATES, PendingSnapshot, PendingSnapshotError, PendingStore

__all__ = [
    "NODE_CANDIDATES",
    "NODE_ENTRY",
    "NODE_LABELS",
    "NODE_MISSING_FIELDS",
    "NODE_PAYLOAD",
    "REQUIRED_NODES",
    "IntegrityIssue",
    "IntegrityReport",
    "SnapshotIntegrityError",
    "assert_snapshot_resumable",
    "assert_snapshots_resumable",
    "check_snapshot",
    "check_snapshots",
    "load_resumable",
]


#: 未对齐结果节点：快照里必须留着原始回报条目。
NODE_ENTRY = "entry"
#: 缺失字段节点：快照里必须记着要人工补齐哪些字段。
NODE_MISSING_FIELDS = "missing_fields"
#: 候选列表节点：快照里必须记着候选部位是谁。
NODE_CANDIDATES = "candidates"
#: 快照内容本身不可解析（JSON 坏了、原因不认识）时的归类节点。
NODE_PAYLOAD = "payload"

#: 续跑必须齐备的三个节点（顺序即清单里的展示顺序）。
REQUIRED_NODES: tuple[str, ...] = (NODE_ENTRY, NODE_MISSING_FIELDS, NODE_CANDIDATES)

#: 节点 → 中文标签（报错信息里用）。
NODE_LABELS: dict[str, str] = {
    NODE_ENTRY: "未对齐结果",
    NODE_MISSING_FIELDS: "缺失字段",
    NODE_CANDIDATES: "候选列表",
    NODE_PAYLOAD: "快照内容",
}

#: 允许「缺失字段」为空的原因：这两类挂起本来就没有字段可补 —— 是要人决定
#: 这条结果怎么办（该退掉还是该重做），不是等人填字段。补齐流程对它们没有意义。
_NO_MISSING_FIELD_REASONS: tuple[PendingReason, ...] = (
    PendingReason.ITEM_CANCELLED,
    PendingReason.ITEM_TERMINATED,
)

#: 必须有候选部位的原因：同分多候选却一个候选都没记，等于把歧义藏起来了 ——
#: 续跑时没有候选可比，只能靠猜，猜错就是错绑定。
_CANDIDATE_REQUIRED_REASONS: tuple[PendingReason, ...] = (PendingReason.AMBIGUOUS,)


@dataclass(frozen=True)
class IntegrityIssue:
    """一处不齐全：缺哪个节点、为什么算缺。"""

    node: str
    detail: str

    @property
    def label(self) -> str:
        """节点的中文标签。"""
        return NODE_LABELS.get(self.node, self.node)

    def as_dict(self) -> dict[str, object]:
        """可打印的摘要视图（写进审计明细与运行摘要）。"""
        return {"node": self.node, "label": self.label, "detail": self.detail}


@dataclass(frozen=True)
class IntegrityReport:
    """一条挂起项的校验结论。

    ``issues`` 为空即齐备（:attr:`ok` 为真）。不齐备的项**不会**被续跑。
    """

    snapshot_id: int
    accession_no: str
    item_code: str
    issues: tuple[IntegrityIssue, ...] = ()

    @property
    def ok(self) -> bool:
        """齐备与否。"""
        return not self.issues

    @property
    def missing_nodes(self) -> tuple[str, ...]:
        """缺了哪些节点（按发现顺序）。"""
        return tuple(issue.node for issue in self.issues)

    def describe(self) -> str:
        """人话一行：哪一行、缺什么、为什么算缺。"""
        parts = "；".join(f"{issue.label}（{issue.detail}）" for issue in self.issues)
        return (
            f"第 {self.snapshot_id} 行"
            f"（{self.accession_no or '未知病理号'} / {self.item_code or '未知项目'}）"
            f"缺：{parts}"
        )

    def as_dict(self) -> dict[str, object]:
        """可打印的摘要视图。"""
        return {
            "snapshot_id": self.snapshot_id,
            "accession_no": self.accession_no,
            "item_code": self.item_code,
            "ok": self.ok,
            "missing_nodes": list(self.missing_nodes),
            "issues": [issue.as_dict() for issue in self.issues],
        }


class SnapshotIntegrityError(RuntimeError):
    """续跑前的完整性校验没过：缺节点或内容不可解析。

    **报错而不是跳过**：缺了节点的快照若被当成「已对齐」续跑，报告就会少一项
    而没人知道；被静默跳过则等于丢结果。报错信息里带**挂起行 ID** 与
    **缺失项清单**，便于直接定位到状态库里的哪一行。
    """

    def __init__(self, reports: Sequence[IntegrityReport]) -> None:
        self.reports: tuple[IntegrityReport, ...] = tuple(reports)
        self.row_ids: tuple[int, ...] = tuple(report.snapshot_id for report in self.reports)
        self.missing_nodes: tuple[str, ...] = tuple(
            dict.fromkeys(node for report in self.reports for node in report.missing_nodes)
        )
        detail = "\n".join(f"  {report.describe()}" for report in self.reports)
        super().__init__(
            "挂起项快照不完整，不能续跑（缺一即报错，不静默跳过、不当成已对齐）：\n" + detail
        )


# --------------------------------------------------------------- 校验


def _entry_issues(snapshot: PendingSnapshot) -> list[IntegrityIssue]:
    """未对齐结果节点的问题。"""
    entry = snapshot.entry
    if entry is None:
        return [
            IntegrityIssue(
                NODE_ENTRY,
                "快照里没有未对齐的原始结果条目（内容读不出来或没落原始字段），"
                "续跑无从重算",
            )
        ]
    issues: list[IntegrityIssue] = []
    if not (entry.accession_no or "").strip():
        issues.append(IntegrityIssue(NODE_ENTRY, "原始结果条目没有病理号，定位不到报告"))
    if not (entry.item_code or "").strip():
        issues.append(IntegrityIssue(NODE_ENTRY, "原始结果条目没有项目码，定位不到检查项"))
    return issues


def _missing_field_issues(snapshot: PendingSnapshot) -> list[IntegrityIssue]:
    """缺失字段节点的问题。"""
    if snapshot.missing_fields:
        return []
    if snapshot.reason in _NO_MISSING_FIELD_REASONS:
        return []
    return [
        IntegrityIssue(
            NODE_MISSING_FIELDS,
            "快照没有记录要补齐的字段，补齐流程没有落点",
        )
    ]


def _candidate_issues(snapshot: PendingSnapshot) -> list[IntegrityIssue]:
    """候选列表节点的问题。"""
    if snapshot.candidates:
        return []
    if snapshot.reason not in _CANDIDATE_REQUIRED_REASONS:
        return []
    return [
        IntegrityIssue(
            NODE_CANDIDATES,
            "挂起原因是同分多候选，却一个候选部位都没记，续跑只能靠猜",
        )
    ]


def check_snapshot(
    snapshot: PendingSnapshot, *, required_nodes: Sequence[str] = REQUIRED_NODES
) -> IntegrityReport:
    """校验一条挂起快照是否齐备（只读，不改任何东西）。

    返回校验结论；不合格**不**在这里抛错 —— 需要抛错用
    :func:`assert_snapshot_resumable`。
    """
    nodes = tuple(required_nodes)
    issues: list[IntegrityIssue] = []
    if NODE_ENTRY in nodes:
        issues.extend(_entry_issues(snapshot))
    if NODE_MISSING_FIELDS in nodes:
        issues.extend(_missing_field_issues(snapshot))
    if NODE_CANDIDATES in nodes:
        issues.extend(_candidate_issues(snapshot))
    return IntegrityReport(
        snapshot_id=snapshot.snapshot_id,
        accession_no=snapshot.accession_no,
        item_code=snapshot.item_code,
        issues=tuple(issues),
    )


def check_snapshots(
    snapshots: Sequence[PendingSnapshot], *, required_nodes: Sequence[str] = REQUIRED_NODES
) -> tuple[IntegrityReport, ...]:
    """逐条校验（顺序与传入一致）。"""
    return tuple(check_snapshot(snapshot, required_nodes=required_nodes) for snapshot in snapshots)


def assert_snapshot_resumable(
    snapshot: PendingSnapshot, *, required_nodes: Sequence[str] = REQUIRED_NODES
) -> IntegrityReport:
    """校验一条挂起快照；不齐备就抛 :class:`SnapshotIntegrityError`。"""
    report = check_snapshot(snapshot, required_nodes=required_nodes)
    if not report.ok:
        raise SnapshotIntegrityError([report])
    return report


def assert_snapshots_resumable(
    snapshots: Sequence[PendingSnapshot], *, required_nodes: Sequence[str] = REQUIRED_NODES
) -> tuple[IntegrityReport, ...]:
    """校验一批挂起快照；**任何一条**不齐备就报错（不放过其中一条）。"""
    reports = check_snapshots(snapshots, required_nodes=required_nodes)
    broken = [report for report in reports if not report.ok]
    if broken:
        raise SnapshotIntegrityError(broken)
    return reports


# --------------------------------------------------------------- 从状态库取


def _report_from_error(exc: PendingSnapshotError) -> IntegrityReport:
    """把读快照时的解析错误包成校验结论。"""
    return IntegrityReport(
        snapshot_id=exc.row_id,
        accession_no="",
        item_code="",
        issues=(IntegrityIssue(NODE_PAYLOAD, exc.reason),),
    )


def _report_from_row_error(store: PendingStore, row_id: int) -> IntegrityReport:
    """取某一行的解析错误（读不出来时给出原因）。"""
    try:
        store.load_by_id(row_id)
    except PendingSnapshotError as exc:
        return _report_from_error(exc)
    return IntegrityReport(
        snapshot_id=row_id,
        accession_no="",
        item_code="",
        issues=(IntegrityIssue(NODE_PAYLOAD, "快照内容读不出来"),),
    )


def _record_rejection(store: PendingStore, reports: Sequence[IntegrityReport]) -> None:
    """把「没续跑」这件事记一行审计，让拒绝本身可查。

    审计写不进去时不掩盖校验错误 —— 完整性校验的报错才是调用方要处理的事。
    """
    database = getattr(store, "database", None)
    if database is None:
        return
    accession_no = next((r.accession_no for r in reports if r.accession_no), None)
    try:
        record_audit(
            database,
            category=CATEGORY_STATE,
            event=EVENT_PENDING_INTEGRITY_REJECTED,
            accession_no=accession_no,
            detail={
                "rejected_rows": [report.snapshot_id for report in reports],
                "missing_nodes": sorted(
                    {node for report in reports for node in report.missing_nodes}
                ),
                "issues": [issue.as_dict() for report in reports for issue in report.issues],
            },
        )
    except sqlite3.Error:
        pass


def load_resumable(
    store: PendingStore,
    *,
    snapshot_ids: Sequence[int] | None = None,
    required_nodes: Sequence[str] = REQUIRED_NODES,
    states: Sequence[str] = PENDING_STATES,
) -> tuple[PendingSnapshot, ...]:
    """从状态库取挂起快照并逐条校验；**不齐备就报错**。

    ``snapshot_ids`` 给定时只取这几行；不给时取 ``states`` 里的全部挂起项。

    报错时：挂起行原样留在库里（不删、不改状态、不改尝试次数），只多一行
    审计流水说明为什么没续跑。**绝不**把不齐备的快照当成已对齐放过去。
    """
    snapshots: list[PendingSnapshot] = []
    broken_reports: list[IntegrityReport] = []

    if snapshot_ids is None:
        loaded = store.load_all(states=states, strict=False)
        snapshots.extend(loaded.snapshots)
        broken_reports.extend(
            _report_from_row_error(store, row_id) for row_id in loaded.broken_rows
        )
    else:
        for snapshot_id in snapshot_ids:
            try:
                snapshot = store.load_by_id(snapshot_id)
            except PendingSnapshotError as exc:
                broken_reports.append(_report_from_error(exc))
                continue
            if snapshot is None:
                broken_reports.append(
                    IntegrityReport(
                        snapshot_id=snapshot_id,
                        accession_no="",
                        item_code="",
                        issues=(IntegrityIssue(NODE_PAYLOAD, "状态库里没有这一行挂起项"),),
                    )
                )
                continue
            snapshots.append(snapshot)

    reports = check_snapshots(snapshots, required_nodes=required_nodes)
    broken = [*broken_reports, *(report for report in reports if not report.ok)]
    if broken:
        _record_rejection(store, broken)
        raise SnapshotIntegrityError(broken)
    return tuple(snapshots)
