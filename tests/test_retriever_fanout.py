"""多路召回扇出：并发、失败隔离与单路超时。

覆盖三件事：**每把键走一路**（并���上限可配）、**一路失败不牵连其余路**
（异常被隔离计数、其余路结果完整返回）、**一路超时不阻塞整体**（到点放弃
这一路，其余路照常回流）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from patho_supplement.align.filtering import (
    REASON_INCOMPLETE_KEY,
    REASON_NO_HIT,
    FilterCandidate,
    FilterPolicy,
    FilterResult,
)
from patho_supplement.align.query_derivation import (
    KEY_KIND_BLOCK,
    KEY_KIND_ORDER,
    KEY_KIND_REGION,
    PendingQuery,
    derive_recall_keys,
)
from patho_supplement.align.retriever import (
    REASON_ROUTE_FAILED,
    REASON_ROUTE_TIMEOUT,
    ROUTE_FAILED,
    ROUTE_OK,
    ROUTE_TIMED_OUT,
    default_route_runner,
    fanout_params_from_config,
    fanout_retrieve,
)
from patho_supplement.config import Config
from patho_supplement.ingest.er7 import parse_message
from patho_supplement.ingest.oru_parser import parse_oru
from patho_supplement.models import ResultEntry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "inbox"


# ---------------------------------------------------------------- 夹具


def _entries(file_name: str) -> list[ResultEntry]:
    """读一条脱敏 ORU 夹具，解析成结果条目。"""
    path = FIXTURE_DIR / file_name
    message = parse_message(path.read_text(encoding="utf-8"), source=str(path))
    return parse_oru(message, source_file=path.name)


def _candidates(entries) -> tuple[FilterCandidate, ...]:
    return tuple(
        FilterCandidate(
            region_seq=entry.region_seq,
            block_no=entry.block_no,
            item_code=entry.item_code,
            region_desc=entry.region_desc,
            payload=entry,
        )
        for entry in entries
    )


def _keys() -> tuple:
    """三把键：block / region / order，各对应一项检查。"""
    query = PendingQuery(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        order_no="ORD-EXAMPLE-0001",
        region_seq=1,
        block_no="B1",
        region_desc="子宫体",
    )
    return derive_recall_keys(query)


# ---------------------------------------------------------------- 扇出骨架


def test_fanout_runs_one_route_per_key() -> None:
    keys = _keys()
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    result = fanout_retrieve(keys, candidates)

    assert len(result.routes) == len(keys)
    assert [route.route_id for route in result.routes] == [key.text for key in keys]
    assert all(route.status == ROUTE_OK for route in result.routes)
    assert result.ok_routes == len(keys)
    assert result.failed_routes == 0


def test_route_carries_recall_key_and_source_kind() -> None:
    keys = _keys()
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    result = fanout_retrieve(keys, candidates)

    block_route = next(r for r in result.routes if r.kind == KEY_KIND_BLOCK)
    assert block_route.key.text == keys[0].text
    assert block_route.items
    item = block_route.items[0]
    # 召回键与来源类型标记随结果回流到合并段
    assert item.recall_key == keys[0].text
    assert item.kind == KEY_KIND_BLOCK
    assert item.route_id == block_route.route_id
    assert item.candidate.payload is not None


def test_items_merge_in_route_order_not_completion_order() -> None:
    """结果顺序按路序（= 键序），不受线程完成顺序影响。"""
    keys = _keys()
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    result = fanout_retrieve(keys, candidates)

    expected = [item.route_id for route in result.routes for item in route.items]
    assert [item.route_id for item in result.items] == expected
    assert [route.route_id for route in result.routes] == [key.text for key in keys]


def test_empty_keys_returns_empty_result() -> None:
    result = fanout_retrieve([], ())
    assert result.routes == ()
    assert result.items == ()
    assert result.summary()["routes_total"] == 0


def test_fanout_is_reproducible() -> None:
    """同一批输入两次扇出，候选集逐项相同。"""
    keys = _keys()
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    first = fanout_retrieve(keys, candidates)
    second = fanout_retrieve(keys, candidates)

    assert [item.to_dict() for item in first.items] == [
        item.to_dict() for item in second.items
    ]


# ---------------------------------------------------------------- 并发上限


def test_fanout_respects_max_workers() -> None:
    """并发上限是硬约束：同时在跑的路数不超过 max_workers。"""
    state = {"current": 0, "peak": 0}
    lock = threading.Lock()

    def runner(key):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.08)
        with lock:
            state["current"] -= 1
        return FilterResult(
            key=None, policy=FilterPolicy.MERGE, items=()
        )

    keys = _keys() * 2
    result = fanout_retrieve(keys, (), max_workers=2, route_runner=runner)

    assert state["peak"] <= 2
    assert len(result.routes) == len(keys)


def test_routes_run_concurrently() -> None:
    """多路是并发跑的：总耗时应远小于各路耗时之和。"""
    def runner(key):
        time.sleep(0.3)
        return FilterResult(key=None, policy=FilterPolicy.MERGE, items=())

    keys = _keys()
    started = time.monotonic()
    result = fanout_retrieve(keys, (), max_workers=4, route_runner=runner)
    elapsed = time.monotonic() - started

    assert result.ok_routes == len(keys)
    assert elapsed < 1.0  # 串行需 0.9s 以上（含开销余量）


# ---------------------------------------------------------------- 失败隔离


def test_failing_route_is_isolated_and_counted() -> None:
    """一路抛错：其余路结果完整返回，失败路计入统计。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    def runner(key):
        if key.kind == KEY_KIND_REGION:
            raise ValueError("部位描述脏数据")
        return healthy(key)

    result = fanout_retrieve(keys, candidates, route_runner=runner)

    assert result.failed_route_count == 1
    assert result.ok_routes == len(keys) - 1
    failed = next(route for route in result.routes if route.failed)
    assert failed.status == ROUTE_FAILED
    assert failed.error_type == "ValueError"
    assert "脏数据" in (failed.error or "")
    assert failed.pending is True
    assert failed.pending_reason == REASON_ROUTE_FAILED


