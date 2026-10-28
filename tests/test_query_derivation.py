"""待查项派生与检索键派生测试。

覆盖任务 id 17 的验收点：

- 派生条数 = 基线已申请项 + 申请侧事件数 − 已取消 / 已暂停 / 已终止项
- 同一申请号重复投递不重复派生
- ``CA`` / ``HD`` / ``DC`` 三种控制动作落到对应状态；终止项从待查集合剔除
- 基线项与申请侧项指向同一检查项时合成一条（不重复派生）
- 检索键按 ``block`` / ``region`` / ``order`` 三类派生，缺字段不派万能钥匙
- 待查项落 SQLite：跨重启去重（更新原行不新增行），状态不被基线复活
"""

from __future__ import annotations

from pathlib import Path

from patho_supplement.align.query_derivation import (
    KEY_KIND_BLOCK,
    KEY_KIND_ORDER,
    KEY_KIND_REGION,
    PendingQuery,
    PendingQueryStore,
    derive_all,
    derive_pending_queries,
    derive_recall_keys,
)
from patho_supplement.db import Database
from patho_supplement.models import (
    Block,
    CheckItemState,
    OrderAction,
    OrderRequest,
    ReportBaseline,
)
from patho_supplement.replay import run_replay

ACCESSION = "ACC-EXAMPLE-0001"

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"


def _baseline(
    *,
    accession_no: str = ACCESSION,
    blocks: tuple[Block, ...] = (),
) -> ReportBaseline:
    return ReportBaseline(
        accession_no=accession_no,
        blocks=blocks,
        base_version="base-v3",
    )


def _block(
    *,
    region_seq: int | None = 1,
    region_desc: str | None = "子宫体",
    block_no: str | None = "B1",
    order_no: str | None = "ORD-EXAMPLE-0001",
    ordered_items: tuple[str, ...] = ("IHC-CD3",),
) -> Block:
    return Block(
        region_seq=region_seq,
        region_desc=region_desc,
        block_no=block_no,
        order_no=order_no,
        ordered_items=ordered_items,
    )


def _order(
    *,
    action: OrderAction = OrderAction.NEW,
    accession_no: str = ACCESSION,
    order_no: str | None = "ORD-EXAMPLE-0001",
    item_code: str = "IHC-CD3",
    item_name: str | None = "CD3",
    region_seq: int | None = 1,
    block_no: str | None = "B1",
    region_desc: str | None = "子宫体",
) -> OrderRequest:
    return OrderRequest(
        accession_no=accession_no,
        order_no=order_no,
        item_code=item_code,
        action=action,
        item_name=item_name,
        order_time="20260101101500",
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
    )


# ------------------------------------------------------------ 派生条数口径


def test_派生条数等于基线项加申请侧事件数减去取消暂停终止():
    baseline = _baseline(
        blocks=(
            _block(ordered_items=("IHC-CD3", "IHC-CD20")),
            _block(
                region_seq=2,
                region_desc="宫颈",
                block_no="B2",
                order_no="ORD-EXAMPLE-0002",
                ordered_items=("IHC-CD8",),
            ),
        )
    )
    # 三个申请侧事件都落在基线之外的项目上：派生量与状态量可直接相减
    orders = (
        _order(
            action=OrderAction.CANCEL,
            item_code="IHC-CK7",
            order_no="ORD-EXAMPLE-0005",
            region_seq=3,
            block_no="B3",
            region_desc="宫肌",
        ),
        _order(
            action=OrderAction.HOLD,
            item_code="IHC-CD10",
            order_no="ORD-EXAMPLE-0006",
            region_seq=3,
            block_no="B3",
            region_desc="宫肌",
        ),
        _order(
            action=OrderAction.TERMINATE,
            item_code="IHC-CD56",
            order_no="ORD-EXAMPLE-0007",
            region_seq=3,
            block_no="B3",
            region_desc="宫肌",
        ),
    )

    result = derive_pending_queries(baseline, orders)

    # 基线 3 项 + 申请侧 3 个事件 = 6 次派生
    assert result.baseline_items == 3
    assert result.order_items == 3
    assert result.merged == 0
    assert result.derived_count == result.baseline_items + result.order_items == 6
    # 取消 1 / 暂停 1 / 终止 1 → 还在等结果的是 3 项
    assert len(result.cancelled) == 1
    assert len(result.held) == 1
    assert len(result.terminated) == 1
    assert len(result.awaiting) == 3
    assert result.derived_count - len(result.cancelled) - len(result.held) - len(
        result.terminated
    ) == len(result.awaiting)


