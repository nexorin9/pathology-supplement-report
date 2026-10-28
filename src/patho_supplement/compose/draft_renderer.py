"""草稿渲染。

按**取材部位**分组，把并入的结果逐项标上来源系统、回报时间与对齐依据
（命中方式 + 置信等级），输出草稿正文文本与结构化条目列表。

渲染出来的正文是给病理医师看的草稿 —— 每一行都要能回答「这个结果哪来的、
什么时候回来的、凭什么算到这块」，这样签发时不必回头翻原始报文；置信「低」
（靠文字相似对上）的那几条一眼就能挑出来重点复核。

**未到项占位**：已申请（基线已申请项 / 申请侧待查项）但**还没有结果**的检查项，
在草稿里以「待回报」占位出现。少写一项，医师就会以为这项没做 ——「没做」
和「还没回来」在报告上是两件事，不能混成一件。

占位**不是一条结果**：它没有值、没有来源系统、没有回报时间，也没有对齐依据
（没对上就不是「可信度低」，是「还没有结论」）。它只回答一件事：这一项申请了，
还没回来。

**「还没回来」的口径必须准**（本模块最容易写错的一处）：结果**回报了但没进
报告**（无法获得 / 值为空 / 归属对不上 / 申请已取消）—— 那是「回来了但没用上」，
写成「待回报」是**假话**。这类项在待人工处理清单里，不在草稿里；草稿只写
「一次都没回来的」。

渲染同时给出两样东西，二者同源、不会打架：

- **正文**（``Draft.body``）：按取材部位分组，逐项标明来源系统与回报时间；
- **结构化条目**（``Draft.entries``）：供审计、导出与回写请求体使用，逐条带
  ``placeholder`` 标志 —— 落库、回写、对账都靠它分清「真结果」与「占位」。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Sequence

from ..align.matcher import MatchCandidate
from ..align.normalizer import normalize_block_no
from ..align.query_derivation import SOURCE_BASELINE, PendingQuery
from ..align.rationale import build_alignment, rationale_tag
from ..models import Alignment, Block, Draft, DraftEntry, ReportBaseline, ResultEntry
from ..timefmt import format_observed_at

__all__ = [
    "AwaitingItem",
    "awaiting_from_baseline",
    "awaiting_from_queries",
    "derive_awaiting",
    "render_draft",
    "PLACEHOLDER_LABEL",
    "PLACEHOLDER_NOTE",
]

#: 占位行在正文里的写法（也是全系统找「待回报」的唯一关键字）。
PLACEHOLDER_LABEL = "待回报"

#: 占位行的补充说明 —— 说清「是没做，还是没回来」。
PLACEHOLDER_NOTE = "（已申请，结果未回报）"


@dataclass(frozen=True)
class AwaitingItem:
    """已申请、但这一版**还没有结果**的检查项。

    定位字段与 :class:`patho_supplement.models.Block` 同一套：部位序号 +
    部位描述 + 蜡块号 —— 归属键还是「病理号 + 部位序号 + 蜡块号」那一套，
    占位行才不会跑到别的块上去。
    """

    item_code: str
    item_name: str | None = None
    region_seq: int | None = None
    region_desc: str | None = None
    block_no: str | None = None
    order_no: str | None = None
    source: str = SOURCE_BASELINE

    @property
    def identity(self) -> tuple[int | None, str | None, str]:
        """去重标识：部位序号 + 归一化蜡块号 + 项目码。

        蜡块号按归一化后比较（``B-1`` 与 ``b01`` 是同一块），项目码去空白转
        大写（基线写 ``ihc-cd3``、申请侧写 ``IHC-CD3`` 是同一项）。
        """
        return (
            self.region_seq,
            normalize_block_no(self.block_no),
            (self.item_code or "").strip().upper(),
        )

    @classmethod
    def from_query(cls, query: PendingQuery) -> "AwaitingItem":
        """待查项 → 未到项（位置信息已经过申请侧与基线的合成）。"""
        return cls(
            item_code=(query.item_code or "").strip(),
            item_name=query.item_name,
            region_seq=query.region_seq,
            region_desc=query.region_desc,
            block_no=query.block_no,
            order_no=query.order_no,
            source=query.source,
        )


# --------------------------------------------------------------- 位置口径


def _same_position(
    left_region: int | None,
    left_block: str | None,
    right_region: int | None,
    right_block: str | None,
) -> bool:
    """两处定位信息是不是指同一块。

    口径与归属判定一致：

    - 两边都有蜡块号 → 归一化后必须相等（蜡块号唯一，部位序号冲突不认）；
    - 蜡块号缺一边 → 看部位序号，序号相同才算同一处；
    - 两边都没有定位信息 → 视为同一处。

    定位不上就**不算同一处** —— 宁可多占一行「待回报」，也不能把一项检查
    算到别的块上。
    """
    left_key = normalize_block_no(left_block)
    right_key = normalize_block_no(right_block)
    if left_key and right_key:
        return left_key == right_key
    if left_region is not None and right_region is not None:
        return left_region == right_region
    return (
        left_key is None
        and right_key is None
        and left_region is None
        and right_region is None
    )


def _blocks_with_item(baseline: ReportBaseline | None, item_code: str) -> list[Block]:
    """基线里申请过这一项的部位。"""
    if baseline is None:
        return []
    code = (item_code or "").strip().upper()
    if not code:
        return []
    return [
        block
        for block in baseline.blocks
        if any((raw or "").strip().upper() == code for raw in block.ordered_items)
    ]


def _match_baseline_block(
    item_code: str,
    *,
    region_seq: int | None,
    block_no: str | None,
    order_no: str | None,
    baseline: ReportBaseline | None,
) -> Block | None:
    """在基线的部位清单里找出这一项挂在哪一块。

    判定顺序（先严后松，与派生层同一套口径）：加做申请号 → 蜡块号 →
    部位序号 → 唯一兜底（这一项在本报告里只申请过一块）。
    """
    blocks = _blocks_with_item(baseline, item_code)
    if not blocks:
        return None

    if order_no:
        for block in blocks:
            if block.order_no and block.order_no == order_no:
                return block
    if block_no:
        key = normalize_block_no(block_no)
        for block in blocks:
            if normalize_block_no(block.block_no) == key:
                return block
    if region_seq is not None:
        for block in blocks:
            if block.region_seq != region_seq:
                continue
            if block_no and block.block_no:
                if normalize_block_no(block.block_no) != normalize_block_no(block_no):
                    continue
            return block
    if len(blocks) == 1:
        return blocks[0]
    return None


def _match_query(item: AwaitingItem, queries: Sequence[PendingQuery]) -> PendingQuery | None:
    """在待查项里找出与基线这一项指向同一处检查的条目。

    找到了才知道这一项的**真实状态**（还在等结果 / 已取消 / 已暂停 /
    已终止）—— 基线只说「申请过」，申请侧才说「还等不等」。
    """
    code = (item.item_code or "").strip().upper()
    candidates = [
        query for query in queries if (query.item_code or "").strip().upper() == code
    ]
    if not candidates:
        return None

    if item.order_no:
        for query in candidates:
            if query.order_no and query.order_no == item.order_no:
                return query
    if item.block_no:
        key = normalize_block_no(item.block_no)
        for query in candidates:
            if normalize_block_no(query.block_no) == key:
                return query
    if item.region_seq is not None:
        for query in candidates:
            if query.region_seq != item.region_seq:
                continue
            if item.block_no and query.block_no:
                if normalize_block_no(query.block_no) != normalize_block_no(item.block_no):
                    continue
            return query
    # 兜底：候选自己没带部位信息，且同项目只剩一条时才算同一项。
    if len(candidates) == 1:
        only = candidates[0]
        if only.region_seq is None and only.block_no is None:
            return only
    return None


# --------------------------------------------------------------- 未到项派生


def awaiting_from_baseline(baseline: ReportBaseline | None) -> tuple[AwaitingItem, ...]:
    """基线里每块已申请的项目 —— 「已申请什么」的权威清单。

    基线是病理系统 ``GET`` 回来的登记事实，比申请侧事件更全（申请侧可能
    只报了几项），所以它排在前面做去重的基准。
    """
    if baseline is None:
        return ()
    items: list[AwaitingItem] = []
    for block in baseline.blocks:
        seen: set[str] = set()
        for raw in block.ordered_items:
            code = (raw or "").strip()
            if not code or code.upper() in seen:
                continue
            seen.add(code.upper())
            items.append(
                AwaitingItem(
                    item_code=code,
                    region_seq=block.region_seq,
                    region_desc=block.region_desc,
                    block_no=block.block_no,
                    order_no=block.order_no,
                    source=SOURCE_BASELINE,
                )
            )
    return tuple(items)


def awaiting_from_queries(queries: Iterable[PendingQuery]) -> tuple[AwaitingItem, ...]:
    """待查项里**还在等结果**的那些。

    已取消 / 已暂停 / 已终止的项不算 —— 申请都不做了，等它做什么。这三种
    状态在派生阶段就分开了，闸门（``align.order_guard``）也会拦它们的回报，
    两处口径一致。
    """
    items: list[AwaitingItem] = []
    for query in queries:
        if not query.awaiting:
            continue
        item = AwaitingItem.from_query(query)
        if not item.item_code:
            continue
        items.append(item)
    return tuple(items)


def _richer(left: AwaitingItem, right: AwaitingItem) -> AwaitingItem:
    """两个来源指向同一项时，取定位信息更全的那份。"""
    if left.region_desc and not right.region_desc:
        return left
    if right.region_desc and not left.region_desc:
        return right
    if left.order_no and not right.order_no:
        return left
    if right.order_no and not left.order_no:
        return right
    return left


def _enrich(item: AwaitingItem, baseline: ReportBaseline | None) -> AwaitingItem:
    """申请侧的待查项常常不带部位信息 —— 用基线把定位补全。

    补全不了就照原样留着：缺定位的占位行会单独成组（部位未登记），
    但**不会**跑到别的块上去。
    """
    if baseline is None:
        return item
    block = _match_baseline_block(
        item.item_code,
        region_seq=item.region_seq,
        block_no=item.block_no,
        order_no=item.order_no,
        baseline=baseline,
    )
    if block is None:
        return item
    return replace(
        item,
        region_seq=item.region_seq if item.region_seq is not None else block.region_seq,
        block_no=item.block_no or block.block_no,
        region_desc=item.region_desc or block.region_desc,
        order_no=item.order_no or block.order_no,
    )


def _reported_covers(
    item: AwaitingItem,
    reported: Sequence[ResultEntry],
    *,
    unique_code: bool,
) -> bool:
    """这一项是不是**已经回报过**了。

    「回报过」不等于「并入草稿」：无法获得 / 值为空 / 归属对不上 / 申请已
    取消，结果都回来了，只是没进报告。这类项写「待回报」是**假话** ——
    报告上「还没回来」和「回来了但没用上」是两件事，前者去等，后者去查。

    口径：项目码相同 + 位置指向同一处。回报完全没带定位信息时，只有当这个
    项目码在本报告里**只有一项**才认作同一项（认不准就不认，宁可多占一行）。
    """
    code = item.identity[2]
    same_code = [
        entry
        for entry in reported
        if (entry.item_code or "").strip().upper() == code
    ]
    if not same_code:
        return False
    for entry in same_code:
        if _same_position(
            item.region_seq, item.block_no, entry.region_seq, entry.block_no
        ):
            return True
    if unique_code and len(same_code) == 1:
        entry = same_code[0]
        if entry.region_seq is None and entry.block_no is None:
            return True
    return False


def derive_awaiting(
    baseline: ReportBaseline | None = None,
    queries: Iterable[PendingQuery] = (),
    *,
    reported: Iterable[ResultEntry] = (),
) -> tuple[AwaitingItem, ...]:
    """已申请、但**还没有结果**的检查项 = 报告里要写「待回报」的那些行。

    三道减法，缺一道就会写错：

    1. **申请侧说不等了**（已取消 / 已暂停 / 已终止）→ 不是「未回报」，是
       「不做了」，不该占位（基线里那一行还挂着申请，别把它复活成待回报）；
    2. **基线已申请项与申请侧待查项合成一条** —— 定位信息取更全的那份，
       同一个项目码不会占两行；
    3. **已经回报过的项不算「未回报」** —— 不论它是并入了草稿，还是被扣下
       等人处理。

    ``reported`` 给的是**这一版收到过的结果**（并入草稿的 + 被扣下的）。
    少了它，「回报了但无法获得」的项会被写成「待回报」—— 假话。
    """
    query_list = list(queries)
    reported_list = list(reported)

    # 1) 还在等结果的待查项。
    candidates: list[AwaitingItem] = [
        _enrich(item, baseline) for item in awaiting_from_queries(query_list)
    ]

    # 2) 基线里已申请、申请侧没提过的项 —— 也算「未回报」。
    #    申请侧提过的（等 / 取消 / 暂停 / 终止）一律以申请侧为准。
    for item in awaiting_from_baseline(baseline):
        if _match_query(item, query_list) is not None:
            continue
        candidates.append(item)

    # 3) 同一项只留一条（定位信息取更全的那份）。
    merged: dict[tuple[int | None, str | None, str], AwaitingItem] = {}
    for item in candidates:
        key = item.identity
        existing = merged.get(key)
        merged[key] = item if existing is None else _richer(existing, item)

    # 4) 已经回报过的项不算「未回报」。
    code_counts: dict[str, int] = {}
    for item in merged.values():
        code_counts[item.identity[2]] = code_counts.get(item.identity[2], 0) + 1

    result: list[AwaitingItem] = []
    for item in merged.values():
        if _reported_covers(
            item, reported_list, unique_code=code_counts.get(item.identity[2], 0) == 1
        ):
            continue
        result.append(item)

    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.region_seq if item.region_seq is not None else 1 << 30,
                normalize_block_no(item.block_no) or "",
                item.item_code,
            ),
        )
    )


# --------------------------------------------------------------- 覆盖判定


def _covers(
    item: AwaitingItem,
    *,
    region_seq: int | None,
    block_no: str | None,
    item_code: str,
) -> bool:
    """这一项是不是已经被并入草稿的那一条覆盖了。

    项目码相同，且位置指向同一块（口径见 :func:`_same_position`）。

    定位不上就**不算覆盖** —— 宁可多占一行「待回报」，也不能让一条已经并入
    草稿的结果在报告里出现两次。
    """
    if (item.item_code or "").strip().upper() != (item_code or "").strip().upper():
        return False
    return _same_position(item.region_seq, item.block_no, region_seq, block_no)


# --------------------------------------------------------------- 渲染


@dataclass(frozen=True)
class _Row:
    """组内的一行（并入项或占位项）。"""

    item_code: str
    item_name: str | None = None
    value: str | None = None
    region_seq: int | None = None
    region_desc: str | None = None
    block_no: str | None = None
    order_no: str | None = None
    source_system: str | None = None
    observed_at: str | None = None
    alignment: Alignment | None = None
    tag: str | None = None
    placeholder: bool = False


def _region_label(
    region_seq: int | None,
    region_desc: str | None,
    block_no: str | None,
    order_no: str | None = None,
) -> str:
    """分组标题：部位 + 蜡块（都没有时退回加做申请号）。"""
    parts: list[str] = []
    if region_desc:
        parts.append(region_desc)
    elif region_seq is not None:
        parts.append(f"部位 {region_seq}")
    else:
        parts.append("部位未登记")
    if block_no:
        parts.append(f"蜡块 {block_no}")
    elif order_no:
        parts.append(f"加做申请 {order_no}")
    return " · ".join(parts)


def _merged_line(row: _Row) -> str:
    """并入项的一行：结果 + 来源系统 + 回报时间 + 对齐依据。"""
    name = row.item_name or row.item_code
    return (
        f"  {row.item_code}｜{name}｜{row.value or '无结果'}｜"
        f"来源 {row.source_system or '未知'}｜"
        f"回报时间 {format_observed_at(row.observed_at)}｜依据 {row.tag}"
    )


def _placeholder_line(row: _Row) -> str:
    """未到项的一行：只说「申请了、还没回来」，不写值也不写依据。"""
    name = row.item_name or row.item_code
    return f"  {row.item_code}｜{name}｜{PLACEHOLDER_LABEL}｜{PLACEHOLDER_NOTE}"


def render_draft(
    accession_no: str,
    matched: Sequence[tuple[ResultEntry, MatchCandidate]],
    *,
    baseline: ReportBaseline | None = None,
    awaiting: Sequence[AwaitingItem] = (),
    version: int = 1,
    created_at: str | None = None,
) -> Draft:
    """渲染一份补充报告草稿。

    ``matched`` 是已经对上号的 (结果, 命中依据) 列表；顺序按部位序号、蜡块号、
    结果项目码排好，渲染出来就是一份可以直接看的补充报告。

    ``awaiting`` 是**已申请但一次都没回报**的检查项（见 :func:`derive_awaiting`）：
    已经在 ``matched`` 里出现过的项不再占位，其余的以「待回报」占位出现 ——
    少写一项，医师就会以为这项没做。
    """
    created = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    #: 分组 → 组内行。每条带自己的对齐依据标注 —— 草稿正文与结构化条目
    #: 共用同一份依据，不会两处打架。
    groups: dict[tuple[int | None, str | None], list[_Row]] = {}

    def _group_of(row: _Row) -> tuple[int | None, str | None]:
        key = (row.region_seq, row.block_no)
        groups.setdefault(key, []).append(row)
        return key

    merged_rows: list[_Row] = []
    for entry, candidate in matched:
        alignment = build_alignment(entry, candidate)
        row = _Row(
            item_code=entry.item_code,
            item_name=entry.item_name,
            value=entry.value,
            region_seq=candidate.block.region_seq,
            region_desc=candidate.block.region_desc,
            block_no=candidate.block.block_no,
            order_no=candidate.block.order_no,
            source_system=entry.source_system,
            observed_at=entry.observed_at,
            alignment=alignment,
            tag=rationale_tag(alignment),
            placeholder=False,
        )
        merged_rows.append(row)
        _group_of(row)

    placeholder_rows: list[_Row] = []
    for item in awaiting:
        if any(
            _covers(
                item,
                region_seq=row.region_seq,
                block_no=row.block_no,
                item_code=row.item_code,
            )
            for row in merged_rows
        ):
            continue
        row = _Row(
            item_code=item.item_code,
            item_name=item.item_name,
            value=None,
            region_seq=item.region_seq,
            region_desc=item.region_desc,
            block_no=item.block_no,
            order_no=item.order_no,
            placeholder=True,
        )
        placeholder_rows.append(row)
        _group_of(row)

    ordered_keys = sorted(
        groups,
        key=lambda key: (
            key[0] if key[0] is not None else 1 << 30,
            normalize_block_no(key[1]) or "",
            key[1] or "",
        ),
    )

    lines: list[str] = []
    lines.append(f"病理号：{accession_no}")
    lines.append(f"补充报告草稿（版本 {version}）")
    if baseline is not None and baseline.base_version:
        lines.append(f"基线版本：{baseline.base_version}")
    lines.append(f"生成时间：{created}")
    merged_count = len(merged_rows)
    placeholder_count = len(placeholder_rows)
    if placeholder_count:
        lines.append(
            f"结果条目：{merged_count} 条（另 {placeholder_count} 项待回报）"
        )
    else:
        lines.append(f"结果条目：{merged_count} 条")
    lines.append("")

    entries: list[DraftEntry] = []
    for key in ordered_keys:
        # 组内先列**已回报的结果**，再列「待回报」占位 —— 医师先看拿到什么，
        # 再看还差什么。同一组内按项目码排，顺序与结果无关。
        rows = sorted(groups[key], key=lambda row: (row.placeholder, row.item_code or ""))
        region_seq, block_no = key
        region_desc = next((row.region_desc for row in rows if row.region_desc), None)
        order_no = next((row.order_no for row in rows if row.order_no), None)
        lines.append(f"【{_region_label(region_seq, region_desc, block_no, order_no)}】")
        for row in rows:
            lines.append(_placeholder_line(row) if row.placeholder else _merged_line(row))
            entries.append(
                DraftEntry(
                    item_code=row.item_code,
                    item_name=row.item_name,
                    value=row.value,
                    region_seq=row.region_seq,
                    region_desc=row.region_desc,
                    block_no=row.block_no,
                    source_system=row.source_system,
                    observed_at=row.observed_at,
                    alignment=row.alignment,
                    placeholder=row.placeholder,
                )
            )
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"

    return Draft(
        accession_no=accession_no,
        version=version,
        entries=tuple(entries),
        body=body,
        base_version=(baseline.base_version if baseline is not None else None),
        created_at=created,
    )
