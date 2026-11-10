"""Historical grouped payloads remain decodable, but new agents use one named array.

Live exact selection, atomicity and retry cases are in test_named_mutations,
test_workflow_store and test_parallel_workflow; scoped alias cases in test_scoped_merge.
"""

from copy import deepcopy

import pytest

from rsc.workflow.change_format import decode_changes
from rsc.workflow.tools import schema, validate_arguments


def test_named_rows_defaults_are_lossless_and_cannot_hide_unknown_keys():
    pair = {"batch_id": "b", "block_id": "a"}
    payload = {
        "groups": [
            {
                "op": "add",
                "kind": "parties",
                "defaults": {"description": "actor"},
                "rows": [
                    {"value": {"name": "client"}, "evidence": ["e1"]},
                    {"value": {"name": "server", "description": "receiver"}, "evidence": [pair]},
                ],
            }
        ]
    }
    before = deepcopy(payload)
    assert decode_changes(payload, {"e1": pair}) == [
        {
            "op": "add",
            "kind": "parties",
            "value": {"name": "client", "description": "actor"},
            "evidence": [pair],
        },
        {
            "op": "add",
            "kind": "parties",
            "value": {"name": "server", "description": "receiver"},
            "evidence": [pair],
        },
    ]
    assert payload == before
    bad = deepcopy(payload)
    bad["groups"][0]["defaults"] = {"@evidence": ["e1"]}
    with pytest.raises(ValueError, match="value fields"):
        decode_changes(bad, {"e1": pair})
    bad = deepcopy(payload)
    bad["groups"][0]["rows"][0]["typo"] = "ignored?"
    with pytest.raises(ValueError, match="unknown row keys"):
        decode_changes(bad, {"e1": pair})


def test_live_schema_has_one_encoding_and_rejects_grouped_or_id_operations():
    changes = schema("apply_changes")["inputSchema"]["properties"]["changes"]
    assert changes["type"] == "array"
    assert "groups" not in changes
    for payload in ({"groups": []}, [{"op": "delete", "record_id": "old-id", "reason": "x"}]):
        with pytest.raises(ValueError):
            validate_arguments(
                "apply_changes", {"expected_revision": 0, "operation_id": "old", "changes": payload}
            )


def test_named_errors_aggregate_invalid_fields_without_dumping_other_variants():
    args = {
        "expected_revision": 0,
        "operation_id": "bad",
        "changes": [
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": name, "description": "peer"},
                "evidence": ["e1"],
                "field_relations": [],
            }
            for name in ("client", "server")
        ],
    }
    with pytest.raises(ValueError) as raised:
        validate_arguments("apply_changes", args)
    message = str(raised.value)
    assert "changes[0]" in message and "changes[1]" in message
    assert "field_relations" in message and "not valid under any" not in message
