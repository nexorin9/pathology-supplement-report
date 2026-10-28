"""入站字段契约：字段登记表、规范文档与解析器的三方一致性检查。

对接院内任何一家病理 / 检验系统时，《入站报文字段规范》（``docs/inbound-hl7-spec.md``）
是**唯一依据**。但文档会漂：改了解析器忘了改规范，或者改了规范忘了改实现，
联调时才发现 —— 那时机器已经在院内机上了。

本模块把规范里「本产品真正消费到的字段」做成一张**登记表**
(:data:`INBOUND_FIELDS`)。每条写明段位、名称、必填性、取值域、脱敏示例、错误处理，
以及**解析器里取这个值用的源码表达式**。

:func:`check_spec_consistency` 做三方对账：

1. **登记表 ↔ 规范文档**：规范里列的字段必须与登记表逐项一致（名称、必填性、
   取值域、脱敏示例、错误处理），一个字段都不能多、不能少、不能对不上；
2. **登记表 ↔ 解析器**：登记的每个字段，取值表达式必须在对应模块的源码里
   真实存在 —— 规范写了、解析器没读，就是假规范；
3. **登记表 ↔ 错误码表**：必填字段必须给出拒收码，且该码在解析器的错误码
   字典里真实存在（消息级拒收与条目级拒收分开两套）。

任何一条对不上，检查即失败。跑法::

    python -m patho_supplement.ingest.contract --check

登记表里的 ``example`` 是**报文里的原始取值**（进规范文档的「脱敏示例」列），
``parsed_example`` 是**解析之后应得的取值**（进契约测试的期望值）。两者有时
不一样 —— 例如 ``OBR-4`` 原文是 ``IHC-CD3^CD3^L``，解析出来是项目码
``IHC-CD3`` 与项目名 ``CD3``。
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .er7 import PARSE_ERROR_REASONS

__all__ = [
    "FieldSpec",
    "INBOUND_FIELDS",
    "KNOWN_ERROR_CODES",
    "ORDER_REJECT_REASONS",
    "SPEC_DOC_RELATIVE_PATH",
    "SpecCheckResult",
    "SpecRow",
    "check_spec_consistency",
    "default_spec_doc_path",
    "main",
    "parse_spec_rows",
    "read_spec_doc",
    "render_markdown_rows",
]

#: 规范文档相对项目根目录的路径。
SPEC_DOC_RELATIVE_PATH = Path("docs") / "inbound-hl7-spec.md"

#: 机器校验段落的起止标记。规范文档里围起来的表格行会被逐字段对账。
TABLE_START_MARKER = "<!-- spec-table:start -->"
TABLE_END_MARKER = "<!-- spec-table:end -->"

#: 申请侧（``ORM^O01``）条目级拒收原因 —— 与 ``orm_parser`` 里写出来的那些一致。
#: 条目级拒收只拒这一条，**同一份报文里的其余条目照常解析**。
ORDER_REJECT_REASONS: frozenset[str] = frozenset(
    {
        "invalid_zbr",
        "missing_control_action",
        "illegal_control_action",
        "missing_obr",
        "missing_orc",
        "missing_accession_no",
        "missing_order_no",
        "missing_item_code",
        "empty_message",
    }
)

#: 全部已知拒收码：消息级（整份报文拒收）+ 条目级（只拒这一条）。
KNOWN_ERROR_CODES: frozenset[str] = frozenset(PARSE_ERROR_REASONS) | ORDER_REJECT_REASONS


@dataclass(frozen=True)
class FieldSpec:
    """一条入站字段的登记条目。

    ``key`` 由段名与字段序号拼出（``OBX-11``），规范文档与登记表都按它对齐。
    """

    segment: str
    index: int
    name: str
    required: str
    domain: str
    example: str
    error: str
    streams: tuple[str, ...]
    mandatory: bool = False
    parsed_example: tuple[str, ...] = ()
    mappings: tuple[tuple[str, str], ...] = ()
    missing_code: str | None = None
    missing_scope: str = ""
    missing_kind: str = "missing"

    @property
    def key(self) -> str:
        return f"{self.segment}-{self.index}"


#: 本产品消费到的入站字段登记表。规范文档的字段表必须与这张表逐项一致。
INBOUND_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        segment="MSH",
        index=1,
        name="字段分隔符",
        required="是",
        domain="单字符",
        example="|",
        error="报文首段不足 4 个字符 → 整份拒收（code=missing_field_sep）",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("|",),
        mappings=(("patho_supplement.ingest.er7", "self.field_sep"),),
        missing_code="missing_field_sep",
        missing_scope="message",
    ),
    FieldSpec(
        segment="MSH",
        index=2,
        name="编码字符",
        required="否",
        domain="组件/重复/转义/子组件分隔符四字符",
        example="^~\\&",
        error="为空 → 回退默认 ^~\\&，不拒收",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("^~\\&",),
        mappings=(("patho_supplement.ingest.er7", "self.parts[1]"),),
    ),
    FieldSpec(
        segment="MSH",
        index=3,
        name="发送方应用（结果来源系统）",
        required="否",
        domain="自由文本",
        example="IMMUNO-LAB",
        error="为空 → 来源系统回落 result-inbox（结果侧）/ pathology-system（申请侧）",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("IMMUNO-LAB",),
        mappings=(("patho_supplement.ingest.er7", "self.field(3)"),),
    ),
    FieldSpec(
        segment="MSH",
        index=9,
        name="报文类型",
        required="是",
        domain="ORU^R01（结果侧）/ ORM^O01（申请侧）",
        example="ORU^R01",
        error="缺 → 整份拒收（code=missing_message_type）；类型不符 → 整份拒收（code=not_oru / not_orm）",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("ORU^R01",),
        mappings=(("patho_supplement.ingest.er7", "self.field(9)"),),
        missing_code="missing_message_type",
        missing_scope="message",
    ),
    FieldSpec(
        segment="MSH",
        index=12,
        name="版本号",
        required="是",
        domain="HL7 版本号（如 2.5.1）",
        example="2.5.1",
        error="缺 → 整份拒收（code=missing_version）",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("2.5.1",),
        mappings=(("patho_supplement.ingest.er7", "self.field(12)"),),
        missing_code="missing_version",
        missing_scope="message",
    ),
    FieldSpec(
        segment="ORC",
        index=1,
        name="订单控制动作",
        required="申请侧是",
        domain="NW 新增 / CA 取消 / HD 暂停 / DC 终止 / RE 结果随后",
        example="NW",
        error="缺 → 拒收该条（reason=missing_control_action）；取值非法 → 拒收该条（reason=illegal_control_action）",
        streams=("ORM^O01",),
        mandatory=True,
        parsed_example=("NW",),
        mappings=(("patho_supplement.ingest.orm_parser", "segment.field(1)"),),
        missing_code="missing_control_action",
        missing_scope="entry",
    ),
    FieldSpec(
        segment="OBR",
        index=2,
        name="病理号",
        required="是",
        domain="自由文本",
        example="ACC-EXAMPLE-0001",
        error="空 → 结果侧整份拒收（code=missing_accession）；申请侧拒收该条（reason=missing_accession_no）",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("ACC-EXAMPLE-0001",),
        mappings=(
            ("patho_supplement.ingest.oru_parser", "segment.field(2)"),
            ("patho_supplement.ingest.orm_parser", "obr.field(2)"),
        ),
        missing_code="missing_accession",
        missing_scope="message",
    ),
    FieldSpec(
        segment="OBR",
        index=3,
        name="加做申请号",
        required="申请侧是 / 结果侧否",
        domain="自由文本",
        example="ORD-EXAMPLE-0001",
        error="空 → 申请侧拒收该条（reason=missing_order_no）；结果侧留空",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("ORD-EXAMPLE-0001",),
        mappings=(
            ("patho_supplement.ingest.oru_parser", "segment.field(3)"),
            ("patho_supplement.ingest.orm_parser", "obr.field(3)"),
        ),
        missing_code="missing_order_no",
        missing_scope="entry",
    ),
    FieldSpec(
        segment="OBR",
        index=4,
        name="申请项目码（组件1=码，组件2=名称）",
        required="申请侧是 / 结果侧否",
        domain="组件分隔符 ^ 分隔的码与名称",
        example="IHC-CD3^CD3^L",
        error="空 → 申请侧拒收该条（reason=missing_item_code）",
        streams=("ORU^R01", "ORM^O01"),
        mandatory=True,
        parsed_example=("IHC-CD3", "CD3"),
        mappings=(
            ("patho_supplement.ingest.oru_parser", "segment.component(4, 1)"),
            ("patho_supplement.ingest.orm_parser", "obr.component(4, 1)"),
        ),
        missing_code="missing_item_code",
        missing_scope="entry",
    ),
    FieldSpec(
        segment="OBR",
        index=7,
        name="申请时间",
        required="否",
        domain="HL7 时间戳 YYYYMMDDHHMMSS",
        example="20260101101500",
        error="空 → 结果侧回落为观察时间；仍空则观察时间为空（幂等键该段留空）",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("20260101101500",),
        mappings=(
            ("patho_supplement.ingest.oru_parser", "segment.field(7)"),
            ("patho_supplement.ingest.orm_parser", "obr.field(7)"),
        ),
    ),
    FieldSpec(
        segment="OBX",
        index=3,
        name="结果项目码（组件1=码，组件2=名称）",
        required="是",
        domain="组件分隔符 ^ 分隔的码与名称",
        example="IHC-CD3^CD3^L",
        error="空 → 整份拒收（code=missing_item_code）",
        streams=("ORU^R01",),
        mandatory=True,
        parsed_example=("IHC-CD3", "CD3"),
        mappings=(
            ("patho_supplement.ingest.oru_parser", "segment.component(3, 1)"),
            ("patho_supplement.ingest.oru_parser", "segment.component(3, 2)"),
        ),
        missing_code="missing_item_code",
        missing_scope="message",
    ),
    FieldSpec(
        segment="OBX",
        index=5,
        name="结果值",
        required="否（空值不进草稿）",
        domain="自由文本",
        example="阴性",
        error="空 → 不并入草稿，进待人工处理清单",
        streams=("ORU^R01",),
        parsed_example=("阴性",),
        mappings=(("patho_supplement.ingest.oru_parser", "segment.field(5)"),),
    ),
    FieldSpec(
        segment="OBX",
        index=11,
        name="结果状态",
        required="取值受限",
        domain="P 初步 / F 终报 / C 更正 / X 无法获得",
        example="F",
        error="取值非法 → 整份拒收（code=illegal_result_status）；为空 → 按终报 F 处理",
        streams=("ORU^R01",),
        parsed_example=("F",),
        mappings=(("patho_supplement.ingest.oru_parser", "segment.field(11)"),),
        missing_code="illegal_result_status",
        missing_scope="message",
        missing_kind="invalid",
    ),
    FieldSpec(
        segment="OBX",
        index=14,
        name="结果观察时间",
        required="否",
        domain="HL7 时间戳 YYYYMMDDHHMMSS",
        example="20260101130000",
        error="空 → 回落 OBR-7；仍空则为空",
        streams=("ORU^R01",),
        parsed_example=("20260101130000",),
        mappings=(("patho_supplement.ingest.oru_parser", "segment.field(14)"),),
    ),
    FieldSpec(
        segment="ZBR",
        index=1,
        name="蜡块号",
        required="否",
        domain="自由文本",
        example="B1",
        error="空 → 不参与一级匹配，进挂起（缺 block_no）",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("B1",),
        mappings=(("patho_supplement.ingest.zbr", "segment.field(1)"),),
    ),
    FieldSpec(
        segment="ZBR",
        index=2,
        name="取材部位描述",
        required="否",
        domain="自由文本",
        example="子宫体",
        error="空 → 不参与二级匹配",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("子宫体",),
        mappings=(("patho_supplement.ingest.zbr", "segment.field(2)"),),
    ),
    FieldSpec(
        segment="ZBR",
        index=3,
        name="部位序号",
        required="否（须为整数）",
        domain="整数",
        example="1",
        error="非整数 → 整份拒收（code=invalid_region_seq）",
        streams=("ORU^R01", "ORM^O01"),
        parsed_example=("1",),
        mappings=(("patho_supplement.ingest.zbr", "segment.field(3)"),),
        missing_code="invalid_region_seq",
        missing_scope="message",
        missing_kind="invalid",
    ),
)


@dataclass(frozen=True)
class SpecRow:
    """规范文档里的一行字段（从机器校验段落解析出来）。"""

    key: str
    name: str
    required: str
    domain: str
    example: str
    error: str


@dataclass
class SpecCheckResult:
    """一致性检查的结果。"""

    errors: list[str] = field(default_factory=list)
    fields_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        lines = ["入站字段规范一致性检查", f"字段条目：{self.fields_checked}"]
        if self.ok:
            lines.append("结论：通过（规范文档、字段登记表、解析器三方一致）")
            return "\n".join(lines)
        lines.append(f"结论：不通过（{len(self.errors)} 处不一致）")
        lines.extend(f"  - {error}" for error in self.errors)
        return "\n".join(lines)


# ------------------------------------------------------------------ 文档解析


def _split_markdown_row(line: str) -> list[str]:
    """按**未转义**的 ``|`` 切表格行。

    表格里的字面竖线写成 ``\\|``（例如字段分隔符本身），切分时不算分隔符，
    还原后交给 :func:`_clean_cell` 去掉反斜杠。
    """
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in line.strip():
        if escaped:
            if char == "|":
                buf.append("|")
            else:
                buf.append("\\")
                buf.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "|":
            cells.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    cells.append("".join(buf))
    # 表格行以 ``|`` 开头结尾时，首尾各多出一个空单元格 —— 去掉。
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def _clean_cell(cell: str) -> str:
    """去掉代码标记与首尾空白。"""
    return cell.strip().replace("`", "").strip()


def parse_spec_rows(doc_text: str) -> list[SpecRow]:
    """从规范文档的机器校验段落里取出字段行。"""
    start = doc_text.find(TABLE_START_MARKER)
    end = doc_text.find(TABLE_END_MARKER)
    if start < 0 or end < 0 or end <= start:
        return []
    region = doc_text[start:end]

    rows: list[SpecRow] = []
    for line in region.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_markdown_row(stripped)
        if len(cells) < 6:
            continue
        key = _clean_cell(cells[0])
        # 表头行与分隔行不算字段行；其余行一律当字段行 ——
        # 段位写错（如 OBX-1l）也要被「规范多字段」揪出来，不能悄悄放过。
        if not key or key == "段位" or set(key) <= set("-: "):
            continue
        rows.append(
            SpecRow(
                key=key,
                name=_clean_cell(cells[1]),
                required=_clean_cell(cells[2]),
                domain=_clean_cell(cells[3]),
                example=_clean_cell(cells[4]),
                error=_clean_cell(cells[5]),
            )
        )
    return rows


def render_markdown_rows(registry: Sequence[FieldSpec] = INBOUND_FIELDS) -> str:
    """把登记表渲染成规范文档里的表格行（管道符做转义）。

    规范文档里的字段表按这个输出誊写即可 —— 誊错会被一致性检查揪出来。
    """
    lines = [
        "| 段位 | 字段名 | 必填 | 取值域 | 脱敏示例 | 错误处理 |",
        "|---|---|---|---|---|---|",
    ]
    for spec in registry:
        example = spec.example.replace("|", "\\|")
        lines.append(
            f"| {spec.key} | {spec.name} | {spec.required} | {spec.domain} | "
            f"`{example}` | {spec.error} |"
        )
    return "\n".join(lines)


def default_spec_doc_path() -> Path:
    """规范文档的默认路径（项目根目录下的 ``docs/inbound-hl7-spec.md``）。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / SPEC_DOC_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return here.parents[3] / SPEC_DOC_RELATIVE_PATH


