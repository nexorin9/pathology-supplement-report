"""入站主干测试：ER7 切分 → ZBR → ORU^R01 解析 → 幂等键 → 重放出草稿。

覆盖任务 id 2 的验收点：

- ER7 段解析与字段位取值；不可解析的段报错并带行号，不静默跳过
- ZBR 扩展段三个字段均可空；部位序号非整数报错
- ORU^R01 解析出结果条目，字段位正确
- 幂等键去重：同一 ORU 重复投递只落一条
- 批处理重放产出草稿与待处理清单，条目数与回报结果条数对得上
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patho_supplement.align.matcher import MatchState, align_entry, align_entries
from patho_supplement.compose.baseline import load_baseline_file
from patho_supplement.ingest.dirty import decode_payload
from patho_supplement.ingest.er7 import (
    Er7ParseError,
    parse_message,
    split_unescaped,
)
from patho_supplement.ingest.idempotency import (
    IdempotencyLedger,
    apply_corrections,
    build_idempotency_key,
)
from patho_supplement.ingest.oru_parser import parse_oru
from patho_supplement.models import MatchLevel, ResultEntry, ResultStatus
from patho_supplement.replay import run_replay

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"

VALID_MESSAGE = (
    "MSH|^~\\&|IMMUNO-LAB|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
    "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101120000\r\n"
    "ZBR|B1|子宫体|1\r\n"
    "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n"
)


# ---------------------------------------------------------------- ER7 切分


def test_parse_message_reads_header_fields():
    message = parse_message(VALID_MESSAGE, source="t.hl7")
    assert message.message_type == "ORU^R01"
    assert message.message_code == "ORU"
    assert message.message_structure == "R01"
    assert message.version == "2.5.1"
    assert message.sending_application == "IMMUNO-LAB"
    assert message.field_sep == "|"


def test_parse_message_reads_segment_fields():
    message = parse_message(VALID_MESSAGE, source="t.hl7")
    obr = message.segments_named("OBR")[0]
    assert obr.field(2) == "ACC-EXAMPLE-0001"   # 病理号
    assert obr.field(3) == "ORD-EXAMPLE-0001"   # 加做申请号
    assert obr.component(4, 1) == "IHC-CD3"      # 项目码
    assert obr.field(7) == "20260101120000"      # 观察时间

    obx = message.segments_named("OBX")[0]
    assert obx.component(3, 1) == "IHC-CD3"       # 结果项目码
    assert obx.component(3, 2) == "CD3"           # 结果项目名
    assert obx.field(5) == "阴性"                # 结果值
    assert obx.field(11) == "F"                  # 结果状态
    assert obx.field(14) == "20260101130000"      # 观察时间


def test_parse_message_rejects_missing_msh9():
    text = "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500|||MSG1|P|2.5.1\r\n"
    with pytest.raises(Er7ParseError) as excinfo:
        parse_message(text, source="bad.hl7")
    assert "MSH-9" in str(excinfo.value)
    assert excinfo.value.line_no == 1


def test_parse_message_rejects_malformed_segment():
    text = VALID_MESSAGE + "1234|坏行\r\n"
    with pytest.raises(Er7ParseError) as excinfo:
        parse_message(text, source="bad.hl7")
    assert excinfo.value.line_no == 5
    assert "段名不合法" in str(excinfo.value)


def test_parse_message_requires_msh_first():
    with pytest.raises(Er7ParseError):
        parse_message("OBR|1|ACC-EXAMPLE-0001\r\n", source="bad.hl7")


def test_split_unescaped_respects_escape_char():
    assert split_unescaped("a\\|b|c", "|", "\\") == ["a\\|b", "c"]


def test_decode_payload_falls_back_to_gbk():
    payload = decode_payload("子宫体".encode("gbk"))
    assert payload.encoding == "gbk"
    assert payload.fallback is True
    assert payload.text == "子宫体"


# ---------------------------------------------------------------- ZBR 段


def test_zbr_all_fields_may_be_empty():
    message = parse_message(
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
        "ZBR|||\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||F|||20260101130000\r\n"
    )
    entries = parse_oru(message)
    assert entries[0].block_no is None
    assert entries[0].region_desc is None
    assert entries[0].region_seq is None


def test_zbr_rejects_non_integer_region_seq():
    message = parse_message(
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
        "ZBR|B1|子宫体|第1块\r\n"
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert "ZBR-3" in str(excinfo.value)


# ---------------------------------------------------------------- ORU 解析


def test_parse_oru_extracts_result_entry():
    message = parse_message(VALID_MESSAGE, source="t.hl7")
    entries = parse_oru(message, source_file="t.hl7")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.accession_no == "ACC-EXAMPLE-0001"
    assert entry.order_no == "ORD-EXAMPLE-0001"
    assert entry.item_code == "IHC-CD3"
    assert entry.item_name == "CD3"
    assert entry.value == "阴性"
    assert entry.status is ResultStatus.FINAL
    assert entry.observed_at == "20260101130000"
    assert entry.region_seq == 1
    assert entry.block_no == "B1"
    assert entry.region_desc == "子宫体"
    assert entry.source_system == "IMMUNO-LAB"
    assert entry.source_file == "t.hl7"


def test_parse_oru_rejects_missing_accession():
    message = parse_message(
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1||ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert "OBR-2" in str(excinfo.value)


def test_parse_oru_rejects_illegal_result_status():
    message = parse_message(
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
        "OBX|1|ST|IHC-CD3^CD3^L||阴性||||||Z|||20260101130000\r\n"
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_oru(message)
    assert "OBX-11" in str(excinfo.value)


# ---------------------------------------------------------------- 幂等键


def _entry(**kwargs) -> ResultEntry:
    base = dict(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-CD3",
        value="阴性",
        status=ResultStatus.FINAL,
        observed_at="20260101130000",
        region_seq=1,
        block_no="B1",
    )
    base.update(kwargs)
    return ResultEntry(**base)


def test_idempotency_key_has_five_parts():
    key = build_idempotency_key(_entry())
    assert key.split("\x1f") == [
        "ACC-EXAMPLE-0001",
        "B1",
        "IHC-CD3",
        "20260101130000",
        "F",
    ]


def test_duplicate_delivery_is_accepted_once():
    ledger = IdempotencyLedger()
    assert ledger.accept(_entry()) is True
    assert ledger.accept(_entry()) is False
    assert ledger.counters.accepted == 1
    assert ledger.counters.duplicate == 1


def test_same_item_on_different_block_is_not_duplicate():
    ledger = IdempotencyLedger()
    assert ledger.accept(_entry(block_no="B1")) is True
    assert ledger.accept(_entry(block_no="B2")) is True
    assert ledger.counters.duplicate == 0


def test_correction_supersedes_older_value():
    original = _entry(value="阴性", observed_at="20260101130000")
    correction = _entry(
        value="弱阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101140000",
    )
    kept, superseded = apply_corrections([original, correction])
    assert superseded == 1
    assert [entry.value for entry in kept] == ["弱阳性"]


def test_correction_does_not_touch_other_blocks():
    original = _entry(block_no="B1", value="阳性")
    other_block = _entry(block_no="B2", value="局灶阳性")
    correction = _entry(
        block_no="B1",
        value="弱阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101140000",
    )
    kept, superseded = apply_corrections([original, other_block, correction])
    assert superseded == 1
    assert {entry.value for entry in kept} == {"弱阳性", "局灶阳性"}


# ---------------------------------------------------------------- 对齐


def test_missing_block_no_aligns_by_region_desc():
    """缺蜡块号但部位描述唯一命中 → L2 成立（id 6 四级匹配起启用）。

    旧口径只有 L1，缺蜡块号一律挂起；现在部位描述唯一命中时可确定归属，
    置信「中」，并入草稿时依据里会记下命中方式。
    """
    baselines = load_baseline_file(FIXTURE_INBOX / "baseline.json")
    outcome = align_entry(
        _entry(block_no=None, region_desc="子宫体"), baselines["ACC-EXAMPLE-0001"]
    )
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.block.block_no == "B1"


def test_missing_block_no_without_region_desc_does_not_align():
    """缺蜡块号又缺部位描述 → 无定位依据，仍走挂起。"""
    baselines = load_baseline_file(FIXTURE_INBOX / "baseline.json")
    outcome = align_entry(
        _entry(block_no=None, region_desc=None), baselines["ACC-EXAMPLE-0001"]
    )
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.matched is False


def test_exact_block_and_item_aligns():
    baselines = load_baseline_file(FIXTURE_INBOX / "baseline.json")
    outcome = align_entry(_entry(), baselines["ACC-EXAMPLE-0001"])
    assert outcome.matched is True
    assert outcome.candidate is not None
    assert outcome.candidate.block.block_no == "B1"


def test_unknown_accession_has_no_baseline():
    matched, unmatched = align_entries(
        [_entry(accession_no="ACC-NOT-IN-BASELINE")], {}
    )
    assert not matched
    assert unmatched[0][1].state == MatchState.NO_BASELINE


# ---------------------------------------------------------------- 批处理重放


def test_replay_produces_draft_and_pending_list(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")

    assert outcome.result_files == 8
    assert outcome.order_files == 1
    assert outcome.parsed_results == 8
    assert outcome.duplicates == 1          # 同一份 ORU 重复投递
    assert outcome.accepted == 7
    assert outcome.superseded == 1          # 更正取代旧值
    assert not outcome.rejected

    # 草稿按病理号分文件
    assert [draft.accession_no for draft in outcome.drafts] == [
        "ACC-EXAMPLE-0001",
        "ACC-EXAMPLE-0002",
    ]
    assert outcome.draft_entry_count == 5
    assert len(outcome.pending) == 1

    # 每条回报结果都有去向：进草稿或进待处理清单，一条都不丢
    assert outcome.draft_entry_count + len(outcome.pending) == outcome.effective_result_count
    assert outcome.effective_result_count == outcome.accepted - outcome.superseded

    for path in outcome.draft_paths:
        assert path.is_file()
        assert path.read_text(encoding="utf-8").strip()
    assert outcome.pending_path is not None and outcome.pending_path.is_file()


def test_replay_draft_keeps_corrected_value_only(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")
    draft = outcome.drafts[0]
    body = draft.body

    # 更正后的值在草稿里，被取代的旧值不在
    assert "弱阳性" in body
    assert "阴性" not in body
    # 每条结果都标了来源系统与回报时间
    assert "来源 IMMUNO-LAB" in body
    assert "回报时间 2026-01-01" in body


def test_replay_groups_draft_by_region(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")
    body = outcome.drafts[0].body
    assert "【子宫体 · 蜡块 B1】" in body
    assert "【宫颈 · 蜡块 B2】" in body


def test_replay_duplicate_delivery_does_not_change_entry_count(tmp_path):
    """重复投递同一条 ORU 后，草稿条目数与待处理条目数都不变。"""
    baseline = load_baseline_file(FIXTURE_INBOX / "baseline.json")
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")

    # 把重复投递的那份再投一次，条目数不变
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for path in FIXTURE_INBOX.iterdir():
        if path.is_file():
            (inbox / path.name).write_bytes(path.read_bytes())
    extra = inbox / "acc-example-0001-04-ki67-duplicate-2.hl7"
    extra.write_bytes(
        (FIXTURE_INBOX / "acc-example-0001-04-ki67-duplicate.hl7").read_bytes()
    )

    again = run_replay(inbox, output_dir=tmp_path / "out2")
    assert again.parsed_results == outcome.parsed_results + 1
    assert again.duplicates == outcome.duplicates + 1
    assert again.draft_entry_count == outcome.draft_entry_count
    assert len(again.pending) == len(outcome.pending)
    assert baseline  # 基线确实被读进来了


def test_replay_pending_entry_explains_missing_block(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")
    record = outcome.pending[0]
    assert record.accession_no == "ACC-EXAMPLE-0001"
    assert record.item_code == "IHC-KI67"
    assert record.missing_fields == ("block_no",)
    assert record.reason.label == "缺蜡块号"
    # 候选部位给出了本报告的取材部位，技术人员照着去查登记
    assert len(record.candidates) == 2


def test_replay_pending_list_has_no_patient_identifiers(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")
    text = outcome.pending_path.read_text(encoding="utf-8")
    for token in ("姓名", "身份证", "联系电话", "patient_name", "id_card"):
        assert token not in text