def test_failing_route_does_not_reduce_other_routes_items() -> None:
    """注入一路异常前后，其余路的命中条目逐项相同。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    baseline = fanout_retrieve(keys, candidates)
    baseline_items = {
        route.route_id: [item.to_dict() for item in route.items]
        for route in baseline.routes
    }

    def runner(key):
        if key.kind == KEY_KIND_REGION:
            raise RuntimeError("这一路炸了")
        return healthy(key)

    result = fanout_retrieve(keys, candidates, route_runner=runner)
    for route in result.routes:
        if route.ok:
            assert [item.to_dict() for item in route.items] == baseline_items[
                route.route_id
            ]


def test_failed_routes_visible_in_run_summary() -> None:
    """失败路在运行摘要里可见（含异常类型与原因）。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    def runner(key):
        if key.kind == KEY_KIND_ORDER:
            raise KeyError("申请号缺失")
        return healthy(key)

    summary = fanout_retrieve(keys, candidates, route_runner=runner).summary()

    assert summary["routes_total"] == len(keys)
    assert summary["routes_ok"] == len(keys) - 1
    assert summary["routes_failed"] == 1
    assert summary["routes_failed_by_error"] == 1
    assert summary["routes_timed_out"] == 0
    assert len(summary["failures"]) == 1
    failure = summary["failures"][0]
    assert failure["error_type"] == "KeyError"
    assert failure["kind"] == KEY_KIND_ORDER
    assert failure["recall_key"]


def test_all_routes_failing_still_returns_result() -> None:
    """全部路都炸了也不能抛到调用方 —— 扇出本身不失败。"""

    def runner(key):
        raise RuntimeError("全挂")

    result = fanout_retrieve(_keys(), (), route_runner=runner)
    assert result.failed_route_count == len(_keys())
    assert result.items == ()


# ---------------------------------------------------------------- 单路超时


def test_timeout_route_is_abandoned_without_blocking_others() -> None:
    """一路卡住：到点放弃这一路，其余路结果完整返回，整批耗时有界。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    def runner(key):
        if key.kind == KEY_KIND_REGION:
            time.sleep(1.5)  # 卡住
        return healthy(key)

    started = time.monotonic()
    result = fanout_retrieve(keys, candidates, route_runner=runner, route_timeout=0.15)
    elapsed = time.monotonic() - started

    # 整批没有等那一路走完
    assert elapsed < 1.0
    assert result.timed_out_routes == 1
    assert result.ok_routes == len(keys) - 1

    timed_out = next(route for route in result.routes if route.timed_out)
    assert timed_out.status == ROUTE_TIMED_OUT
    assert timed_out.pending_reason == REASON_ROUTE_TIMEOUT
    assert timed_out.items == ()

    # 其余路结果完整
    ok_route = next(route for route in result.routes if route.ok)
    assert ok_route.items


def test_timeout_does_not_block_other_routes_from_returning() -> None:
    """超时路被放弃后，其余路的命中条目不受影响。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)
    baseline = fanout_retrieve(keys, candidates)
    block_items = [
        item.to_dict()
        for item in next(r for r in baseline.routes if r.kind == KEY_KIND_BLOCK).items
    ]

    def runner(key):
        if key.kind == KEY_KIND_REGION:
            time.sleep(1.5)
        return healthy(key)

    result = fanout_retrieve(keys, candidates, route_runner=runner, route_timeout=0.15)
    block_route = next(route for route in result.routes if route.kind == KEY_KIND_BLOCK)
    assert [item.to_dict() for item in block_route.items] == block_items


def test_timeout_is_counted_in_summary() -> None:
    def runner(key):
        if key.kind == KEY_KIND_REGION:
            time.sleep(1.5)
        return FilterResult(key=None, policy=FilterPolicy.MERGE, items=())

    summary = fanout_retrieve(
        _keys(), (), route_runner=runner, route_timeout=0.15
    ).summary()
    assert summary["routes_timed_out"] == 1
    assert summary["routes_failed"] == 1
    assert summary["failures"][0]["status"] == ROUTE_TIMED_OUT
    assert summary["failures"][0]["pending_reason"] == REASON_ROUTE_TIMEOUT


