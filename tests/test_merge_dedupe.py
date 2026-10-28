"""合并、去重与按分排序。

覆盖三件事：**各路结果合并成候选集**、**按业务幂等键去重（保留分最高的一条）**、
**按分降序且同分有稳定序**（结果与召回顺序、线程调度无关）。

最后一条是本任务的核心护栏：并行扇出的完成顺序受调度影响，若排序只按分数
（同分靠输入顺序定），同一批数据两次跑出来的候选集就可能不同 —— 报告里
条目的先后会变，签出去的补充报告也会跟着变。
"""

from __future__ import annotations

import json
from pathlib import Path

from patho_supplement.align.filtering import FilterCandidate, FilterPolicy
from patho_supplement.align.merger import (
    business_key_of,
    dedupe_by_business_key,
    merge_and_rank,
    merge_fanout,
    merge_routes,
    order_key,
    rank_by_score,
)
from patho_supplement.align.query_derivation import (
    KEY_KIND_BLOCK,
    KEY_KIND_REGION,
    PendingQuery,
    derive_recall_keys,
)
from patho_supplement.align.retriever import RetrievedItem, fanout_retrieve
from patho_supplement.ingest.er7 import parse_message
from patho_supplement.ingest.idempotency import build_idempotency_key
from patho_supplement.ingest.oru_parser import parse_oru
from patho_supplement.models import ResultEntry, ResultStatus

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "inbox"


# ---------------------------------------------------------------- 夹具


def _entry(
    *,
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-CD20",
    block_no: str | None = "B1",
    region_seq: int | None = 1,
    region_desc: str | None = "子宫体",
    observed_at: str | None = "20260101120000",
    status: ResultStatus = ResultStatus.FINAL,
) -> ResultEntry:
    """一条脱敏结果回报。"""
    return ResultEntry(
        accession_no=accession_no,
        item_code=item_code,
        item_name="CD20 免疫组化",
        value="阳性",
        status=status,
        observed_at=observed_at,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
    )


def _key(
    *,
    kind: str = KEY_KIND_BLOCK,
    accession_no: str = "ACC-EXAMPLE-0001",
    item_code: str = "IHC-CD20",
    region_seq: int | None = 1,
    block_no: str | None = "B1",
    region_desc: str | None = "子宫体",
):
    """一把检索键。"""
    from patho_supplement.align.query_derivation import RecallKey

    return RecallKey(
        kind=kind,
        accession_no=accession_no,
        item_code=item_code,
        region_seq=region_seq,
        block_no=block_no,
        region_desc=region_desc,
    )


def _item(entry: ResultEntry, key, *, score: float = 1.0) -> RetrievedItem:
    """一条召回项（候选带上业务身份）。"""
    return RetrievedItem(
        candidate=FilterCandidate.from_entry(entry),
        key=key,
        route_id=key.text,
        kind=key.kind,
        score=score,
    )


def _parse(file_name: str) -> list[ResultEntry]:
    path = FIXTURE_DIR / file_name
    message = parse_message(path.read_text(encoding="utf-8"), source=str(path))
    return parse_oru(message, source_file=path.name)


# ---------------------------------------------------------------- 合并段


def test_merge_routes_concatenates_in_route_order() -> None:
    """合并段：各路结果按路序（= 键序）合并成候选集。"""
    key_a = _key(block_no="B1")
    key_b = _key(kind=KEY_KIND_REGION, block_no=None, region_desc="宫颈")
    entry_a = _entry()
    entry_b = _entry(block_no="B2", region_seq=2, region_desc="宫颈")

    merged = merge_routes(
        [
            _route(key_a, [_item(entry_a, key_a)]),
            _route(key_b, [_item(entry_b, key_b)]),
        ]
    )

    assert [item.route_id for item in merged] == [key_a.text, key_b.text]


def test_merge_routes_ignores_failed_route_items() -> None:
    """失败与超时的路没有条目，不参与合并（它们的失败另有统计）。"""
    key = _key()
    routes = [_route(key, [], status="failed"), _route(key, [_item(_entry(), key)])]
    merged = merge_routes(routes)

    assert len(merged) == 1


# ---------------------------------------------------------------- 去重段


def test_dedupe_removes_same_business_key() -> None:
    """同一业务幂等键的多条命中只留一条，其余计入 duplicates。"""
    key_block = _key(block_no="B1")
    key_region = _key(kind=KEY_KIND_REGION, block_no=None)
    entry = _entry()

    outcome = dedupe_by_business_key(
        [_item(entry, key_block), _item(entry, key_region)]
    )

    assert outcome.count == 1
    assert outcome.duplicates == 1
    assert len(outcome.dropped_keys) == 1