def read_spec_doc(path: Path | None = None) -> str:
    """读规范文档全文。"""
    doc_path = path or default_spec_doc_path()
    if not doc_path.is_file():
        raise FileNotFoundError(f"找不到入站字段规范文档：{doc_path}")
    return doc_path.read_text(encoding="utf-8")


def _module_source(dotted: str) -> str | None:
    """取一个模块的源码文本（找不到模块时返回 ``None``）。"""
    try:
        module = importlib.import_module(dotted)
    except Exception:  # pragma: no cover - 依赖缺失时的兜底
        return None
    file_path = getattr(module, "__file__", None)
    if not file_path:
        return None
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 读不到源码时的兜底
        return None


# ------------------------------------------------------------------ 一致性检查


def check_spec_consistency(
    *,
    doc_text: str | None = None,
    registry: Sequence[FieldSpec] = INBOUND_FIELDS,
    parser_sources: dict[str, str] | None = None,
) -> SpecCheckResult:
    """三方对账：规范文档 ↔ 字段登记表 ↔ 解析器。

    校验内容：

    - 规范里列的字段与登记表**逐项一致**（名称 / 必填性 / 取值域 / 示例 / 错误处理）；
    - 规范里的字段**不多不少**（登记有规范缺、规范有登记缺都算不一致）；
    - 登记的每个字段在对应解析模块里**真有取值表达式**；
    - 必填字段给出了拒收码，且该码在已知拒收码集合里真实存在。
    """
    result = SpecCheckResult()

    if doc_text is None:
        doc_text = read_spec_doc()
    rows = parse_spec_rows(doc_text)
    if not rows:
        result.errors.append(
            f"规范文档里找不到机器校验段落（{TABLE_START_MARKER} … {TABLE_END_MARKER}）"
        )
        return result

    doc_by_key: dict[str, SpecRow] = {}
    for row in rows:
        if row.key in doc_by_key:
            result.errors.append(f"规范里字段重复：{row.key}")
        doc_by_key[row.key] = row

    reg_by_key: dict[str, FieldSpec] = {spec.key: spec for spec in registry}
    result.fields_checked = len(reg_by_key)

    for key, spec in reg_by_key.items():
        row = doc_by_key.get(key)
        if row is None:
            result.errors.append(f"规范缺字段：{key}（{spec.name}）")
            continue
        for attribute in ("name", "required", "domain", "example", "error"):
            expected = getattr(spec, attribute)
            actual = getattr(row, attribute)
            if expected != actual:
                result.errors.append(
                    f"字段属性不一致 {key}.{attribute}：规范={actual!r} 登记={expected!r}"
                )

    for key in doc_by_key:
        if key not in reg_by_key:
            result.errors.append(f"规范多字段：{key}（登记表里没有）")

    for key, spec in reg_by_key.items():
        for owner, accessor in spec.mappings:
            source = (parser_sources or {}).get(owner)
            if source is None:
                source = _module_source(owner)
            if source is None:
                result.errors.append(f"解析器缺映射：{key} 的取值模块 {owner} 找不到")
            elif accessor not in source:
                result.errors.append(
                    f"解析器缺映射：{key} 在 {owner} 里没有 {accessor} 取值"
                )

    for key, spec in reg_by_key.items():
        if not spec.mandatory:
            continue
        if not spec.missing_code:
            result.errors.append(f"必填字段未写错误码：{key}")
            continue
        if spec.missing_code not in KNOWN_ERROR_CODES:
            result.errors.append(
                f"错误码不存在：{key} → {spec.missing_code}（不在已知拒收码集合里）"
            )

    return result


# ------------------------------------------------------------------ 命令行


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m patho_supplement.ingest.contract`` 的入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m patho_supplement.ingest.contract",
        description="入站字段规范与解析器的一致性检查",
    )
    parser.add_argument(
        "--check", action="store_true", help="检查规范文档、登记表与解析器是否一致（默认动作）"
    )
    parser.add_argument(
        "--doc", type=Path, default=None, help="规范文档路径（默认 docs/inbound-hl7-spec.md）"
    )
    parser.add_argument(
        "--print-table", action="store_true", help="打印登记表对应的规范表格行（誊写规范文档用）"
    )
    args = parser.parse_args(argv)

    if args.print_table:
        print(render_markdown_rows())
        return 0

    try:
        doc_text = read_spec_doc(args.doc)
    except FileNotFoundError as exc:
        print(f"入站字段规范一致性检查\n结论：不通过\n  - {exc}")
        return 1

    result = check_spec_consistency(doc_text=doc_text)
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