def test_route_returning_illegal_result_is_isolated() -> None:
    """执行体返回的不是过滤结论 → 这一路算失败，其余路不受影响。"""

    def runner(key):
        if key.kind == KEY_KIND_REGION:
            return "这不是过滤结论"
        return FilterResult(key=None, policy=FilterPolicy.MERGE, items=())

    result = fanout_retrieve(_keys(), (), route_runner=runner)

    assert result.failed_route_count == 1
    failed = next(route for route in result.routes if route.failed)
    assert failed.error_type == "RouteContractError"
    assert result.ok_routes == len(_keys()) - 1


# ---------------------------------------------------------------- 参数校验


def test_invalid_max_workers_rejected() -> None:
    with pytest.raises(ValueError, match="并发上限"):
        fanout_retrieve(_keys(), (), max_workers=0)


def test_invalid_route_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="超时"):
        fanout_retrieve(_keys(), (), route_timeout=0)


def test_fanout_params_from_config() -> None:
    config = Config(fanout_max_workers=6, fanout_route_timeout=2.5)
    assert fanout_params_from_config(config) == (6, 2.5)
    # 缺省配置也有确定值
    assert fanout_params_from_config(Config()) == (4, 5.0)


# ---------------------------------------------------------------- 默认召回体


def test_default_runner_hard_hits_on_block_key() -> None:
    """block 键（部位序号 + 蜡块号 + 项目码）字面全中 → 硬命中。"""
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    keys = _keys()
    result = fanout_retrieve(keys, candidates, policy=FilterPolicy.REPLACE)

    block_route = next(route for route in result.routes if route.kind == KEY_KIND_BLOCK)
    assert len(block_route.items) == 1
    assert block_route.items[0].score == 1.0
    assert block_route.items[0].pending is False


def test_zero_hit_route_is_marked_pending_not_dropped() -> None:
    """硬过滤 0 命中 → 这一路挂起（不静默丢弃）。"""
    from patho_supplement.align.query_derivation import RecallKey

    key = RecallKey(
        kind=KEY_KIND_BLOCK,
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-NOT-ORDERED",
        region_seq=9,
        block_no="B9",
    )
    result = fanout_retrieve([key], (), policy=FilterPolicy.REPLACE)

    route = result.routes[0]
    assert route.pending is True
    assert route.pending_reason == REASON_NO_HIT
    assert result.pending_routes == 1


def test_incomplete_key_route_is_marked_pending() -> None:
    """order 键没有定位字段 → 键不可用 → 挂起（根因是「键字段不全」）。"""
    from patho_supplement.align.query_derivation import RecallKey

    key = RecallKey(
        kind=KEY_KIND_ORDER,
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        order_no="ORD-EXAMPLE-0001",
    )
    result = fanout_retrieve([key], (), policy=FilterPolicy.REPLACE)

    route = result.routes[0]
    assert route.pending is True
    assert route.pending_reason == REASON_INCOMPLETE_KEY


def test_merge_policy_degrades_to_fuzzy_and_marks_pending() -> None:
    """MERGE 策略：0 命中降级模糊，条目逐条标 pending。"""
    from patho_supplement.align.query_derivation import RecallKey

    key = RecallKey(
        kind=KEY_KIND_REGION,
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        region_seq=1,
        region_desc="宫体",
    )
    candidates = _candidates(_entries("acc-example-0001-02-cd20-alias.hl7"))
    # 部位序号冲突 → 硬过滤 0 命中 → 降级
    conflicted = RecallKey(
        kind=KEY_KIND_REGION,
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        region_seq=7,
        region_desc="宫体",
    )
    result = fanout_retrieve([conflicted], candidates, policy=FilterPolicy.MERGE)

    route = result.routes[0]
    assert route.pending is True
    assert route.items
    assert all(item.pending for item in route.items)
    assert all(item.pending_reason for item in route.items)
    assert key.kind == KEY_KIND_REGION


# ---------------------------------------------------------------- 真实夹具


def test_fanout_on_real_desensitized_fixtures() -> None:
    """真实脱敏夹具：部位别名（宫体）也该被硬过滤对上同一块。"""
    entries = _entries("acc-example-0001-02-cd20-alias.hl7")
    candidates = _candidates(entries)
    keys = _keys()

    result = fanout_retrieve(keys, candidates, policy=FilterPolicy.MERGE)

    block_route = next(route for route in result.routes if route.kind == KEY_KIND_BLOCK)
    assert len(block_route.items) == 1
    assert block_route.items[0].candidate.block_no == "B1"
    assert block_route.items[0].candidate.item_code == "IHC-CD20"

    region_route = next(route for route in result.routes if route.kind == KEY_KIND_REGION)
    assert region_route.items
    assert region_route.items[0].candidate.region_desc == "宫体"

    # 申请号那一路没有定位字段 → 挂起，不进结果集
    order_route = next(route for route in result.routes if route.kind == KEY_KIND_ORDER)
    assert order_route.pending is True
    assert order_route.pending_reason == REASON_INCOMPLETE_KEY
    assert all(item.pending for item in order_route.items)