def test_dedupe_keeps_highest_score() -> None:
    """去重保留分最高的一条。"""
    key_low = _key(block_no="B1")
    key_high = _key(kind=KEY_KIND_REGION, block_no=None)
    entry = _entry()

    outcome = dedupe_by_business_key(
        [_item(entry, key_low, score=0.42), _item(entry, key_high, score=0.91)]
    )

    assert outcome.count == 1
    assert outcome.items[0].score == 0.91
    assert outcome.items[0].key.text == key_high.text


def test_dedupe_count_equals_unique_business_keys() -> None:
    """去重后条数 == 业务幂等键去重后的条数。"""
    entries = [
        _entry(block_no="B1", region_seq=1),
        _entry(block_no="B1", region_seq=1),  # 与上一条同键
        _entry(block_no="B2", region_seq=2),
        _entry(block_no="B2", region_seq=2),
        _entry(block_no="B3", region_seq=3),
    ]
    key = _key()
    items = [_item(entry, key) for entry in entries]

    outcome = dedupe_by_business_key(items)
    expected = len({business_key_of(item.candidate) for item in items})

    assert outcome.count == expected == 3
    assert outcome.duplicates == len(items) - expected


def test_dedupe_keeps_entries_without_accession_number() -> None:
    """缺病理号 → 算不出业务幂等键 → 全部保留，一条都不合并。

    去重的方向性：合并错了是「两份结果被吞成一份，报告少一项而没人知道」；
    不合并顶多候选集大一点，下游还会再判一次。宁可多留，不可少吞。
    """
    key = _key()
    anonymous = ResultEntry(
        accession_no="",
        item_code="IHC-CD20",
        value="阳性",
        observed_at="20260101120000",
        region_seq=1,
        block_no="B1",
    )
    items = [_item(anonymous, key), _item(anonymous, key)]

    outcome = dedupe_by_business_key(items)

    assert outcome.count == 2
    assert outcome.duplicates == 0
    assert outcome.unkeyed == 2


def test_business_key_matches_ingest_ledger() -> None:
    """业务幂等键与入站层幂等账本是同一把钥匙。

    两层若各拼各的，同一条结果会在一层被去重、在另一层被重复计数，
    核对时对不上。这条断言是那把「唯一的钥匙」的护栏。
    """
    entry = _entry()
    candidate = FilterCandidate.from_entry(entry)

    assert business_key_of(candidate) == build_idempotency_key(entry)


def test_business_key_distinguishes_observed_time_and_status() -> None:
    """观察时间与结果状态不同 → 业务幂等键不同。"""
    key = _key()
    base = _entry()
    later = _entry(observed_at="20260101130000")
    preliminary = _entry(status=ResultStatus.PRELIMINARY)

    keys = {
        business_key_of(item.candidate)
        for item in (_item(base, key), _item(later, key), _item(preliminary, key))
    }

    assert len(keys) == 3


# ---------------------------------------------------------------- 排序段


def test_rank_by_score_descending() -> None:
    """按对齐分降序。"""
    key = _key()
    items = [
        _item(_entry(block_no="B1"), key, score=0.2),
        _item(_entry(block_no="B2", region_seq=2), key, score=0.9),
        _item(_entry(block_no="B3", region_seq=3), key, score=0.5),
    ]

    ranked = rank_by_score(items)

    assert [item.score for item in ranked] == [0.9, 0.5, 0.2]


def test_same_score_order_independent_of_input() -> None:
    """同分候选的排序结果与输入顺序无关。

    这是本任务的核心护栏：并行扇出的完成顺序受调度影响，若同分靠输入
    顺序定先后，同一批数据两次跑出来的候选集就可能不同。
    """
    key_a = _key(block_no="B1")
    key_b = _key(block_no="B2", region_seq=2)
    key_c = _key(block_no="B3", region_seq=3)
    items = [
        _item(_entry(block_no="B1"), key_a, score=0.5),
        _item(_entry(block_no="B2", region_seq=2), key_b, score=0.5),
        _item(_entry(block_no="B3", region_seq=3), key_c, score=0.5),
    ]

    forward = rank_by_score(items)
    backward = rank_by_score(list(reversed(items)))
    shuffled = rank_by_score([items[2], items[0], items[1]])

    assert [item.key.text for item in forward] == [item.key.text for item in backward]
    assert [item.key.text for item in forward] == [item.key.text for item in shuffled]
    # 同分时按召回键字典序 —— 与输入顺序无关的确定性次序
    assert [item.key.text for item in forward] == sorted(item.key.text for item in items)


