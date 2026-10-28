"""迟到结果自动续挂 + 重复 resolve 幂等。

这两条线保的是同一件事：**补齐流程不会被重复执行搞乱**。

1. **迟到结果自动接回** —— 加做结果晚于报告发出是常事（先发报告、后回免疫
   组化；或第一次回报没带蜡块号，核对登记本后重报一条）。结果回报到达时，
   状态库里若已有同一条结果的挂起行，就自动接回去：对上了并入草稿并了结那
   行，还是对不上就把那一行重拍（尝试次数 +1、快照时间前移），人不用翻清单。
2. **重复 resolve 幂等** —— 同一组补齐值重复提交一次，返回上一次的结论，
   **不产出第二份草稿**（技术员点两下「补齐并续跑」不该让报告里多一条）。

护栏不变的两条：
- 多条挂起行都说得通 → **不猜**，退回常规路径等人判；
- 归属判定仍由四级匹配决定，本模块只决定「续跑哪一行」—— 错绑定数仍是 0。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from patho_supplement.cli import main
from patho_supplement.config import Config
from patho_supplement.compose.pathology_client import PathologyClient
from patho_supplement.daemon import Daemon
from patho_supplement.db import Database
from patho_supplement.ingest.inbox_watcher import InboxWatcher
from patho_supplement.models import (
    Block,
    CheckItemState,
    ReportBaseline,
    ResultEntry,
    ResultStatus,
)
from patho_supplement.pending.late_arrival import (
    LATE_STATUS_AMBIGUOUS,
    LATE_STATUS_MERGED,
    LATE_STATUS_NO_PENDING,
    LATE_STATUS_STILL_PENDING,
    apply_late_arrival,
    find_open_pending,
    handle_late_arrival,
    resolve_after_merge,
)
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    PENDING_STATE_RESOLVED,
    PendingRecord,
)
from patho_supplement.pending.resume import (
    RESOLVE_STATUS_STILL_PENDING,
    ResumeError,
    record_fills,
    resume_pending,
)
from patho_supplement.pending.store import PENDING_STATES, PendingStore

# --------------------------------------------------------------- 样例

#: 基线：IHC-KI67 只在 B1（子宫体）上申请过；B2（宫颈部）上申请的是 CD20。
BASELINE = ReportBaseline(
    accession_no="ACC-EXAMPLE-0001",
    base_version="base-v3",
    blocks=(
        Block(
            region_seq=1,
            region_desc="子宫体",
            block_no="B1",
            order_no="ORD-EXAMPLE-0001",
            ordered_items=("IHC-KI67",),
        ),
        Block(
            region_seq=2,
            region_desc="宫颈部",
            block_no="B2",
            order_no="ORD-EXAMPLE-0002",
            ordered_items=("IHC-CD20",),
        ),
    ),
)

#: 蜡块号登记重复的基线：两块都写 B1 —— 补蜡块号也消不掉这个歧义。
AMBIGUOUS_BASELINE = ReportBaseline(
    accession_no="ACC-EXAMPLE-0002",
    base_version="base-v1",
    blocks=(
        Block(region_seq=1, region_desc="宫颈部", block_no="B1", ordered_items=("IHC-CD20",)),
        Block(region_seq=2, region_desc="宫颈部", block_no="B1", ordered_items=("IHC-CD20",)),
    ),
)

#: B1 上没申请过 IHC-KI67：晚到结果报了 B1 也对不上（登记与回报打架了）。
NO_ITEM_BASELINE = ReportBaseline(
    accession_no="ACC-EXAMPLE-0001",
    base_version="base-v3",
    blocks=(
        Block(
            region_seq=1,
            region_desc="子宫体",
            block_no="B1",
            order_no="ORD-EXAMPLE-0001",
            ordered_items=("IHC-CD20",),
        ),
    ),
)


def _fill(field_name: str, value: str):
    """构造一个补齐值（走词表校验，不手搓对象）。"""
    from patho_supplement.pending.fills import parse_fill

    return parse_fill(f"{field_name}={value}")


def _entry(
    *,
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-KI67",
    value: str | None = "约15%",
    region_seq: int | None = None,
    block_no: str | None = None,
    region_desc: str | None = None,
    observed_at: str = "20260101132000",
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="Ki-67 免疫组化",
        value=value,
        status=ResultStatus.FINAL,
        observed_at=observed_at,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file="oru-ki67.hl7",
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


def _pending_row(
    database: Database,
    *,
    entry: ResultEntry | None = None,
    reason: PendingReason = PendingReason.MISSING_BLOCK_NO,
    missing_fields: tuple[str, ...] = ("block_no",),
    candidates: tuple[Block, ...] = BASELINE.blocks,
    max_attempts: int = 3,
) -> int:
    """落一条挂起行，返回行 ID。"""
    source = entry if entry is not None else _entry()
    snapshot = PendingStore(database).save(
        PendingRecord.from_entry(
            source,
            reason,
            missing_fields=missing_fields,
            candidates=candidates,
        ),
        max_attempts=max_attempts,
    )
    return snapshot.snapshot_id


# --------------------------------------------------------------- 找挂起行


def test_find_open_pending_matches_by_accession_item_and_block(db):
    """接回键：病理号 + 项目码 + 蜡块号。"""
    row_id = _pending_row(db)
    store = PendingStore(db)

    found = find_open_pending(store, _entry(block_no="B1"))
    assert [row.snapshot_id for row in found] == [row_id]

    # 项目码不对 → 不是同一条
    assert find_open_pending(store, _entry(item_code="IHC-CD20")) == ()
    # 病理号不对 → 不是同一条
    assert find_open_pending(store, _entry(accession_no="ACC-EXAMPLE-9999")) == ()


def test_find_open_pending_normalizes_block_no(db):
    """蜡块号按归一化口径比对：B-1 ≡ b01 ≡ B1。"""
    row_id = _pending_row(db, entry=_entry(block_no="B-1"))
    found = find_open_pending(PendingStore(db), _entry(block_no="b01"))
    assert [row.snapshot_id for row in found] == [row_id]


def test_find_open_pending_ignores_resolved_rows(db):
    """已了结的行不再接回 —— 那条结果已经并进过草稿了。"""
    row_id = _pending_row(db)
    PendingStore(db).mark_resolved(row_id)
    assert find_open_pending(PendingStore(db), _entry(block_no="B1")) == ()


# --------------------------------------------------------------- 迟到结果接回


def test_late_arrival_merges_into_draft_and_closes_the_pending_row(db):
    """晚到结果补上了蜡块号：接回挂起行、对上、并入草稿、那一行了结。"""
    row_id = _pending_row(db)

    outcome = apply_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_MERGED
    assert outcome.pending_id == row_id
    assert outcome.match_level == "L1_block_exact"
    assert outcome.confidence == "高"
    # 草稿里只有这一条
    assert outcome.draft_entry_count == 1
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
    body = db.query_value("SELECT body FROM drafts WHERE id = ?", (outcome.draft_id,))
    assert "IHC-KI67" in body
    assert "B1" in body

    # 挂起行了结，不再出现在待人工处理清单里
    reloaded = PendingStore(db).load_by_id(row_id)
    assert reloaded.state == PENDING_STATE_RESOLVED
    assert reloaded.resolved is True
    open_ids = {s.snapshot_id for s in PendingStore(db).load_all(states=PENDING_STATES).snapshots}
    assert row_id not in open_ids

    # 审计留痕：接回并入 + 了结
    events = [row["event"] for row in db.query_all("SELECT event FROM audit_log")]
    assert "late_arrival_merged" in events
    assert "pending_resolved" in events


def test_late_arrival_that_still_does_not_match_restamps_the_same_row(db):
    """接回来了还是对不上：同一行重拍（不新建行），尝试次数 +1、快照时间前移。

    这里的场景：实验室报了蜡块号 B1，但**这块上没申请过**这个项目 —— 归属
    对不上，结果仍要等人判，不能塞进草稿。
    """
    row_id = _pending_row(db, candidates=(NO_ITEM_BASELINE.blocks[0],))
    before = PendingStore(db).load_by_id(row_id)

    outcome = handle_late_arrival(
        db,
        entry=_entry(block_no="B1", observed_at="20260101142000"),
        baseline=NO_ITEM_BASELINE,
        max_attempts=3,
    )

    assert outcome.status == LATE_STATUS_STILL_PENDING
    assert outcome.pending_id == row_id
    assert outcome.attempts == 2
    # 还是只有一行挂起项（没有新建重复行）
    assert db.query_value("SELECT COUNT(*) FROM pending_items") == 1

    reloaded = PendingStore(db).load_by_id(row_id)
    assert reloaded.state == PENDING_STATE_PENDING
    assert reloaded.attempts == 2
    assert reloaded.snapshot_at >= before.snapshot_at
    # 快照内容换成晚到这条原始回报；补齐值仍不写进 result_json
    assert reloaded.entry.observed_at == "20260101142000"
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0

    events = [row["event"] for row in db.query_all("SELECT event FROM audit_log")]
    assert "late_arrival_requeued" in events


def test_late_arrival_without_a_block_number_is_not_attached(db):
    """晚到的那条也没带蜡块号：键不可比，不接回（宁可不接，也不丢结果）。"""
    row_id = _pending_row(db)

    outcome = handle_late_arrival(
        db, entry=_entry(observed_at="20260101142000"), baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_NO_PENDING
    # 原来的挂起行一动不动（尝试次数没涨、快照没换）
    reloaded = PendingStore(db).load_by_id(row_id)
    assert reloaded.attempts == 1
    assert reloaded.entry.observed_at == "20260101132000"
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_late_arrival_without_a_pending_row_is_a_no_op(db):
    """没有对应的挂起行：照常走常规路径，不动任何状态。"""
    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, max_attempts=3
    )
    assert outcome.status == LATE_STATUS_NO_PENDING
    assert db.query_value("SELECT COUNT(*) FROM pending_items") == 0
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_late_arrival_does_not_guess_when_several_rows_match(db):
    """多条挂起行都说得通：不猜 —— 谁也不动，退回常规路径。"""
    first = _pending_row(db, entry=_entry(block_no="B1"))
    second = _pending_row(db, entry=_entry(block_no="B1", observed_at="20260101133000"))
    before = {
        row_id: PendingStore(db).load_by_id(row_id).attempts for row_id in (first, second)
    }

    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_AMBIGUOUS
    assert set(outcome.ambiguous_rows) == {first, second}
    # 两行都没被动过（尝试次数没涨）
    for row_id, attempts in before.items():
        assert PendingStore(db).load_by_id(row_id).attempts == attempts
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0

    events = [row["event"] for row in db.query_all("SELECT event FROM audit_log")]
    assert "late_arrival_ambiguous" in events


def test_late_arrival_ignores_a_block_outside_the_recorded_candidates(db):
    """晚到结果的蜡块号不在挂起行记下的候选部位里 → 不接回（宁可不接）。"""
    _pending_row(db, candidates=(BASELINE.blocks[0],))

    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B2"), baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_NO_PENDING
    assert db.query_value("SELECT COUNT(*) FROM pending_items") == 1


def test_late_arrival_keeps_the_later_report_value_over_the_manual_fill(db):
    """晚到结果自己带的值说了算；人工补齐值只补它仍然缺的字段。"""
    row_id = _pending_row(
        db,
        entry=_entry(block_no=None, region_desc="子宫体"),
        missing_fields=("block_no", "region_desc"),
    )
    record_fills(db, pending_id=row_id, fills=[_fill("region_desc", "宫颈部")])

    outcome = handle_late_arrival(
        db,
        entry=_entry(block_no="B1", region_desc="子宫体"),
        baseline=BASELINE,
        max_attempts=3,
    )

    assert outcome.status == LATE_STATUS_MERGED
    # 晚到结果带了部位描述 → 用它，不被人工补齐值盖掉
    assert outcome.entry.region_desc == "子宫体"


def test_late_arrival_fills_only_the_fields_the_late_report_still_lacks(db):
    """晚到结果仍然缺部位描述 → 用技术员补过的值顶上。"""
    row_id = _pending_row(
        db,
        entry=_entry(block_no=None, region_desc=None),
        missing_fields=("block_no", "region_desc"),
    )
    record_fills(db, pending_id=row_id, fills=[_fill("region_desc", "子宫体")])

    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_MERGED
    assert outcome.entry.region_desc == "子宫体"


def test_late_arrival_respects_the_order_state_guard(db):
    """申请侧已取消的加做：晚到结果也不并入草稿（仍走挂起）。"""
    from patho_supplement.align.order_guard import OrderStateGuard
    from patho_supplement.align.query_derivation import PendingQuery

    row_id = _pending_row(db)
    queries = [
        PendingQuery(
            accession_no="ACC-EXAMPLE-0001",
            order_no="ORD-EXAMPLE-0001",
            item_code="IHC-KI67",
            item_name="Ki-67 免疫组化",
            region_seq=1,
            block_no="B1",
            region_desc="子宫体",
            state=CheckItemState.CANCELLED,
            source="order",
        )
    ]
    guard = OrderStateGuard.from_queries(queries)

    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, guard=guard, max_attempts=3
    )

    assert outcome.status == LATE_STATUS_STILL_PENDING
    assert outcome.reason == PendingReason.ITEM_CANCELLED.value
    # 晚到结果接回了那一行（重拍，尝试次数 +1），但申请侧已取消 → 仍不进草稿
    assert PendingStore(db).load_by_id(row_id).attempts == 2
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_late_arrival_escalates_after_the_attempt_limit(db):
    """接回也算一次尝试：超过上限转人工处置。"""
    row_id = _pending_row(db, candidates=(NO_ITEM_BASELINE.blocks[0],))

    handle_late_arrival(
        db,
        entry=_entry(block_no="B1", observed_at="20260101142000"),
        baseline=NO_ITEM_BASELINE,
        max_attempts=1,
    )

    reloaded = PendingStore(db).load_by_id(row_id)
    assert reloaded.attempts == 2
    assert reloaded.state == PENDING_STATE_ESCALATED
    assert reloaded.escalated is True


def test_resolve_after_merge_closes_the_row_without_touching_the_draft(db):
    """守护进程用的那一步：草稿由本轮统一成稿，这一步只了结挂起行。"""
    row_id = _pending_row(db)
    outcome = handle_late_arrival(
        db, entry=_entry(block_no="B1"), baseline=BASELINE, max_attempts=3
    )
    assert outcome.merged is True

    resolve_after_merge(db, outcome=outcome)

    assert PendingStore(db).load_by_id(row_id).state == PENDING_STATE_RESOLVED
    # 没有在这里另起一份草稿
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


# --------------------------------------------------------------- 重复 resolve 幂等


def test_repeated_resolve_with_the_same_fill_is_idempotent(db):
    """同一组补齐值重复提交：返回上一次的结论，不产出第二份草稿。"""
    row_id = _pending_row(db)
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])
    first = resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)
    assert first.merged is True
    assert first.idempotent is False

    # 重复提交同一组补齐值（技术员点了两下）
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])
    second = resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    assert second.merged is True
    assert second.idempotent is True
    assert second.draft_id == first.draft_id
    assert second.draft_version == first.draft_version
    assert second.draft_entry_count == first.draft_entry_count
    # 草稿仍然只有一份
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
    # 并入的仍然只有那一条（B2 的 CD20 还没回来，单独占一行「待回报」）
    assert (
        db.query_value("SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0") == 1
    )
    assert (
        db.query_value("SELECT COUNT(*) FROM draft_entries WHERE placeholder = 1") == 1
    )


def test_resolve_with_a_different_fill_on_a_resolved_row_is_refused(db):
    """补了别的值就是另一件事：不拿旧结论顶，也不并第二份草稿。"""
    row_id = _pending_row(db)
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])
    resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B2")])
    with pytest.raises(ResumeError) as excinfo:
        resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    assert "已了结" in str(excinfo.value)
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1


def test_bare_rerun_on_a_resolved_row_is_still_refused(db):
    """不重新提交补齐值就重跑：仍报「已了结」—— 那是手工重跑，不是重复提交。"""
    row_id = _pending_row(db)
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])
    resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    with pytest.raises(ResumeError) as excinfo:
        resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    assert "已了结" in str(excinfo.value)
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1


def test_repeated_fill_does_not_duplicate_the_audit_row(db):
    """值没变的重复提交不再多写一行「已补齐」审计（提交次数仍记在行上）。"""
    row_id = _pending_row(db)
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])

    filled = db.query_value(
        "SELECT COUNT(*) FROM audit_log WHERE event = 'pending_filled'"
    )
    assert filled == 1
    # 提交次数记在行上：了结之后还能分辨出「又提交过一次」
    assert PendingStore(db).load_by_id(row_id).fills_revision == 2


# --------------------------------------------------------------- 边界（补齐值）


def test_fill_that_still_cannot_align_keeps_the_row_pending(db):
    """补齐值仍对不上：挂起行保持 pending，尝试次数累加，不并入草稿。"""
    row_id = _pending_row(db)
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B9")])

    outcome = resume_pending(db, pending_id=row_id, baseline=BASELINE, max_attempts=3)

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.attempts == 2
    assert PendingStore(db).load_by_id(row_id).state == PENDING_STATE_PENDING
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_fill_that_creates_ambiguity_stays_pending(db):
    """补齐值补出同分歧义：仍走挂起，不硬绑到某一块。"""
    entry = _entry(
        accession_no="ACC-EXAMPLE-0002",
        item_code="IHC-CD20",
        region_desc="宫颈部",
    )
    row_id = _pending_row(
        db,
        entry=entry,
        reason=PendingReason.AMBIGUOUS,
        missing_fields=("block_no", "region_desc"),
        candidates=AMBIGUOUS_BASELINE.blocks,
    )
    record_fills(db, pending_id=row_id, fills=[_fill("block_no", "B1")])

    outcome = resume_pending(
        db, pending_id=row_id, baseline=AMBIGUOUS_BASELINE, max_attempts=3
    )

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.reason == PendingReason.AMBIGUOUS.value
    assert len(outcome.candidates) == 2
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


# --------------------------------------------------------------- 命令行


def test_cli_pending_resolve_twice_keeps_one_draft(db, tmp_path, capsys, monkeypatch):
    """命令行重复执行「补齐并续跑」：第二次沿用上一次结论，草稿仍只有一份。"""
    row_id = _pending_row(db)
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "accession_no": "ACC-EXAMPLE-0001",
                        "base_version": "base-v3",
                        "blocks": [
                            {
                                "region_seq": 1,
                                "region_desc": "子宫体",
                                "block_no": "B1",
                                "order_no": "ORD-EXAMPLE-0001",
                                "ordered_items": ["IHC-KI67"],
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PATHOLOGY_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.delenv("PATHOLOGY_BASE_URL", raising=False)
    monkeypatch.delenv("PATHOLOGY_API_TOKEN", raising=False)

    argv = [
        "pending",
        "resolve",
        "--id",
        str(row_id),
        "--set",
        "block_no=B1",
        "--state-db",
        str(tmp_path / "state.db"),
        "--baseline",
        str(baseline_file),
        "--out",
        str(tmp_path / "out"),
    ]

    assert main(argv) == 0
    capsys.readouterr()
    assert main(argv) == 0
    out = capsys.readouterr().out

    assert "未重复产出草稿" in out
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
    assert db.query_value("SELECT COUNT(*) FROM draft_entries") == 1


# --------------------------------------------------------------- 守护进程端到端


class _StubPathology:
    """本地协议替身（标准库 http.server）：GET 基线 + POST 草稿。

    只出现在测试数据层 —— 产品代码走的是与生产完全相同的一条路径。
    """

    def __init__(self, baselines: dict[str, dict]) -> None:
        self.baselines = baselines
        self.posts: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _make_handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - 静音标准库的访问日志
                pass

            def _send(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - 标准库要求的方法名
                parts = [part for part in self.path.split("/") if part]
                accession = parts[-1] if parts else ""
                baseline = outer.baselines.get(accession)
                if baseline is None:
                    self._send({"message": "报告不存在"}, status=404)
                    return
                self._send(baseline)

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求的方法名
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.posts.append(json.loads(raw.decode("utf-8")))
                self._send(
                    {
                        "status": "accepted",
                        "version": "draft-v1",
                        "draft_id": "SD-EXAMPLE-0001",
                    }
                )

        return _Handler


#: 第一轮：回报没带蜡块号也没带部位描述 —— 对不上，进挂起清单。
ORU_NO_BLOCK = (
    "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101132500"
    "||ORU^R01|MSG-EXAMPLE-0101|P|2.5.1\r\n"
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-KI67^Ki-67^L|||20260101120000\r\n"
    "OBX|1|ST|IHC-KI67^Ki-67^L||约15%||||||F|||20260101132000\r\n"
)

#: 第二轮（晚到）：同一条结果，这次带上了蜡块号。
ORU_WITH_BLOCK = (
    "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|20260101142500"
    "||ORU^R01|MSG-EXAMPLE-0102|P|2.5.1\r\n"
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-KI67^Ki-67^L|||20260101120000\r\n"
    "ZBR|B1|子宫体|1\r\n"
    "OBX|1|ST|IHC-KI67^Ki-67^L||约15%||||||F|||20260101142000\r\n"
)

STUB_BASELINES = {
    "ACC-EXAMPLE-0001": {
        "accession_no": "ACC-EXAMPLE-0001",
        "base_version": "base-v3",
        "blocks": [
            {
                "region_seq": 1,
                "region_desc": "子宫体",
                "block_no": "B1",
                "order_no": "ORD-EXAMPLE-0001",
                "ordered_items": ["IHC-KI67"],
            },
            {
                "region_seq": 2,
                "region_desc": "宫颈部",
                "block_no": "B2",
                "order_no": "ORD-EXAMPLE-0002",
                "ordered_items": ["IHC-CD20"],
            },
        ],
    }
}


def _daemon_config(inbox: Path, base_url: str, db_path: Path) -> Config:
    return Config.from_env(
        {
            "PATHOLOGY_BASE_URL": base_url,
            "PATHOLOGY_API_TOKEN": "test-token",
            "PATHOLOGY_RESULT_INBOX": str(inbox),
            "PATHOLOGY_STATE_DB": str(db_path),
        }
    )


def test_daemon_late_arrival_closes_the_pending_row_and_writes_the_draft(tmp_path):
    """端到端：第一轮挂起，第二轮晚到结果接回 —— 草稿成稿、挂起行了结。"""
    stub = _StubPathology(STUB_BASELINES)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    db_path = tmp_path / "state.db"
    messages: list[str] = []

    try:
        # 第一轮：回报没带蜡块号 → 挂起
        (inbox / "round1.hl7").write_text(ORU_NO_BLOCK, encoding="utf-8")
        config = _daemon_config(inbox, stub.base_url, db_path)
        watcher = InboxWatcher(inbox, poll_interval=0.01, settle_seconds=0.0)
        daemon = Daemon(
            config,
            client=PathologyClient.from_config(config),
            database=Database(db_path),
            watcher=watcher,
            output_dir=tmp_path / "out",
            echo=messages.append,
        )
        first = daemon.run_once()
        assert first.pending_count == 1
        assert first.drafts_written == 0
        with Database(db_path) as db:
            assert db.query_value("SELECT COUNT(*) FROM pending_items") == 1
            assert db.query_value("SELECT COUNT(*) FROM drafts") == 0

        # 第二轮：晚到的结果带上了蜡块号
        (inbox / "round2.hl7").write_text(ORU_WITH_BLOCK, encoding="utf-8")
        second = daemon.run_once()
        daemon.close()

        # 晚到结果被接回：并入草稿、挂起行了结
        assert second.late_resolved == 1
        assert second.drafts_written == 1
        assert len(stub.posts) == 1
        assert stub.posts[0]["accession_no"] == "ACC-EXAMPLE-0001"
        assert stub.posts[0]["entries"][0]["item_code"] == "IHC-KI67"

        with Database(db_path) as db:
            assert db.query_value("SELECT COUNT(*) FROM drafts") == 1
            # 并入的只有晚到接回来的那一条；B2 的 CD20 还没回来，占一行「待回报」
            assert (
                db.query_value(
                    "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 0"
                )
                == 1
            )
            assert (
                db.query_value(
                    "SELECT COUNT(*) FROM draft_entries WHERE placeholder = 1"
                )
                == 1
            )
            assert db.query_value(
                "SELECT state FROM pending_items WHERE id = 1"
            ) == PENDING_STATE_RESOLVED
            events = [
                row["event"] for row in db.query_all("SELECT event FROM audit_log")
            ]
            assert "late_arrival_merged" in events

        # 清单里那条已经不在了
        pending_text = (tmp_path / "out" / "pending_list.txt").read_text(encoding="utf-8")
        assert "IHC-KI67" not in pending_text
    finally:
        stub.close()
