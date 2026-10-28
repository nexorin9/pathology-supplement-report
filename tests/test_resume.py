"""人工补齐入口 + 从挂起点续跑。

这条线保的是三件事：

1. **技术员补得动** —— 挂起清单说得清缺哪个字段、候选是谁，补齐入口只收
   「挂起行 ID + 缺失字段值」，不收整条结果重投；
2. **补齐后能接着跑** —— 续跑从挂起的那一步继续，不是把整批回报重跑一遍；
   命中就并入草稿并产出**新一版**，旧版本链保留；
3. **补齐也不乱绑** —— 补齐值仍然对不上（或者补出了同分歧义）就继续挂起，
   尝试次数累加，绝不硬绑到某一块。
"""

from __future__ import annotations

import json

import pytest

from patho_supplement.align.matcher import MatchCandidate
from patho_supplement.compose.draft_renderer import render_draft
from patho_supplement.compose.draft_store import load_draft_body, persist_draft
from patho_supplement.db import Database
from patho_supplement.models import (
    Block,
    MatchLevel,
    ReportBaseline,
    ResultEntry,
    ResultStatus,
)
from patho_supplement.pending.fills import FillError, apply_fills, parse_fill, parse_fills
from patho_supplement.pending.integrity import SnapshotIntegrityError
from patho_supplement.pending.reasons import PendingReason
from patho_supplement.pending.records import (
    PENDING_STATE_ESCALATED,
    PENDING_STATE_PENDING,
    PENDING_STATE_RESOLVED,
    PendingRecord,
)
from patho_supplement.pending.resume import (
    RESOLVE_STATUS_MERGED,
    RESOLVE_STATUS_STILL_PENDING,
    ResumeError,
    record_fills,
    resume_pending,
)
from patho_supplement.pending.store import PENDING_STATES, PendingStore


# --------------------------------------------------------------- 样例