def test_result_independent_of_route_order() -> None:
    """结果与召回路序无关。"""
    key_a = _key(block_no="B1")
    key_b = _key(block_no="B2", region_seq=2)
    entry_a = _entry(block_no="B1")
    entry_b = _entry(block_no="B2", region_seq=2)

    forward = merge_fanout(
        [_route(key_a, [_item(entry_a, key_a)]), _route(key_b, [_item(entry_b, key_b)])]
    )
    backward = merge_fanout(
        [_route(key_b, [_item(entry_b, key_b)]), _route(key_a, [_item(entry_a, key_a)])]
    )

    assert [item.key.text for item in forward.items] == [
        item.key.text for item in backward.items
    ]


def test_nan_score_does_not_break_sorting() -> None:
    """分是 NaN 时按 0.0 处理，排序不崩、结果仍确定。"""
    key_a = _key(block_no="B1")
    key_b = _key(block_no="B2", region_seq=2)
    items = [
        _item(_entry(block_no="B1"), key_a, score=float("nan")),
        _item(_entry(block_no="B2", region_seq=2), key_b, score=0.3),
    ]

    ranked = rank_by_score(items)
    again = rank_by_score(list(reversed(items)))

    assert [item.key.text for item in ranked] == [item.key.text for item in again]
    assert ranked[0].score == 0.3


def test_order_key_is_total_order_for_distinct_candidates() -> None:
    """次序键对不同候选给出严格可比的次序（不靠输入顺序兜底）。"""
    key = _key()
    items = [
        _item(_entry(block_no="B1"), key, score=0.5),
        _item(_entry(block_no="B2", region_seq=2), key, score=0.5),
    ]

    keys = [order_key(item) for item in items]

    assert keys[0] < keys[1]
    assert keys[1] > keys[0]


# ---------------------------------------------------------------- 三段合起来


def test_merge_and_rank_summary() -> None:
    """摘要里的计数与实际条数一致，且可序列化。"""
    key_a = _key(block_no="B1")
    key_b = _key(block_no="B2", region_seq=2)
    entry_a = _entry(block_no="B1")
    entry_b = _entry(block_no="B2", region_seq=2)

    outcome = merge_and_rank(
        [
            _item(entry_a, key_a, score=0.4),
            _item(entry_a, key_a, score=0.8),  # 同键重复
            _item(entry_b, key_b, score=0.6),
        ]
    )
    summary = outcome.summary()

    assert outcome.merged_count == 3
    assert outcome.count == 2
    assert outcome.duplicates == 1
    assert summary["merged"] == 3
    assert summary["kept"] == 2
    assert summary["duplicates"] == 1
    json.dumps(summary)  # 必须可序列化


def test_merge_and_rank_is_reproducible() -> None:
    """同一批输入两次跑出来的候选集逐项相同。"""
    key_a = _key(block_no="B1")
    key_b = _key(block_no="B2", region_seq=2)
    items = [
        _item(_entry(block_no="B1"), key_a, score=0.5),
        _item(_entry(block_no="B2", region_seq=2), key_b, score=0.5),
    ]

    first = merge_and_rank(items)
    second = merge_and_rank(items)

    assert [item.to_dict() for item in first.items] == [
        item.to_dict() for item in second.items
    ]


# ---------------------------------------------------------------- 端到端（脱敏夹具）


def test_merge_fanout_on_real_fixture() -> None:
    """真实脱敏夹具：扇出 → 合并 → 去重 → 排序，条数守恒且键唯一。"""
    entries = _parse("acc-example-0001-02-cd20-alias.hl7")
    candidates = tuple(FilterCandidate.from_entry(entry) for entry in entries)
    query = PendingQuery(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        order_no="ORD-EXAMPLE-0001",
        region_seq=1,
        block_no="B1",
        region_desc="子宫体",
    )
    keys = derive_recall_keys(query)

    result = fanout_retrieve(keys, candidates, policy=FilterPolicy.MERGE)
    outcome = merge_fanout(result.routes)

    # 合并条数 = 各路条数之和
    assert outcome.merged_count == sum(len(route.items) for route in result.routes)
    # 去重后条数 = 不同业务幂等键的条数
    keys_seen = {business_key_of(item.candidate) for item in outcome.items}
    assert len(keys_seen) == outcome.count
    # 同一批输入再来一次，结果逐项相同
    again = merge_fanout(
        fanout_retrieve(keys, candidates, policy=FilterPolicy.MERGE).routes
    )
    assert [item.to_dict() for item in outcome.items] == [
        item.to_dict() for item in again.items
    ]


# ---------------------------------------------------------------- 内部工具


def _route(key, items, *, status: str = "ok"):
    """造一路召回结论。"""
    from patho_supplement.align.retriever import RouteOutcome

    return RouteOutcome(
        route_id=key.text,
        key=key,
        kind=key.kind,
        status=status,
        items=tuple(items),
        pending=False,
    )
