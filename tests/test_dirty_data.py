"""真实数据形态边界用例。

院内回报文件不是干净的 UTF-8 文本，也不是格式严整的 HL7 报文：编码有 UTF-8
也有 GBK（甚至同一个文件里两种都有）、部位描述可能超长、可能带零宽字符、
可能整段是标点，某一行的分隔符还可能多写一个。

这一组用例把上面每一种形态都钉住，判据只有一条：

- **要么给出确定结果**（归一化 / 打分不抛异常，返回可预期的值）；
- **要么给出结构化错误**（带**字段名**与**行号**，说清是哪一行哪个字段坏了）。

绝不允许「看不懂但看起来还行」的结果混进草稿 —— 那就是一份写错部位的补充报告。
"""

from __future__ import annotations

import json

import pytest

from patho_supplement.align.matcher import align_entry
from patho_supplement.align.normalizer import normalize_block_no, normalize_region_desc
from patho_supplement.align.scorer import LexicalScorer, tokenize
from patho_supplement.audit.ledger import EVENT_ENCODING_DETECTED
from patho_supplement.db import Database
from patho_supplement.ingest.dirty import decode_payload
from patho_supplement.ingest.er7 import Er7ParseError, parse_message
from patho_supplement.ingest.oru_parser import parse_oru
from patho_supplement.models import ReportBaseline, Block, MatchLevel
from patho_supplement.replay import run_replay

# ---------------------------------------------------------------- 报文样板

_HEAD = (
    "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101120000||ORU^R01|MSG1|P|2.5.1\r\n"
)
_OBR = "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
_OBX = "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n"


def _message(*lines: str) -> str:
    return _HEAD + "".join(lines)


# ---------------------------------------------------------------- 归一化

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("！！！", None),
        ("·—…", None),
        (None, None),
        ("　", None),
        ("​‌﻿", None),
    ],
)
def test_normalize_region_desc_returns_none_for_empty_and_punctuation(raw, expected):
    """空串、纯标点、纯空白、纯不可见字符 → 一律视为「没填」。"""
    assert normalize_region_desc(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("---", None),
        ("***", None),
        ("", None),
        ("​", None),
    ],
)
def test_normalize_block_no_returns_none_for_empty_and_punctuation(raw, expected):
    """蜡块号同理：空值就是没填，不参与相等比较。"""
    assert normalize_block_no(raw) == expected


def test_normalize_handles_none_without_raising():
    """None 值不抛未捕获异常。"""
    assert normalize_block_no(None) is None
    assert normalize_region_desc(None) is None


def test_normalize_strips_invisible_format_characters():
    """零宽空格 / BOM / 软连字符不该让同一部位变成两个键。"""
    assert normalize_region_desc("子宫​体") == "子宫体"
    assert normalize_region_desc("﻿子宫体") == "子宫体"
    assert normalize_region_desc("子­宫体") == "子宫体"
    assert normalize_block_no("B​3") == "B3"


def test_normalize_handles_very_long_description():
    """>200 字的超长描述：归一化照常，不抛异常、不截断。"""
    long_desc = "子宫体内侧" * 100  # 500 字
    assert len(long_desc) > 200
    normalized = normalize_region_desc(long_desc)
    assert normalized is not None
    assert len(normalized) == len(long_desc)
    # 幂等
    assert normalize_region_desc(normalized) == normalized


def test_normalize_handles_rare_characters():
    """生僻字（扩展 B 区）照常归一化，不抛异常。"""
    rare = "子宫体𠀀𪚥"
    normalized = normalize_region_desc(rare)
    assert normalized is not None
    assert "𠀀" in normalized


def test_scorer_handles_all_dirty_inputs_without_raising():
    """打分器对上面每一种脏输入都返回确定结果。"""
    scorer = LexicalScorer(["子宫体内侧", "宫颈部", "乳腺组织"])
    dirty_inputs = [
        "",
        "   ",
        "！！！",
        None,
        "子宫体内侧" * 100,
        "子宫体𠀀𪚥",
        "子宫​体",
        "﻿",
    ]
    for value in dirty_inputs:
        score = scorer.score(value, "子宫体内侧")
        assert 0.0 <= score <= 1.0
        assert scorer.score_candidates(value, ["宫颈部", "乳腺组织"])


