"""结果侧报文解析（ORU^R01）。

一条 ``ORU^R01`` 里可以有多组 ``OBR``（每组一次加做申请），每组下面挂若干
``OBX``（每个检查项一条结果）。本模块把报文拆成
:class:`~patho_supplement.models.ResultEntry` 列表 —— 每条结果都带着**它自己
报的部位信息**（``ZBR``），后面的归属对齐就靠这三个字段（部位序号 / 蜡块号 /
部位描述）。

字段位按 HL7 v2 第 5 章报文结构落位：

======================  ====================================================
字段                     含义
======================  ====================================================
``OBR-2``                病理号（哪一份报告）
``OBR-3``                加做申请号
``OBR-4``                申请项目码
``OBR-7``                申请 / 观察时间
``OBX-3``                结果项目码（组件 1 为码，组件 2 为名称）
``OBX-5``                结果值
``OBX-11``               结果状态（``P`` 初步 / ``F`` 终报 / ``C`` 更正 / ``X`` 无法获得）
``OBX-14``               结果观察时间
``ZBR-1/2/3``            蜡块号 / 取材部位描述 / 部位序号
======================  ====================================================
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ResultEntry, ResultStatus
from .dirty import DIRTY_ERROR_CODE, describe_shift, looks_shifted
from .er7 import Er7ParseError, Message, Segment
from .zbr import ZbrInfo, parse_zbr

__all__ = ["parse_oru", "OrderGroup"]

#: ``OBX-11`` 允许的结果状态取值。
_ALLOWED_STATUSES = {status.value for status in ResultStatus}


def _is_allowed_status(value: str) -> bool:
    """是否是合法的结果状态取值（用于脏行判定）。"""
    return value.strip() in _ALLOWED_STATUSES


@dataclass
class OrderGroup:
    """一组 ``OBR``（一次加做申请）及其下面的结果。"""

    accession_no: str
    order_no: str | None
    ordered_item_code: str | None
    order_time: str | None
    zbr: ZbrInfo = ZbrInfo()


def _order_group_from_segment(segment: Segment) -> OrderGroup:
    """从 ``OBR`` 段取出申请侧字段。

    ``OBR-2``（病理号）为空直接报错：没有病理号就无法归属到任何一份报告，
    这样的结果进不了草稿，也不该悄悄进来。
    """
    accession_no = segment.field(2).strip()
    if not accession_no:
        raise Er7ParseError(
            "OBR-2 病理号为空，无法归属报告",
            segment="OBR",
            line_no=segment.line_no,
            field="OBR-2",
            code="missing_accession",
        )
    return OrderGroup(
        accession_no=accession_no,
        order_no=segment.field(3).strip() or None,
        ordered_item_code=segment.component(4, 1).strip() or None,
        order_time=segment.field(7).strip() or None,
    )


def _result_from_obx(
    segment: Segment, group: OrderGroup, message: Message, source_file: str | None
) -> ResultEntry:
    """从 ``OBX`` 段取出一条结果条目。"""
    item_code = segment.component(3, 1).strip()
    if not item_code:
        raise Er7ParseError(
            "OBX-3 结果项目码为空",
            segment="OBX",
            line_no=segment.line_no,
            source=message.source,
            field="OBX-3",
            code="missing_item_code",
        )
    item_name = segment.component(3, 2).strip() or None

    value = segment.field(5).strip() or None

    status_raw = segment.field(11).strip()
    if not status_raw:
        # 为空按终报 F 处理 —— 但先排掉「多了一个分隔符导致状态整体右移」：
        # 那种行第 12 位才是状态，右移一位能校验通过。
        if looks_shifted(segment, 11, _is_allowed_status):
            raise Er7ParseError(
                "OBX-11 结果状态为空，且该行疑似多了一个字段分隔符"
                f"（{describe_shift(segment, 11)}）",
                segment="OBX",
                line_no=segment.line_no,
                source=message.source,
                field="OBX-11",
                code=DIRTY_ERROR_CODE,
            )
        status = ResultStatus.FINAL
    elif status_raw in _ALLOWED_STATUSES:
        status = ResultStatus(status_raw)
    else:
        if looks_shifted(segment, 11, _is_allowed_status):
            raise Er7ParseError(
                f"OBX-11 结果状态取值非法：{status_raw!r}，且该行疑似多了一个字段"
                f"分隔符（{describe_shift(segment, 11)}）",
                segment="OBX",
                line_no=segment.line_no,
                source=message.source,
                field="OBX-11",
                code=DIRTY_ERROR_CODE,
            )
        raise Er7ParseError(
            f"OBX-11 结果状态取值非法：{status_raw!r}",
            segment="OBX",
            line_no=segment.line_no,
            source=message.source,
            field="OBX-11",
            code="illegal_result_status",
        )

    observed_at = segment.field(14).strip() or group.order_time or None

    return ResultEntry(
        accession_no=group.accession_no,
        item_code=item_code,
        item_name=item_name,
        value=value,
        status=status,
        observed_at=observed_at,
        order_no=group.order_no,
        region_seq=group.zbr.region_seq,
        block_no=group.zbr.block_no,
        region_desc=group.zbr.region_desc,
        source_system=message.sending_application or "result-inbox",
        source_file=source_file or message.source,
    )


def parse_oru(message: Message, *, source_file: str | None = None) -> list[ResultEntry]:
    """把 ``ORU^R01`` 报文解析成结果条目列表。

    报文类型不对就报错 —— 调用方按报文类型分流，不该把申请侧报文喂进来。
    """
    if message.message_code.upper() != "ORU" or message.message_structure.upper() != "R01":
        raise Er7ParseError(
            f"不是 ORU^R01 报文（MSH-9={message.message_type!r}）",
            segment="MSH",
            line_no=message.msh.line_no,
            source=message.source,
            code="not_oru",
        )

    entries: list[ResultEntry] = []
    group: OrderGroup | None = None

    for segment in message.segments[1:]:
        if segment.name == "OBR":
            group = _order_group_from_segment(segment)
            continue
        if segment.name == "ZBR":
            if group is None:
                raise Er7ParseError(
                    "ZBR 段没有对应的 OBR 段",
                    segment="ZBR",
                    line_no=segment.line_no,
                    source=message.source,
                    code="orphan_zbr",
                )
            group.zbr = parse_zbr(segment)
            continue
        if segment.name == "OBX":
            if group is None:
                raise Er7ParseError(
                    "OBX 段没有对应的 OBR 段",
                    segment="OBX",
                    line_no=segment.line_no,
                    source=message.source,
                    code="orphan_obx",
                )
            entry = _result_from_obx(segment, group, message, source_file)
            entries.append(entry)
            continue
        # 其余段（PID / PV1 / NTE …）本产品用不到，属于正常报文内容，跳过即可。

    return entries
