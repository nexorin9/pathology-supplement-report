"""病理报告基线。

基线是「这份报告原本有哪些取材部位、每块申请了哪些加做项目」的权威清单，
对齐时拿它当参照系 —— 没有基线，结果就无处归属。

正式对接时基线来自病理系统 HTTP 接口（:mod:`patho_supplement.compose.pathology_client`，
任务 id 3 落地）。批处理重放与对照实验需要一个可离线复现的基线来源，因此这里
提供一份本地 JSON 快照的读取器：字段语义与出站接口一致，字段名不同。

快照格式::

    {
      "reports": [
        {
          "accession_no": "ACC-EXAMPLE-0001",
          "base_version": "base-v3",
          "blocks": [
            {"region_seq": 1, "region_desc": "子宫体", "block_no": "B1",
             "order_no": "ORD-EXAMPLE-0001", "ordered_items": ["IHC-CD3"]}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..models import Block, ReportBaseline

__all__ = [
    "BaselineError",
    "load_baseline_file",
    "baselines_from_payload",
    "baseline_from_payload",
]


class BaselineError(ValueError):
    """基线快照缺字段或格式不对。"""
    def __init__(self, detail: str, *, path: Path | None = None) -> None:
        self.detail = detail
        self.path = path
        location = f"{path}：" if path else ""
        super().__init__(f"{location}{detail}")


def _block_from_payload(payload: Mapping[str, Any], where: str) -> Block:
    region_seq_raw = payload.get("region_seq")
    region_seq: int | None
    if region_seq_raw is None or region_seq_raw == "":
        region_seq = None
    else:
        try:
            region_seq = int(region_seq_raw)
        except (TypeError, ValueError) as exc:
            raise BaselineError(f"{where} region_seq 不是整数：{region_seq_raw!r}") from exc

    ordered_items = payload.get("ordered_items") or ()
    if isinstance(ordered_items, str):
        ordered_items = (ordered_items,)
    return Block(
        region_seq=region_seq,
        region_desc=(payload.get("region_desc") or None),
        block_no=(payload.get("block_no") or None),
        order_no=(payload.get("order_no") or None),
        ordered_items=tuple(str(code) for code in ordered_items),
    )


def baseline_from_payload(
    payload: Mapping[str, Any], *, expected_accession_no: str | None = None
) -> ReportBaseline:
    """从一份报告的 JSON 载荷构造基线。

    本地基线快照与出站 ``GET`` 响应共用这一套字段词汇（``accession_no`` /
    ``base_version`` / ``blocks``），因此字段映射只写一遍，不会两处漂移。

    ``expected_accession_no`` 给了就核对病理号 —— 出站响应与请求对不上时直接
    报错，不把别的报告的基线当成这份报告的。
    """
    if not isinstance(payload, Mapping):
        raise BaselineError("报告基线必须是对象")
    accession_no = str(payload.get("accession_no") or "").strip()
    if not accession_no:
        raise BaselineError("报告基线缺 accession_no")
    if expected_accession_no and accession_no != expected_accession_no:
        raise BaselineError(
            f"报告基线病理号与请求不一致：{accession_no} != {expected_accession_no}"
        )
    blocks_raw = payload.get("blocks") or []
    if not isinstance(blocks_raw, list):
        raise BaselineError("报告基线的 blocks 必须是数组")
    blocks = tuple(
        _block_from_payload(block, f"blocks[{b_index}]")
        for b_index, block in enumerate(blocks_raw)
    )
    return ReportBaseline(
        accession_no=accession_no,
        blocks=blocks,
        base_version=(payload.get("base_version") or None),
    )


def baselines_from_payload(payload: Mapping[str, Any]) -> dict[str, ReportBaseline]:
    """从已解析的 JSON 载荷构造基线字典。"""
    reports = payload.get("reports")
    if reports is None:
        raise BaselineError("基线快照缺少 reports 字段")
    if not isinstance(reports, list):
        raise BaselineError("基线快照的 reports 必须是数组")

    baselines: dict[str, ReportBaseline] = {}
    for index, item in enumerate(reports):
        where = f"reports[{index}]"
        if not isinstance(item, Mapping):
            raise BaselineError(f"{where} 必须是对象")
        try:
            baseline = baseline_from_payload(item)
        except BaselineError as exc:
            raise BaselineError(f"{where}：{exc.detail}") from exc
        baselines[baseline.accession_no] = baseline
    return baselines


def load_baseline_file(path: Path | str) -> dict[str, ReportBaseline]:
    """读取本地基线快照。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise BaselineError("基线快照文件不存在", path=file_path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineError(f"基线快照不是合法 JSON：{exc}", path=file_path) from exc
    if not isinstance(payload, Mapping):
        raise BaselineError("基线快照顶层必须是对象", path=file_path)
    return baselines_from_payload(payload)
