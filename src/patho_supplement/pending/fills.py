"""人工补齐值：可补字段词表与套用。

挂起项等人补齐时，技术员一次只补**一个字段**。本模块把「能补哪些字段」收成
一份词表，好处有三：

- 命令行只接受词表里的字段名 —— 别的名字一律拒收。没人能把一整条回报
  （更别说一整份报文）当成「补齐值」塞进来；
- 词表同时给出该字段在结果条目上的落点，补齐值套上去不会错位；
- 词表给出中文标签，挂起清单与报错信息都用同一套说法。

**补齐值不覆盖原始快照**：原始回报留在快照里（那是「进对齐之前」的样子），
补齐值单独落一张表（``pending_resolutions``），续跑时两者叠加。原始快照被
覆盖了，就再也说不清「当时回报里到底有没有蜡块号」—— 而这正是要问技术员
的第一个问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..models import ResultEntry, ResultStatus

__all__ = [
    "FIELD_LABELS",
    "FillError",
    "FieldFill",
    "RESOLVABLE_FIELDS",
    "apply_fills",
    "parse_fill",
    "parse_fills",
]

#: 可人工补齐的字段 → 结果条目上的字段名。
#:
#: 只有**定位与取值**这两类字段能人工补：补错了定位就是把结果挂到别的块上，
#: 补错了取值就是把结论写错。回报时间也允许补（回报时间倒挂时要人来定哪条
#: 为准），但它不改变归属。
RESOLVABLE_FIELDS: dict[str, str] = {
    "block_no": "block_no",
    "region_desc": "region_desc",
    "region_seq": "region_seq",
    "item_code": "item_code",
    "result_value": "value",
    "observed_at": "observed_at",
}

#: 可补字段 → 中文标签（清单与报错用同一套说法）。
FIELD_LABELS: dict[str, str] = {
    "block_no": "蜡块号",
    "region_desc": "取材部位描述",
    "region_seq": "部位序号",
    "item_code": "检查项目码",
    "result_value": "结果值",
    "observed_at": "回报时间",
}


class FillError(ValueError):
    """补齐值不合法：字段不认识、值为空、序号不是整数。

    消息面向技术员：一眼看出**哪个字段**、**为什么不行**、**能补什么**。
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class FieldFill:
    """一个补齐值：字段名 + 值。"""

    field_name: str
    value: str

    @property
    def label(self) -> str:
        """字段的中文标签。"""
        return FIELD_LABELS.get(self.field_name, self.field_name)

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field_name, "label": self.label, "value": self.value}


def parse_fill(text: str) -> FieldFill:
    """解析一个补齐值（``字段名=值``）。

    字段名不认识 → 报错并列出能补的字段；值为空 → 报错（空值不是值）。
    """
    raw = (text or "").strip()
    if "=" not in raw:
        raise FillError(
            f"补齐值格式不对：{raw!r}；应写成 字段名=值（例如 block_no=B1）"
        )
    name, _, value = raw.partition("=")
    field_name = name.strip()
    field_value = value.strip()
    if field_name not in RESOLVABLE_FIELDS:
        allowed = "、".join(FIELD_LABELS.get(key, key) for key in RESOLVABLE_FIELDS)
        raise FillError(
            f"不能补的字段：{field_name!r}；可补的字段只有：{allowed}"
        )
    if not field_value:
        raise FillError(f"字段「{FIELD_LABELS.get(field_name, field_name)}」的补齐值不能为空")
    return FieldFill(field_name=field_name, value=field_value)


def parse_fills(items: Sequence[str]) -> tuple[FieldFill, ...]:
    """解析一组补齐值；同一个字段出现多次时以最后一个为准。"""
    parsed: dict[str, FieldFill] = {}
    for item in items:
        fill = parse_fill(item)
        parsed[fill.field_name] = fill
    return tuple(parsed.values())


def _parse_region_seq(value: str) -> int:
    """部位序号必须是整数。"""
    try:
        return int(value)
    except ValueError as exc:
        raise FillError(f"部位序号必须是整数，当前值 {value!r}") from exc


def apply_fills(entry: ResultEntry, fills: Mapping[str, str]) -> ResultEntry:
    """把补齐值套到原始结果条目上，返回**新**条目（原对象不动）。

    套不上的字段名直接报错 —— 词表之外的东西不许进这条路径。
    """
    data = entry.to_dict()
    for field_name, value in fills.items():
        target = RESOLVABLE_FIELDS.get(field_name)
        if target is None:
            raise FillError(f"不能补的字段：{field_name!r}")
        if target == "region_seq":
            data["region_seq"] = _parse_region_seq(str(value))
        else:
            data[target] = value
    data["status"] = ResultStatus(data["status"])
    return ResultEntry(**data)
