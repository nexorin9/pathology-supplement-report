"""草稿渲染器（按部位分组 + 未到项占位）。

补充报告草稿是给病理医师看的签发底稿，两条要求：

1. **按取材部位分组**，逐项标明来源系统与回报时间 —— 每一行都要能回答
   「这个结果哪来的、什么时候回来的」，签发时不必回头翻原始报文；
2. **未到项占位** —— 已申请但一次都没回来的检查项以「待回报」出现在草稿里。
   少写一项，医师就会以为这项没做：「没做」和「还没回来」是两件事。

本模块盯住最容易写错的一处：**「还没回来」的口径**。结果**回报了但没进
报告**（无法获得 / 值为空 / 归属对不上 / 申请已取消）—— 那是「回来了但没用上」，
写成「待回报」是假话。这类项在待人工处理清单里，不在草稿里。
"""

from __future__ import annotations

from patho_supplement.align.matcher import MatchCandidate
from patho_supplement.align.query_derivation import (
    SOURCE_BASELINE,
    SOURCE_ORDER,
    PendingQuery,
)
from patho_supplement.compose.draft_renderer import (
    PLACEHOLDER_LABEL,
    AwaitingItem,
    awaiting_from_baseline,
    derive_awaiting,
    render_draft,
)
from patho_supplement.models import (
    Block,
    CheckItemState,
    MatchLevel,
    ReportBaseline,
    ResultEntry,
    ResultStatus,
)

ACCESSION = "ACC-EXAMPLE-0001"


# ------------------------------------------------------------------ 夹具


def _entry(**kwargs) -> ResultEntry:
    base = dict(
        accession_no=ACCESSION,
        item_code="IHC-CD3",
        item_name="CD3",
        value="阴性",
        status=ResultStatus.FINAL,
        region_seq=1,
        block_no="B1",
        region_desc="子宫体",
        observed_at="20260101130000",
        source_system="IMMUNO-LAB",
    )
    base.update(kwargs)
    return ResultEntry(**base)


def _block(**kwargs) -> Block:
    base = dict(
        region_seq=1,
        region_desc="子宫体",
        block_no="B1",
        order_no="ORD-EXAMPLE-0001",
        ordered_items=("IHC-CD3",),
    )
    base.update(kwargs)
    return Block(**base)


def _baseline(*blocks: Block) -> ReportBaseline:
    return ReportBaseline(
        accession_no=ACCESSION,
        blocks=blocks or (_block(),),
        base_version="base-v3",
    )


def _candidate(block: Block | None = None, **kwargs) -> MatchCandidate:
    base = dict(
        block=block or _block(),
        match_level=MatchLevel.L1_BLOCK_EXACT,
        score=1.0,
        recall_key="1|B1|IHC-CD3",
    )
    base.update(kwargs)
    return MatchCandidate(**base)


def _query(
    *,
    item_code: str = "IHC-CD3",
    state: CheckItemState = CheckItemState.AWAITING,
    region_seq: int | None = 1,
    block_no: str | None = "B1",
    region_desc: str | None = "子宫体",
    order_no: str | None = "ORD-EXAMPLE-0001",
    source: str = SOURCE_BASELINE,
) -> PendingQuery:
    return PendingQuery(
        accession_no=ACCESSION,
        item_code=item_code,
        order_no=order_no,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
        state=state,
        source=source,
    )


def _awaiting_lines(body: str) -> list[str]:
    """正文里的占位条目行（缩进两格，缩进的是条目不是组标题）。"""
    return [
        line
        for line in body.splitlines()
        if line.startswith("  ") and PLACEHOLDER_LABEL in line
    ]


def _merged_lines(body: str) -> list[str]:
    return [
        line
        for line in body.splitlines()
        if line.startswith("  ") and PLACEHOLDER_LABEL not in line
    ]


# --------------------------------------------------------------- 分组渲染


