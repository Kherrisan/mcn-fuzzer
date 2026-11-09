"""Network-action steps and token-budget MCP responses."""

import copy

import pytest

from rsc.protocol.models import CloseStep, ConnectStep, ListenStep, SendStep, validate_step
from rsc.protocol.references import reference_errors
from rsc.workflow.paging import PageError, compact_page, decode_compact, encode_compact, records_page
from rsc.workflow.sources import token_count


def _records():
    evidence = [{"batch_id": "b", "block_id": "1"}]
    rows = []
    for index in range(3):
        rows.append(
            {
                "record_id": f"r{index}",
                "kind": "steps",
                "value": {
                    "name": f"send {index}",
                    "action": "send",
                    "message_name": "USER",
                    "channel_name": "control",
                    "sender_name": "client",
                    "receiver_name": "server",
                    "condition": 'quote "λ"' if index == 0 else "",
                },
                "evidence": evidence if index else [{"batch_id": "b", "block_id": "2"}],
            }
        )
    rows.append(
        {
            "record_id": "listen",
            "kind": "steps",
            "value": {
                "name": "listen control",
                "action": "listen",
                "channel_name": "control",
                "party_name": "server",
                "condition": None,
            },
            "evidence": evidence,
        }
    )
    return rows


def test_compact_round_trip_keeps_nested_values_and_shared_evidence():
    rows = _records()
    encoded = encode_compact(rows)
    assert len(encoded["evidence"]) == 2
    assert decode_compact(encoded) == rows
    page = compact_page(rows, 0, 24000, 3)
    assert page["format"] == "compact"
    assert page["revision"] == 3
    assert decode_compact(page) == rows
    again = records_page(rows, 0, 24000, 3)
    assert again["items"] == rows


def test_short_records_exceeding_fifty_fit_in_one_budget():
    rows = [
        {"record_id": f"r{i}", "kind": "parties", "value": {"name": f"p{i}", "description": "role"}}
        for i in range(60)
    ]
    page = records_page(rows, 0, 24000, 1)
    assert page["next_cursor"] is None
    assert len(page["items"]) == 60


def test_empty_value_round_trips_and_final_page_stays_inside_the_budget():
    empty = {"record_id": "empty", "kind": "parties", "value": {}, "evidence": []}
    summary = {"record_id": "sum", "kind": "parties", "name": "only"}
    assert decode_compact(encode_compact([empty, summary])) == [empty, summary]
    rows = [
        {
            "record_id": f"r{index}",
            "kind": "parties",
            "value": {"name": f"n{index}", "description": "x" * 40},
        }
        for index in range(4)
    ]
    one = records_page(rows[:1], 0, 24000, 7)
    assert one["tokens"] == token_count(one)
    assert one["delivered_ids"] == ["r0"]
    assert one["next_cursor"] is None
    assert one["revision"] == 7
    page = records_page(rows, 0, one["tokens"], 7)
    assert page["tokens"] <= page["max_tokens"]
    assert page["tokens"] == token_count(page)
    assert page["delivered_ids"] == [row["record_id"] for row in page["items"]]
    assert page["revision"] == 7
    assert page["next_cursor"] == (None if len(page["items"]) == len(rows) else len(page["items"]))
    with pytest.raises(PageError, match=r"requires (\d+)") as raised:
        records_page(rows[:1], 0, 1, 7)
    needed = int(raised.value.args[0].split("requires ", 1)[1].split(" ", 1)[0])
    fitted = records_page(rows[:1], 0, needed, 7)
    assert fitted["tokens"] <= needed
    assert fitted["tokens"] == token_count(fitted)
    with pytest.raises(PageError):
        records_page(rows[:1], 0, needed - 1, 7)


def test_one_oversized_record_names_the_required_budget():
    rows = [{"record_id": "big", "kind": "parties", "value": {"name": "p", "description": "x" * 200000}}]
    with pytest.raises(PageError, match="single record requires"):
        records_page(rows, 0, 1000, 1)


def test_network_steps_are_unique_per_party_channel_and_action():
    channel = {
        "name": "data",
        "endpoint_party_names": ["client", "server"],
        "initiator_party_name": "client",
        "acceptor_party_name": "server",
        "connection": "connection_oriented",
        "endpoint_sources": [],
    }
    listen = {
        "name": "server listens",
        "action": "listen",
        "channel_name": "data",
        "party_name": "server",
        "condition": "",
    }
    other = {**listen, "name": "server listens again"}
    connect = {
        "name": "client connects",
        "action": "connect",
        "channel_name": "data",
        "party_name": "client",
        "condition": "",
    }
    records = {
        "ch": {"kind": "channels", "value": channel},
        "a": {"kind": "parties", "value": {"name": "client", "description": "c"}},
        "b": {"kind": "parties", "value": {"name": "server", "description": "s"}},
        "l": {"kind": "steps", "value": listen},
        "c": {"kind": "steps", "value": connect},
    }
    assert reference_errors(records) == []
    duplicated = copy.deepcopy(records)
    duplicated["l2"] = {"kind": "steps", "value": other}
    assert any("duplicate network action" in error for error in reference_errors(duplicated))
    assert isinstance(validate_step(listen), ListenStep)
    assert isinstance(validate_step(connect), ConnectStep)
    close = {**listen, "action": "close", "name": "server closes"}
    assert isinstance(validate_step(close), CloseStep)
    with pytest.raises(Exception):
        SendStep.model_validate({**listen, "action": "send"})
