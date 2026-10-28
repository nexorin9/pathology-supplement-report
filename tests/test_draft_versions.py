"""草稿版本链：追加不覆盖 + 基础版本号派生。

这条线保的是三件事：

1. **每一轮成稿都留下痕迹** —— 补齐一轮就出新一版，旧版本不被覆盖。医师
   签发后被追问「上一版写了什么」，按版本号回查就能答上来；
2. **版本号接得上基线** —— 基线说这份报告现在是第 3 版（``base-v3``），
   补充报告接着它往下写就是第 4 版；基线没给版本号（老系统）就从第 1 版起，
   按本地链递增；
3. **版本链只增不改** —— 发出去的版本号不回头改，撞号直接报错，不覆盖别人
   的内容。
"""

from __future__ import annotations

import sqlite3

import pytest

from patho_supplement.align.matcher import MatchCandidate
from patho_supplement.compose.draft_renderer import render_draft
from patho_supplement.compose.draft_store import (
    DraftVersionConflict,
    derive_base_version_number,
    list_draft_versions,
    load_draft_version,
    load_draft_version_entries,
    next_draft_version,
    persist_draft,
)
from patho_supplement.db import Database
from patho_supplement.models import (
    Block,
    MatchLevel,
    ReportBaseline,
    ResultEntry,
    ResultStatus,
)

ACCESSION = "ACC-EXAMPLE-0001"

#: 基线：B1 申请过 CD3 与 Ki-67，B2 申请过 CD20。
BASELINE = ReportBaseline(
    accession_no=ACCESSION,
    base_version="base-v3",
    blocks=(
        Block(
            region_seq=1,
            region_desc="子宫体",
            block_no="B1",
            order_no="ORD-EXAMPLE-001",
            ordered_items=("IHC-CD3", "IHC-KI67"),
        ),
        Block(
            region_seq=2,
            region_desc="宫颈部",
            block_no="B2",
            order_no="ORD-EXAMPLE-002",
            ordered_items=("IHC-CD20",),
        ),
    ),
)

#: 老系统不返回版本号的基线。
NO_VERSION_BASELINE = ReportBaseline(
    accession_no=ACCESSION,
    base_version=None,
    blocks=BASELINE.blocks,
)


# --------------------------------------------------------------- 样例


def _entry(
    *,
    accession_no: str = ACCESSION,
    item_code: str = "IHC-CD3",
    value: str | None = "阴性",
    block_no: str | None = "B1",
    region_seq: int | None = 1,
    region_desc: str | None = "子宫体",
    observed_at: str | None = "20260101103000",
) -> ResultEntry:
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="CD3 免疫组化",
        value=value,
        status=ResultStatus.FINAL,
        observed_at=observed_at,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        source_system="ihc-lab",
        source_file="oru-cd3.hl7",
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


def _build_draft(
    *,
    version: int,
    baseline: ReportBaseline = BASELINE,
    item_code: str = "IHC-CD3",
    value: str = "阴性",
    observed_at: str = "20260101103000",
    created_at: str = "2026-01-01 10:30:00",
):
    """按基线渲染一版草稿（内容随 ``item_code`` / ``value`` 变化，便于比对）。"""
    return render_draft(
        baseline.accession_no,
        [(_entry(item_code=item_code, value=value, observed_at=observed_at),
          _candidate(baseline.blocks[0]))],
        baseline=baseline,
        version=version,
        created_at=created_at,
    )


# --------------------------------------------------------------- 版本号派生


def test_version_is_derived_from_the_baseline_base_version(db):
    """基线给了基础版本号：版本号从它派生并递增（base-v3 → 首版 v4）。"""
    assert next_draft_version(db, ACCESSION, BASELINE.base_version) == 4


def test_version_falls_back_to_local_chain_when_base_version_missing(db):
    """基线没给版本号（老系统）：按本地链递增，从 v1 起。"""
    assert next_draft_version(db, ACCESSION, None) == 1
    assert next_draft_version(db, ACCESSION, "") == 1


def test_local_chain_continues_after_the_first_version(db):
    """链已存在就只增不改：库里已有 v4 → 下一版 v5。"""
    persist_draft(db, _build_draft(version=4))
    assert next_draft_version(db, ACCESSION, BASELINE.base_version) == 5
    # 基线版本号后来又变了，也不回头改已发出去的号
    assert next_draft_version(db, ACCESSION, "base-v9") == 5


def test_versions_are_isolated_per_accession(db):
    """另一份报告从自己的起点起，不受这份报告影响。"""
    persist_draft(db, _build_draft(version=4))
    assert next_draft_version(db, "ACC-EXAMPLE-0002", "base-v1") == 2