def test_按取材部位分组渲染():
    """两个部位各成一组，组标题写清部位与蜡块。"""
    block_b1 = _block(region_seq=1, region_desc="子宫体", block_no="B1")
    block_b2 = _block(region_seq=2, region_desc="宫颈", block_no="B2")
    draft = render_draft(
        ACCESSION,
        [
            (_entry(item_code="IHC-CD3"), _candidate(block_b1)),
            (_entry(item_code="IHC-CD20", block_no="B2", region_seq=2), _candidate(block_b2)),
        ],
        baseline=_baseline(block_b1, block_b2),
    )

    assert "【子宫体 · 蜡块 B1】" in draft.body
    assert "【宫颈 · 蜡块 B2】" in draft.body
    # 两个部位各一条，条目不会串组
    assert draft.merged_count == 2
    assert [entry.region_seq for entry in draft.entries] == [1, 2]


def test_每条并入项标明来源系统与回报时间():
    """医师看一行就知道：这结果哪来的、什么时候回来的。"""
    draft = render_draft(
        ACCESSION,
        [(_entry(value="弱阳性", source_system="IMMUNO-LAB"), _candidate())],
        baseline=_baseline(),
    )

    line = _merged_lines(draft.body)[0]
    assert "IHC-CD3" in line
    assert "弱阳性" in line
    assert "来源 IMMUNO-LAB" in line
    assert "回报时间 2026-01-01 13:00" in line
    # 依据也在同一行里（命中方式 + 置信等级）
    assert "依据 L1 蜡块号精确（置信 高）" in line


def test_草稿条数与并入条目数一致():
    """草稿正文里的条目行数 == 结构化条目数 == 并入的结果条数。"""
    draft = render_draft(
        ACCESSION,
        [
            (_entry(item_code="IHC-CD3"), _candidate()),
            (_entry(item_code="IHC-ER"), _candidate()),
        ],
        baseline=_baseline(),
    )

    assert draft.merged_count == 2
    assert draft.placeholder_count == 0
    assert len(draft.entries) == 2
    assert len(_merged_lines(draft.body)) == 2


def test_正文与结构化条目同源不打架():
    """结构化条目逐条都能在正文里找到（回写请求体与草稿正文说的是同一件事）。"""
    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3", value="阴性"), _candidate())],
        baseline=_baseline(),
    )

    entry = draft.entries[0]
    assert entry.item_code in draft.body
    assert (entry.value or "") in draft.body
    assert (entry.source_system or "") in draft.body
    assert entry.alignment is not None
    assert "依据" in draft.body


# --------------------------------------------------------------- 未到项占位


def test_已申请未回报的项以待回报占位出现():
    """少写一项，医师就会以为这项没做 —— 占位不能少。"""
    baseline = _baseline(
        _block(ordered_items=("IHC-CD3", "IHC-KI67")),
    )
    queries = [
        _query(item_code="IHC-CD3"),
        _query(item_code="IHC-KI67"),
    ]
    awaiting = derive_awaiting(baseline, queries)

    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3"), _candidate())],
        baseline=baseline,
        awaiting=awaiting,
    )

    lines = _awaiting_lines(draft.body)
    assert len(lines) == 1
    assert "IHC-KI67" in lines[0]
    assert PLACEHOLDER_LABEL in lines[0]
    assert draft.placeholder_count == 1
    # 头部把「还有几项没回来」写清楚
    assert "另 1 项待回报" in draft.body