def test_tokenize_keeps_rare_characters_in_supplementary_planes():
    """扩展 B 区的生僻字不该被整段丢掉。"""
    tokens = tokenize("子宫体𠀀")
    assert "子宫" in tokens and "宫体" in tokens
    assert any("𠀀" in token for token in tokens)


def test_scorer_is_deterministic_for_long_descriptions():
    """超长描述的打分可复现（同输入两次同结果）。"""
    scorer = LexicalScorer(["子宫体内侧"])
    query = "子宫体" + "腺" * 300
    first = scorer.score(query, "子宫体内侧")
    second = scorer.score(query, "子宫体内侧")
    assert first == second


# ---------------------------------------------------------------- 编码

def test_decode_utf8_file():
    """UTF-8 文件：解出来的文本与原文一致。"""
    raw = _message(_OBR, _OBX).encode("utf-8")
    payload = decode_payload(raw)
    assert payload.encoding == "utf-8"
    assert payload.fallback is False
    assert payload.text == _message(_OBR, _OBX)
    assert payload.byte_length == len(raw)


def test_decode_gbk_file():
    """GBK 文件：回退解得开，编码口径记为 gbk。"""
    raw = _message(_OBR, _OBX).encode("gbk")
    payload = decode_payload(raw)
    assert payload.encoding == "gbk"
    assert payload.fallback is True
    assert "子宫体" not in payload.text  # 这条报文里没有部位描述
    assert "ACC-EXAMPLE-0001" in payload.text


def test_decode_mixed_utf8_and_gbk_file():
    """同一文件里 UTF-8 与 GBK 混编：逐行解，编码口径记为 mixed。"""
    utf8_line = "ZBR|B3|子宫体|1\r\n"
    gbk_line = "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n"
    raw = _HEAD.encode("utf-8") + utf8_line.encode("utf-8") + gbk_line.encode("gbk")
    payload = decode_payload(raw)
    assert payload.encoding == "mixed"
    assert payload.fallback is True
    assert "子宫体" in payload.text  # UTF-8 那行
    assert "阴性" in payload.text  # GBK 那行


def test_decode_mixed_file_keeps_line_numbers():
    """混编文件逐行记下用了哪种编码，行号对得上。"""
    raw = (
        _HEAD.encode("utf-8")
        + "ZBR|B3|子宫体|1\r\n".encode("utf-8")
        + "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n".encode("gbk")
    )
    payload = decode_payload(raw)
    encodings = dict(payload.line_encodings)
    assert encodings[1] == "utf-8"
    assert encodings[2] == "utf-8"
    assert encodings[3] == "gbk"


def test_decode_rejects_bytes_that_are_neither_utf8_nor_gbk():
    """两种编码都解不开 → 结构化错误，带行号。"""
    raw = _HEAD.encode("utf-8") + b"OBX|1|\xff\xfe\xfa bad\r\n"
    with pytest.raises(Er7ParseError) as excinfo:
        decode_payload(raw)
    assert excinfo.value.code == "bad_encoding"
    assert excinfo.value.line_no == 2


def test_parse_message_records_decode_method():
    """报文带着解码方式一路传下去，审计与摘要里查得到。"""
    raw = _message(_OBR, _OBX).encode("gbk")
    payload = decode_payload(raw)
    message = parse_message(payload.text, source="t.hl7", encoding=payload.encoding)
    assert message.encoding == "gbk"


# ---------------------------------------------------------------- 脏行

