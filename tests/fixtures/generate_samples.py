#!/usr/bin/env python3
"""脱敏样例合成生成器。

为什么要有这个脚本
------------------

真实院内数据本轮拿不到，而「手写几行 JSON 冒充真实数据」比没有数据更危险：
手写样例覆盖不到脏数据形态，跑出来的绿灯也不能说明产品经得起真实报文的折腾。
本脚本按固定种子**生成**脱敏样例，特点：

- **可重跑**：同一 ``--seed`` 两次生成，目录指纹（sha256）逐字节相同；
- **断网可用**：只用标准库，不发任何网络请求，不装第三方依赖也能跑；
- **规模可配**：默认 3 份报告 × 8–10 条 ORU + 2 条 ORM；
  ``--scale 2000`` 可放大到 2000 条 ORU（约 285–300 份报告）；
- **形态齐全**：部位别名、蜡块号缺失、重复投递、``OBX-11=C`` 更正、回报时间倒挂、
  UTF-8 与 GBK 混编、结果值为空、超长字段 —— 每种形态都真实落在报文里；
- **数据边界**：标识一律类目化占位（``ACC-GEN-0001`` / ``ORD-GEN-0001-1`` /
  ``<EXAMPLE_LOT>``），不出现姓名、身份证、住院号、批号等患者可识别明细。

生成出来的东西
--------------

数据集目录长这样（可直接喂给 ``patho_supplement.cli replay``）::

    <数据集目录>/
    ├── baseline.json        # 病理报告基线（部位与已申请项目）
    ├── acc-gen-0001-01-*.hl7   # ORU^R01 结果回报
    ├── acc-gen-0001-90-*.hl7   # ORM^O01 申请侧报文
    └── manifest.json        # 种子、规模、形态覆盖与逐文件指纹

**为什么写在 ``--out`` 的子目录里**：结果投递目录（inbox）的顶层放的是活报文，
守护进程只扫顶层普通文件 —— 合成数据落到子目录，既不干扰监听，也不覆盖手写夹具。
需要让守护进程直接监听这批数据时，加 ``--flat`` 把报文写在顶层即可。

用法::

    python tests/fixtures/generate_samples.py --out tests/fixtures/inbox --seed 42
    python tests/fixtures/generate_samples.py --scale 2000 --flat --out tests/fixtures/scale
    python tests/fixtures/generate_samples.py --self-check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

__all__ = [
    "DEFAULT_SEED",
    "DatasetPlan",
    "generate",
    "dataset_fingerprint",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 默认随机种子。同一个种子 → 同一批数据。
DEFAULT_SEED = 42

#: 默认规模：3 份报告，每份 8–10 条 ORU。
DEFAULT_REPORTS = 3

#: 默认 ORM 报文条数。
DEFAULT_ORM_MESSAGES = 2

#: 默认数据集子目录名。
DEFAULT_DATASET_NAME = "generated"

#: 数据集清单文件名。
MANIFEST_NAME = "manifest.json"

#: 基线快照文件名（与 ``replay`` 的默认约定一致）。
BASELINE_NAME = "baseline.json"

#: 报文时间基准（固定值 —— 跑出来的数据必须与墙上时钟无关）。
_BASE_TIME = datetime(2026, 1, 1, 8, 0, 0)

#: 部位池：规范词 + 别名。
#:
#: 别名一律取自**无科室限定**的词条 —— 科室未知时限定词条不套用，
#: 那样别名就折不回规范词，「部位别名」这一形态就测不到 L2 归位。
REGION_POOL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("宫颈", ("宫颈部", "子宫颈", "宫颈组织")),
    ("乳腺", ("乳腺组织", "乳腺实质", "乳腺腺体")),
    ("胃", ("胃壁", "胃组织", "胃黏膜")),
    ("肝", ("肝脏", "肝组织", "肝实质")),
    ("结肠", ("结肠壁", "结肠组织", "结肠黏膜")),
    ("肺", ("肺组织", "肺叶", "肺实质")),
    ("淋巴结", ("淋巴结组织", "淋巴结构", "淋巴结窦")),
    ("食管", ("食管壁", "食管黏膜", "食管组织")),
    ("甲状腺", ("甲状腺组织", "甲状组织")),
    ("骨", ("骨组织", "骨质", "骨组织块")),
)

#: 免疫组化项目池（项目码、名称）。采样时从中挑。
ITEM_POOL: tuple[tuple[str, str], ...] = (
    ("IHC-CD3", "CD3"),
    ("IHC-CD20", "CD20"),
    ("IHC-KI67", "Ki-67"),
    ("IHC-CK7", "CK7"),
    ("IHC-ER", "ER"),
    ("IHC-PR", "PR"),
    ("IHC-P53", "P53"),
    ("IHC-COX2", "COX2"),
)

#: 留给申请侧报文用的项目（不进任何块的已申请清单，用来造「待回报」占位）。
RESERVED_ORDER_ITEMS: tuple[tuple[str, str], ...] = (
    ("IHC-HER2", "HER2"),
    ("IHC-P16", "P16"),
)

#: 结果值池。
VALUE_POOL: tuple[str, ...] = ("阴性", "阳性", "弱阳性", "局灶阳性", "约15%", "2+", "3+")

#: 每块申请的免疫组化项目数。
ITEMS_PER_BLOCK = 5

#: 每份报告的取材部位数（三块：主块 A、次块 B、第三块 C）。
BLOCKS_PER_REPORT = 3

# --------------------------------------------------------------------------
# 形态标签
# --------------------------------------------------------------------------

SHAPE_NORMAL = "normal"
SHAPE_REGION_ALIAS = "region_alias"
SHAPE_MISSING_BLOCK = "missing_block_no"
SHAPE_DUPLICATE = "duplicate_delivery"
SHAPE_CORRECTION = "correction"
SHAPE_INVERTED = "inverted_time"
SHAPE_GBK = "gbk_encoding"
SHAPE_MIXED = "mixed_encoding"
SHAPE_EMPTY_VALUE = "empty_value"
SHAPE_OVERLONG = "overlong_value"

#: 形态的中文名（摘要里给运维看）。
SHAPE_LABELS: dict[str, str] = {
    SHAPE_REGION_ALIAS: "部位别名",
    SHAPE_MISSING_BLOCK: "蜡块号缺失",
    SHAPE_DUPLICATE: "重复投递",
    SHAPE_CORRECTION: "OBX-11=C 更正",
    SHAPE_INVERTED: "回报时间倒挂",
    SHAPE_GBK: "GBK 编码",
    SHAPE_MIXED: "UTF-8 与 GBK 混编",
    SHAPE_EMPTY_VALUE: "结果值为空",
    SHAPE_OVERLONG: "超长字段",
}

#: 一份报告内部的形态排布（前 11 条按位置固定，之后都是普通回报）。
#:
#: 位置是有讲究的：
#:
#: - 第 0 条是「更正」的原件，第 1 条是更正它的那条（时间更晚）；
#: - 第 3 条是第 0 条的重复投递（内容逐字节相同，文件名不同）；
#: - 第 5 条是「倒挂」的原件，第 6 条是比它还早的更正 —— 倒挂要有一对
#:   「同键、一早一晚」的消息才测得出来。
_REPORT_LAYOUT: tuple[str, ...] = (
    SHAPE_NORMAL,          # 0 更正原件
    SHAPE_CORRECTION,      # 1 更正（比第 0 条晚 60 分钟）
    SHAPE_REGION_ALIAS,    # 2 部位别名
    SHAPE_DUPLICATE,       # 3 重复投递（第 0 条的副本）
    SHAPE_MISSING_BLOCK,   # 4 蜡块号缺失
    SHAPE_NORMAL,          # 5 倒挂原件
    SHAPE_INVERTED,        # 6 更正（比第 5 条早 30 分钟 → 倒挂）
    SHAPE_GBK,             # 7 整份 GBK 编码
    SHAPE_MIXED,           # 8 UTF-8 与 GBK 混编
    SHAPE_EMPTY_VALUE,     # 9 结果值为空
    SHAPE_OVERLONG,        # 10 超长结果值
)

#: 第一份报告固定铺满整套形态。
_FIRST_REPORT_MESSAGES = len(_REPORT_LAYOUT)

#: 其余报告的报文条数取值范围。
_OTHER_REPORT_RANGE = (8, 9)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _BlockPlan:
    """一块取材部位。"""

    region_seq: int
    region_desc: str
    alias: str
    block_no: str
    order_no: str
    ordered_items: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _MessagePlan:
    """一条结果回报（ORU^R01）。"""

    filename: str
    control_id: str
    accession_no: str
    order_no: str
    item_code: str
    item_name: str
    region_seq: int
    block_no: str
    region_desc: str
    order_time: str
    observed_at: str
    message_time: str
    status: str
    value: str
    encoding: str
    shape: str


@dataclass(frozen=True)
class _OrderGroup:
    """申请侧报文里的一组申请（ORC + OBR + ZBR）。"""

    action: str
    accession_no: str
    order_no: str
    item_code: str
    item_name: str
    order_time: str
    region_seq: int
    block_no: str
    region_desc: str


@dataclass(frozen=True)
class _OrderPlan:
    """一条申请侧报文（ORM^O01）。"""

    filename: str
    control_id: str
    message_time: str
    groups: tuple[_OrderGroup, ...]


@dataclass(frozen=True)
class DatasetPlan:
    """一份数据集的生成计划。"""

    seed: int
    reports: tuple[dict[str, object], ...]
    messages: tuple[_MessagePlan, ...]
    orders: tuple[_OrderPlan, ...]

    @property
    def oru_count(self) -> int:
        return len(self.messages)

    @property
    def orm_count(self) -> int:
        return len(self.orders)

    @property
    def report_count(self) -> int:
        return len(self.reports)

    def shape_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for message in self.messages:
            counts[message.shape] = counts.get(message.shape, 0) + 1
        for order in self.orders:
            counts["order_side"] = counts.get("order_side", 0) + 1
        return counts


# --------------------------------------------------------------------------
# 报文渲染
# --------------------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    """HL7 时间戳（``YYYYMMDDHHMMSS``）。"""
    return moment.strftime("%Y%m%d%H%M%S")


def _oru_text(message: _MessagePlan) -> str:
    """把一条结果计划渲染成 ORU^R01 报文文本。

    字段位按 HL7 v2 第 5 章报文结构落位（与 ``docs/inbound-hl7-spec.md`` 一致）：
    ``OBR-2`` 病理号 / ``OBR-3`` 加做申请号 / ``OBR-4`` 项目码 / ``OBR-7`` 申请时间；
    ``OBX-3`` 项目码 / ``OBX-5`` 结果值 / ``OBX-11`` 结果状态 / ``OBX-14`` 观察时间；
    ``ZBR-1`` 蜡块号 / ``ZBR-2`` 取材部位描述 / ``ZBR-3`` 部位序号。
    """
    return "\n".join(
        (
            "MSH|^~\\&|IMMUNO-LAB|MICRO-BENCH|RECIPIENT|HOSP-PATH|"
            f"{message.message_time}||ORU^R01|{message.control_id}|P|2.5.1",
            f"OBR|1|{message.accession_no}|{message.order_no}|"
            f"{message.item_code}^{message.item_name}^L|||{message.order_time}",
            f"ZBR|{message.block_no}|{message.region_desc}|{message.region_seq}",
            f"OBX|1|ST|{message.item_code}^{message.item_name}^L||{message.value}"
            f"||||||{message.status}|||{message.observed_at}",
        )
    ) + "\n"


def _orm_text(order: _OrderPlan) -> str:
    """把一条申请计划渲染成 ORM^O01 报文文本。"""
    lines = [
        "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|"
        f"{order.message_time}||ORM^O01|{order.control_id}|P|2.5.1"
    ]
    for group in order.groups:
        lines.append(f"ORC|{group.action}|{group.order_no}")
        lines.append(
            f"OBR|1|{group.accession_no}|{group.order_no}|"
            f"{group.item_code}^{group.item_name}^L|||{group.order_time}"
        )
        lines.append(
            f"ZBR|{group.block_no}|{group.region_desc}|{group.region_seq}"
        )
    return "\n".join(lines) + "\n"


def _control_id(filename: str) -> str:
    """报文控制号（由文件名派生，逐字节可复现）。"""
    return "MSG-" + filename[: -len(".hl7")].upper()


def _encode_message(text: str, encoding: str) -> bytes:
    """按指定编码写出报文字节。

    - ``utf-8``：整份 UTF-8；
    - ``gbk``：整份 GBK（模拟老系统导出的中文编码）；
    - ``mixed``：**同一个文件里两种编码混排** —— ZBR 段（带中文）按 UTF-8，
      其余段按 GBK。这正是「不同系统各写各的、导出时被拼到一起」的真实形态。
    """
    if encoding == "utf-8":
        return text.encode("utf-8")
    if encoding == "gbk":
        return text.encode("gbk")
    if encoding == "mixed":
        chunks: list[bytes] = []
        for line in text.split("\n"):
            if line.startswith("ZBR"):
                chunks.append(line.encode("utf-8"))
            else:
                chunks.append(line.encode("gbk"))
        return b"\n".join(chunks)
    raise ValueError(f"未知编码口径：{encoding}")


# --------------------------------------------------------------------------
# 生成计划
# --------------------------------------------------------------------------


def _plan_reports(
    rng: random.Random, report_count: int, per_report: Sequence[int]
) -> list[dict[str, object]]:
    """规划各份报告的基线（部位、蜡块号、已申请项目）。"""
    reports: list[dict[str, object]] = []
    for index in range(report_count):
        accession_no = f"ACC-GEN-{index + 1:04d}"
        regions = rng.sample(REGION_POOL, k=BLOCKS_PER_REPORT)
        blocks: list[dict[str, object]] = []
        for block_index, (canonical, aliases) in enumerate(regions):
            item_count = min(ITEMS_PER_BLOCK, len(ITEM_POOL))
            items = rng.sample(ITEM_POOL, k=item_count)
            blocks.append(
                {
                    "region_seq": block_index + 1,
                    "region_desc": canonical,
                    "alias": aliases[0],
                    "block_no": f"B{block_index + 1}",
                    "order_no": f"ORD-GEN-{index + 1:04d}-{block_index + 1}",
                    "ordered_items": items,
                }
            )
        reports.append(
            {
                "accession_no": accession_no,
                "base_version": f"base-gen-v{index + 1}",
                "blocks": blocks,
                "message_count": per_report[index],
            }
        )
    return reports


def _message_plan(
    *,
    filename: str,
    report: dict[str, object],
    block: dict[str, object],
    item: tuple[str, str],
    moment: datetime,
    shape: str,
    status: str = "F",
    value: str | None = None,
    block_no: str | None = None,
    region_desc: str | None = None,
    region_seq: int | None = None,
) -> _MessagePlan:
    """造一条结果计划。"""
    item_code, item_name = item
    observed = moment
    return _MessagePlan(
        filename=filename,
        control_id=_control_id(filename),
        accession_no=str(report["accession_no"]),
        order_no=str(block["order_no"]),
        item_code=item_code,
        item_name=item_name,
        region_seq=int(region_seq if region_seq is not None else block["region_seq"]),
        block_no=str(block_no if block_no is not None else block["block_no"]),
        region_desc=str(
            region_desc if region_desc is not None else block["region_desc"]
        ),
        order_time=_stamp(moment - timedelta(hours=2)),
        observed_at=_stamp(observed),
        message_time=_stamp(observed + timedelta(minutes=1)),
        status=status,
        value=value if value is not None else "阴性",
        encoding="utf-8",
        shape=shape,
    )


def _overlong_value() -> str:
    """一条超长结果值（>200 字），模拟自由文本描述。

    真实回报里这类字段是「镜下描述 + 判读」的长段落，不截断、不改写 ——
    原样保留才对得起「回写什么就是什么」。
    """
    text = "；".join(["局灶区域可见散在阳性细胞，免疫组化染色呈弱阳性，比例约10%"] * 7)
    if len(text) <= 200:  # pragma: no cover —— 常量固定，走不到
        text = text * 2
    return text


def _build_messages(
    rng: random.Random, reports: Sequence[dict[str, object]]
) -> list[_MessagePlan]:
    """按形态排布生成全部结果回报。"""
    messages: list[_MessagePlan] = []
    clock = _BASE_TIME

    for report_index, report in enumerate(reports):
        blocks = list(report["blocks"])  # type: ignore[arg-type]
        count = int(report["message_count"])
        block_a, block_b, block_c = blocks[0], blocks[1], blocks[2]
        items_a = list(block_a["ordered_items"])  # type: ignore[arg-type]
        items_b = list(block_b["ordered_items"])  # type: ignore[arg-type]
        items_c = list(block_c["ordered_items"])  # type: ignore[arg-type]

        layout = list(_REPORT_LAYOUT[:count])
        layout += [SHAPE_NORMAL] * max(0, count - len(_REPORT_LAYOUT))

        # 每条消息落在哪一块、报哪个项目 —— 先定下来，形态再改字段。
        placements: list[tuple[dict[str, object], tuple[str, str]]] = []
        for position in range(count):
            if position == 0:
                placements.append((block_a, items_a[0]))
            elif position == 1:
                placements.append((block_a, items_a[0]))
            elif position == 2:
                placements.append((block_b, items_b[0]))
            elif position == 3:
                placements.append((block_a, items_a[0]))
            elif position == 4:
                placements.append((block_b, items_b[1]))
            elif position == 5:
                placements.append((block_a, items_a[1]))
            elif position == 6:
                placements.append((block_a, items_a[1]))
            elif position == 7:
                placements.append((block_b, items_b[2]))
            elif position == 8:
                placements.append((block_c, items_c[0]))
            elif position == 9:
                placements.append((block_c, items_c[1]))
            elif position == 10:
                placements.append((block_c, items_c[2]))
            else:
                block = blocks[(position - 11) % len(blocks)]
                pool = list(block["ordered_items"])  # type: ignore[arg-type]
                placements.append((block, pool[(position - 11) % len(pool)]))

        built: list[_MessagePlan] = []
        for position, (block, item) in enumerate(placements):
            shape = layout[position]
            moment = clock + timedelta(minutes=6 * position)
            clock += timedelta(minutes=6)
            stem = f"acc-gen-{report_index + 1:04d}-{position + 1:02d}"

            if shape == SHAPE_CORRECTION:
                # 比第 0 条更晚的更正：同键、更晚、改值。
                original = built[0]
                built.append(
                    _message_plan(
                        filename=f"{stem}-correction.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=moment,
                        shape=shape,
                        status="C",
                        value=_correction_value(original.value),
                        block_no=original.block_no,
                        region_desc=original.region_desc,
                        region_seq=original.region_seq,
                    )
                )
                continue

            if shape == SHAPE_INVERTED:
                # 比第 5 条还早的更正：同键、更早 → 倒挂，不许取代原值。
                original = built[5]
                built.append(
                    _message_plan(
                        filename=f"{stem}-inverted.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=original_moment(built, original),
                        shape=shape,
                        status="C",
                        value=_correction_value(original.value),
                        block_no=original.block_no,
                        region_desc=original.region_desc,
                        region_seq=original.region_seq,
                    )
                )
                continue

            if shape == SHAPE_DUPLICATE:
                # 重复投递：内容逐字节相同，只有文件名不同。
                original = built[0]
                built.append(
                    replace(
                        original,
                        filename=f"{stem}-duplicate.hl7",
                        shape=SHAPE_DUPLICATE,
                    )
                )
                continue

            if shape == SHAPE_REGION_ALIAS:
                # 部位写的是别名，没有蜡块号 —— 靠词典折回规范词才归得回去。
                built.append(
                    _message_plan(
                        filename=f"{stem}-alias.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=moment,
                        shape=shape,
                        block_no="",
                        region_desc=str(block["alias"]),
                    )
                )
                continue

            if shape == SHAPE_MISSING_BLOCK:
                # 蜡块号缺失，部位描述写的是规范词 —— 能靠描述归位。
                built.append(
                    _message_plan(
                        filename=f"{stem}-noblock.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=moment,
                        shape=shape,
                        block_no="",
                    )
                )
                continue

            if shape == SHAPE_EMPTY_VALUE:
                built.append(
                    _message_plan(
                        filename=f"{stem}-emptyvalue.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=moment,
                        shape=shape,
                        value="",
                    )
                )
                continue

            if shape == SHAPE_OVERLONG:
                built.append(
                    _message_plan(
                        filename=f"{stem}-overlong.hl7",
                        report=report,
                        block=block,
                        item=item,
                        moment=moment,
                        shape=shape,
                        value=_overlong_value(),
                    )
                )
                continue

            encoding = "utf-8"
            value = VALUE_POOL[(report_index * 3 + position) % len(VALUE_POOL)]
            status = "P" if position % 5 == 4 else "F"
            if shape == SHAPE_GBK:
                encoding = "gbk"
            elif shape == SHAPE_MIXED:
                encoding = "mixed"
                value = "阴性"  # 混编文件里必须有一行中文才能测出两种编码
            built.append(
                _message_plan(
                    filename=f"{stem}-{item[0].lower().replace('ihc-', '')}.hl7",
                    report=report,
                    block=block,
                    item=item,
                    moment=moment,
                    shape=shape,
                    status=status,
                    value=value,
                )
            )
            built[-1] = replace(built[-1], encoding=encoding)

        messages.extend(built)

    return messages


def original_moment(built: Sequence[_MessagePlan], original: _MessagePlan) -> datetime:
    """取被更正那条的观察时间（倒挂用例要早于它）。"""
    return datetime.strptime(original.observed_at, "%Y%m%d%H%M%S") - timedelta(minutes=30)


def _correction_value(old_value: str) -> str:
    """更正后的值：与原值不同，且仍在正常取值域内。"""
    if old_value == "弱阳性":
        return "阴性"
    if old_value == "阴性":
        return "弱阳性"
    return "局灶阳性"


def _build_orders(
    rng: random.Random,
    reports: Sequence[dict[str, object]],
    order_count: int,
) -> list[_OrderPlan]:
    """生成申请侧报文。

    申请侧报文是「哪些检查项还在等结果」的权威口径：这里造两类 ——
    ``NW``（新增一项还没回报 → 草稿里出「待回报」占位）与
    ``CA``（取消一项 → 不再等它，也不占位）。
    """
    orders: list[_OrderPlan] = []
    for index in range(order_count):
        report = reports[index % len(reports)]
        blocks = list(report["blocks"])  # type: ignore[arg-type]
        block = blocks[index % len(blocks)]
        reserved = RESERVED_ORDER_ITEMS[index % len(RESERVED_ORDER_ITEMS)]
        order_time = _BASE_TIME + timedelta(hours=6 * (index + 1))

        groups: list[_OrderGroup] = [
            _OrderGroup(
                action="NW",
                accession_no=str(report["accession_no"]),
                order_no=str(block["order_no"]),
                item_code=reserved[0],
                item_name=reserved[1],
                order_time=_stamp(order_time),
                region_seq=int(block["region_seq"]),
                block_no=str(block["block_no"]),
                region_desc=str(block["region_desc"]),
            )
        ]

        # 第二组：取消一项**没有回报**的加做 —— 取消不占位，也不该把结果挡在门外。
        if index == 1:
            cancel_block = blocks[-1]
            cancel_items = list(cancel_block["ordered_items"])  # type: ignore[arg-type]
            cancel_item = cancel_items[-1]
            groups.append(
                _OrderGroup(
                    action="CA",
                    accession_no=str(report["accession_no"]),
                    order_no=str(cancel_block["order_no"]),
                    item_code=cancel_item[0],
                    item_name=cancel_item[1],
                    order_time=_stamp(order_time + timedelta(minutes=10)),
                    region_seq=int(cancel_block["region_seq"]),
                    block_no=str(cancel_block["block_no"]),
                    region_desc=str(cancel_block["region_desc"]),
                )
            )

        orders.append(
            _OrderPlan(
                filename=f"acc-gen-order-{index + 1:02d}.hl7",
                control_id=f"ORDMSG-GEN-{index + 1:02d}",
                message_time=_stamp(order_time + timedelta(minutes=1)),
                groups=tuple(groups),
            )
        )
    return orders


# --------------------------------------------------------------------------
# 规模计算
# --------------------------------------------------------------------------


def _per_report_counts(
    rng: random.Random, report_count: int, total_messages: int | None
) -> list[int]:
    """算每份报告各摊多少条 ORU。

    默认规模：第一份铺满整套形态（11 条），其余 8–9 条 —— 总量落在 24–30 之间。
    规模档：第一份留足整套形态的位置，其余按报告顺序摊开，总条数正好等于
    ``--scale``。摊到某份报告的条数不足 7 时，形态排布会被截断（更正对与倒挂对
    需要两条同键消息才成立），但形态覆盖由第一份报告兜住。
    """
    if total_messages is None:
        counts = [_FIRST_REPORT_MESSAGES]
        low, high = _OTHER_REPORT_RANGE
        while len(counts) < report_count:
            counts.append(rng.randint(low, high))
        return counts

    if total_messages <= 0:
        raise ValueError("--scale 必须大于 0")

    # 每份报告至少摊到一条，别留空报告。
    report_count = max(1, min(report_count, total_messages))
    first = min(_FIRST_REPORT_MESSAGES, total_messages)
    remaining = total_messages - first
    counts = [first]
    others = report_count - 1
    if others > 0:
        base, extra = divmod(remaining, others)
        for index in range(others):
            counts.append(base + (1 if index < extra else 0))
    return counts


def _report_count_for_scale(scale: int) -> int:
    """规模档下的报告份数：约每份 6–7 条 ORU。"""
    return max(1, round(scale * 3 / 20))


# --------------------------------------------------------------------------
# 写盘
# --------------------------------------------------------------------------


def _baseline_payload(reports: Sequence[dict[str, object]]) -> dict[str, object]:
    """基线快照（字段语义与出站接口一致，字段名与本地快照一致）。"""
    payload_reports = []
    for report in reports:
        blocks = []
        for block in report["blocks"]:  # type: ignore[union-attr]
            blocks.append(
                {
                    "region_seq": block["region_seq"],
                    "region_desc": block["region_desc"],
                    "block_no": block["block_no"],
                    "order_no": block["order_no"],
                    "ordered_items": [item[0] for item in block["ordered_items"]],  # type: ignore[index]
                }
            )
        payload_reports.append(
            {
                "accession_no": report["accession_no"],
                "base_version": report["base_version"],
                "blocks": blocks,
            }
        )
    return {"reports": payload_reports}


def dataset_fingerprint(root: Path | str) -> str:
    """算数据集指纹：逐文件（按文件名排序）哈希后再哈希一次。

    清单文件本身不参与 —— 它记的就是这个指纹，算进去会自指。
    """
    root_path = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root_path.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _write_dataset(root: Path, plan: DatasetPlan) -> dict[str, object]:
    """把计划写成一个数据集目录，返回清单。"""
    root.mkdir(parents=True, exist_ok=True)

    # 清掉上一轮自己写的文件（按文件名模式认领），不碰别人的文件。
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.name == MANIFEST_NAME or path.name == BASELINE_NAME or path.suffix == ".hl7":
            path.unlink()

    files: list[dict[str, object]] = []

    for message in plan.messages:
        payload = _encode_message(_oru_text(message), message.encoding)
        (root / message.filename).write_bytes(payload)
        files.append(
            {
                "name": message.filename,
                "kind": "ORU^R01",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "encoding": message.encoding,
                "shape": message.shape,
                "accession_no": message.accession_no,
                "item_code": message.item_code,
            }
        )

    for order in plan.orders:
        payload = _encode_message(_orm_text(order), "utf-8")
        (root / order.filename).write_bytes(payload)
        files.append(
            {
                "name": order.filename,
                "kind": "ORM^O01",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "encoding": "utf-8",
                "shape": "order_side",
                "accession_no": order.groups[0].accession_no,
                "item_code": order.groups[0].item_code,
            }
        )

    baseline = json.dumps(
        _baseline_payload(plan.reports), ensure_ascii=False, indent=2, sort_keys=False
    ) + "\n"
    (root / BASELINE_NAME).write_text(baseline, encoding="utf-8")
    files.append(
        {
            "name": BASELINE_NAME,
            "kind": "baseline",
            "bytes": len(baseline.encode("utf-8")),
            "sha256": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "encoding": "utf-8",
            "shape": "baseline",
        }
    )

    manifest: dict[str, object] = {
        "generator": "tests/fixtures/generate_samples.py",
        "seed": plan.seed,
        "reports": plan.report_count,
        "oru_messages": plan.oru_count,
        "orm_messages": plan.orm_count,
        "shape_counts": plan.shape_counts(),
        "data_boundary": "标识一律类目化占位，不含姓名 / 身份证 / 住院号 / 批号等患者可识别明细",
        "files": sorted(files, key=lambda item: str(item["name"])),
    }
    manifest["dataset_sha256"] = dataset_fingerprint(root)
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def generate(
    *,
    out: Path | str,
    dataset: str = DEFAULT_DATASET_NAME,
    flat: bool = False,
    seed: int = DEFAULT_SEED,
    scale: int | None = None,
    reports: int | None = None,
    orm_messages: int | None = None,
) -> tuple[Path, dict[str, object]]:
    """生成一份数据集，返回 ``(数据集目录, 清单)``。"""
    rng = random.Random(seed)

    if reports is None:
        report_count = (
            _report_count_for_scale(scale) if scale is not None else DEFAULT_REPORTS
        )
    else:
        report_count = max(1, reports)

    per_report = _per_report_counts(rng, report_count, scale)
    report_count = len(per_report)

    if orm_messages is None:
        if scale is None:
            orm_count = DEFAULT_ORM_MESSAGES
        else:
            orm_count = max(2, round(report_count * DEFAULT_ORM_MESSAGES / DEFAULT_REPORTS))
    else:
        orm_count = max(0, orm_messages)

    report_plans = _plan_reports(rng, report_count, per_report)
    messages = _build_messages(rng, report_plans)
    orders = _build_orders(rng, report_plans, orm_count)

    plan = DatasetPlan(
        seed=seed,
        reports=tuple(report_plans),
        messages=tuple(messages),
        orders=tuple(orders),
    )

    out_root = Path(out).expanduser()
    dataset_dir = out_root if flat else out_root / dataset
    manifest = _write_dataset(dataset_dir, plan)
    return dataset_dir, manifest


def _self_check(seed: int, scale: int | None) -> int:
    """同一���子连跑两次，比对目录指纹 —— 可重跑是硬要求，当场验。"""
    fingerprints = []
    counts = []
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="patho-gen-") as tmp:
            root, manifest = generate(
                out=Path(tmp), flat=True, seed=seed, scale=scale
            )
            fingerprints.append(str(manifest["dataset_sha256"]))
            counts.append((int(manifest["oru_messages"]), int(manifest["orm_messages"])))
    same = fingerprints[0] == fingerprints[1]
    print(f"自检｜两次生成的目录指纹：{fingerprints[0]}")
    print(f"自检｜                      {fingerprints[1]}")
    print(f"自检｜结论：{'一致（可重跑）' if same else '不一致（不可重跑）'}")
    if not same:
        return 1
    print(f"自检｜规模：ORU {counts[0][0]} 条 / ORM {counts[0][1]} 条")
    return 0


def _print_summary(dataset_dir: Path, manifest: dict[str, object]) -> None:
    """打印人看得懂的摘要。"""
    print(f"数据集目录：{dataset_dir}")
    print(f"随机种子：{manifest['seed']}")
    print(f"报告份数：{manifest['reports']}")
    print(f"ORU 报文：{manifest['oru_messages']} 条")
    print(f"ORM 报文：{manifest['orm_messages']} 条")

    counts = manifest["shape_counts"]
    labels: list[str] = []
    for shape, label in SHAPE_LABELS.items():
        count = counts.get(shape, 0)
        labels.append(f"{label} {count} 条")
    print("形态覆盖：" + "、".join(labels))
    print(f"目录指纹（sha256）：{manifest['dataset_sha256']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成脱敏病理结果样例（可重跑 / 断网可用 / 规模可配）"
    )
    parser.add_argument(
        "--out",
        default="tests/fixtures",
        help="输出根目录（默认 tests/fixtures）；数据集写在它下面的子目录里",
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET_NAME,
        help=f"数据集子目录名（默认 {DEFAULT_DATASET_NAME}）",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="直接写在 --out 顶层（不再建子目录），供守护进程直接监听",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"随机种子（默认 {DEFAULT_SEED}）"
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=None,
        help="目标 ORU 报文条数（如 2000）；不给则走默认规模",
    )
    parser.add_argument(
        "--reports", type=int, default=None, help="报告份数（不给则按规模推算）"
    )
    parser.add_argument(
        "--orm", type=int, default=None, help="ORM 报文条数（不给则按规模推算）"
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="同一种子连跑两次并比对目录指纹",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出清单摘要")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check(args.seed, args.scale)

    dataset_dir, manifest = generate(
        out=args.out,
        dataset=args.dataset,
        flat=args.flat,
        seed=args.seed,
        scale=args.scale,
        reports=args.reports,
        orm_messages=args.orm,
    )

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        _print_summary(dataset_dir, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