def test_占位行不写值不写来源不写依据():
    """占位不是一条结果：没有值、没有来源、没有回报时间、没有依据。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))
    awaiting = derive_awaiting(baseline, [_query(item_code="IHC-CD3"), _query(item_code="IHC-KI67")])

    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3"), _candidate())],
        baseline=baseline,
        awaiting=awaiting,
    )

    placeholder = next(entry for entry in draft.entries if entry.placeholder)
    assert placeholder.item_code == "IHC-KI67"
    assert placeholder.value is None
    assert placeholder.source_system is None
    assert placeholder.observed_at is None
    assert placeholder.alignment is None
    # 正文里那一行同样干净
    line = _awaiting_lines(draft.body)[0]
    assert "来源 " not in line
    assert "回报时间 " not in line
    assert "依据 " not in line


def test_已并入的项不重复占位():
    """同一项不能在报告里出现两次（一次有值、一次待回报）。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3",)))
    awaiting = derive_awaiting(baseline, [_query(item_code="IHC-CD3")])

    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3"), _candidate())],
        baseline=baseline,
        awaiting=awaiting,
    )

    assert draft.placeholder_count == 0
    assert draft.body.count("IHC-CD3") == 1


def test_回报过但没进报告的项不算未回报():
    """结果回来了只是没进报告（无法获得 / 空值 / 对不上）—— 写「待回报」是假话。

    这类项在待人工处理清单里，不在草稿里；草稿只写「一次都没回来的」。
    """
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))
    queries = [_query(item_code="IHC-CD3"), _query(item_code="IHC-KI67")]
    reported = [
        # 无法获得：回报了，但拿不到结果
        _entry(item_code="IHC-KI67", value=None, status=ResultStatus.NOT_OBTAINED),
        # 归属对不上：回报了，但不知道是哪一块
        _entry(item_code="IHC-CD3", block_no=None),
    ]

    awaiting = derive_awaiting(baseline, queries, reported=reported)

    assert [item.item_code for item in awaiting] == []


def test_已取消的项不被基线复活成待回报():
    """申请都取消了，基线里那一行还挂着申请 —— 那是「不做了」，不是「还没回来」。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-CD20")))
    queries = [
        _query(item_code="IHC-CD3"),
        _query(
            item_code="IHC-CD20",
            state=CheckItemState.CANCELLED,
            source=SOURCE_ORDER,
        ),
    ]

    awaiting = derive_awaiting(baseline, queries)

    assert [item.item_code for item in awaiting] == ["IHC-CD3"]


def test_已暂停与已终止的项也不占位():
    """暂停 / 终止同理：不等了，就不该在报告里占一行。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67", "IHC-ER")))
    queries = [
        _query(item_code="IHC-CD3"),
        _query(item_code="IHC-KI67", state=CheckItemState.HELD),
        _query(item_code="IHC-ER", state=CheckItemState.TERMINATED),
    ]

    awaiting = derive_awaiting(baseline, queries)

    assert [item.item_code for item in awaiting] == ["IHC-CD3"]


def test_同一项目码在两块上各占一行():
    """同一项目码申请在两块上：定位得开就各占一行，认不准就不猜。"""
    baseline = _baseline(
        _block(region_seq=1, block_no="B1", ordered_items=("IHC-CD20",)),
        _block(region_seq=2, block_no="B2", ordered_items=("IHC-CD20",)),
    )
    queries = [
        _query(item_code="IHC-CD20", region_seq=1, block_no="B1"),
        _query(item_code="IHC-CD20", region_seq=2, block_no="B2"),
    ]

    awaiting = derive_awaiting(baseline, queries)

    assert len(awaiting) == 2
    assert {(item.region_seq, item.block_no) for item in awaiting} == {(1, "B1"), (2, "B2")}


def test_归一化后同一块的蜡块号不重复占位():
    """B-1 与 b01 是同一块 —— 归一化后只占一行。"""
    baseline = _baseline(_block(block_no="B-1", ordered_items=("IHC-CD3",)))
    queries = [
        _query(item_code="IHC-CD3", block_no="b01"),
    ]

    awaiting = derive_awaiting(baseline, queries)

    assert len(awaiting) == 1