def test_zbr_dirty_row_with_leading_separator_is_rejected():
    """ZBR 前面多了一个分隔符：部位序号整体右移 → 结构化错误（字段 + 行号）。"""
    message = parse_message(
        _message(
            _OBR,
            "ZBR||B3|子宫体|1\r\n",
            _OBX,
        )
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert excinfo.value.code == "dirty_extra_separator"
    assert excinfo.value.field == "ZBR-3"
    assert excinfo.value.line_no == 3
    assert excinfo.value.segment == "ZBR"


def test_zbr_dirty_row_with_extra_fourth_field_is_rejected():
    """ZBR 多出第 4 个字段位（本段只有三个字段位）→ 结构化错误。"""
    message = parse_message(
        _message(
            _OBR,
            "ZBR|B3|子宫体|1|2\r\n",
            _OBX,
        )
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert excinfo.value.code == "dirty_extra_separator"
    assert excinfo.value.field == "ZBR-4"
    assert excinfo.value.line_no == 3


def test_zbr_trailing_empty_separator_is_tolerated():
    """行尾多一个空分隔符是无害的，不该拒收。"""
    message = parse_message(
        _message(
            _OBR,
            "ZBR|B3|子宫体|1|\r\n",
            _OBX,
        )
    )
    entries = parse_oru(message)
    assert entries[0].region_seq == 1
    assert entries[0].block_no == "B3"


def test_zbr_non_integer_without_shift_stays_plain_error():
    """不是错位、就是这一位本身不合法 → 仍报 invalid_region_seq（口径不变）。"""
    message = parse_message(
        _message(
            _OBR,
            "ZBR|B1|子宫体|第1块\r\n",
            _OBX,
        )
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert excinfo.value.code == "invalid_region_seq"
    assert excinfo.value.field == "ZBR-3"


def test_obx_dirty_row_with_shifted_status_is_rejected():
    """OBX 前面多了一个分隔符：结果状态右移 → 不能默默按终报 F 处理。"""
    message = parse_message(
        _message(
            _OBR,
            "OBX||1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n",
        )
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert excinfo.value.code == "dirty_extra_separator"
    assert excinfo.value.field == "OBX-11"


def test_obx_illegal_status_without_shift_stays_plain_error():
    """状态取值非法、又不是错位 → 仍报 illegal_result_status。"""
    message = parse_message(
        _message(
            _OBR,
            "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||Z|||20260101130000\r\n",
        )
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert excinfo.value.code == "illegal_result_status"
    assert excinfo.value.field == "OBX-11"


def test_dirty_error_detail_carries_field_and_line():
    """结构化错误同时带字段名与行号，运维能直接定位。"""
    message = parse_message(_message(_OBR, "ZBR||B3|子宫体|1\r\n", _OBX))
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    detail = excinfo.value.to_dict()
    assert detail["field"] == "ZBR-3"
    assert detail["line_no"] == 3
    assert detail["reason"]
    assert "ZBR-3" in str(excinfo.value)


# ---------------------------------------------------------------- 端到端

def _baseline_payload(*accessions: str) -> str:
    """造一份基线快照（reports 结构），每个病理号一块部位。"""
    reports = []
    for accession in accessions:
        reports.append(
            {
                "accession_no": accession,
                "base_version": "base-v1",
                "blocks": [
                    {
                        "region_seq": 1,
                        "region_desc": "子宫体",
                        "block_no": "B3",
                        "order_no": "ORD-EXAMPLE-0001",
                        "ordered_items": ["IHC-CD3"],
                    }
                ],
            }
        )
    return json.dumps({"reports": reports}, ensure_ascii=False)


def test_dirty_inputs_do_not_leak_into_draft(tmp_path):
    """脏数据不得混进草稿：结果要么进草稿，要么进待人工处理清单。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "baseline.json").write_text(
        _baseline_payload("ACC-EXAMPLE-0001", "ACC-EXAMPLE-0002"), encoding="utf-8"
    )
    # 一份正常报文 + 一份脏行报文（多一个分隔符）+ 一份 GBK 报文
    (inbox / "ok.hl7").write_bytes(_message(_OBR, "ZBR|B3|子宫体|1\r\n", _OBX).encode("utf-8"))
    (inbox / "dirty.hl7").write_bytes(
        _message(_OBR, "ZBR||B3|子宫体|1\r\n", _OBX).encode("utf-8")
    )
    (inbox / "gbk.hl7").write_bytes(
        _message(
            _OBR.replace("ACC-EXAMPLE-0001", "ACC-EXAMPLE-0002"),
            "ZBR|B3|子宫体|1\r\n",
            _OBX,
        ).encode("gbk")
    )

    outcome = run_replay(inbox, output_dir=tmp_path / "out")

    assert outcome.malformed == 1
    assert len(outcome.rejected) == 1
    assert "dirty" in outcome.rejected[0][0]
    # 脏行不进草稿；正常报文与 GBK 报文各自成稿
    assert outcome.draft_entry_count == 2
    assert outcome.decode_counts.get("utf-8") == 2
    assert outcome.decode_counts.get("gbk") == 1


def test_replay_records_decode_method_in_audit(tmp_path):
    """审计流水里能查到每份报文是用哪种编码读进来的。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "baseline.json").write_text(
        _baseline_payload("ACC-EXAMPLE-0001", "ACC-EXAMPLE-0002"), encoding="utf-8"
    )
    (inbox / "utf8.hl7").write_bytes(
        _message(_OBR, "ZBR|B3|子宫体|1\r\n", _OBX).encode("utf-8")
    )
    (inbox / "gbk.hl7").write_bytes(
        _message(
            _OBR.replace("ACC-EXAMPLE-0001", "ACC-EXAMPLE-0002"),
            "ZBR|B3|子宫体|1\r\n",
            _OBX,
        ).encode("gbk")
    )

    with Database(tmp_path / "state.db") as database:
        outcome = run_replay(inbox, output_dir=tmp_path / "out", database=database)
        rows = database.query_all(
            "SELECT detail_json FROM audit_log WHERE event = ? ORDER BY id",
            (EVENT_ENCODING_DETECTED,),
        )

    assert len(rows) == 2
    details = [json.loads(row["detail_json"]) for row in rows]
    encodings = {detail["file"]: detail["encoding"] for detail in details}
    assert encodings["utf8.hl7"] == "utf-8"
    assert encodings["gbk.hl7"] == "gbk"
    assert any(detail["fallback"] for detail in details)
    assert outcome.decode_counts == {"utf-8": 1, "gbk": 1}


def test_dirty_block_no_falls_into_pending_not_draft():
    """脏蜡块号（纯符号）且部位描述也对不上 → 挂着等人，不进草稿。

    纯符号蜡块号按归一化层口径等于「没填」，于是走部位描述层；只有部位
    描述也定不到块时才挂起 —— 这条用的就是部位描述与基线不一致的形态。
    """
    entry = _result_entry(block_no="----", region_seq=1, region_desc="乳腺")
    baseline = ReportBaseline(
        accession_no="ACC-EXAMPLE-0001",
        blocks=[
            Block(
                region_seq=1,
                region_desc="子宫体",
                block_no="B3",
                order_no="ORD-EXAMPLE-0001",
                ordered_items=["IHC-CD3"],
            )
        ],
    )
    outcome = align_entry(entry, baseline)
    assert outcome.matched is False
    assert outcome.state == "missing_block_no"
    assert "block_no" in outcome.missing_fields


def test_dirty_block_no_with_unique_region_desc_aligns_at_l2():
    """脏蜡块号（纯符号）+ 部位描述唯一命中 → L2 成立（id 6 四级匹配起启用）。

    纯符号蜡块号等于没填，部位描述与部位序号都对得上时归属是确定的，
    置信「中」；依据里会记下命中方式，医师复核时看得见。
    """
    entry = _result_entry(block_no="----", region_seq=1)
    baseline = ReportBaseline(
        accession_no="ACC-EXAMPLE-0001",
        blocks=[
            Block(
                region_seq=1,
                region_desc="子宫体",
                block_no="B3",
                order_no="ORD-EXAMPLE-0001",
                ordered_items=["IHC-CD3"],
            )
        ],
    )
    outcome = align_entry(entry, baseline)
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.block.block_no == "B3"


def test_missing_block_no_goes_pending_with_candidates():
    """蜡块号整个没填、部位描述也没填 → 挂起，并把候选部位给出来供人工补齐。"""
    entry = _result_entry(block_no=None, region_seq=1, region_desc=None)
    baseline = ReportBaseline(
        accession_no="ACC-EXAMPLE-0001",
        blocks=[
            Block(
                region_seq=1,
                region_desc="子宫体",
                block_no="B3",
                order_no="ORD-EXAMPLE-0001",
                ordered_items=["IHC-CD3"],
            )
        ],
    )
    outcome = align_entry(entry, baseline)
    assert outcome.matched is False
    assert outcome.state == "missing_block_no"
    assert outcome.missing_fields == ("block_no", "region_desc")
    assert len(outcome.candidates) == 1


def _result_entry(**kwargs):
    from patho_supplement.models import ResultEntry, ResultStatus

    defaults = dict(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD3",
        item_name="CD3",
        value="阴性",
        status=ResultStatus.FINAL,
        observed_at="20260101130000",
        order_no="ORD-EXAMPLE-0001",
        region_seq=1,
        block_no="B3",
        region_desc="子宫体",
        source_system="IMMUNO-LAB",
        source_file="t.hl7",
    )
    defaults.update(kwargs)
    return ResultEntry(**defaults)