def test_合成后派生条数等于两项之和减去合成条数():
    baseline = _baseline(
        blocks=(
            _block(ordered_items=("IHC-CD3", "IHC-CD20")),
            _block(
                region_seq=2,
                region_desc="宫颈",
                block_no="B2",
                order_no="ORD-EXAMPLE-0002",
                ordered_items=("IHC-CD8",),
            ),
        )
    )
    orders = (
        _order(action=OrderAction.NEW, item_code="IHC-CD3"),
        _order(action=OrderAction.NEW, item_code="IHC-CD20", order_no="ORD-EXAMPLE-0003"),
    )

    result = derive_pending_queries(baseline, orders)

    assert result.merged == 2
    assert result.derived_count == (
        result.baseline_items + result.order_items - result.merged
    ) == 3
    assert len(result.awaiting) == 3


def test_派生条数在基线与申请侧不相交时等于两项之和():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    orders = (
        _order(
            action=OrderAction.NEW,
            order_no="ORD-EXAMPLE-0009",
            item_code="IHC-CK7",
            region_seq=3,
            block_no="B3",
        ),
    )

    result = derive_pending_queries(baseline, orders)

    assert result.merged == 0
    assert result.derived_count == result.baseline_items + result.order_items == 2
    assert len(result.awaiting) == 2


# ------------------------------------------------------------ 同一项合成


def test_基线项与申请侧项指向同一检查项时合成一条():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    orders = (_order(action=OrderAction.NEW),)

    result = derive_pending_queries(baseline, orders)

    assert result.baseline_items == 1
    assert result.order_items == 1
    assert result.merged == 1
    assert result.derived_count == 1
    assert len(result.awaiting) == 1
    item = result.awaiting[0]
    assert item.item_code == "IHC-CD3"
    assert item.block_no == "B1"
    assert item.source == "baseline+order"


def test_申请侧项带部位信息时与基线项合成():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    orders = (
        _order(
            action=OrderAction.NEW,
            order_no="ORD-EXAMPLE-0009",
            item_code="IHC-CD3",
            region_seq=1,
            block_no="B1",
        ),
    )

    result = derive_pending_queries(baseline, orders)

    assert result.merged == 1
    assert result.derived_count == 1


def test_部位不同的两项不合成():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    orders = (
        _order(
            action=OrderAction.NEW,
            order_no="ORD-EXAMPLE-0009",
            item_code="IHC-CD3",
            region_seq=2,
            block_no="B2",
            region_desc="宫颈",
        ),
    )

    result = derive_pending_queries(baseline, orders)

    assert result.merged == 0
    assert result.derived_count == 2


def test_同一申请号重复投递不重复派生():
    orders = tuple(_order(action=OrderAction.NEW) for _ in range(5))

    result = derive_pending_queries(None, orders)

    assert result.derived_count == 1
    assert len(result.awaiting) == 1
    assert result.merged == 4


# ------------------------------------------------------------ 控制动作与状态


def test_取消暂停终止三种动作落到对应状态():
    orders = (
        _order(action=OrderAction.CANCEL, item_code="IHC-CD3", order_no="ORD-EXAMPLE-0001"),
        _order(action=OrderAction.HOLD, item_code="IHC-CD20", order_no="ORD-EXAMPLE-0002"),
        _order(action=OrderAction.TERMINATE, item_code="IHC-CD8", order_no="ORD-EXAMPLE-0003"),
    )

    result = derive_pending_queries(None, orders)

    states = {item.item_code: item.state for item in result.items}
    assert states["IHC-CD3"] is CheckItemState.CANCELLED
    assert states["IHC-CD20"] is CheckItemState.HELD
    assert states["IHC-CD8"] is CheckItemState.TERMINATED
    # 三项都不再等结果
    assert len(result.awaiting) == 0


def test_终止项从待查集合剔除但仍留在派生结果里():
    orders = (
        _order(action=OrderAction.TERMINATE, item_code="IHC-CD8", order_no="ORD-EXAMPLE-0003"),
        _order(action=OrderAction.NEW, item_code="IHC-CD3", order_no="ORD-EXAMPLE-0001"),
    )

    result = derive_pending_queries(None, orders)

    assert len(result.terminated) == 1
    assert [item.item_code for item in result.awaiting] == ["IHC-CD3"]
    assert len(result.items) == 2


