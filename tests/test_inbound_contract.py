"""入站字段契约测试（任务 id 19）。

三条线：

1. **逐字段取值**：规范里登记的每个字段，在报文里按规范出现时，
   解析出来的值必须等于规范给出的期望值；
2. **逐字段拒收**：必填字段缺失（取值受限时是取值非法）时，必须按规范
   写明的错误码拒收 —— 整份拒收还是条目级拒收，都按规范来；
3. **规范与实现一致**：规范文档、字段登记表、解析器三者必须对得上；
   少一个字段、少一处映射、错一个属性，检查都要报出来。

规范文档是 ``docs/inbound-hl7-spec.md``，字段登记表在
``src/patho_supplement/ingest/contract.py``。
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Sequence

import pytest

from patho_supplement.ingest.contract import (
    INBOUND_FIELDS,
    KNOWN_ERROR_CODES,
    ORDER_REJECT_REASONS,
    SPEC_DOC_RELATIVE_PATH,
    check_spec_consistency,
    default_spec_doc_path,
    parse_spec_rows,
    read_spec_doc,
)
from patho_supplement.ingest.er7 import Er7ParseError, parse_message
from patho_supplement.ingest.orm_parser import parse_orm
from patho_supplement.ingest.oru_parser import parse_oru

# ------------------------------------------------------------------ 样例报文

ORU_MSH = "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG-EXAMPLE-0001|P|2.5.1"
ORU_SEGMENTS = (
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
    "ZBR|B1|子宫体|1",
    "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000",
)

ORM_MSH = "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|20260101101500||ORM^O01|ORDMSG-EXAMPLE-0001|P|2.5.1"
ORM_SEGMENTS = (
    "ORC|NW|ORD-EXAMPLE-0001",
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500",
)


def _message(header: str, segments: Sequence[str]) -> str:
    return "\r\n".join([header, *segments]) + "\r\n"


ORU_MESSAGE = _message(ORU_MSH, ORU_SEGMENTS)
ORM_MESSAGE = _message(ORM_MSH, ORM_SEGMENTS)


def _oru(text: str = ORU_MESSAGE):
    """解析一条结果侧报文，返回结果条目列表。"""
    message = parse_message(text, source="contract.hl7")
    return parse_oru(message, source_file="contract.hl7")


def _orm(text: str = ORM_MESSAGE):
    """解析一条申请侧报文，返回解析结果。"""
    message = parse_message(text, source="contract.hl7")
    return parse_orm(message, source_file="contract.hl7")


# ------------------------------------------------------------------ 逐字段取值

#: 每个字段的取值探针：解析报文后取到的那几个值（与登记表 parsed_example 对齐）。
PROBES: dict[str, Callable[[], tuple[str, ...]]] = {
    "MSH-1": lambda: (parse_message(ORU_MESSAGE).field_sep,),
    "MSH-2": lambda: (parse_message(ORU_MESSAGE).encoding_chars,),
    "MSH-3": lambda: (parse_message(ORU_MESSAGE).sending_application,),
    "MSH-9": lambda: (parse_message(ORU_MESSAGE).message_type,),
    "MSH-12": lambda: (parse_message(ORU_MESSAGE).version,),
    "ORC-1": lambda: (_orm().orders[0].action.value,),
    "OBR-2": lambda: (_oru()[0].accession_no,),
    "OBR-3": lambda: (_oru()[0].order_no,),
    "OBR-4": lambda: (_orm().orders[0].item_code, _orm().orders[0].item_name),
    "OBR-7": lambda: (_orm().orders[0].order_time,),
    "OBX-3": lambda: (_oru()[0].item_code, _oru()[0].item_name),
    "OBX-5": lambda: (_oru()[0].value,),
    "OBX-11": lambda: (_oru()[0].status.value,),
    "OBX-14": lambda: (_oru()[0].observed_at,),
    "ZBR-1": lambda: (_oru()[0].block_no,),
    "ZBR-2": lambda: (_oru()[0].region_desc,),
    "ZBR-3": lambda: (str(_oru()[0].region_seq),),
}


@pytest.mark.parametrize("spec", INBOUND_FIELDS, ids=lambda spec: spec.key)
def test_field_parses_to_spec_value(spec):
    """规范里登记的字段，解析值必须等于规范给出的期望值。"""
    probe = PROBES.get(spec.key)
    assert probe is not None, f"{spec.key} 没有取值探针"
    assert probe() == spec.parsed_example


# ------------------------------------------------------------------ 逐字段拒收

#: 必填字段缺失 / 取值非法时的样例报文（缺什么就改哪一段）。
MISSING_CASES: dict[str, str] = {
    # 报文头不成型
    "MSH-1": "MSH",
    "MSH-9": _message(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500|||MSG-EXAMPLE-0001|P|2.5.1",
        ORU_SEGMENTS,
    ),
    "MSH-12": _message(
        "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG-EXAMPLE-0001|P|",
        ORU_SEGMENTS,
    ),
    # 结果侧
    "OBR-2": _message(
        ORU_MSH,
        (
            "OBR|1||ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
            "ZBR|B1|子宫体|1",
            "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000",
        ),
    ),
    "OBX-3": _message(
        ORU_MSH,
        (
            "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
            "ZBR|B1|子宫体|1",
            "OBX|1|ST||阴性||||||F|||20260101130000",
        ),
    ),
    # 结果状态取值非法
    "OBX-11": _message(
        ORU_MSH,
        (
            "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
            "ZBR|B1|子宫体|1",
            "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||Z|||20260101130000",
        ),
    ),
    # 部位序号不是整数
    "ZBR-3": _message(
        ORU_MSH,
        (
            "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
            "ZBR|B1|子宫体|1.0",
            "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000",
        ),
    ),
    # 申请侧（条目级拒收）
    "ORC-1": _message(
        ORM_MSH,
        (
            "ORC||ORD-EXAMPLE-0001",
            "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500",
        ),
    ),
    "OBR-3": _message(
        ORM_MSH,
        (
            "ORC|NW|ORD-EXAMPLE-0001",
            "OBR|1|ACC-EXAMPLE-0001||IHC-CD3^CD3^L|||20260101101500",
        ),
    ),
    "OBR-4": _message(
        ORM_MSH,
        (
            "ORC|NW|ORD-EXAMPLE-0001",
            "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001||",
        ),
    ),
}


@pytest.mark.parametrize(
    "spec", [spec for spec in INBOUND_FIELDS if spec.mandatory], ids=lambda spec: spec.key
)
def test_mandatory_field_rejected_when_missing_or_invalid(spec):
    """必填字段缺失（取值受限时是取值非法）必须按规范给出的码拒收。"""
    text = MISSING_CASES[spec.key]
    assert spec.missing_code is not None
    assert spec.missing_scope in {"message", "entry"}

    if spec.missing_scope == "message":
        with pytest.raises(Er7ParseError) as excinfo:
            _oru(text)
        assert excinfo.value.code == spec.missing_code
    else:
        result = _orm(text)
        reasons = [rejected.reason for rejected in result.rejected]
        assert spec.missing_code in reasons
        # 条目级拒收：坏的一条不拖累好的一条
        assert result.orders == []


def test_orm_missing_accession_is_entry_level_rejection():
    """申请侧缺病理号只拒这一条，拒收原因按规范写明。"""
    result = _orm(
        _message(
            ORM_MSH,
            (
                "ORC|NW|ORD-EXAMPLE-0001",
                "OBR|1||ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500",
            ),
        )
    )
    reasons = [rejected.reason for rejected in result.rejected]
    assert "missing_accession_no" in reasons
    assert result.orders == []


def test_illegal_control_action_is_entry_level_rejection():
    """非法控制动作拒收该条，原因码按规范写明。"""
    result = _orm(
        _message(
            ORM_MSH,
            (
                "ORC|XX|ORD-EXAMPLE-0001",
                "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500",
            ),
        )
    )
    reasons = [rejected.reason for rejected in result.rejected]
    assert "illegal_control_action" in reasons


# ------------------------------------------------- 规范里写明的「可空 / 回落」口径


def test_zbr_all_fields_may_be_empty():
    """ZBR 三个字段都允许为空，空就是「不知道」，不是「没有」。"""
    entries = _oru(
        _message(
            ORU_MSH,
            (
                "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
                "ZBR|||",
                "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000",
            ),
        )
    )
    assert len(entries) == 1
    assert entries[0].block_no is None
    assert entries[0].region_desc is None
    assert entries[0].region_seq is None


def test_obx14_falls_back_to_obr7():
    """OBX-14 为空时回落 OBR-7（规范里写明的口径）。"""
    entries = _oru(
        _message(
            ORU_MSH,
            (
                "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
                "ZBR|B1|子宫体|1",
                "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||",
            ),
        )
    )
    assert entries[0].observed_at == "20260101120000"


def test_empty_obx11_defaults_to_final():
    """OBX-11 为空按终报 F 处理（规范里写明的口径）。"""
    entries = _oru(
        _message(
            ORU_MSH,
            (
                "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
                "ZBR|B1|子宫体|1",
                "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||",
            ),
        )
    )
    assert entries[0].status.value == "F"


def test_empty_obx5_is_not_mergeable():
    """OBX-5 为空不并入草稿（规范里写明的口径）。"""
    entries = _oru(
        _message(
            ORU_MSH,
            (
                "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000",
                "ZBR|B1|子宫体|1",
                "OBX|1|ST|IHC-CD3^CD3^L|||||||F|||20260101130000",
            ),
        )
    )
    assert entries[0].value is None
    assert entries[0].mergeable is False


# ------------------------------------------------------------------ 规范文档本身


def test_spec_doc_exists_at_expected_path():
    path = default_spec_doc_path()
    assert path.is_file()
    assert path.relative_to(default_spec_doc_path().parents[1]) == SPEC_DOC_RELATIVE_PATH


def test_spec_doc_has_zbr_table_and_desensitized_examples():
    text = read_spec_doc()
    rows = {row.key for row in parse_spec_rows(text)}
    assert {"ZBR-1", "ZBR-2", "ZBR-3"} <= rows
    # 脱敏示例：类目化占位，不得出现真实住院号 / 患者化名
    assert "ACC-EXAMPLE-0001" in text
    assert "IMMUNO-LAB" in text
    for forbidden in ("住院号", "患者姓名", "身份证号"):
        assert forbidden not in text


def test_spec_doc_marks_zbr_as_hospital_convention():
    """ZBR 段位是「院内约定（经验推断）」，差异集中在适配层一处。"""
    text = read_spec_doc()
    assert "院内约定（经验推断）" in text
    assert "适配层" in text
    assert "以目标医院的实际段位为准" in text


def test_every_known_reject_code_appears_in_spec():
    """规范里列出的拒收码必须覆盖解析器真正会抛的那些码。"""
    text = read_spec_doc()
    missing = sorted(code for code in KNOWN_ERROR_CODES if code not in text)
    assert not missing, f"规范里没写的拒收码：{missing}"


def test_registry_error_codes_exist():
    """登记表里写的拒收码必须真实存在。"""
    for spec in INBOUND_FIELDS:
        if spec.missing_code is None:
            continue
        assert spec.missing_code in KNOWN_ERROR_CODES
        if spec.missing_scope == "entry":
            assert spec.missing_code in ORDER_REJECT_REASONS


# ------------------------------------------------------------------ 一致性检查


def test_spec_matches_registry_and_parser():
    """规范文档 ↔ 字段登记表 ↔ 解析器三方对账必须通过。"""
    result = check_spec_consistency()
    assert result.ok, result.report()
    assert result.fields_checked == len(INBOUND_FIELDS)


def test_checker_detects_field_missing_from_spec():
    """规范里少一个字段 → 检查必须报出来。"""
    text = read_spec_doc()
    broken = "\n".join(
        line for line in text.splitlines() if not line.startswith("| ZBR-2 ")
    )
    result = check_spec_consistency(doc_text=broken)
    assert not result.ok
    assert any("规范缺字段：ZBR-2" in error for error in result.errors)


def test_checker_detects_field_missing_in_parser():
    """解析器里没有的取值表达式 → 检查必须报出来。"""
    broken_registry = tuple(
        dataclasses.replace(spec, mappings=(("patho_supplement.ingest.er7", "field(999)"),))
        if spec.key == "ZBR-1"
        else spec
        for spec in INBOUND_FIELDS
    )
    result = check_spec_consistency(registry=broken_registry)
    assert not result.ok
    assert any("解析器缺映射：ZBR-1" in error for error in result.errors)


def test_checker_detects_attribute_drift():
    """规范里的必填性与登记表对不上 → 检查必须报出来。"""
    text = read_spec_doc()
    broken = text.replace("| ZBR-3 | 部位序号 | 否（须为整数） |", "| ZBR-3 | 部位序号 | 是 |")
    result = check_spec_consistency(doc_text=broken)
    assert not result.ok
    assert any("字段属性不一致 ZBR-3.required" in error for error in result.errors)


def test_checker_detects_unknown_error_code():
    """登记表的拒收码不存在 → 检查必须报出来。"""
    broken_registry = tuple(
        dataclasses.replace(spec, missing_code="no_such_code")
        if spec.key == "MSH-12"
        else spec
        for spec in INBOUND_FIELDS
    )
    result = check_spec_consistency(registry=broken_registry)
    assert not result.ok
    assert any("错误码不存在：MSH-12" in error for error in result.errors)


def test_checker_detects_doc_without_machine_checked_section():
    """规范里没有机器校验段落 → 检查必须报出来。"""
    result = check_spec_consistency(doc_text="# 只是个标题\n")
    assert not result.ok
    assert any("机器校验段落" in error for error in result.errors)
