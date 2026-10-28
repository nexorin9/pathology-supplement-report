"""幂等与失败路径专项测试（任务 id 18）。

覆盖四条硬规则：

- **幂等键**：病理号 + 蜡块号 + 项目码 + 观察时间 + 结果状态；同键重复投递只落一条；
- **更正取代**（``OBX-11=C``）：取代旧值、写审计（旧值 → 新值）、不追加成第二条；
- **回报时间倒挂**：更正比已有结果还早时不生效，较新的原值留着，更正扣下来等人；
- **无法获得 / 空值 / 畸形报文**：一律**不写进报告**，进待人工处理清单或拒收计数。

另含边界形态：同一观察时间不同结果状态、超长自由文本值、同一 ORU 重复投递 100 次。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patho_supplement.db import Database
from patho_supplement.ingest.er7 import Er7ParseError, parse_message
from patho_supplement.ingest.idempotency import (
    IdempotencyLedger,
    apply_corrections,
    build_idempotency_key,
    idempotency_key_parts,
    resolve_corrections,
)
from patho_supplement.models import ResultEntry, ResultStatus
from patho_supplement.pending.reasons import (
    PendingReason,
    missing_fields_for_entry,
    reason_for_entry,
)
from patho_supplement.replay import run_replay

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"
EDGE_INBOX = Path(__file__).parent / "fixtures" / "edge"


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


# ---------------------------------------------------------------- 幂等键


def test_idempotency_key_exposes_five_parts():
    parts = idempotency_key_parts(_entry())
    assert parts == ("ACC-EXAMPLE-0001", "B1", "IHC-CD3", "20260101130000", "F")
    assert build_idempotency_key(_entry()).split("\x1f") == list(parts)


def test_same_item_different_status_is_not_duplicate():
    """结果状态进键 —— 初步 / 终报 / 更正是三条不同的回报，不是重复投递。"""
    ledger = IdempotencyLedger()
    assert ledger.accept(_entry(status=ResultStatus.PRELIMINARY)) is True
    assert ledger.accept(_entry(status=ResultStatus.FINAL)) is True
    assert ledger.accept(_entry(status=ResultStatus.CORRECTION)) is True
    assert ledger.counters.accepted == 3
    assert ledger.counters.duplicate == 0


def test_duplicate_delivery_100_times_keeps_one_entry():
    """同一 ORU 重复投递 100 次，条目数不变。"""
    ledger = IdempotencyLedger()
    accepted = sum(1 for _ in range(100) if ledger.accept(_entry()))
    assert accepted == 1
    assert ledger.counters.accepted == 1
    assert ledger.counters.duplicate == 99


def test_replay_with_100_copies_keeps_draft_entry_count(tmp_path):
    """端到端：同一份 ORU 投 100 次，草稿条目数与只投一次时相同。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = FIXTURE_INBOX / "acc-example-0001-01-cd3.hl7"
    for index in range(100):
        (inbox / f"copy-{index:03d}.hl7").write_bytes(source.read_bytes())
    (inbox / "baseline.json").write_bytes(
        (FIXTURE_INBOX / "baseline.json").read_bytes()
    )

    outcome = run_replay(inbox, output_dir=tmp_path / "out")

    assert outcome.parsed_results == 100
    assert outcome.accepted == 1
    assert outcome.duplicates == 99
    assert outcome.draft_entry_count == 1
    assert len(outcome.pending) == 0


# ---------------------------------------------------------------- 更正取代


def test_correction_supersedes_older_value_and_records_old_to_new():
    original = _entry(value="阴性", observed_at="20260101130000")
    correction = _entry(
        value="弱阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101140000",
    )

    outcome = resolve_corrections([original, correction])

    assert [entry.value for entry in outcome.kept] == ["弱阳性"]
    assert outcome.superseded == 1
    assert len(outcome.applied) == 1
    event = outcome.applied[0]
    assert event.old_value == "阴性"
    assert event.new_value == "弱阳性"
    assert event.old_status == "F"
    assert event.new_status == "C"


def test_correction_is_not_appended_as_a_second_entry():
    """更正取代旧值 —— 不是「旧值留着 + 新值追加一条」。"""
    original = _entry(value="阴性")
    correction = _entry(value="弱阳性", status=ResultStatus.CORRECTION)

    kept, superseded = apply_corrections([original, correction])

    assert len(kept) == 1
    assert superseded == 1