def test_回报没带定位信息时只在项目码唯一时才认作同一项():
    """回报完全没带部位信息：认不准就不认 —— 宁可多占一行，也不认错块。"""
    baseline = _baseline(_block(block_no="B1", ordered_items=("IHC-CD3",)))
    queries = [_query(item_code="IHC-CD3")]
    reported = [_entry(item_code="IHC-CD3", region_seq=None, block_no=None)]

    # 报告里只有这一项 → 认作同一项，不再占位
    assert derive_awaiting(baseline, queries, reported=reported) == ()

    # 同一项目码申请在两块上 → 认不准，两块都占位
    baseline_two = _baseline(
        _block(region_seq=1, block_no="B1", ordered_items=("IHC-CD3",)),
        _block(region_seq=2, block_no="B2", ordered_items=("IHC-CD3",)),
    )
    queries_two = [
        _query(item_code="IHC-CD3", region_seq=1, block_no="B1"),
        _query(item_code="IHC-CD3", region_seq=2, block_no="B2"),
    ]
    assert len(derive_awaiting(baseline_two, queries_two, reported=reported)) == 2


# --------------------------------------------------------------- 派生口径


def test_基线已申请项在申请侧没提过时也算未回报():
    """申请侧只报了新增项 —— 基线里已申请但没提到的那些，一样在等结果。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))
    queries = [_query(item_code="IHC-CD3")]
    # CD3 已经回报了（这一版收到过的结果），KI67 一次都没回来
    reported = [_entry(item_code="IHC-CD3")]

    awaiting = derive_awaiting(baseline, queries, reported=reported)

    assert [item.item_code for item in awaiting] == ["IHC-KI67"]


def test_申请侧不带部位信息时用基线补全定位():
    """申请侧往往只报项目码 —— 位置从基线补，占位行才不会跑到别的块上。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3",)))
    queries = [
        _query(item_code="IHC-CD3", region_seq=None, block_no=None, region_desc=None),
    ]

    awaiting = derive_awaiting(baseline, queries)

    assert len(awaiting) == 1
    assert awaiting[0].region_seq == 1
    assert awaiting[0].block_no == "B1"
    assert awaiting[0].region_desc == "子宫体"


def test_基线来源的未到项直接派生():
    """没有申请侧事件时，基线已申请项就是「还在等结果」的全部。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))

    items = awaiting_from_baseline(baseline)

    assert [item.item_code for item in items] == ["IHC-CD3", "IHC-KI67"]


def test_未到项派生结果稳定可复现():
    """同输入两次派生，顺序与内容逐项一致（报告不能因为调度变了而变）。"""
    baseline = _baseline(
        _block(region_seq=1, block_no="B1", ordered_items=("IHC-CD3", "IHC-KI67")),
        _block(region_seq=2, block_no="B2", ordered_items=("IHC-CD20",)),
    )
    queries = [
        _query(item_code="IHC-CD3"),
        _query(item_code="IHC-KI67"),
        _query(item_code="IHC-CD20", region_seq=2, block_no="B2"),
    ]

    first = derive_awaiting(baseline, queries)
    second = derive_awaiting(baseline, queries)

    assert [item.identity for item in first] == [item.identity for item in second]


# --------------------------------------------------------------- 整稿渲染


def test_占位与结果在同一份草稿里互不干扰():
    """一份草稿同时有结果与占位：条数守恒，正文两行都在。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))
    queries = [_query(item_code="IHC-CD3"), _query(item_code="IHC-KI67")]
    awaiting = derive_awaiting(baseline, queries)

    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3"), _candidate())],
        baseline=baseline,
        awaiting=awaiting,
    )

    assert draft.merged_count == 1
    assert draft.placeholder_count == 1
    assert len(draft.entries) == 2
    assert len(_merged_lines(draft.body)) == 1
    assert len(_awaiting_lines(draft.body)) == 1
    # 同一组里先列结果、再列待回报
    assert draft.body.index("IHC-CD3｜") < draft.body.index("IHC-KI67｜")


