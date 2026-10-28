"""对照测试组：扇出顺序无关性 / 过滤降级 / 失败隔离。

三条对照各自**成对跑同一个场景的两侧**，再把两侧结果逐项比对。这是本产品敢
承诺「错绑定数 = 0」的回归护栏 —— 一个顺序都不确定的系统，没资格承诺结果
不变。

- **对照 A 扇出顺序无关性**：同一批结果用两种不同扇出顺序执行，去重后条数与
  排序结果完全一致。并行扇出的完成顺序受线程调度影响；若最终候选集跟着调度
  变，同一批数据两次跑出来的草稿条目顺序就可能不同 —— 签出去的补充报告也
  跟着变。
- **对照 B 过滤降级**：硬过滤命中 0 条时输出条数不为 0 且每条带 pending 标记。
  硬过滤没命中只说明「字面对不上」，不代表这条结果不存在；悄悄丢掉它等于让
  一份到手的加做结果凭空消失，而报告里少一项医师看不出来。
- **对照 C 失败隔离**：一路召回注入异常时其余路结果仍完整返回，异常被隔离
  计数。一把检索键对应「某一块的某一项检查」，一路失败不得牵连其余路。

全部对照固定随机种子（:data:`SEED`），保证结果可复现 —— 对照失败时能原样
重跑出同一条失败。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from patho_supplement.align.filtering import (
    REASON_FUZZY_DEGRADED,
    REASON_NO_HIT,
    FilterCandidate,
    FilterPolicy,
    apply_filter,
    build_filter_key,
)
from patho_supplement.align.merger import business_key_of, merge_fanout
from patho_supplement.align.query_derivation import (
    KEY_KIND_BLOCK,
    KEY_KIND_ORDER,
    KEY_KIND_REGION,
    PendingQuery,
    RecallKey,
    derive_recall_keys,
)
from patho_supplement.align.retriever import (
    REASON_ROUTE_FAILED,
    default_route_runner,
    fanout_retrieve,
)
from patho_supplement.ingest.er7 import parse_message
from patho_supplement.ingest.oru_parser import parse_oru
from patho_supplement.models import ResultEntry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "inbox"

#: 全部对照共用的随机种子。固定种子 = 同一组对照每次跑出来的乱序排列都一样。
SEED = 20260101


# ---------------------------------------------------------------- 夹具


def _parse_oru(file_name: str) -> list[ResultEntry]:
    """读一条脱敏 ORU 夹具，解析成结果条目。"""
    path = FIXTURE_DIR / file_name
    message = parse_message(path.read_text(encoding="utf-8"), source=str(path))
    return parse_oru(message, source_file=path.name)


def _candidate_pool() -> tuple[FilterCandidate, ...]:
    """全部 ORU 夹具解析成候选池（带业务身份，去重段才有活干）。

    候选用 :meth:`FilterCandidate.from_entry` 构造 —— 病理号 / 观察时间 /
    结果状态都带上，去重段才能按业务幂等键合并；手工拼候选漏了病理号就会退
    化成「一条都不合并」，对照就白跑了。

    池子里含一份**重复投递**（同一份 KI67 回报投了两次），去重段因此有真实
    的重复可去 —— 若没有重复，对照 A 的「去重后条数」与「合并条数」就永远
    相等，去重这一步等于没被验到。
    """
    entries: list[ResultEntry] = []
    for path in sorted(FIXTURE_DIR.glob("*.hl7")):
        if "ORU^R01" not in path.read_text(encoding="utf-8"):
            continue  # 申请侧报文（ORM^O01）不是结果回报
        entries.extend(_parse_oru(path.name))
    return tuple(FilterCandidate.from_entry(entry) for entry in entries)


def _pending_queries() -> tuple[PendingQuery, ...]:
    """待查项集合：基线已申请项 + 回报里有、基线没登记的那一项。

    基线（``baseline.json``）给出各部位的已申请项目码；回报里还多了一项
    KI67（申请号 ORD-EXAMPLE-0004，基线未登记）。把它也算进待查项，KI67 的
    两份重复回报才会落进同一个合并集，去重段才有东西可去。
    """
    baseline = json.loads(
        (FIXTURE_DIR / "baseline.json").read_text(encoding="utf-8")
    )
    queries: list[PendingQuery] = []
    for report in baseline["reports"]:
        for block in report["blocks"]:
            for item_code in block["ordered_items"]:
                queries.append(
                    PendingQuery(
                        accession_no=report["accession_no"],
                        item_code=item_code,
                        order_no=block["order_no"],
                        region_seq=block["region_seq"],
                        block_no=block["block_no"],
                        region_desc=block["region_desc"],
                    )
                )
    queries.append(
        PendingQuery(
            accession_no="ACC-EXAMPLE-0001",
            item_code="IHC-KI67",
            order_no="ORD-EXAMPLE-0004",
            region_seq=1,
            block_no="B1",
            region_desc="子宫体",
        )
    )
    return tuple(queries)


def _all_recall_keys() -> tuple[RecallKey, ...]:
    """全部待查项派生出的检索键（按派生顺序）。"""
    keys: list[RecallKey] = []
    for query in _pending_queries():
        keys.extend(derive_recall_keys(query))
    return tuple(keys)


def _shuffled(keys: tuple[RecallKey, ...], seed: int = SEED) -> tuple[RecallKey, ...]:
    """固定种子的乱序排列。

    对照 A 的「另一种扇出顺序」用它 —— 固定种子保证每次跑出来的排列一样，
    对照失败时能原样复现。
    """
    items = list(keys)
    random.Random(seed).shuffle(items)
    return tuple(items)


def _item_dicts(items) -> list[dict]:
    """候选集的可比对形式（逐项 dict）。"""
    return [item.to_dict() for item in items]


def _stable_summary(result) -> dict:
    """去掉耗时字段后的运行摘要。

    摘要里的 ``elapsed_ms`` 是**实测耗时**，两次跑必然不同 —— 拿它做「两次
    结果是否一致」的比较，比的就不再是结果而是机器快慢。这里把耗时字段剔
    掉，其余（路数、失败数、失败原因、条目数）逐项可比。
    """
    summary = dict(result.summary())
    summary.pop("elapsed_ms", None)
    failures = []
    for failure in summary.get("failures", []):
        failure = dict(failure)
        failure.pop("elapsed_ms", None)
        failures.append(failure)
    summary["failures"] = failures
    return summary


def _failure_view(route) -> dict:
    """失败路的可比对视图（同样剔掉耗时字段）。"""
    view = dict(route.to_dict())
    view.pop("elapsed_ms", None)
    return view


# ---------------------------------------------------------------- 对照 A：扇出顺序无关性


def test_fanout_order_does_not_change_deduped_count() -> None:
    """对照 A（条数）：两种扇出顺序去重后的条数完全一致。

    同一批结果、同一个候选池，只把检索键的顺序换掉 —— 去重后的条数若变了，
    说明去重结果依赖扇出顺序，那是「同一份报告两次跑出来不一样」的温床。
    """
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    shuffled = _shuffled(keys)
    assert keys != shuffled, "对照组必须真的换了顺序，否则本对照不成立"

    forward = merge_fanout(fanout_retrieve(keys, candidates).routes)
    backward = merge_fanout(fanout_retrieve(shuffled, candidates).routes)

    assert forward.count == backward.count


def test_fanout_order_does_not_change_sorted_result() -> None:
    """对照 A（内容）：两种扇出顺序的最终候选集逐项完全相同。

    这一条才是对照 A 的本体：不只是条数一样，是**每一条的召回键、来源类型、
    分数、部位、病理号、观察时间都一模一样**。
    """
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    shuffled = _shuffled(keys)

    forward = merge_fanout(fanout_retrieve(keys, candidates).routes)
    backward = merge_fanout(fanout_retrieve(shuffled, candidates).routes)

    assert _item_dicts(forward.items) == _item_dicts(backward.items)


def test_fanout_order_changes_route_order_but_not_final_result() -> None:
    """对照 A（前提）：扇出顺序确实变了，最终结果却没变。

    顺序没变的对照是空跑 —— 这一条先证明「乱序生效了」，再由上面两条证明
    「结果没跟着变」。
    """
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    shuffled = _shuffled(keys)

    forward = fanout_retrieve(keys, candidates)
    backward = fanout_retrieve(shuffled, candidates)

    assert [route.route_id for route in forward.routes] != [
        route.route_id for route in backward.routes
    ]

    # 合并段（去重前）的条目集合是同一个，只是先后不同
    forward_merged = _item_dicts(forward.items)
    backward_merged = _item_dicts(backward.items)
    assert forward_merged != backward_merged
    assert sorted(map(json.dumps, forward_merged)) == sorted(
        map(json.dumps, backward_merged)
    )


def test_dedupe_actually_merged_something_in_the_comparison() -> None:
    """对照 A（对照本身有效）：合并条数 > 去重后条数。

    若候选池里没有重复投递，去重段无事可做，对照 A 就退化成「顺序无关的
    排序」这一条，验不到去重。断言合并条数确实大于去重后条数，保证对照
    A 覆盖的是「合并 → 去重 → 排序」整条链。
    """
    outcome = merge_fanout(fanout_retrieve(_all_recall_keys(), _candidate_pool()).routes)

    assert outcome.merged_count > outcome.count
    assert outcome.duplicates > 0


def test_fanout_order_comparison_is_reproducible_with_fixed_seed() -> None:
    """对照 A（可复现）：固定种子下两次乱序排列完全一致，两次结果也一致。"""
    keys = _all_recall_keys()

    first_shuffle = _shuffled(keys)
    second_shuffle = _shuffled(keys)

    assert [key.text for key in first_shuffle] == [key.text for key in second_shuffle]

    candidates = _candidate_pool()
    first = merge_fanout(fanout_retrieve(first_shuffle, candidates).routes)
    second = merge_fanout(fanout_retrieve(second_shuffle, candidates).routes)

    assert _item_dicts(first.items) == _item_dicts(second.items)
    assert _stable_summary(first) == _stable_summary(second)


# ---------------------------------------------------------------- 对照 B：过滤降级


def _conflicting_key() -> RecallKey:
    """一把硬过滤必然命中 0 条的键（部位序号与候选冲突）。

    蜡块号写 B9、部位序号写 9 —— 候选池里没有这一块，硬过滤必然 0 命中。
    """
    return RecallKey(
        kind=KEY_KIND_BLOCK,
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD20",
        region_seq=9,
        block_no="B9",
        region_desc="子宫体",
    )


def test_zero_hit_degrades_to_non_empty_result_with_pending_mark() -> None:
    """对照 B（降级）：硬过滤 0 命中 → 输出条数不为 0，且每条都带 pending。

    降级出来的每一条都是「像」，不是「是」—— 它们只能进待人工处理清单，
    不能直接并入草稿。**条数不为 0** 是这一条的关键：命中 0 条时若输出为空
    且不标 pending，这条结果就被静默丢掉了。
    """
    result = fanout_retrieve([_conflicting_key()], _candidate_pool(), policy=FilterPolicy.MERGE)

    route = result.routes[0]
    assert route.items, "硬过滤 0 命中时输出条数必须不为 0"
    assert route.pending is True
    assert route.pending_reason == REASON_FUZZY_DEGRADED
    assert all(item.pending for item in route.items)
    assert all(item.pending_reason for item in route.items)


def test_zero_hit_degradation_keeps_item_code_constraint() -> None:
    """对照 B（不放宽项目码）：降级只放宽「位置」，不放宽「项目」。

    CD20 的待查项降级出来的候选里不能混进 CK20 的回报 —— 放宽项目码就是
    把两项检查混成一项，是「误并入」。
    """
    key = _conflicting_key()
    outcome = apply_filter(
        build_filter_key(key.region_seq, key.block_no, key.item_code),
        _candidate_pool(),
        policy=FilterPolicy.MERGE,
    )

    assert outcome.hard_hits == 0
    assert outcome.fuzzy_hits == outcome.count > 0
    assert outcome.degraded is True
    assert outcome.pending is True
    assert outcome.pending_reason == REASON_FUZZY_DEGRADED
    assert all(
        item.candidate.normalized_item_code == "IHC-CD20" for item in outcome.items
    )
    assert all(item.pending for item in outcome.items)


def test_zero_hit_under_replace_policy_is_pending_not_dropped() -> None:
    """对照 B（另一档策略）：REPLACE 下 0 命中 → 结果集为空，但本条仍挂起。

    REPLACE 语义是「硬过滤完全覆盖检索空间」，模糊候选不进结果集。但**本条
    仍必须显式挂起** —— 否则这条结果就凭空消失了，报告里少一项没人知道。
    """
    result = fanout_retrieve([_conflicting_key()], _candidate_pool(), policy=FilterPolicy.REPLACE)

    route = result.routes[0]
    assert route.items == ()
    assert route.pending is True
    assert route.pending_reason == REASON_NO_HIT
    assert result.pending_routes == 1


def test_degradation_comparison_is_reproducible() -> None:
    """对照 B（可复现）：两次降级跑出来的条目逐项相同。"""
    candidates = _candidate_pool()
    key = _conflicting_key()

    first = fanout_retrieve([key], candidates, policy=FilterPolicy.MERGE)
    second = fanout_retrieve([key], candidates, policy=FilterPolicy.MERGE)

    assert _item_dicts(first.routes[0].items) == _item_dicts(second.routes[0].items)
    assert _stable_summary(first) == _stable_summary(second)


# ---------------------------------------------------------------- 对照 C：失败隔离


def _target_region_key() -> RecallKey:
    """挑一把部位描述那一路的键，作为注入异常的目标。

    按**具体这把键**注入（而不是按类型）—— 本对照要的是「恰好一路坏掉」，
    按类型注入会一次打掉全部部位路，验的就不是隔离而是「全挂」。
    """
    return next(key for key in _all_recall_keys() if key.kind == KEY_KIND_REGION)


def _inject_failure(key: RecallKey, healthy, error: Exception):
    """包一层执行体：指定这把键抛错，其余路照常走。"""

    def runner(route_key):
        if route_key.text == key.text:
            raise error
        return healthy(route_key)

    return runner


def test_failing_route_leaves_other_routes_intact() -> None:
    """对照 C（主体）：注入一路异常后，其余路的命中条目逐项不变。

    一把检索键对应「某一块的某一项检查」。这一路的报文脏了、蜡块号写错了，
    都不该让**别的块**的结果跟着消失 —— 报告里少一项，医师是看不出来的。
    """
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    baseline = fanout_retrieve(keys, candidates)
    baseline_items = {
        route.route_id: _item_dicts(route.items) for route in baseline.routes
    }

    injected = fanout_retrieve(
        keys,
        candidates,
        route_runner=_inject_failure(
            _target_region_key(), healthy, ValueError("部位描述脏数据")
        ),
    )

    for route in injected.routes:
        if route.ok:
            assert _item_dicts(route.items) == baseline_items[route.route_id]


def test_failing_route_is_isolated_and_counted() -> None:
    """对照 C（计数）：异常被隔离计数，并在运行摘要里可见。"""
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    result = fanout_retrieve(
        keys,
        candidates,
        route_runner=_inject_failure(
            _target_region_key(), healthy, ValueError("部位描述脏数据")
        ),
    )

    assert result.failed_route_count == 1
    assert result.ok_routes == len(keys) - 1

    failed = next(route for route in result.routes if route.failed)
    assert failed.items == ()
    assert failed.pending is True
    assert failed.pending_reason == REASON_ROUTE_FAILED
    assert failed.error_type == "ValueError"

    summary = result.summary()
    assert summary["routes_failed"] == 1
    assert summary["routes_failed_by_error"] == 1
    assert summary["failures"][0]["error_type"] == "ValueError"


def test_failing_route_does_not_shrink_final_candidate_set() -> None:
    """对照 C（去向）：失败路的条目不并入候选集，其余路的条目一条不少。

    失败路的条目本来就没有（那一路没跑出来），所以最终候选集与「不带异常」
    时一致 —— 差别只在统计里：失败路被计了数，而不是静默消失。
    """
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    baseline = merge_fanout(fanout_retrieve(keys, candidates).routes)
    injected = merge_fanout(
        fanout_retrieve(
            keys,
            candidates,
                route_runner=_inject_failure(
                    _target_region_key(), healthy, ValueError("部位描述脏数据")
                ),
        ).routes
    )

    assert injected.count == baseline.count
    assert injected.merged_count <= baseline.merged_count


def test_failure_isolation_comparison_is_reproducible() -> None:
    """对照 C（可复现）：两次注入同一异常，摘要与失败路逐项相同。"""
    candidates = _candidate_pool()
    keys = _all_recall_keys()
    healthy = default_route_runner(candidates, FilterPolicy.MERGE)

    def run():
        return fanout_retrieve(
            keys,
            candidates,
                route_runner=_inject_failure(
                    _target_region_key(), healthy, ValueError("部位描述脏数据")
                ),
        )

    first, second = run(), run()

    assert _stable_summary(first) == _stable_summary(second)
    assert [_failure_view(route) for route in first.failures] == [
        _failure_view(route) for route in second.failures
    ]


# ---------------------------------------------------------------- 三条对照的公共前提


def test_comparison_pool_has_business_identity() -> None:
    """公共前提：候选都带业务身份，去重段算得出幂等键。

    缺病理号的候选算不出幂等键，去重段会「全部保留」—— 那时对照 A 验的
    就不是去重，而是一条没去过的候选集。这条断言把前提钉死。
    """
    candidates = _candidate_pool()

    assert candidates
    assert all(candidate.accession_no for candidate in candidates)
    assert all(business_key_of(candidate) for candidate in candidates)


def test_comparison_uses_real_fixtures() -> None:
    """公共前提：对照跑的是真实脱敏夹具，不是手搓的假数据。"""
    candidates = _candidate_pool()
    keys = _all_recall_keys()

    assert len(candidates) >= 8
    assert len(keys) >= 20
    assert {key.kind for key in keys} == {KEY_KIND_BLOCK, KEY_KIND_REGION, KEY_KIND_ORDER}


def test_order_kind_route_is_pending_and_never_hard_hits() -> None:
    """公共前提：申请号那一路键不可用 → 只挂起，绝不硬命中。

    这一路只带加做申请号，没有部位序号也没有蜡块号 —— 拿它做硬过滤等于
    「万能钥匙」（把结果挂到别的块上）。因此它**一条都不能硬命中**：降级
    出来的模糊候选逐条带 pending，只能进待人工处理清单。
    """
    candidates = _candidate_pool()
    result = fanout_retrieve(_all_recall_keys(), candidates)

    order_routes = [route for route in result.routes if route.kind == KEY_KIND_ORDER]
    assert order_routes
    assert all(route.pending for route in order_routes)
    assert all(item.pending for route in order_routes for item in route.items)

    order_key = next(key for key in _all_recall_keys() if key.kind == KEY_KIND_ORDER)
    outcome = apply_filter(
        build_filter_key(
            order_key.region_seq, order_key.block_no, order_key.item_code
        ),
        candidates,
        policy=FilterPolicy.MERGE,
    )
    assert outcome.hard_hits == 0