def test_取消后再新增则回到待回报():
    orders = (
        _order(action=OrderAction.CANCEL, item_code="IHC-CD3", order_no="ORD-EXAMPLE-0001"),
        _order(action=OrderAction.NEW, item_code="IHC-CD3", order_no="ORD-EXAMPLE-0001"),
    )

    result = derive_pending_queries(None, orders)

    assert len(result.awaiting) == 1
    assert result.awaiting[0].state is CheckItemState.AWAITING


def test_基线已申请项默认在等结果():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3", "IHC-CD20")),))

    result = derive_pending_queries(baseline, ())

    assert len(result.awaiting) == 2
    assert all(item.state is CheckItemState.AWAITING for item in result.awaiting)


def test_基线里重复的项目只派生一次():
    baseline = _baseline(
        blocks=(_block(ordered_items=("IHC-CD3", "IHC-CD3", "IHC-CD20")),)
    )

    result = derive_pending_queries(baseline, ())

    assert result.baseline_items == 2
    assert len(result.awaiting) == 2


# ------------------------------------------------------------ 检索键派生


def test_检索键按三类派生并带来源类型标记():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    result = derive_pending_queries(baseline, ())
    item = result.awaiting[0]

    kinds = [key.kind for key in item.recall_keys]
    assert kinds == [KEY_KIND_BLOCK, KEY_KIND_REGION, KEY_KIND_ORDER]
    assert item.recall_keys[0].text == f"block|{ACCESSION}|1|B1|IHC-CD3"
    assert item.recall_keys[1].text == f"region|{ACCESSION}|1|子宫体|IHC-CD3"
    assert item.recall_keys[2].text == f"order|{ACCESSION}|ORD-EXAMPLE-0001|IHC-CD3"


def test_缺蜡块号只派部位键与申请号键():
    query = PendingQuery(
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        order_no="ORD-EXAMPLE-0001",
        region_seq=1,
        region_desc="子宫体",
        block_no=None,
    )

    keys = derive_recall_keys(query)

    assert [key.kind for key in keys] == [KEY_KIND_REGION, KEY_KIND_ORDER]


def test_部位信息全缺只派申请号键():
    query = PendingQuery(
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        order_no="ORD-EXAMPLE-0001",
    )

    keys = derive_recall_keys(query)

    assert [key.kind for key in keys] == [KEY_KIND_ORDER]
    assert keys[0].text == f"order|{ACCESSION}|ORD-EXAMPLE-0001|IHC-CD3"


def test_空部位描述不派部位键():
    query = PendingQuery(
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        region_desc="   ",
    )

    assert derive_recall_keys(query) == ()


def test_集合级检索键去重且按精确度排序():
    baseline = _baseline(
        blocks=(
            _block(ordered_items=("IHC-CD3", "IHC-CD20")),
        )
    )

    result = derive_pending_queries(baseline, ())
    kinds = [key.kind for key in result.recall_keys]

    assert kinds == [
        KEY_KIND_BLOCK,
        KEY_KIND_BLOCK,
        KEY_KIND_REGION,
        KEY_KIND_REGION,
        KEY_KIND_ORDER,
        KEY_KIND_ORDER,
    ]


# ------------------------------------------------------------ 落库


def test_待查项落库并能读回(tmp_path: Path):
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    result = derive_pending_queries(baseline, ())

    with Database(tmp_path / "state.db") as db:
        store = PendingQueryStore(db)
        outcome = store.upsert(result.items)
        loaded = store.load(ACCESSION)

    assert outcome.inserted == 1
    assert outcome.updated == 0
    assert len(loaded) == 1
    assert loaded[0].item_code == "IHC-CD3"
    assert loaded[0].block_no == "B1"
    assert loaded[0].state is CheckItemState.AWAITING


def test_重复落库不新增行(tmp_path: Path):
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    result = derive_pending_queries(baseline, ())

    with Database(tmp_path / "state.db") as db:
        store = PendingQueryStore(db)
        store.upsert(result.items)
        second = store.upsert(result.items)
        rows = db.query_all("SELECT * FROM pending_queries")

    assert second.inserted == 0
    assert second.updated == 1
    assert len(rows) == 1


def test_跨重启重新派生同一项仍是一条(tmp_path: Path):
    """守护进程重启后重新派生同一项检查 —— 更新原行，不新增行。"""
    db_path = tmp_path / "state.db"
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))

    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(derive_pending_queries(baseline, ()).items)

    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(derive_pending_queries(baseline, ()).items)
        rows = db.query_all("SELECT * FROM pending_queries")
        loaded = store.load(ACCESSION)

    assert len(rows) == 1
    assert len(loaded) == 1