@pytest.mark.parametrize(
    ("base_version", "expected"),
    [
        ("base-v3", 3),
        ("v3", 3),
        ("3", 3),
        ("base-v12", 12),
        ("base-bench-v1", 1),
        ("2026-10-28-v4", 4),
        ("base-v0", 0),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_derive_base_version_number(base_version, expected):
    """基础版本号 → 版本序号：取最后一串数字，取不到就给 None（不猜）。"""
    assert derive_base_version_number(base_version) == expected


def test_next_version_is_reproducible(db):
    """同样的输入两次调用给同样的号（可复现，不靠随机）。"""
    first = next_draft_version(db, ACCESSION, BASELINE.base_version)
    second = next_draft_version(db, ACCESSION, BASELINE.base_version)
    assert first == second == 4


# --------------------------------------------------------------- 版本链


def test_two_drafts_produce_two_versions_and_the_first_is_still_readable(db):
    """连续成稿两次 → 链上两版；第一版的内容仍可回查。"""
    first = _build_draft(version=4, item_code="IHC-CD3", value="阴性")
    first_id = persist_draft(db, first)
    second = _build_draft(version=5, item_code="IHC-KI67", value="约15%")
    second_id = persist_draft(db, second)

    versions = list_draft_versions(db, ACCESSION)
    assert [v.version for v in versions] == [4, 5]
    assert first_id != second_id

    # 旧版本没被覆盖：正文、条目数、内容指纹、生成时间逐项不变
    old = load_draft_version(db, ACCESSION, 4)
    assert old is not None
    assert old.body == first.body
    assert "IHC-CD3" in old.body
    assert "阴性" in old.body
    assert old.entry_count == 1
    assert old.created_at == "2026-01-01 10:30:00"

    # 新版本是累加的：新内容在，旧内容也在自己那一版里
    new = load_draft_version(db, ACCESSION, 5)
    assert new is not None
    assert "IHC-KI67" in new.body
    assert "约15%" in new.body


def test_first_version_is_v1_when_baseline_has_no_version(db):
    """基线没给版本号：链从 v1 起，两版就是 v1、v2。"""
    persist_draft(db, _build_draft(version=1, baseline=NO_VERSION_BASELINE))
    persist_draft(db, _build_draft(version=2, baseline=NO_VERSION_BASELINE))

    versions = list_draft_versions(db, ACCESSION)
    assert [v.version for v in versions] == [1, 2]

    # v1 的内容仍可回查
    first = load_draft_version(db, ACCESSION, 1)
    assert first is not None
    assert "IHC-CD3" in first.body


def test_version_record_carries_count_entries_time_and_base_version(db):
    """版本记录四件套：条目数、来源条目、生成时间、基础版本号。"""
    persist_draft(db, _build_draft(version=4))

    version = load_draft_version(db, ACCESSION, 4)
    assert version is not None
    assert version.entry_count == 1
    assert version.base_version == "base-v3"
    assert version.created_at == "2026-01-01 10:30:00"
    assert version.content_hash

    # 来源条目可回查：值、来源系统、回报时间、对齐依据都还原得出来
    entries = load_draft_version_entries(db, ACCESSION, 4)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.item_code == "IHC-CD3"
    assert entry.value == "阴性"
    assert entry.source_system == "ihc-lab"
    assert entry.observed_at == "20260101103000"
    assert entry.block_no == "B1"
    assert entry.alignment is not None
    assert entry.alignment.match_level is MatchLevel.L1_BLOCK_EXACT
    assert entry.alignment.confidence == "高"


def test_load_draft_version_returns_none_for_unknown_version(db):
    """查不到的版本给 None（不返回别的版本充数）。"""
    persist_draft(db, _build_draft(version=4))
    assert load_draft_version(db, ACCESSION, 99) is None
    assert load_draft_version(db, "ACC-EXAMPLE-0002", 4) is None
    assert load_draft_version_entries(db, ACCESSION, 99) == []


def test_duplicate_version_is_refused_and_old_version_untouched(db):
    """撞号直接报错：版本链只追加，不覆盖。"""
    persist_draft(db, _build_draft(version=4, value="阴性"))
    before = load_draft_version(db, ACCESSION, 4)
    assert before is not None

    with pytest.raises(DraftVersionConflict) as excinfo:
        persist_draft(db, _build_draft(version=4, value="弱阳性"))
    assert "只追加不覆盖" in str(excinfo.value)
    assert excinfo.value.version == 4
    assert excinfo.value.accession_no == ACCESSION

    after = load_draft_version(db, ACCESSION, 4)
    assert after is not None
    assert after.body == before.body
    assert "弱阳性" not in after.body
    assert len(list_draft_versions(db, ACCESSION)) == 1


def test_failed_entry_write_leaves_no_half_draft(db, monkeypatch):
    """落库中途失败不留半截草稿（草稿行、条目行、状态迁移同属一个事务）。"""
    original_execute = db.execute

    def _boom(sql, params=()):
        if "INSERT INTO draft_entries" in sql:
            raise sqlite3.OperationalError("模拟条目写入失败")
        return original_execute(sql, params)

    monkeypatch.setattr(db, "execute", _boom)

    with pytest.raises(sqlite3.OperationalError):
        persist_draft(db, _build_draft(version=4))

    monkeypatch.undo()
    assert db.query_value("SELECT COUNT(*) FROM drafts") == 0
    assert db.query_value("SELECT COUNT(*) FROM draft_entries") == 0
    assert db.query_value("SELECT COUNT(*) FROM audit_log") == 0


def test_state_of_each_version_is_recorded(db):
    """每一版都记着自己的状态（成稿 → 已生成，等回写）。"""
    persist_draft(db, _build_draft(version=4))
    versions = list_draft_versions(db, ACCESSION)
    assert len(versions) == 1
    assert versions[0].state.value == "generated"
    assert versions[0].to_dict()["state"] == "generated"
