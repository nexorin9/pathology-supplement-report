"""院内扩展段 ZBR。

蜡块号与取材部位描述不在 HL7 v2 标准段里 —— 病理标本的这层信息各家 LIS 写法
不一，本项目用扩展段 ``ZBR`` 承载：

- ``ZBR-1`` 蜡块号
- ``ZBR-2`` 取材部位描述
- ``ZBR-3`` 部位序号

三个字段**都允许为空**：院内回报里常常缺其一。空就是「不知道」，不是「没有」——
下游据此决定挂起还是走降级匹配，绝不拿空值当键去硬对。

段位按**院内约定（经验推断）**落盘，正式对接时以目标医院实际段位为准，
差异集中在适配层一处。
"""

from __future__ import annotations

from dataclasses import dataclass

from .dirty import DIRTY_ERROR_CODE, describe_shift, is_integer_text, looks_shifted
from .er7 import Er7ParseError, Segment

__all__ = ["ZbrInfo", "parse_zbr"]


@dataclass(frozen=True)
class ZbrInfo:
    """一条 ZBR 扩展段解析结果。"""

    block_no: str | None = None
    region_desc: str | None = None
    region_seq: int | None = None
    line_no: int | None = None

    def is_empty(self) -> bool:
        """三个字段全空 —— 这条回报没带任何部位信息。"""
        return self.block_no is None and self.region_desc is None and self.region_seq is None

    def missing_fields(self) -> tuple[str, ...]:
        """缺了哪些字段（用于挂起时告诉技术人员该补哪一项）。"""
        missing: list[str] = []
        if self.block_no is None:
            missing.append("block_no")
        if self.region_desc is None:
            missing.append("region_desc")
        if self.region_seq is None:
            missing.append("region_seq")
        return tuple(missing)


def parse_zbr(segment: Segment) -> ZbrInfo:
    """解析 ZBR 段。

    部位序号必须是整数；写成 ``1.0`` 或 ``第1块`` 属于登记口径不一致，
    直接报结构化错误让人去改，不猜。

    **脏行另有一说**：这一行多写了一个字段分隔符时，后面的字段整体右移一位 ——
    部位序号那一列变成了「子宫体」，第 4 位才是真正的序号。判据是「右移一位
    后这一位就能通过校验」，命中就单独报 ``dirty_extra_separator``，并指明是
    哪一行、哪个字段。ZBR 是本产品自定义的段，只有三个字段位，第 4 位出现内容
    同样按脏行处理。
    """
    block_no = segment.field(1).strip() or None
    region_desc = segment.field(2).strip() or None
    seq_raw = segment.field(3).strip()
    extra = segment.field(4).strip()

    region_seq: int | None = None
    if seq_raw:
        try:
            region_seq = int(seq_raw)
        except ValueError as exc:
            if looks_shifted(segment, 3, is_integer_text):
                raise Er7ParseError(
                    f"ZBR-3 部位序号不是整数：{seq_raw!r}，且该行疑似多了一个字段"
                    f"分隔符（{describe_shift(segment, 3)}）",
                    segment="ZBR",
                    line_no=segment.line_no,
                    field="ZBR-3",
                    code=DIRTY_ERROR_CODE,
                ) from exc
            raise Er7ParseError(
                f"ZBR-3 部位序号不是整数：{seq_raw!r}",
                segment="ZBR",
                line_no=segment.line_no,
                field="ZBR-3",
                code="invalid_region_seq",
            ) from exc
    elif looks_shifted(segment, 3, is_integer_text):
        # ZBR-3 空着、第 4 位却是整数 —— 也是右移了一位。
        raise Er7ParseError(
            "ZBR-3 部位序号为空，且该行疑似多了一个字段分隔符"
            f"（{describe_shift(segment, 3)}）",
            segment="ZBR",
            line_no=segment.line_no,
            field="ZBR-3",
            code=DIRTY_ERROR_CODE,
        )

    if extra:
        raise Er7ParseError(
            f"ZBR 段多出一个字段位（第 4 位是 {extra!r}）：ZBR 只有 ZBR-1/2/3 "
            "三个字段位，该行疑似多了一个字段分隔符",
            segment="ZBR",
            line_no=segment.line_no,
            field="ZBR-4",
            code=DIRTY_ERROR_CODE,
        )

    return ZbrInfo(
        block_no=block_no,
        region_desc=region_desc,
        region_seq=region_seq,
        line_no=segment.line_no,
    )
