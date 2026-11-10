"""Compact mutation inputs preserve values, operation order and evidence identity."""

from copy import deepcopy

import pytest

from rsc.workflow.change_format import decode_changes, encode_changes
from rsc.workflow.sources import token_count


def example():
    evidence = [{"batch_id": "b", "block_id": "1"}]
    return [
        {
            "op": "add",
            "kind": "parties",
            "value": {"name": "client", "description": "λ\n role"},
            "evidence": evidence,
        },
        {
            "op": "add",
            "kind": "parties",
            "value": {"name": "server", "description": "peer"},
            "evidence": evidence,
        },
        {
            "op": "update",
            "record_id": "r1",
            "value": {"name": "client", "description": "sender"},
            "evidence": evidence,
        },
        {"op": "delete", "record_id": "r2", "reason": "source-backed duplicate"},
        {
            "op": "add",
            "kind": "unresolved",
            "value": {"description": "source ambiguity", "object_names": []},
            "evidence": evidence,
        },
    ]


def test_compact_changes_preserve_operation_order_and_exact_objects():
    original = example()
    before = deepcopy(original)
    compact = encode_changes(original)
    assert decode_changes(compact) == original == before
    assert len(compact["evidence"]) == 1
    assert compact["groups"][0]["op"] == "add"
    assert len(compact["groups"][0]["rows"]) == 2
    assert compact["groups"][-1]["op"] == "add"
    assert token_count(compact) < token_count(original)


@pytest.mark.parametrize("index", [-1, 5, True, "0"])
def test_bad_pool_indexes_cannot_alias_evidence(index):
    compact = encode_changes(example())
    column = compact["groups"][0]["columns"].index("@evidence")
    compact["groups"][0]["rows"][0][column] = [index]
    with pytest.raises(ValueError, match="pool indexes|unknown citation"):
        decode_changes(compact)


def test_malformed_rows_and_reserved_columns_are_rejected():
    compact = encode_changes(example())
    compact["groups"][0]["rows"][0].pop()
    with pytest.raises(ValueError, match="length"):
        decode_changes(compact)
    compact = encode_changes(example())
    compact["groups"][0]["columns"][0] = "@unknown"
    with pytest.raises(ValueError, match="reserved"):
        decode_changes(compact)
    compact = encode_changes(example())
    compact["groups"][0]["columns"][0] = compact["groups"][0]["columns"][1]
    with pytest.raises(ValueError, match="unique"):
        decode_changes(compact)