def test_基线重新派生不复活已取消的项(tmp_path: Path):
    db_path = tmp_path / "state.db"
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))

    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(
            derive_pending_queries(
                baseline, (_order(action=OrderAction.CANCEL),)
            ).items
        )

    # 下一轮只有基线（申请侧事件还没重投）—— 已取消的项不能变回「在等结果」
    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(derive_pending_queries(baseline, ()).items)
        loaded = store.load(ACCESSION)
        awaiting = store.awaiting(ACCESSION)

    assert len(loaded) == 1
    assert loaded[0].state is CheckItemState.CANCELLED
    assert awaiting == []


def test_申请侧事件可以改回已取消的项(tmp_path: Path):
    db_path = tmp_path / "state.db"
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))

    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(
            derive_pending_queries(
                baseline, (_order(action=OrderAction.CANCEL),)
            ).items
        )

    with Database(db_path) as db:
        store = PendingQueryStore(db)
        store.upsert(
            derive_pending_queries(baseline, (_order(action=OrderAction.NEW),)).items
        )
        loaded = store.load(ACCESSION)

    assert len(loaded) == 1
    assert loaded[0].state is CheckItemState.AWAITING


# ------------------------------------------------------------ 批量派生


def test_按病理号批量派生():
    baselines = {
        ACCESSION: _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),)),
        "ACC-EXAMPLE-0002": _baseline(
            accession_no="ACC-EXAMPLE-0002",
            blocks=(
                _block(
                    region_seq=1,
                    block_no="B1",
                    order_no="ORD-EXAMPLE-0021",
                    ordered_items=("IHC-CK7",),
                ),
            ),
        ),
    }
    orders = (
        _order(
            action=OrderAction.NEW,
            accession_no="ACC-EXAMPLE-0002",
            order_no="ORD-EXAMPLE-0022",
            item_code="IHC-CD3",
            region_seq=1,
            block_no="B1",
        ),
    )

    result = derive_all(baselines, orders)

    assert set(result) == {ACCESSION, "ACC-EXAMPLE-0002"}
    assert len(result[ACCESSION].awaiting) == 1
    assert len(result["ACC-EXAMPLE-0002"].awaiting) == 2


def test_派生结果不含患者可识别字段():
    baseline = _baseline(blocks=(_block(ordered_items=("IHC-CD3",)),))
    result = derive_pending_queries(baseline, ())

    for item in result.items:
        for key in item.recall_keys:
            assert "姓名" not in key.text
            assert "身份证" not in key.text


# ------------------------------------------------------------ 主路径接入


def test_批处理重放派生待查项并落库(tmp_path: Path):
    """重放跑一轮：待查项按基线 + 申请侧事件派生，并落进状态库。"""
    with Database(tmp_path / "state.db") as db:
        outcome = run_replay(
            FIXTURE_INBOX, output_dir=tmp_path / "out", database=db
        )
        rows = db.query_all(
            "SELECT accession_no, item_code, region_seq, block_no, state"
            " FROM pending_queries ORDER BY accession_no, region_seq, block_no, item_code"
        )

    # 基线两份报告共 6 个已申请项目；申请侧 1 条 NW 与基线同项，合成后仍是 6 项
    assert outcome.derived_queries == 6
    assert outcome.awaiting_queries == 6
    assert len(rows) == 6
    assert {row["accession_no"] for row in rows} == {
        "ACC-EXAMPLE-0001",
        "ACC-EXAMPLE-0002",
    }
    # 同一个项目在两块上各有一条 —— 不是重复派生
    cd20 = [
        row
        for row in rows
        if row["item_code"] == "IHC-CD20" and row["accession_no"] == "ACC-EXAMPLE-0001"
    ]
    assert len(cd20) == 2
    assert {row["block_no"] for row in cd20} == {"B1", "B2"}


def test_批处理重放重跑不新增待查项(tmp_path: Path):
    """同一批结果重跑一遍：待查项表条数不变（跨批次去重）。"""
    db_path = tmp_path / "state.db"
    with Database(db_path) as db:
        first = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out1", database=db)
        count_first = db.query_value("SELECT COUNT(*) FROM pending_queries")
    with Database(db_path) as db:
        second = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out2", database=db)
        count_second = db.query_value("SELECT COUNT(*) FROM pending_queries")

    assert first.derived_queries == second.derived_queries == 6
    assert count_first == count_second == 6
