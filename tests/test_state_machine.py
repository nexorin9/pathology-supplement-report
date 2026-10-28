"""业务状态机：迁移表、非法迁移拒绝、审计口径。

这一组用例守的是「状态只能沿着合法路径往前走」这条底线：

- 允许的迁移对是**显式枚举**出来的，不是靠 if/else 顺手带出来的；
- 非法迁移一律**拒绝**：状态原样不动（不回退、不猜测），并且留下一行可查的
  审计流水说明「当时想从哪一态到哪一态、为什么不行」；
- 审计行数 = 迁移尝试次数（接受与拒绝各记一行），这条口径可以直接对账。

对应 spec.md 闭环表「业务状态生命周期」一行的验收：非法迁移用例被拒并记
审计；状态迁移审计行数 = 变更次数。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from patho_supplement.audit.ledger import (
    CATEGORY_STATE,
    EVENT_STATE_TRANSITION,
    EVENT_STATE_TRANSITION_REJECTED,
)
from patho_supplement.compose.draft_store import mark_draft_written_back, persist_draft
from patho_supplement.compose.state_machine import (
    CHECK_ITEM_TERMINAL_STATES,
    CHECK_ITEM_TRANSITIONS,
    DRAFT_TERMINAL_STATES,
    DRAFT_TRANSITIONS,
    StateTransitionError,
    allowed_check_item_targets,
    allowed_draft_targets,
    can_transition_check_item,
    can_transition_draft,
    describe_check_item_transitions,
    describe_draft_transitions,
    load_check_item_state,
    register_check_item,
    transition_check_item_state,
    transition_draft_state,
)
from patho_supplement.db import Database
from patho_supplement.models import CheckItemState, Draft, DraftState

ACCESSION = "ACC-EXAMPLE-0001"


# --------------------------------------------------------------- 夹具


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.db")
    database.init_schema()
    yield database
    database.close()


def _insert_draft(
    database: Database,
    *,
    accession_no: str = ACCESSION,
    version: int = 1,
    state: DraftState = DraftState.PENDING,
) -> int:
    cursor = database.execute(
        "INSERT INTO drafts(accession_no, version, base_version, state, entry_count,"
        " content_hash, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            accession_no,
            version,
            "v1",
            state.value,
            0,
            "hash",
            "正文",
            "2026-10-28 09:00:00",
        ),
    )
    return int(cursor.lastrowid)


def _state_audit_rows(database: Database) -> list[sqlite3.Row]:
    return database.query_all(
        "SELECT category, event, detail_json FROM audit_log"
        " WHERE category = ? ORDER BY id",
        (CATEGORY_STATE,),
    )


def _detail(row: sqlite3.Row) -> dict:
    return json.loads(row["detail_json"])


# --------------------------------------------------------------- 迁移表


def test_草稿四态与合法迁移对是显式枚举():
    """草稿：待生成 → 已生成 → 已回写 → 已签发，已签发是终态。"""
    assert set(DRAFT_TRANSITIONS) == set(DraftState)
    assert allowed_draft_targets(DraftState.PENDING) == frozenset({DraftState.GENERATED})
    assert allowed_draft_targets(DraftState.GENERATED) == frozenset(
        {DraftState.WRITTEN_BACK}
    )
    assert allowed_draft_targets(DraftState.WRITTEN_BACK) == frozenset({DraftState.SIGNED})
    assert allowed_draft_targets(DraftState.SIGNED) == frozenset()
    assert DRAFT_TERMINAL_STATES == frozenset({DraftState.SIGNED})


def test_检查项八态与合法迁移对是显式枚举():
    """检查项：待回报 → 已回报 → 已对齐 → 已并入；旁路待人工；订单控制三态。"""
    assert set(CHECK_ITEM_TRANSITIONS) == set(CheckItemState)
    assert CheckItemState.RETURNED in allowed_check_item_targets(CheckItemState.AWAITING)
    assert CheckItemState.ALIGNED in allowed_check_item_targets(CheckItemState.RETURNED)
    assert CheckItemState.MERGED in allowed_check_item_targets(CheckItemState.ALIGNED)
    assert CheckItemState.ALIGNED in allowed_check_item_targets(CheckItemState.MANUAL)
    assert CheckItemState.MERGED in allowed_check_item_targets(CheckItemState.MANUAL)
    assert CheckItemState.CANCELLED in allowed_check_item_targets(CheckItemState.AWAITING)
    assert CHECK_ITEM_TERMINAL_STATES == frozenset(
        {CheckItemState.MERGED, CheckItemState.TERMINATED}
    )


def test_迁移表没有留下越级或倒退的口子():
    """不能跳过中间态，也不能倒退，也不能复活终态。"""
    assert not can_transition_draft(DraftState.PENDING, DraftState.WRITTEN_BACK)
    assert not can_transition_draft(DraftState.GENERATED, DraftState.PENDING)
    assert not can_transition_draft(DraftState.SIGNED, DraftState.GENERATED)
    assert not can_transition_check_item(CheckItemState.AWAITING, CheckItemState.MERGED)
    assert not can_transition_check_item(CheckItemState.MERGED, CheckItemState.RETURNED)
    assert not can_transition_check_item(CheckItemState.TERMINATED, CheckItemState.AWAITING)
    assert not can_transition_check_item(CheckItemState.MERGED, CheckItemState.MANUAL)


def test_迁移表可以打印成可读文本():
    """迁移表要能被直接贴给科室看 —— 状态机不是只有代码能懂。"""
    draft_text = describe_draft_transitions()
    assert "待生成 → 已生成" in draft_text
    assert "终态" in draft_text
    check_text = describe_check_item_transitions()
    assert "待人工" in check_text
    assert "已并入" in check_text


# --------------------------------------------------------------- 合法迁移


def test_草稿可以沿合法路径一路推进(db):
    """待生成 → 已生成 → 已回写 → 已签发，每一步都落库。"""
    draft_id = _insert_draft(db)

    transition_draft_state(db, draft_id, DraftState.GENERATED)
    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "generated"

    transition_draft_state(db, draft_id, DraftState.WRITTEN_BACK)
    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "written_back"

    transition_draft_state(db, draft_id, DraftState.SIGNED)
    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "signed"


def test_检查项可以沿合法路径一路推进(db):
    """待回报 → 已回报 → 已对齐 → 已并入。"""
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.RETURNED
    )
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.ALIGNED
    )
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.MERGED
    )
    assert (
        load_check_item_state(db, accession_no=ACCESSION, item_code="IHC-CD20")
        is CheckItemState.MERGED
    )


def test_状态迁移会重启后仍在(db, tmp_path):
    """状态落 SQLite：换个连接读回来还是那个状态。"""
    draft_id = _insert_draft(db)
    transition_draft_state(db, draft_id, DraftState.GENERATED)

    other = Database(tmp_path / "state.db")
    other.init_schema()
    try:
        assert (
            other.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,))
            == "generated"
        )
    finally:
        other.close()


# --------------------------------------------------------------- 非法迁移拒绝


def test_非法草稿迁移被拒且状态不变(db):
    """已签发的草稿不能退回已生成 —— 签过的东西不许被程序改回去。"""
    draft_id = _insert_draft(db, state=DraftState.SIGNED)

    with pytest.raises(StateTransitionError) as excinfo:
        transition_draft_state(db, draft_id, DraftState.GENERATED)

    # 状态原样不动：没有回退，也没有猜测
    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "signed"
    # 错误信息里带齐当前态、目标态与被拒原因
    message = str(excinfo.value)
    assert "已签发" in message
    assert "已生成" in message
    assert "终态" in message


def test_越级迁移被拒(db):
    """待生成不能直接跳到已回写 —— 中间那一跳没走，状态就还没到。"""
    draft_id = _insert_draft(db, state=DraftState.PENDING)

    with pytest.raises(StateTransitionError) as excinfo:
        transition_draft_state(db, draft_id, DraftState.WRITTEN_BACK)

    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "pending"
    assert "已生成" in str(excinfo.value)


def test_已并入的检查项不能被改回待回报(db):
    """进了草稿的项不再复活。"""
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.RETURNED
    )
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.ALIGNED
    )
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.MERGED
    )

    with pytest.raises(StateTransitionError):
        transition_check_item_state(
            db,
            accession_no=ACCESSION,
            item_code="IHC-CD20",
            target=CheckItemState.RETURNED,
        )

    assert (
        load_check_item_state(db, accession_no=ACCESSION, item_code="IHC-CD20")
        is CheckItemState.MERGED
    )


def test_非严格模式下拒绝也只返回结果不改状态(db):
    """strict=False：记下来、返回结论，状态照样不动。"""
    draft_id = _insert_draft(db, state=DraftState.SIGNED)

    outcome = transition_draft_state(
        db, draft_id, DraftState.GENERATED, strict=False
    )

    assert outcome.accepted is False
    assert outcome.rejected is True
    assert "已签发" in outcome.reason
    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "signed"


def test_草稿行不存在时迁移被拒并说明原因(db):
    """对着一行都不存在的草稿迁移：拒绝并说清「行不存在」。"""
    with pytest.raises(StateTransitionError) as excinfo:
        transition_draft_state(db, 4242, DraftState.GENERATED)

    assert "不存在" in str(excinfo.value)


# --------------------------------------------------------------- 审计口径


def test_审计行数等于迁移尝试次数(db):
    """三次尝试（两次被拒）→ 恰好三行状态审计。"""
    draft_id = _insert_draft(db, state=DraftState.PENDING)

    transition_draft_state(db, draft_id, DraftState.GENERATED)          # 接受
    with pytest.raises(StateTransitionError):
        transition_draft_state(db, draft_id, DraftState.SIGNED)          # 越级，被拒
    with pytest.raises(StateTransitionError):
        transition_draft_state(db, draft_id, DraftState.GENERATED)       # 重复迁移，被拒

    rows = _state_audit_rows(db)
    assert len(rows) == 3
    assert [row["event"] for row in rows] == [
        EVENT_STATE_TRANSITION,
        EVENT_STATE_TRANSITION_REJECTED,
        EVENT_STATE_TRANSITION_REJECTED,
    ]
    assert all(row["category"] == CATEGORY_STATE for row in rows)


def test_状态审计明细带齐来态去态与被拒原因(db):
    """审计行要能独立回答「当时想干什么、被拒了」。"""
    draft_id = _insert_draft(db, state=DraftState.SIGNED)

    with pytest.raises(StateTransitionError):
        transition_draft_state(db, draft_id, DraftState.GENERATED)

    detail = _detail(_state_audit_rows(db)[0])
    assert detail["entity"] == "draft"
    assert detail["from_state"] == "signed"
    assert detail["to_state"] == "generated"
    assert detail["from_label"] == "已签发"
    assert detail["to_label"] == "已生成"
    assert detail["accepted"] is False
    assert "终态" in detail["reason"]
    assert detail["allowed"] == []


def test_审计行不含患者可识别明细(db):
    """状态审计只留业务标识。"""
    draft_id = _insert_draft(db)
    transition_draft_state(db, draft_id, DraftState.GENERATED)

    text = json.dumps([_detail(row) for row in _state_audit_rows(db)], ensure_ascii=False)
    for word in ("姓名", "身份证", "手机号", "patient_name", "id_card"):
        assert word not in text


# --------------------------------------------------------------- 检查项定位


def test_未登记的检查项先登记为待回报(db):
    """第一次迁移时把检查项登记进来，起点是「待回报」。"""
    transition_check_item_state(
        db, accession_no=ACCESSION, item_code="IHC-KI67", target=CheckItemState.RETURNED
    )

    row = db.query_one(
        "SELECT state, order_no, block_no FROM check_items"
        " WHERE accession_no = ? AND item_code = ?",
        (ACCESSION, "IHC-KI67"),
    )
    assert row is not None
    assert row["state"] == "returned"


def test_未登记且不允许创建时迁移被拒(db):
    """明确要求「已存在才准迁」时，查不到就拒绝。"""
    with pytest.raises(StateTransitionError) as excinfo:
        transition_check_item_state(
            db,
            accession_no=ACCESSION,
            item_code="IHC-KI67",
            target=CheckItemState.RETURNED,
            create=False,
        )

    assert "尚未登记" in str(excinfo.value)
    assert db.query_value("SELECT COUNT(*) FROM check_items", default=0) == 0


def test_同一项目多行时要求补蜡块号(db):
    """同项目在两块上都有行，又没给蜡块号 → 不猜，拒绝并说清要补什么。"""
    register_check_item(
        db, accession_no=ACCESSION, item_code="IHC-CD20", block_no="B1"
    )
    register_check_item(
        db, accession_no=ACCESSION, item_code="IHC-CD20", block_no="B2"
    )

    with pytest.raises(StateTransitionError) as excinfo:
        transition_check_item_state(
            db, accession_no=ACCESSION, item_code="IHC-CD20", target=CheckItemState.RETURNED
        )

    assert "蜡块号" in str(excinfo.value)


def test_给了蜡块号就能定位到那一行(db):
    """蜡块号写法不同没关系 —— 归一化后按同一块定位。"""
    register_check_item(
        db, accession_no=ACCESSION, item_code="IHC-CD20", block_no="B1"
    )
    register_check_item(
        db, accession_no=ACCESSION, item_code="IHC-CD20", block_no="B2"
    )

    transition_check_item_state(
        db,
        accession_no=ACCESSION,
        item_code="IHC-CD20",
        target=CheckItemState.RETURNED,
        block_no="b-1",
    )

    states = [
        row["state"]
        for row in db.query_all(
            "SELECT state FROM check_items WHERE accession_no = ? AND item_code = ?",
            (ACCESSION, "IHC-CD20"),
        )
    ]
    assert sorted(states) == ["awaiting", "returned"]


# --------------------------------------------------------------- 与草稿落库口径接上


def test_草稿落库会走过状态机并留下审计(db):
    """成稿这一跳走状态机：待生成 → 已生成，且有审计。"""
    draft = Draft(accession_no=ACCESSION, version=1, body="正文")
    draft_id = persist_draft(db, draft)

    assert db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,)) == "generated"
    rows = _state_audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["event"] == EVENT_STATE_TRANSITION
    detail = _detail(rows[0])
    assert detail["from_state"] == "pending"
    assert detail["to_state"] == "generated"


def test_回写成功推进草稿状态(db):
    """回写成功：已生成 → 已回写。"""
    draft = Draft(accession_no=ACCESSION, version=1, body="正文")
    draft_id = persist_draft(db, draft)

    class _Receipt:
        def to_dict(self):
            return {"accepted_version": "v2", "status": "accepted"}

    mark_draft_written_back(db, draft_id, _Receipt())

    assert (
        db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,))
        == "written_back"
    )
    assert "v2" in str(db.query_value("SELECT receipt FROM drafts WHERE id = ?", (draft_id,)))


def test_重复回写不产生第二次状态迁移(db):
    """退避重试重发同一份草稿时，状态没变就不该再记一次迁移。"""
    draft = Draft(accession_no=ACCESSION, version=1, body="正文")
    draft_id = persist_draft(db, draft)

    class _Receipt:
        def to_dict(self):
            return {"accepted_version": "v2", "status": "accepted"}

    mark_draft_written_back(db, draft_id, _Receipt())
    mark_draft_written_back(db, draft_id, _Receipt())

    rows = _state_audit_rows(db)
    assert [row["event"] for row in rows] == [
        EVENT_STATE_TRANSITION,   # 待生成 → 已生成
        EVENT_STATE_TRANSITION,   # 已生成 → 已回写
    ]
    assert (
        db.query_value("SELECT state FROM drafts WHERE id = ?", (draft_id,))
        == "written_back"
    )
