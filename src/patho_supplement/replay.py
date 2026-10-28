"""批处理重放：把一个目录里的结果回报跑成草稿与待人工处理清单。

这是整条主干的**第 1 步** —— 从入站报文一路跑到「一份可核对的草稿 + 一份待
人工处理清单」。守护进程（任务 id 3）复用同一套解析、对齐与渲染逻辑，只是
触发方式从「跑一次」换成「目录监听」。

一条结果只有两个去向：**进草稿**（带上对齐依据）或**进待人工处理清单**（带上原因
与缺失字段）。没有第三个去向 —— 悄悄丢掉的结果，就是医师看不到的检查项。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .align.filtering import FilterCandidate, FilterPolicy
from .align.lexicon import load_lexicon
from .align.matcher import (
    MatchOutcome,
    MatchThresholds,
    align_entries,
    summarize_match_levels,
)
from .align.merger import merge_fanout
from .align.order_guard import OrderStateGuard
from .align.query_derivation import PendingQuerySet, PendingQueryStore, derive_all
from .align.rationale import (
    build_alignment,
    build_pending_alignment,
    record_alignment_audit,
)
from .align.retriever import DEFAULT_MAX_WORKERS, DEFAULT_ROUTE_TIMEOUT, fanout_retrieve
from .audit.ledger import (
    CATEGORY_INGEST,
    EVENT_ALIGNMENT_PENDING,
    EVENT_ALIGNMENT_RATIONALE,
    EVENT_MALFORMED,
    record_audit,
    record_encoding_audit,
)
from .compose.baseline import BaselineError, load_baseline_file
from .compose.draft_renderer import derive_awaiting, render_draft
from .db import Database
from .ingest.dirty import decode_payload
from .ingest.er7 import Er7ParseError, parse_message
from .ingest.idempotency import IdempotencyLedger, resolve_corrections
from .ingest.orm_parser import OrderIngestor
from .ingest.oru_parser import parse_oru
from .models import Draft, OrderRequest, ReportBaseline, ResultEntry, ResultStatus
from .pending.reasons import (
    PendingReason,
    classify_reason,
    missing_fields_for_entry,
    missing_fields_for_reason,
    missing_fields_for_state,
    reason_for_entry,
)
from .pending.records import PendingRecord, render_pending_text

__all__ = ["ReplayOutcome", "run_replay", "DEFAULT_BASELINE_NAME"]

#: 结果目录里默认的基线快照文件名。
DEFAULT_BASELINE_NAME = "baseline.json"


@dataclass
class ReplayOutcome:
    """一轮重放的产出与计数。"""

    inbox: Path
    output_dir: Path
    baseline_source: str
    files_scanned: int = 0
    result_files: int = 0
    order_files: int = 0
    parsed_results: int = 0
    parsed_orders: int = 0
    order_malformed: int = 0
    malformed: int = 0
    accepted: int = 0
    duplicates: int = 0
    superseded: int = 0
    corrections_applied: int = 0
    inverted_corrections: int = 0
    withheld: int = 0
    not_obtained: int = 0
    empty_value: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    order_rejected: list[tuple[str, str]] = field(default_factory=list)
    orders: list[OrderRequest] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    derived_queries: int = 0
    awaiting_queries: int = 0
    cancelled_queries: int = 0
    held_queries: int = 0
    terminated_queries: int = 0
    recall_key_count: int = 0
    drafts: list[Draft] = field(default_factory=list)
    pending: list[PendingRecord] = field(default_factory=list)
    draft_paths: list[Path] = field(default_factory=list)
    pending_path: Path | None = None
    #: 本次生效的部位同义词典（版本 / 条目数 / 来源 / 回退告警）。
    lexicon: dict[str, object] = field(default_factory=dict)
    #: 本轮读到的报文文件按解码方式计数（utf-8 / gbk / mixed）。
    decode_counts: dict[str, int] = field(default_factory=dict)
    #: 多路召回的旁路观察统计（路的成功 / 失败 / 超时、命中条数、失败明细）。
    fanout: dict[str, object] = field(default_factory=dict)
    #: 四级判定分布（L1 蜡块号精确 / L2 部位描述精确 / L3 词法模糊 / L4 挂起）。
    match_levels: dict[str, int] = field(default_factory=dict)
    #: 本轮生成的对齐依据条数（已并入草稿的）。
    rationale_count: int = 0
    #: 本轮生成的对齐依据条数（挂起的）—— 挂起的依据**没有置信等级**。
    pending_rationale_count: int = 0
    #: 草稿里的「待回报」占位条数（已申请但这一版还没结果）。
    placeholder_count: int = 0
    #: 本轮**送进对齐层**的生效结果条目（去重与更正取代之后的那一批）。
    #: 对照实验要用它当「三方共用的同一份输入」—— 见
    #: :mod:`patho_supplement.align.comparison`。
    effective_entries: list[ResultEntry] = field(default_factory=list)
    #: 已回报但没有可写进报告的值（无法获得 / 值为空）—— 一律不并入草稿。
    withheld_entries: list[ResultEntry] = field(default_factory=list)
    #: 回报时间倒挂、没生效的更正（等人工确认）。
    inverted_entries: list[ResultEntry] = field(default_factory=list)

    @property
    def draft_entry_count(self) -> int:
        return sum(draft.merged_count for draft in self.drafts)

    @property
    def effective_result_count(self) -> int:
        """生效结果数（去重与更正取代之后）= 草稿条目 + 待处理条目。"""
        return self.draft_entry_count + len(self.pending)

    def as_dict(self) -> dict[str, object]:
        return {
            "inbox": str(self.inbox),
            "output_dir": str(self.output_dir),
            "baseline_source": self.baseline_source,
            "files_scanned": self.files_scanned,
            "result_files": self.result_files,
            "order_files": self.order_files,
            "parsed_results": self.parsed_results,
            "parsed_orders": self.parsed_orders,
            "order_malformed": self.order_malformed,
            "malformed": self.malformed,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "superseded": self.superseded,
            "corrections_applied": self.corrections_applied,
            "inverted_corrections": self.inverted_corrections,
            "withheld": self.withheld,
            "not_obtained": self.not_obtained,
            "empty_value": self.empty_value,
            "rejected": [{"file": name, "reason": reason} for name, reason in self.rejected],
            "order_rejected": [
                {"file": name, "reason": reason} for name, reason in self.order_rejected
            ],
            "deferred": list(self.deferred),
            "derived_queries": self.derived_queries,
            "awaiting_queries": self.awaiting_queries,
            "cancelled_queries": self.cancelled_queries,
            "held_queries": self.held_queries,
            "terminated_queries": self.terminated_queries,
            "recall_key_count": self.recall_key_count,
            "lexicon": dict(self.lexicon),
            "decode_counts": dict(self.decode_counts),
            "fanout": dict(self.fanout),
            "match_levels": dict(self.match_levels),
            "rationale_count": self.rationale_count,
            "pending_rationale_count": self.pending_rationale_count,
            "placeholder_count": self.placeholder_count,
            "draft_entry_count": self.draft_entry_count,
            "pending_count": len(self.pending),
            "effective_result_count": self.effective_result_count,
            "drafts": [
                {
                    "accession_no": draft.accession_no,
                    "entry_count": draft.merged_count,
                    "path": str(path),
                }
                for draft, path in zip(self.drafts, self.draft_paths)
            ],
            "pending_path": str(self.pending_path) if self.pending_path else None,
        }


def _iter_message_files(inbox: Path) -> list[Path]:
    """列出结果目录里的报文文件。

    只取普通文件；``*.json`` 是基线快照等本地资料，不按报文解析；
    子目录（``processed/`` 等）是归档，不参与本轮。
    """
    files: list[Path] = []
    for path in sorted(inbox.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            continue
        if path.name.startswith("."):
            continue
        files.append(path)
    return files


def _resolve_baseline(
    inbox: Path, baseline_path: Path | None
) -> tuple[dict[str, ReportBaseline], str]:
    """确定基线来源：显式指定优先，其次结果目录里的默认快照文件。"""
    candidate = baseline_path or (inbox / DEFAULT_BASELINE_NAME)
    if not candidate.is_file():
        raise BaselineError(
            "未找到病理报告基线：请用 --baseline 指定文件，或在结果目录放 "
            f"{DEFAULT_BASELINE_NAME}",
            path=candidate,
        )
    return load_baseline_file(candidate), str(candidate)


def _pending_record_for(entry: ResultEntry, outcome: MatchOutcome) -> PendingRecord:
    """把一条对不上的结果转成挂起记录。"""
    reason = reason_for_entry(entry)
    if reason is not None:
        # 结果本身有问题（无法获得 / 空值）：与对齐无关，一律不能写进报告。
        return PendingRecord.from_entry(
            entry,
            reason,
            missing_fields=missing_fields_for_entry(entry),
        )
    reason = classify_reason(entry, outcome)
    return PendingRecord.from_entry(
        entry,
        reason,
        missing_fields=(
            outcome.missing_fields
            or missing_fields_for_reason(reason)
            or missing_fields_for_state(outcome.state)
        ),
        candidates=outcome.candidates,
    )


def _withheld_record(entry: ResultEntry) -> PendingRecord:
    """一条已回报但写不进报告的结果（无法获得 / 空值）→ 挂起记录。"""
    reason = reason_for_entry(entry) or PendingReason.RESULT_NOT_OBTAINED
    return PendingRecord.from_entry(
        entry,
        reason,
        missing_fields=missing_fields_for_entry(entry),
    )


def _inverted_record(inversion) -> PendingRecord:
    """一条没生效的更正（回报时间倒挂）→ 挂起记录，等人确认。"""
    return PendingRecord.from_entry(
        inversion.entry,
        PendingReason.TIME_INVERSION,
        missing_fields=("observed_at",),
    )


def _record_malformed(
    database: Database | None,
    file_name: str,
    exc: Er7ParseError,
) -> None:
    """把一条畸形报文写进审计流水（给了状态库才写）。

    畸形报文**绝不**进草稿 —— 宁可少一条结果，也不能让看不懂的数据混进报告。
    """
    if database is None:
        return
    record_audit(
        database,
        category=CATEGORY_INGEST,
        event=EVENT_MALFORMED,
        accession_no=None,
        detail={"file": file_name, **exc.to_dict()},
    )


def _fanout_observation(
    derived: Mapping[str, PendingQuerySet],
    entries: Sequence[ResultEntry],
    *,
    policy: FilterPolicy | str,
    max_workers: int,
    route_timeout: float,
) -> dict[str, object]:
    """多路召回的旁路观察。

    **不改草稿内容**：批处理重放刻意不回写病理系统，所以这里的扇出只做路况
    统计。**守护主循环那条路已经装配成图**（见
    :mod:`patho_supplement.pipeline`：入站解析 → 取基线 → 派生待查项 →
    多路召回 → 部位-蜡块对齐 → 成稿 → 回写 → 通知），召回层的失败在那条
    路上同样看得见。

    之所以要这么早就有这份统计：**失败路必须看得见**。一把钥匙坏了（部位
    描述脏了、某次打分卡住），如果只体现在「草稿里少了这一条」，没人知道
    少了还是本来就没有；写成「这一路失败了、错在哪」，运维一眼能定位。

    扇出之后接**合并 → 去重 → 排序**三段（见
    :mod:`patho_supplement.align.merger`）：同一份结果被多把钥匙召回时会
    在候选集里出现多次，去重后才是「一份结果一条」。去重条数与最终条数
    一并落进摘要 —— 差得离谱时说明召回键派生得太散，运维要看得见这件事。
    """
    by_accession: dict[str, list[ResultEntry]] = {}
    for entry in entries:
        by_accession.setdefault(entry.accession_no, []).append(entry)

    routes_total = routes_ok = routes_failed = routes_timed_out = 0
    items = pending_items = 0
    merged = kept = duplicates = unkeyed = 0
    failures: list[dict[str, object]] = []

    for accession_no in sorted(derived):
        keys = derived[accession_no].recall_keys
        if not keys:
            continue
        candidates = tuple(
            FilterCandidate.from_entry(entry)
            for entry in by_accession.get(accession_no, [])
        )
        result = fanout_retrieve(
            keys,
            candidates,
            policy=policy,
            max_workers=max_workers,
            route_timeout=route_timeout,
        )
        routes_total += len(result.routes)
        routes_ok += result.ok_routes
        routes_failed += result.failed_routes
        routes_timed_out += result.timed_out_routes
        items += len(result.items)
        pending_items += result.pending_items

        merged_outcome = merge_fanout(result.routes)
        merged += merged_outcome.merged_count
        kept += merged_outcome.count
        duplicates += merged_outcome.duplicates
        unkeyed += merged_outcome.unkeyed

        for route in result.failures:
            failures.append(
                {
                    "accession_no": accession_no,
                    "route_id": route.route_id,
                    "kind": route.kind,
                    "status": route.status,
                    "error_type": route.error_type,
                    "error": route.error,
                }
            )

    return {
        "routes_total": routes_total,
        "routes_ok": routes_ok,
        "routes_failed": routes_failed,
        "routes_timed_out": routes_timed_out,
        "items": items,
        "pending_items": pending_items,
        "merged": merged,
        "kept": kept,
        "duplicates": duplicates,
        "unkeyed": unkeyed,
        "max_workers": max_workers,
        "route_timeout": route_timeout,
        "policy": FilterPolicy(policy).value,
        "failures": failures,
        "note": "旁路观察：多路召回跑一遍统计路况，草稿内容仍由精确对齐层决定",
    }


def run_replay(
    inbox: Path | str,
    *,
    baseline_path: Path | str | None = None,
    output_dir: Path | str = "data/replay",
    database: Database | None = None,
    lexicon_path: Path | str | None = None,
    filter_policy: FilterPolicy | str = FilterPolicy.MERGE,
    fanout_max_workers: int = DEFAULT_MAX_WORKERS,
    fanout_route_timeout: float = DEFAULT_ROUTE_TIMEOUT,
    thresholds: MatchThresholds | None = None,
) -> ReplayOutcome:
    """跑一轮批处理重放。

    ``database`` 给了就用持久化账本（跨批次去重）；不给则只在本次进程内去重，
    保证同一批结果重跑产出一致的草稿（便于排查与复现）。

    ``lexicon_path`` 给的是部位同义词典文件；不给则用内置最小词表。词典装载
    失败会回退到内置词表并留下告警，**不中断这一轮**。

    ``filter_policy`` / ``fanout_max_workers`` / ``fanout_route_timeout`` 是
    多路召回的运行参数（来自配置面），决定硬过滤策略、并发上限与单路超时。

    ``thresholds`` 是归属绑定的判定阈值（来自配置面）：BM25 阈值与领先幅度
    阈值。缺省用内置默认值。
    """
    inbox_path = Path(inbox).expanduser().resolve()
    out_path = Path(output_dir).expanduser().resolve()
    if not inbox_path.is_dir():
        raise BaselineError("结果目录不存在", path=inbox_path)

    baselines, baseline_source = _resolve_baseline(
        inbox_path, Path(baseline_path) if baseline_path else None
    )

    outcome = ReplayOutcome(
        inbox=inbox_path, output_dir=out_path, baseline_source=baseline_source
    )
    # 词典版本随运行摘要留痕：同一批回报在不同词典版本下可能算成不同结果，
    # 事后复盘时必须知道当时生效的是哪一版。
    outcome.lexicon = load_lexicon(lexicon_path).summary()

    ledger = IdempotencyLedger(database)
    order_ingestor = OrderIngestor(database)
    entries: list[ResultEntry] = []

    for path in _iter_message_files(inbox_path):
        outcome.files_scanned += 1
        try:
            raw = path.read_bytes()
            # 逐行 UTF-8 → GBK 回退：同一批文件里两种编码都有是常事。
            payload = decode_payload(raw)
            outcome.decode_counts[payload.encoding] = (
                outcome.decode_counts.get(payload.encoding, 0) + 1
            )
            if database is not None:
                record_encoding_audit(
                    database,
                    file_name=path.name,
                    encoding=payload.encoding,
                    fallback=payload.fallback,
                    byte_length=payload.byte_length,
                )
            message = parse_message(
                payload.text, source=str(path), encoding=payload.encoding
            )
        except (OSError, Er7ParseError) as exc:
            outcome.malformed += 1
            outcome.rejected.append((path.name, str(exc)))
            if isinstance(exc, Er7ParseError):
                _record_malformed(database, path.name, exc)
            continue

        code = message.message_code.upper()
        structure = message.message_structure.upper()

        if code == "ORU" and structure == "R01":
            outcome.result_files += 1
            try:
                parsed = parse_oru(message, source_file=path.name)
            except Er7ParseError as exc:
                outcome.malformed += 1
                outcome.rejected.append((path.name, str(exc)))
                _record_malformed(database, path.name, exc)
                continue
            for entry in parsed:
                outcome.parsed_results += 1
                if ledger.accept(entry, source_file=path.name):
                    entries.append(entry)
            continue

        if code == "ORM":
            # 申请侧事件：解析出「哪些检查项在等结果、哪些被取消/暂停/终止」。
            # 本轮把申请条目解析清楚、去重，拒收的条目逐条记下；
            # 待查项派生（基线已申请项 + 申请侧新增项合并）在文件读完后统一做。
            outcome.order_files += 1
            try:
                ingested = order_ingestor.ingest(message, source_file=path.name)
            except Er7ParseError as exc:
                outcome.rejected.append((path.name, str(exc)))
                continue
            outcome.parsed_orders += ingested.accepted
            outcome.orders.extend(ingested.orders)
            outcome.order_malformed += ingested.malformed
            for rejected in ingested.rejected:
                outcome.order_rejected.append((path.name, rejected.detail))
            outcome.deferred.append(path.name)
            continue

        outcome.rejected.append(
            (path.name, f"未知报文类型 MSH-9={message.message_type!r}，不属入站事件流")
        )

    outcome.accepted = ledger.counters.accepted
    outcome.duplicates = ledger.counters.duplicate

    # 待查项派生：基线已申请项 + 申请侧新增项合并，派生出多组检索键。
    # 给了状态库就落库 —— 跨重启去重靠它（同一项重复投递不新增行）。
    derived = derive_all(baselines, outcome.orders)
    for query_set in derived.values():
        outcome.derived_queries += query_set.derived_count
        outcome.awaiting_queries += len(query_set.awaiting)
        outcome.cancelled_queries += len(query_set.cancelled)
        outcome.held_queries += len(query_set.held)
        outcome.terminated_queries += len(query_set.terminated)
        outcome.recall_key_count += len(query_set.recall_keys)
    if database is not None:
        store = PendingQueryStore(database)
        for query_set in derived.values():
            store.upsert(query_set.items)

    # 结果状态取舍：更正取代、回报时间倒挂、无法获得 / 空值扣下等人。
    resolution = resolve_corrections(entries, database=database)
    outcome.superseded = resolution.superseded
    outcome.corrections_applied = len(resolution.applied)
    outcome.inverted_corrections = len(resolution.inverted)
    outcome.withheld = len(resolution.withheld)
    outcome.not_obtained = sum(
        1 for entry in resolution.withheld if entry.status is ResultStatus.NOT_OBTAINED
    )
    outcome.empty_value = outcome.withheld - outcome.not_obtained

    # 对照实验要拿「三方共用的同一份输入」：送进对齐层的生效条目 + 被扣下的
    # 两类（无法获得 / 值为空、回报时间倒挂）。只留在内存里，不进运行摘要
    # —— 摘要里不写结果值。
    outcome.effective_entries = list(resolution.kept)
    outcome.withheld_entries = list(resolution.withheld)
    outcome.inverted_entries = [item.entry for item in resolution.inverted]

    matched, unmatched = align_entries(
        resolution.kept,
        baselines,
        thresholds=thresholds,
        # 申请侧状态闸门：已取消 / 已终止的待查项，其结果不并入草稿。
        guard=OrderStateGuard.from_query_sets(derived),
    )
    # 四级判定分布：L3（靠文字相似猜上的）置信低，医师要重点复核这几条。
    outcome.match_levels = summarize_match_levels(matched, unmatched)

    # 对齐依据：一条结果一条依据。
    #   - 已并入草稿的：命中方式 / 得分 / 召回键 / 来源系统 / 回报时间 / 置信等级；
    #   - 挂起的：同样留一条，但**没有置信等级**（不敢下结论 ≠ 可信度低）。
    # 依据同时进草稿条目标注与审计流水 —— 内审 / 飞检语境下要拿得出「凭什么」。
    if database is not None:
        for entry, candidate in matched:
            record_alignment_audit(
                database, build_alignment(entry, candidate), event=EVENT_ALIGNMENT_RATIONALE
            )
            outcome.rationale_count += 1
        for entry, item in unmatched:
            record_alignment_audit(
                database,
                build_pending_alignment(entry, item),
                event=EVENT_ALIGNMENT_PENDING,
            )
            outcome.pending_rationale_count += 1
    else:
        outcome.rationale_count = len(matched)
        outcome.pending_rationale_count = len(unmatched)

    outcome.pending = [_pending_record_for(entry, item) for entry, item in unmatched]
    outcome.pending.extend(_withheld_record(entry) for entry in resolution.withheld)
    outcome.pending.extend(_inverted_record(inv) for inv in resolution.inverted)

    # 多路召回旁路观察：跑一遍扇出，把每路的成败落进运行摘要。
    # **草稿内容不受影响** —— 批处理重放刻意不回写病理系统，召回层在这里只
    # 做路况统计；守护主循环那条路已装配成图（见 pipeline 模块）。
    outcome.fanout = _fanout_observation(
        derived,
        resolution.kept,
        policy=filter_policy,
        max_workers=fanout_max_workers,
        route_timeout=fanout_route_timeout,
    )

    by_accession: dict[str, list] = {}
    for entry, candidate in matched:
        by_accession.setdefault(entry.accession_no, []).append((entry, candidate))

    # 这一版**收到过**的结果（并入草稿的 + 被扣下等人的）—— 「未回报」的口径
    # 要用它：结果回来了但没进报告（无法获得 / 值为空 / 归属对不上），
    # 写「待回报」是假话。
    reported_by_accession: dict[str, list] = {}
    for entry in (*resolution.kept, *resolution.withheld):
        reported_by_accession.setdefault(entry.accession_no, []).append(entry)
    for inversion in resolution.inverted:
        reported_by_accession.setdefault(inversion.entry.accession_no, []).append(
            inversion.entry
        )

    drafts_dir = out_path / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    for accession_no in sorted(by_accession):
        baseline = baselines.get(accession_no)
        query_set = derived.get(accession_no)
        # 未到项占位：已申请（基线 / 申请侧）但这一版**一次都没回来**的检查项，
        # 以「待回报」出现在草稿里 —— 少写一项，医师就会以为这项没做。
        awaiting = derive_awaiting(
            baseline,
            query_set.items if query_set is not None else (),
            reported=reported_by_accession.get(accession_no, ()),
        )
        draft = render_draft(
            accession_no,
            by_accession[accession_no],
            baseline=baseline,
            awaiting=awaiting,
            version=1,
        )
        draft_path = drafts_dir / f"{accession_no}.txt"
        draft_path.write_text(draft.body, encoding="utf-8")
        outcome.drafts.append(draft)
        outcome.draft_paths.append(draft_path)
        outcome.placeholder_count += draft.placeholder_count

    pending_path = out_path / "pending_list.txt"
    pending_path.write_text(render_pending_text(outcome.pending), encoding="utf-8")
    outcome.pending_path = pending_path

    summary_path = out_path / "summary.json"
    summary_path.write_text(
        json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return outcome