def test_correction_writes_audit_row_with_old_and_new_value(tmp_path):
    with Database(tmp_path / "state.db") as db:
        resolve_corrections(
            [
                _entry(value="阴性", observed_at="20260101130000"),
                _entry(
                    value="弱阳性",
                    status=ResultStatus.CORRECTION,
                    observed_at="20260101140000",
                ),
            ],
            database=db,
        )

        rows = db.query_all(
            "SELECT event, accession_no, detail_json FROM audit_log ORDER BY id"
        )
        assert len(rows) == 1
        assert rows[0]["event"] == "correction_applied"
        assert rows[0]["accession_no"] == "ACC-EXAMPLE-0001"
        detail = json.loads(rows[0]["detail_json"])
        assert detail["old_value"] == "阴性"
        assert detail["new_value"] == "弱阳性"


def test_correction_does_not_touch_other_blocks():
    original = _entry(block_no="B1", value="阳性")
    other_block = _entry(block_no="B2", value="局灶阳性")
    correction = _entry(
        block_no="B1",
        value="弱阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101140000",
    )

    outcome = resolve_corrections([original, other_block, correction])

    assert outcome.superseded == 1
    assert {entry.value for entry in outcome.kept} == {"弱阳性", "局灶阳性"}


def test_same_observed_time_correction_wins_over_final():
    """同一观察时间、不同结果状态：更正压过终报。"""
    final = _entry(value="阴性", status=ResultStatus.FINAL, observed_at="20260101130000")
    correction = _entry(
        value="弱阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101130000",
    )

    outcome = resolve_corrections([final, correction])

    assert [entry.value for entry in outcome.kept] == ["弱阳性"]
    assert outcome.superseded == 1
    assert len(outcome.applied) == 1


def test_same_observed_time_final_wins_over_preliminary():
    """同一观察时间：终报压过初步（终报才是能签发的结论）。"""
    preliminary = _entry(
        value="疑似阳性", status=ResultStatus.PRELIMINARY, observed_at="20260101130000"
    )
    final = _entry(value="阴性", status=ResultStatus.FINAL, observed_at="20260101130000")

    outcome = resolve_corrections([preliminary, final])

    assert [entry.value for entry in outcome.kept] == ["阴性"]
    assert outcome.superseded == 1


def test_later_final_supersedes_earlier_preliminary():
    preliminary = _entry(
        value="疑似阳性", status=ResultStatus.PRELIMINARY, observed_at="20260101120000"
    )
    final = _entry(value="阴性", status=ResultStatus.FINAL, observed_at="20260101130000")

    outcome = resolve_corrections([preliminary, final])

    assert [entry.value for entry in outcome.kept] == ["阴性"]
    assert outcome.superseded == 1


# ---------------------------------------------------------------- 回报时间倒挂


def test_inverted_correction_does_not_replace_newer_value():
    """更正的回报时间比已有结果还早 —— 不生效，较新的原值留着。"""
    newer = _entry(value="阴性", observed_at="20260101110000")
    inverted = _entry(
        value="阳性",
        status=ResultStatus.CORRECTION,
        observed_at="20260101100000",
    )

    outcome = resolve_corrections([newer, inverted])

    assert [entry.value for entry in outcome.kept] == ["阴性"]
    assert len(outcome.inverted) == 1
    assert outcome.inverted[0].entry.value == "阳性"
    assert outcome.inverted[0].kept.value == "阴性"
    assert outcome.superseded == 0, "倒挂的更正是被拒绝，不是被取代"
    assert outcome.applied == []


def test_inverted_correction_writes_audit_row(tmp_path):
    with Database(tmp_path / "state.db") as db:
        resolve_corrections(
            [
                _entry(value="阴性", observed_at="20260101110000"),
                _entry(
                    value="阳性",
                    status=ResultStatus.CORRECTION,
                    observed_at="20260101100000",
                ),
            ],
            database=db,
        )

        rows = db.query_all("SELECT event, detail_json FROM audit_log ORDER BY id")
        assert len(rows) == 1
        assert rows[0]["event"] == "correction_inverted"
        detail = json.loads(rows[0]["detail_json"])
        assert detail["correction_value"] == "阳性"
        assert detail["kept_value"] == "阴性"