#: 基线：B1 上申请过 CD3 与 Ki-67，B2 上申请过 CD20。
BASELINE = ReportBaseline(
    accession_no="ACC-EXAMPLE-0001",
    base_version="base-v3",
    blocks=(
        Block(
            region_seq=1,
            region_desc="子宫体",
            block_no="B1",
            order_no="ORD-EXAMPLE-0001",
            ordered_items=("IHC-CD3", "IHC-KI67"),
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


def _entry(
    *,
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-KI67",
    value: str | None = "约15%",
    region_seq: int | None = None,
    block_no: str | None = None,
    region_desc: str | None = None,
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="Ki-67 免疫组化",
        value=value,
        status=ResultStatus.FINAL,
        observed_at="20260101132000",
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file="oru-ki67.hl7",
    )


def _record(
    *,
    entry: ResultEntry | None = None,
    reason: PendingReason = PendingReason.FIELDS_MISSING,
    missing_fields: tuple[str, ...] = ("block_no", "region_desc"),
    candidates: tuple[Block, ...] = (),
) -> PendingRecord:
    source = entry if entry is not None else _entry()
    return PendingRecord.from_entry(
        source,
        reason,
        missing_fields=missing_fields,
        candidates=candidates,
    )


def _candidate(block: Block, *, level: MatchLevel = MatchLevel.L1_BLOCK_EXACT) -> MatchCandidate:
    return MatchCandidate(
        block=block,
        match_level=level,
        score=1.0,
        recall_key=f"{block.region_seq}|{block.block_no}|IHC-CD3",
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture()
def pending_row(db):
    """一条挂起项：回报没带蜡块号与部位描述，要人补。"""
    store = PendingStore(db)
    snapshot = store.save(
        _record(candidates=BASELINE.blocks),
        max_attempts=3,
    )
    return snapshot


# --------------------------------------------------------------- 补齐值词表


def test_parse_fill_accepts_known_field():
    fill = parse_fill("block_no=B1")
    assert fill.field_name == "block_no"
    assert fill.value == "B1"
    assert fill.label == "蜡块号"


def test_parse_fill_rejects_unknown_field():
    """词表之外的字段名一律拒收 —— 不能把整条结果当成补齐值塞进来。"""
    with pytest.raises(FillError) as excinfo:
        parse_fill("patient_name=某某")
    assert "不能补的字段" in str(excinfo.value)


def test_parse_fill_rejects_empty_value():
    with pytest.raises(FillError):
        parse_fill("block_no=")


def test_parse_fill_rejects_malformed_pair():
    with pytest.raises(FillError):
        parse_fill("block_no")


def test_parse_fills_keeps_last_value_for_same_field():
    fills = parse_fills(["block_no=B1", "block_no=B2"])
    assert len(fills) == 1
    assert fills[0].value == "B2"


def test_apply_fills_returns_new_entry_without_touching_original():
    entry = _entry()
    filled = apply_fills(entry, {"block_no": "B1", "region_seq": "2"})
    assert entry.block_no is None
    assert filled.block_no == "B1"
    assert filled.region_seq == 2


def test_apply_fills_rejects_non_integer_region_seq():
    with pytest.raises(FillError):
        apply_fills(_entry(), {"region_seq": "两块"})


# --------------------------------------------------------------- 补齐入口


def test_record_fills_writes_value_without_overwriting_snapshot(db, pending_row):
    """补齐值单独落一张表：**原始快照不动**。"""
    store = PendingStore(db)
    record_fills(
        db,
        pending_id=pending_row.snapshot_id,
        fills=parse_fills(["block_no=B1"]),
    )

    reloaded = store.load_by_id(pending_row.snapshot_id)
    # 原始快照仍是回报原样：没有蜡块号
    assert reloaded.entry.block_no is None
    assert reloaded.entry.region_desc is None
    # 补齐值单独记着
    assert reloaded.fill_map == {"block_no": "B1"}
    # 叠加之后的条目才带上蜡块号
    assert reloaded.effective_entry.block_no == "B1"


def test_record_fills_rejects_field_that_is_not_missing(db, pending_row):
    """只接受这一行要补的字段 —— 不是「缺什么都能填」。"""
    with pytest.raises(ResumeError) as excinfo:
        record_fills(
            db,
            pending_id=pending_row.snapshot_id,
            fills=parse_fills(["item_code=IHC-XX"]),
        )
    assert "不需要补" in str(excinfo.value)


def test_record_fills_rejects_unknown_pending_row(db):
    with pytest.raises(ResumeError):
        record_fills(db, pending_id=9999, fills=parse_fills(["block_no=B1"]))


def test_record_fills_allows_correcting_a_previously_filled_field(db, pending_row):
    """补错了要能改：同一个字段再补一次是改值。"""
    store = PendingStore(db)
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B2"]))

    reloaded = store.load_by_id(pending_row.snapshot_id)
    assert reloaded.fill_map == {"block_no": "B2"}


def test_record_fills_rejects_empty_fill_set(db, pending_row):
    with pytest.raises(ResumeError):
        record_fills(db, pending_id=pending_row.snapshot_id, fills=())


def test_record_fills_reuses_recorded_values_when_no_new_fill_given(db, pending_row):
    """补齐值已在库里时可以不重打 —— 登记本更正之后重试是常事。"""
    store = PendingStore(db)
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))

    reused = record_fills(db, pending_id=pending_row.snapshot_id, fills=())
    assert [fill.value for fill in reused] == ["B1"]
    # 没有重复记，也没有覆盖
    assert store.load_fills(pending_row.snapshot_id) == {"block_no": "B1"}


# --------------------------------------------------------------- 续跑：命中


def test_resume_merges_into_draft_and_keeps_old_version_chain(db, pending_row):
    """补齐后命中：产出新一版草稿，旧版本链保留。"""
    # 先造一版 v1（守护进程批次成稿的样子）
    first = render_draft(
        "ACC-EXAMPLE-0001",
        [(_entry(item_code="IHC-CD3", value="阴性", block_no="B1"), _candidate(BASELINE.blocks[0]))],
        baseline=BASELINE,
        version=1,
    )
    first_id = persist_draft(db, first)

    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    outcome = resume_pending(
        db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == RESOLVE_STATUS_MERGED
    assert outcome.merged is True
    assert outcome.match_level == MatchLevel.L1_BLOCK_EXACT.value
    assert outcome.confidence == "高"
    assert outcome.draft_version == 2
    # 新版本是累加的：上一版的 CD3 + 这次补齐的 Ki-67
    assert outcome.draft_entry_count == 2
    assert "IHC-KI67" in load_draft_body(db, outcome.draft_id)
    assert "IHC-CD3" in load_draft_body(db, outcome.draft_id)

    # 旧版本链保留：v1 还在库里，内容没被覆盖
    versions = [
        int(row["version"])
        for row in db.query_all(
            "SELECT version FROM drafts WHERE accession_no = ? ORDER BY version",
            ("ACC-EXAMPLE-0001",),
        )
    ]
    assert versions == [1, 2]
    old_body = db.query_value("SELECT body FROM drafts WHERE id = ?", (first_id,))
    assert "IHC-CD3" in old_body
    assert "IHC-KI67" not in old_body


def test_resume_marks_the_row_resolved_and_it_leaves_the_pending_list(db, pending_row):
    store = PendingStore(db)
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    resume_pending(db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3)

    reloaded = store.load_by_id(pending_row.snapshot_id)
    assert reloaded.state == PENDING_STATE_RESOLVED
    assert reloaded.resolved is True
    # 了结的行不再出现在待人工处理清单里
    pending_ids = {s.snapshot_id for s in store.load_all(states=PENDING_STATES).snapshots}
    assert pending_row.snapshot_id not in pending_ids


def test_resume_writes_audit_rows(db, pending_row):
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    resume_pending(db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3)

    events = [row["event"] for row in db.query_all("SELECT event FROM audit_log ORDER BY id")]
    assert "pending_filled" in events
    assert "pending_resolved" in events
    assert "alignment_rationale" in events


# --------------------------------------------------------------- 续跑：仍挂起


def test_resume_stays_pending_when_filled_value_still_cannot_align(db, pending_row):
    """补的值仍然对不上：挂起行保持 pending，尝试次数累加。"""
    store = PendingStore(db)
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B9"]))
    outcome = resume_pending(
        db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3
    )

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.merged is False
    # 回报压根没带部位描述 → 标「部位描述缺失」，别让人去查一个根本没填的字段
    # （与守护进程那一条用同一个分类口径）。
    assert outcome.reason == PendingReason.REGION_DESC_MISSING.value
    assert outcome.missing_fields == ("region_desc",)
    assert outcome.attempts == 2  # 1 → 2

    reloaded = store.load_by_id(pending_row.snapshot_id)
    assert reloaded.state == PENDING_STATE_PENDING
    assert reloaded.attempts == 2
    # 没产出草稿
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_resume_escalates_after_attempt_limit(db, pending_row):
    """超过尝试上限转人工处置，不再自动续跑。"""
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B9"]))
    outcome = resume_pending(
        db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=1
    )

    assert outcome.escalated is True
    reloaded = PendingStore(db).load_by_id(pending_row.snapshot_id)
    assert reloaded.state == PENDING_STATE_ESCALATED


def test_resume_does_not_hard_bind_when_fill_creates_ambiguity(db):
    """补齐值补出同分歧义时：仍走挂起，不取第一个。"""
    store = PendingStore(db)
    entry = _entry(
        accession_no="ACC-EXAMPLE-0002",
        item_code="IHC-CD20",
        region_desc="宫颈部",
    )
    snapshot = store.save(
        _record(
            entry=entry,
            reason=PendingReason.AMBIGUOUS,
            missing_fields=("block_no", "region_desc"),
            candidates=AMBIGUOUS_BASELINE.blocks,
        ),
        max_attempts=3,
    )

    record_fills(db, pending_id=snapshot.snapshot_id, fills=parse_fills(["block_no=B1"]))
    outcome = resume_pending(
        db, pending_id=snapshot.snapshot_id, baseline=AMBIGUOUS_BASELINE, max_attempts=3
    )

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.reason == PendingReason.AMBIGUOUS.value
    # 两个候选都记着，人来指认是哪一块
    assert len(outcome.candidates) == 2
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


def test_resume_reruns_only_the_one_pending_entry(db, pending_row):
    """续跑只重跑挂起的那一条，不碰其它挂起项。"""
    store = PendingStore(db)
    other = store.save(
        _record(
            entry=_entry(item_code="IHC-CD20"),
            reason=PendingReason.MISSING_BLOCK_NO,
            missing_fields=("block_no",),
        ),
        max_attempts=3,
    )

    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    outcome = resume_pending(
        db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3
    )

    assert outcome.merged is True
    # 另一条挂起项原封不动：没被并入、没被重拍
    untouched = store.load_by_id(other.snapshot_id)
    assert untouched.attempts == 1
    assert untouched.state == PENDING_STATE_PENDING
    assert untouched.fill_map == {}
    # 草稿里只有补齐的那一条
    assert outcome.draft_entry_count == 1
    assert "IHC-CD20" not in load_draft_body(db, outcome.draft_id)


# --------------------------------------------------------------- 护栏


def test_resume_refuses_to_run_on_an_already_resolved_row(db, pending_row):
    """已了结的行不再续跑 —— 否则同一条结果会被并进草稿两次。"""
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    resume_pending(db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3)

    with pytest.raises(ResumeError) as excinfo:
        resume_pending(
            db, pending_id=pending_row.snapshot_id, baseline=BASELINE, max_attempts=3
        )
    assert "已了结" in str(excinfo.value)
    # 草稿仍然只有一版
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 1


def test_resume_refuses_to_run_on_a_broken_snapshot(db):
    """快照缺节点就报错，不静默跳过，也不当成已对齐。"""
    db.execute(
        "INSERT INTO pending_items(accession_no, item_code, result_json, missing_fields,"
        " candidates_json, reason, attempts, state, snapshot_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ACC-EXAMPLE-0001",
            "IHC-KI67",
            "{ 这不是合法 JSON",
            "block_no",
            "[]",
            PendingReason.MISSING_BLOCK_NO.value,
            1,
            PENDING_STATE_PENDING,
            "2026-01-01 10:00:00",
        ),
    )

    with pytest.raises(SnapshotIntegrityError):
        resume_pending(db, pending_id=1, baseline=BASELINE, max_attempts=3)

    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0
    events = [row["event"] for row in db.query_all("SELECT event FROM audit_log")]
    assert "pending_integrity_rejected" in events


def test_resume_rejects_unknown_pending_row(db):
    with pytest.raises(ResumeError):
        resume_pending(db, pending_id=4242, baseline=BASELINE, max_attempts=3)


def test_resume_without_baseline_stays_pending(db, pending_row):
    """取不到基线就还是挂起（不猜），并记下原因。"""
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    outcome = resume_pending(db, pending_id=pending_row.snapshot_id, baseline=None, max_attempts=3)

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.reason == PendingReason.NO_BASELINE.value


def test_resume_does_not_merge_a_cancelled_item(db, pending_row):
    """申请侧已取消的加做：补齐了也不并入草稿。"""
    from patho_supplement.align.order_guard import OrderStateGuard
    from patho_supplement.align.query_derivation import PendingQuery, PendingQueryStore
    from patho_supplement.models import CheckItemState

    PendingQueryStore(db).upsert(
        [
            PendingQuery(
                accession_no="ACC-EXAMPLE-0001",
                item_code="IHC-KI67",
                region_seq=1,
                block_no="B1",
                region_desc="子宫体",
                state=CheckItemState.CANCELLED,
            )
        ]
    )
    guard = OrderStateGuard.from_queries(PendingQueryStore(db).load("ACC-EXAMPLE-0001"))

    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))
    outcome = resume_pending(
        db,
        pending_id=pending_row.snapshot_id,
        baseline=BASELINE,
        guard=guard,
        max_attempts=3,
    )

    assert outcome.status == RESOLVE_STATUS_STILL_PENDING
    assert outcome.reason == PendingReason.ITEM_CANCELLED.value
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0


