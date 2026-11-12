"""Network-action steps and token-budget MCP responses."""

import copy

import pytest
from pydantic import ValidationError

from rsc.protocol.models import CloseStep, ConnectStep, ListenStep, SendStep, validate_step
from rsc.protocol.references import reference_errors
from rsc.workflow.paging import HARD_BUDGET, PageError, records_page
from rsc.workflow.sources import token_count
from rsc.workflow.tools import validate_arguments


def _records():
    evidence = [{"batch_id": "b", "block_id": "1"}]
    rows = []
    for index in range(3):
        rows.append(
            {
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


def test_records_preserve_nested_values_evidence_and_input_file_metadata():
    rows = _records()
    before = copy.deepcopy(rows)
    metadata = {"input_files": ["parsers/parser-1.json"]}
    page = records_page(rows, max_tokens=24000, metadata=metadata)
    assert page["items"] == rows == before
    assert page["input_files"] == metadata["input_files"]
    assert page["next_cursor"] is None
    assert page["tokens"] == token_count(page) <= page["max_tokens"]
    assert set(page) == {"items", "next_cursor", "max_tokens", "input_files", "tokens"}
    assert all("id" not in row and "record_id" not in row for row in page["items"])


def test_short_records_exceeding_fifty_fit_in_one_budget():
    rows = [
        {"kind": "parties", "value": {"name": f"p{i}", "description": "role"}}
        for i in range(60)
    ]
    page = records_page(rows, max_tokens=24000)
    assert page["next_cursor"] is None
    assert len(page["items"]) == 60


def test_bounded_pages_deliver_each_complete_record_once_without_revisions():
    rows = [
        {
            "kind": "parties",
            "value": {"name": f"party {index}", "description": "source-backed role " * 30},
            "evidence": [{"batch_id": "b", "block_id": str(index)}],
            "input_file": "parsers/parser-1.json",
        }
        for index in range(7)
    ]
    metadata = {"input_files": ["parsers/parser-1.json"]}
    before = copy.deepcopy(rows)
    cursor, delivered, pages = 0, [], []
    while True:
        page = records_page(rows, cursor, 400, metadata=metadata)
        assert page["items"]
        assert page["tokens"] == token_count(page) <= 400
        assert page["max_tokens"] == 400
        assert page["input_files"] == metadata["input_files"]
        assert not {"revision", "delivered_ids", "format"}.intersection(page)
        assert page["items"] == rows[cursor:cursor + len(page["items"])]
        delivered.extend(page["items"])
        pages.append(page)
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] == cursor + len(page["items"])
        cursor = page["next_cursor"]
    assert len(pages) > 1
    assert delivered == rows == before


@pytest.mark.parametrize("rows", [[], _records()])
def test_empty_and_exhausted_pages_are_bounded(rows):
    page = records_page(rows, cursor=len(rows), max_tokens=100)
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert page["tokens"] == token_count(page) <= 100
    assert "revision" not in page


def test_metadata_counts_toward_the_response_budget():
    with pytest.raises(PageError, match="page metadata requires"):
        records_page([], max_tokens=100, metadata={"input_files": ["long-path-" * 100]})


def test_one_oversized_record_reports_a_budget_that_can_fit_it():
    rows = [{"kind": "parties", "value": {"name": "p", "description": "detail " * 500}}]
    with pytest.raises(PageError, match=r"single record requires (\d+) tokens") as raised:
        records_page(rows, max_tokens=100)
    needed = int(str(raised.value).split("requires ", 1)[1].split(" ", 1)[0])
    fitted = records_page(rows, max_tokens=needed)
    assert fitted["items"] == rows and fitted["next_cursor"] is None
    assert fitted["tokens"] == token_count(fitted) <= needed
    with pytest.raises(PageError, match="single record requires"):
        records_page(rows, max_tokens=needed - 1)


def test_one_oversized_record_names_the_required_budget():
    rows = [{"kind": "parties", "value": {"name": "p", "description": "x" * 200000}}]
    with pytest.raises(PageError, match="single record requires"):
        records_page(rows, max_tokens=1000)


@pytest.mark.parametrize("cursor", [-1, 5, True, "0", 0.5])
def test_invalid_cursors_are_rejected(cursor):
    with pytest.raises(PageError, match="invalid cursor"):
        records_page(_records(), cursor=cursor)


@pytest.mark.parametrize("budget", [0, -1, True, "100", 1.5, HARD_BUDGET + 1])
def test_invalid_response_budgets_are_rejected(budget):
    with pytest.raises(PageError, match="max_tokens must be"):
        records_page(_records(), max_tokens=budget)


def test_query_model_paging_contract_has_no_revision_argument():
    validate_arguments("query_model", {"cursor": 0, "max_tokens": 100})
    with pytest.raises(ValueError, match="revision"):
        validate_arguments("query_model", {"cursor": 0, "revision": 1})


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
    with pytest.raises(ValidationError):
        SendStep.model_validate({**listen, "action": "send"})
