"""申请侧报文测试：ORM^O01 解析 + 订单控制动作枚举 + 去重与拒收审计。

覆盖任务 id 4 的验收点：

- ``ORC-1`` 五种控制动作各落到对应检查项状态
- 缺 ``OBR-3`` / ``OBR-4`` 的条目拒收并计 ``malformed``
- 非法 ``ORC-1`` 取值拒收该条并写审计流水
- 同一申请重复投递只保留一条；同一检查项的「新增 → 取消」不被当成重复投递吞掉
- 批处理重放把申请侧报文解析出来（不再只识别类型）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patho_supplement.db import Database
from patho_supplement.ingest.er7 import Er7ParseError, parse_message
from patho_supplement.ingest.orm_parser import (
    ACTION_TO_ITEM_STATE,
    OrderIngestor,
    build_order_event_key,
    item_state_for_action,
    parse_orm,
)
from patho_supplement.models import CheckItemState, OrderAction
from patho_supplement.replay import run_replay

FIXTURE_INBOX = Path(__file__).parent / "fixtures" / "inbox"


def _orm_message(
    action: str = "NW",
    *,
    accession_no: str = "ACC-EXAMPLE-0001",
    order_no: str = "ORD-EXAMPLE-0001",
    item_code: str = "IHC-CD3",
    item_name: str = "CD3",
    order_time: str = "20260101101500",
    zbr: str | None = None,
) -> str:
    """造一条 ORM^O01 报文。

    字段位与结果侧一致：``OBR-2`` 病理号 / ``OBR-3`` 加做申请号 /
    ``OBR-4`` 项目码 / ``OBR-7`` 申请时间。
    """
    obr = f"OBR|1|{accession_no}|{order_no}|{item_code}^{item_name}^L|||{order_time}"
    if zbr is not None:
        obr += "\r\n" + zbr
    return (
        "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|20260101101500"
        "||ORM^O01|ORDMSG-EXAMPLE-0001|P|2.5.1\r\n"
        f"ORC|{action}|ORD-EXAMPLE-0001\r\n"
        f"{obr}\r\n"
    )


def _parse(text: str):
    return parse_orm(parse_message(text, source="t.orm.hl7"), source_file="t.orm.hl7")


# ---------------------------------------------------------------- 解析


def test_parse_orm_extracts_order_request():
    result = _parse(_orm_message("NW"))
    assert len(result.orders) == 1
    assert not result.rejected

    order = result.orders[0]
    assert order.accession_no == "ACC-EXAMPLE-0001"
    assert order.order_no == "ORD-EXAMPLE-0001"
    assert order.item_code == "IHC-CD3"
    assert order.item_name == "CD3"
    assert order.action is OrderAction.NEW
    assert order.order_time == "20260101101500"
    assert order.source_system == "PATHOLOGY-REQ"


def test_parse_orm_carries_zbr_region_fields():
    result = _parse(_orm_message("NW", zbr="ZBR|B2|宫颈|2"))
    order = result.orders[0]
    assert order.block_no == "B2"
    assert order.region_desc == "宫颈"
    assert order.region_seq == 2


def test_parse_orm_rejects_non_orm_message():
    text = (
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORU^R01|MSG1|P|2.5.1\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3|||20260101120000\r\n"
    )
    with pytest.raises(Er7ParseError) as excinfo:
        parse_orm(parse_message(text))
    assert "ORM" in str(excinfo.value)


def test_parse_orm_rejects_message_without_order_segments():
    text = (
        "MSH|^~\\&|APP|BENCH|RECV|HOSP|20260101130500||ORM^O01|MSG1|P|2.5.1\r\n"
        "PID|1||EXAMPLE-PATIENT-0001\r\n"
    )
    result = _parse(text)
    assert not result.orders
    assert result.malformed == 1
    assert result.rejected[0].reason == "empty_message"


# ---------------------------------------------------------------- 控制动作 → 状态


def test_action_new_maps_to_awaiting():
    result = _parse(_orm_message("NW"))
    assert result.orders[0].action is OrderAction.NEW
    assert item_state_for_action(result.orders[0].action) is CheckItemState.AWAITING


def test_action_cancel_maps_to_cancelled():
    result = _parse(_orm_message("CA"))
    assert result.orders[0].action is OrderAction.CANCEL
    assert item_state_for_action(result.orders[0].action) is CheckItemState.CANCELLED


def test_action_hold_maps_to_held():
    result = _parse(_orm_message("HD"))
    assert result.orders[0].action is OrderAction.HOLD
    assert item_state_for_action(result.orders[0].action) is CheckItemState.HELD


def test_action_terminate_maps_to_terminated():
    result = _parse(_orm_message("DC"))
    assert result.orders[0].action is OrderAction.TERMINATE
    assert item_state_for_action(result.orders[0].action) is CheckItemState.TERMINATED


def test_action_result_follows_keeps_awaiting():
    """``RE``（结果随后）：订单已确认、结果将至，仍在「待回报」态。"""
    result = _parse(_orm_message("RE"))
    assert result.orders[0].action is OrderAction.RESULT_FOLLOWS
    assert item_state_for_action(result.orders[0].action) is CheckItemState.AWAITING
    assert OrderAction.RESULT_FOLLOWS.label == "结果随后"


def test_mapping_covers_exactly_the_five_actions():
    assert set(ACTION_TO_ITEM_STATE) == set(OrderAction)
    assert len(ACTION_TO_ITEM_STATE) == 5


# ---------------------------------------------------------------- 拒收


def test_illegal_control_action_is_rejected():
    result = _parse(_orm_message("XX"))
    assert not result.orders
    assert result.malformed == 1
    rejected = result.rejected[0]
    assert rejected.reason == "illegal_control_action"
    assert "ORC-1" in rejected.detail
    assert rejected.segment == "ORC"
    assert rejected.line_no == 2
    # 拒收记录要指明是哪个文件带来的
    assert rejected.source == "t.orm.hl7"


def test_missing_control_action_is_rejected():
    result = _parse(_orm_message(""))
    assert not result.orders
    assert result.rejected[0].reason == "missing_control_action"


def test_missing_order_no_is_rejected_and_counted_malformed():
    result = _parse(_orm_message("NW", order_no=""))
    assert not result.orders
    assert result.malformed == 1
    rejected = result.rejected[0]
    assert rejected.reason == "missing_order_no"
    assert "OBR-3" in rejected.detail
    assert rejected.accession_no == "ACC-EXAMPLE-0001"


def test_missing_item_code_is_rejected_and_counted_malformed():
    result = _parse(_orm_message("NW", item_code=""))
    assert not result.orders
    assert result.malformed == 1
    rejected = result.rejected[0]
    assert rejected.reason == "missing_item_code"
    assert "OBR-4" in rejected.detail


def test_missing_accession_no_is_rejected():
    result = _parse(_orm_message("NW", accession_no=""))
    assert not result.orders
    assert result.rejected[0].reason == "missing_accession_no"


def test_bad_entry_does_not_block_good_entries():
    """一条写错只拒这一条，其余照常 —— 整份报废会让一批加做全停摆。"""
    text = (
        "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|20260101101500"
        "||ORM^O01|ORDMSG-EXAMPLE-0001|P|2.5.1\r\n"
        "ORC|XX|ORD-EXAMPLE-0001\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500\r\n"
        "ORC|NW|ORD-EXAMPLE-0002\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0002|IHC-KI67^KI-67^L|||20260101101600\r\n"
    )
    result = _parse(text)
    assert len(result.orders) == 1
    assert result.orders[0].item_code == "IHC-KI67"
    assert result.malformed == 1
    assert result.rejected[0].reason == "illegal_control_action"


def test_invalid_zbr_rejects_only_that_entry():
    text = (
        "MSH|^~\\&|PATHOLOGY-REQ|PATHOLOGY-DEPT|IMMUNO-LAB|HOSP-PATH|20260101101500"
        "||ORM^O01|ORDMSG-EXAMPLE-0001|P|2.5.1\r\n"
        "ORC|NW|ORD-EXAMPLE-0001\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0001|IHC-CD3^CD3^L|||20260101101500\r\n"
        "ZBR|B1|子宫体|第1块\r\n"
        "ORC|NW|ORD-EXAMPLE-0002\r\n"
        "OBR|1|ACC-EXAMPLE-0001|ORD-EXAMPLE-0002|IHC-KI67^KI-67^L|||20260101101600\r\n"
    )
    result = _parse(text)
    assert len(result.orders) == 1
    assert result.orders[0].item_code == "IHC-KI67"
    assert result.rejected[0].reason == "invalid_zbr"


# ---------------------------------------------------------------- 去重


def test_order_event_key_contains_control_action():
    order = _parse(_orm_message("NW")).orders[0]
    key = build_order_event_key(order)
    assert key.split("\x1f") == [
        "ACC-EXAMPLE-0001",
        "ORD-EXAMPLE-0001",
        "IHC-CD3",
        "NW",
    ]


def test_duplicate_order_delivery_is_accepted_once():
    ingestor = OrderIngestor()
    message = parse_message(_orm_message("NW"))

    first = ingestor.ingest(message)
    second = ingestor.ingest(message)

    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicate == 1
    assert ingestor.counters.accepted == 1
    assert ingestor.counters.duplicate == 1


def test_cancel_after_new_is_not_swallowed_as_duplicate():
    """同一检查项先新增后取消是**两件事** —— 取消不能被当重复投递吞掉。"""
    ingestor = OrderIngestor()
    new_order = ingestor.ingest(parse_message(_orm_message("NW")))
    cancel_order = ingestor.ingest(parse_message(_orm_message("CA")))

    assert new_order.accepted == 1
    assert cancel_order.accepted == 1
    assert cancel_order.duplicate == 0
    assert cancel_order.orders[0].action is OrderAction.CANCEL


# ---------------------------------------------------------------- 审计


def test_rejected_order_is_written_to_audit_log(tmp_path):
    with Database(tmp_path / "state.db") as database:
        ingestor = OrderIngestor(database)
        ingestor.ingest(parse_message(_orm_message("XX")))

        rows = database.query_all(
            "SELECT category, event, accession_no, detail_json FROM audit_log"
        )
        assert len(rows) == 1
        assert rows[0]["category"] == "ingest"
        assert rows[0]["event"] == "order_rejected"
        assert "illegal_control_action" in rows[0]["detail_json"]


def test_audit_is_skipped_without_database():
    """批处理重放默认不落库，拒收原因仍要出现在本轮结果里。"""
    ingestor = OrderIngestor()
    outcome = ingestor.ingest(parse_message(_orm_message("XX")))
    assert outcome.malformed == 1
    assert outcome.rejected[0].reason == "illegal_control_action"


def test_accepted_order_is_not_audited_as_rejected(tmp_path):
    with Database(tmp_path / "state.db") as database:
        ingestor = OrderIngestor(database)
        outcome = ingestor.ingest(parse_message(_orm_message("NW")))
        assert outcome.accepted == 1
        assert database.query_all("SELECT * FROM audit_log") == []


# ---------------------------------------------------------------- 批处理重放接入


def test_replay_parses_order_side_message(tmp_path):
    outcome = run_replay(FIXTURE_INBOX, output_dir=tmp_path / "out")

    assert outcome.order_files == 1
    assert outcome.parsed_orders == 1
    assert outcome.order_malformed == 0
    assert not outcome.order_rejected

    order = outcome.orders[0]
    assert order.accession_no == "ACC-EXAMPLE-0001"
    assert order.order_no == "ORD-EXAMPLE-0001"
    assert order.item_code == "IHC-CD3"
    assert order.action is OrderAction.NEW


def test_replay_counts_malformed_order_entries(tmp_path):
    """申请侧写错的那一条要计出来，不能悄悄吞掉。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "baseline.json").write_bytes((FIXTURE_INBOX / "baseline.json").read_bytes())
    (inbox / "bad.orm.hl7").write_text(_orm_message("XX"), encoding="utf-8")

    outcome = run_replay(inbox, output_dir=tmp_path / "out")
    assert outcome.order_files == 1
    assert outcome.parsed_orders == 0
    assert outcome.order_malformed == 1
    assert len(outcome.order_rejected) == 1
    assert "ORC-1" in outcome.order_rejected[0][1]