# --------------------------------------------------------------- 命令行


def _baseline_file(tmp_path, baseline: ReportBaseline = BASELINE) -> str:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "accession_no": baseline.accession_no,
                        "base_version": baseline.base_version,
                        "blocks": [
                            {
                                "region_seq": block.region_seq,
                                "region_desc": block.region_desc,
                                "block_no": block.block_no,
                                "order_no": block.order_no,
                                "ordered_items": list(block.ordered_items),
                            }
                            for block in baseline.blocks
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path)


def _cli(argv):
    from patho_supplement import cli

    return cli.main(argv)


def test_cli_pending_list_shows_missing_fields_candidates_attempts_and_time(
    db, pending_row, capsys
):
    code = _cli(["pending", "list", "--state-db", str(db.path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "待人工处理清单" in out
    assert "ACC-EXAMPLE-0001" in out
    assert "IHC-KI67" in out
    assert "蜡块号" in out          # 要补的字段
    assert "子宫体" in out          # 候选部位
    assert "合计：1 条" in out


def test_cli_pending_list_reports_empty_when_nothing_pending(db, capsys):
    code = _cli(["pending", "list", "--state-db", str(db.path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "当前没有待人工处理的挂起项" in out


def test_cli_pending_list_fails_when_state_db_missing(tmp_path, capsys):
    code = _cli(["pending", "list", "--state-db", str(tmp_path / "nope.db")])
    assert code == 1
    assert "状态库不存在" in capsys.readouterr().err


def test_cli_pending_resolve_merges_and_exits_zero(db, pending_row, tmp_path, capsys):
    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            str(pending_row.snapshot_id),
            "--set",
            "block_no=B1",
            "--state-db",
            str(db.path),
            "--baseline",
            _baseline_file(tmp_path),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "已并入草稿" in out
    # 版本号从基线的基础版本号派生并递增：基线是 base-v3 → 这一版是 v4
    assert "版本 4" in out
    draft_file = tmp_path / "out" / "drafts" / "ACC-EXAMPLE-0001-v4.txt"
    assert draft_file.is_file()
    assert "IHC-KI67" in draft_file.read_text(encoding="utf-8")


def test_cli_pending_resolve_reruns_with_recorded_fill(db, pending_row, tmp_path, capsys):
    """补齐值已记在库里时：不带 --set 也能重跑续跑（登记本更正后的重试）。"""
    record_fills(db, pending_id=pending_row.snapshot_id, fills=parse_fills(["block_no=B1"]))

    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            str(pending_row.snapshot_id),
            "--state-db",
            str(db.path),
            "--baseline",
            _baseline_file(tmp_path),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "已并入草稿" in out
    assert "蜡块号=B1" in out  # 补齐值从库里读出来，一并报出来


def test_cli_pending_resolve_exits_three_when_still_pending(db, pending_row, tmp_path, capsys):
    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            str(pending_row.snapshot_id),
            "--set",
            "block_no=B9",
            "--state-db",
            str(db.path),
            "--baseline",
            _baseline_file(tmp_path),
        ]
    )
    out = capsys.readouterr().out

    assert code == 3
    assert "仍对不上" in out
    assert "部位描述缺失" in out
    assert "待补 取材部位描述" in out


def test_cli_pending_resolve_rejects_unknown_field(db, pending_row, tmp_path, capsys):
    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            str(pending_row.snapshot_id),
            "--set",
            "patient_name=某某",
            "--state-db",
            str(db.path),
            "--baseline",
            _baseline_file(tmp_path),
        ]
    )
    assert code == 2
    assert "不能补的字段" in capsys.readouterr().err


def test_cli_pending_resolve_rejects_unknown_pending_row(db, tmp_path, capsys):
    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            "999",
            "--set",
            "block_no=B1",
            "--state-db",
            str(db.path),
            "--baseline",
            _baseline_file(tmp_path),
        ]
    )
    assert code == 1
    assert "没有第 999 行挂起项" in capsys.readouterr().err


def test_cli_pending_resolve_fails_without_baseline_or_credentials(
    db, pending_row, tmp_path, capsys, monkeypatch
):
    """既没有本地基线、也没有配凭据 → 明确报错，不猜。"""
    for key in ("PATHOLOGY_BASE_URL", "PATHOLOGY_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    code = _cli(
        [
            "pending",
            "resolve",
            "--id",
            str(pending_row.snapshot_id),
            "--set",
            "block_no=B1",
            "--state-db",
            str(db.path),
        ]
    )
    assert code == 1
    assert "取基线失败" in capsys.readouterr().err


def test_cli_pending_without_subcommand_prints_usage(capsys):
    code = _cli(["pending"])
    assert code == 2
    assert "pending" in capsys.readouterr().out


def test_cli_pending_resolve_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        _cli(["pending", "--help"])
    out = capsys.readouterr().out
    assert "list" in out
    assert "resolve" in out