def test_渲染结果可复现():
    """同输入两次渲染，正文逐字相同（报告不能因为调度变了而变）。"""
    baseline = _baseline(_block(ordered_items=("IHC-CD3", "IHC-KI67")))
    awaiting = derive_awaiting(baseline, [_query(item_code="IHC-CD3"), _query(item_code="IHC-KI67")])

    def _render() -> str:
        return render_draft(
            ACCESSION,
            [(_entry(item_code="IHC-CD3"), _candidate())],
            baseline=baseline,
            awaiting=awaiting,
            created_at="2026-01-01 00:00:00",
        ).body

    assert _render() == _render()


def test_未到项可以显式传入不依赖派生():
    """渲染层不强制派生 —— 调用方给了就照给的渲染（回写与导出共用一套渲染）。"""
    draft = render_draft(
        ACCESSION,
        [(_entry(item_code="IHC-CD3"), _candidate())],
        baseline=_baseline(),
        awaiting=[AwaitingItem(item_code="IHC-KI67", region_seq=1, block_no="B1")],
    )

    assert draft.placeholder_count == 1
    assert "IHC-KI67" in draft.body


# --------------------------------------------------------------- 续跑重绘


def test_续跑后剩下的待回报占位仍在_补齐的那项不再占位(tmp_path):
    """补齐一项重绘草稿：占位是「已申请 − 已回报」算出来的，不是搬上一版的。

    旧版本里占着位的 KI67 补齐后并入草稿（不再是占位）；还没回来的 CD20
    继续占位 —— 少写一项，医师就会以为这项没做。
    """
    from patho_supplement.compose.draft_store import load_draft_body, persist_draft
    from patho_supplement.db import Database
    from patho_supplement.pending.fills import parse_fills
    from patho_supplement.pending.reasons import PendingReason
    from patho_supplement.pending.records import PendingRecord
    from patho_supplement.pending.resume import record_fills, resume_pending
    from patho_supplement.pending.store import PendingStore

    block_b1 = _block(ordered_items=("IHC-CD3", "IHC-KI67"))
    block_b2 = _block(region_seq=2, region_desc="宫颈", block_no="B2", ordered_items=("IHC-CD20",))
    baseline = _baseline(block_b1, block_b2)

    database = Database(tmp_path / "state.db")
    database.init_schema()
    try:
        # v1：CD3 有结果，KI67 与 CD20 都还没回来
        first = render_draft(
            ACCESSION,
            [(_entry(item_code="IHC-CD3"), _candidate(block_b1))],
            baseline=baseline,
            awaiting=[
                AwaitingItem(item_code="IHC-KI67", region_seq=1, block_no="B1"),
                AwaitingItem(item_code="IHC-CD20", region_seq=2, block_no="B2"),
            ],
            version=1,
        )
        persist_draft(database, first)

        # KI67 的结果回来了但没带蜡块号 → 挂起等人补齐
        pending_entry = ResultEntry(
            accession_no=ACCESSION,
            item_code="IHC-KI67",
            item_name="Ki-67 免疫组化",
            value="约15%",
            status=ResultStatus.FINAL,
            observed_at="20260101132000",
            source_system="ihc-lab",
        )
        store = PendingStore(database)
        row = store.save(
            PendingRecord.from_entry(
                pending_entry,
                PendingReason.FIELDS_MISSING,
                missing_fields=("block_no",),
            ),
            max_attempts=3,
        )

        record_fills(
            database, pending_id=row.snapshot_id, fills=parse_fills(["block_no=B1"])
        )
        outcome = resume_pending(
            database,
            pending_id=row.snapshot_id,
            baseline=baseline,
            max_attempts=3,
        )

        assert outcome.merged is True
        body = load_draft_body(database, outcome.draft_id)
        # 补齐的那项已经是结果，不再是占位
        assert "约15%" in body
        # 还没回来的那一项继续占位
        assert "IHC-CD20" in body
        assert PLACEHOLDER_LABEL in body
        # 旧版本链保留：v1 里 KI67 还是占位
        old_body = database.query_value(
            "SELECT body FROM drafts WHERE accession_no = ? AND version = 1",
            (ACCESSION,),
        )
        assert PLACEHOLDER_LABEL in old_body
    finally:
        database.close()