def test_inverted_correction_goes_to_pending_list(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    assert outcome.inverted_corrections == 1
    inverted = [record for record in outcome.pending if record.item_code == "IHC-CD20"]
    assert len(inverted) == 1
    assert inverted[0].reason is PendingReason.TIME_INVERSION
    assert inverted[0].missing_fields == ("observed_at",)


# ---------------------------------------------------------------- 无法获得 / 空值


def test_not_obtained_result_is_not_mergeable():
    entry = _entry(value="标本溶血，无法获得", status=ResultStatus.NOT_OBTAINED)
    assert entry.mergeable is False
    assert reason_for_entry(entry) is PendingReason.RESULT_NOT_OBTAINED
    assert missing_fields_for_entry(entry) == ("result_value",)


def test_empty_value_result_is_not_mergeable():
    entry = _entry(value="   ")
    assert entry.mergeable is False
    assert reason_for_entry(entry) is PendingReason.EMPTY_VALUE


def test_valued_result_is_mergeable():
    assert _entry().mergeable is True
    assert reason_for_entry(_entry()) is None


def test_empty_value_does_not_supersede_valued_result():
    """空值永远不盖掉一个有值的真结果。"""
    valued = _entry(value="阴性", observed_at="20260101120000")
    empty = _entry(value=None, observed_at="20260101130000")

    outcome = resolve_corrections([valued, empty])

    assert [entry.value for entry in outcome.kept] == ["阴性"]
    assert [entry.value for entry in outcome.withheld] == [None]
    assert outcome.superseded == 0


def test_not_obtained_result_goes_to_pending_not_draft(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    not_obtained = [
        record for record in outcome.pending if record.item_code == "IHC-CD3"
    ]
    assert len(not_obtained) == 1
    assert not_obtained[0].reason is PendingReason.RESULT_NOT_OBTAINED

    body = outcome.drafts[0].body
    assert "IHC-CD3" not in body
    assert "标本溶血" not in body


def test_empty_value_goes_to_pending_not_draft(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    empty = [record for record in outcome.pending if record.item_code == "IHC-KI67"]
    assert len(empty) == 1
    assert empty[0].reason is PendingReason.EMPTY_VALUE
    assert "IHC-KI67" not in outcome.drafts[0].body


# ---------------------------------------------------------------- 超长字段


def test_long_value_is_preserved_intact_in_draft(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    body = outcome.drafts[0].body
    assert "IHC-ER" in body
    assert body.count("腺体结构紊乱伴炎性细胞浸润") == 3
    assert len(body) > 500


def test_long_value_does_not_change_idempotency_key():
    long_value = "长" * 400
    short = _entry(value=long_value)
    other = _entry(value="阴性")

    assert build_idempotency_key(short) == build_idempotency_key(other)
    assert len(build_idempotency_key(short)) < 120


# ---------------------------------------------------------------- 畸形报文拒收


def test_malformed_messages_are_rejected_and_counted(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    assert outcome.malformed == 3
    assert len(outcome.rejected) == 3
    assert outcome.accepted == 7, "畸形报文一条结果都不该产生"


def test_malformed_messages_never_enter_draft(tmp_path):
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    body = outcome.drafts[0].body
    assert body.count("IHC-CD3") == 0


def test_malformed_messages_write_audit_rows(tmp_path):
    with Database(tmp_path / "state.db") as db:
        run_replay(EDGE_INBOX, output_dir=tmp_path / "out", database=db)

        rows = db.query_all(
            "SELECT detail_json FROM audit_log WHERE event = 'malformed' ORDER BY id"
        )
        assert len(rows) == 3
        codes = {json.loads(row["detail_json"])["code"] for row in rows}
        assert codes == {"missing_message_type", "missing_version", "invalid_segment_name"}
        for row in rows:
            detail = json.loads(row["detail_json"])
            assert detail["file"].startswith("malformed-")
            assert detail["reason"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (
            "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500|||MSG1|P|2.5.1",
            "missing_message_type",
        ),
        (
            "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P",
            "missing_version",
        ),
    ],
)
def test_parse_error_carries_machine_readable_code(text, code):
    with pytest.raises(Er7ParseError) as excinfo:
        parse_message(text, source="t.hl7")
    assert excinfo.value.code == code


# ---------------------------------------------------------------- 守恒


def test_every_result_has_exactly_one_destination(tmp_path):
    """草稿 + 待处理 + 被取代 = 入账条数。一条结果都不许凭空消失。"""
    outcome = run_replay(EDGE_INBOX, output_dir=tmp_path / "out")

    assert outcome.draft_entry_count + len(outcome.pending) + outcome.superseded == (
        outcome.accepted
    )
    assert outcome.draft_entry_count == 3
    assert len(outcome.pending) == 3
    assert outcome.superseded == 1


def test_existing_inbox_still_balances(tmp_path):
    """既有脱敏样例的口径不受本轮改动影响。"""
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")

    assert outcome.accepted == 7
    assert outcome.superseded == 1
    assert outcome.draft_entry_count == 5
    assert len(outcome.pending) == 1
    assert outcome.malformed == 0
