"""Patches retain full-record validation, provenance and whole-array replacement."""

from copy import deepcopy

import pytest

from rsc.workflow.mutations import plan_changes, record_key


def edited(record, change):
    def evidence(entries):
        if not entries:
            raise ValueError("missing evidence")

    return plan_changes({record_key(record): record}, [change], evidence_check=evidence)


def test_patch_retains_untouched_values_old_evidence_and_origin_mapping():
    old = {
        "kind": "parties",
        "value": {"name": "client", "description": "sender"},
        "evidence": [{"batch_id": "old", "block_id": "one"}],
        "origins": [{"scope": "parse-0/parser-1", "kind": "parties", "target": {"name": "client"}}],
    }
    before = deepcopy(old)
    new_evidence = [{"batch_id": "new", "block_id": "two"}]
    delta, _, _ = edited(
        old,
        {
            "op": "patch",
            "kind": "parties",
            "target": {"name": "client"},
            "value": {"description": "HELLO sender"},
            "evidence": new_evidence,
        },
    )
    previous, after = next(iter(delta.values()))
    assert previous == old == before
    assert after["value"] == {"name": "client", "description": "HELLO sender"}
    assert after["evidence"] == old["evidence"] + new_evidence
    assert after["origins"] == old["origins"]


@pytest.mark.parametrize(
    "patch", [{"name": "renamed"}, {"action": "send"}, {"condition": None}, {}]
)
def test_patch_cannot_rename_steps_or_bypass_schema(patch):
    record = {
        "kind": "steps",
        "value": {
            "name": "listen",
            "action": "listen",
            "channel_name": "control",
            "party_name": "server",
            "condition": "",
        },
        "evidence": [{"batch_id": "b1", "block_id": "one"}],
    }
    before = deepcopy(record)
    with pytest.raises(ValueError):
        edited(
            record,
            {
                "op": "patch",
                "kind": "steps",
                "target": {"name": "listen"},
                "value": patch,
                "evidence": record["evidence"],
            },
        )
    assert record == before


def test_patch_replaces_arrays_and_update_requires_full_value():
    record = {
        "kind": "messages",
        "value": {"name": "HELLO", "associated_fields": [{"name": "one"}]},
        "evidence": [{"batch_id": "b1", "block_id": "one"}],
    }
    change = {
        "op": "update",
        "kind": "messages",
        "target": {"name": "HELLO"},
        "value": {"associated_fields": [{"name": "two"}]},
        "evidence": record["evidence"],
    }
    with pytest.raises(ValueError):
        edited(record, change)
    change["op"] = "patch"
    delta, _, _ = edited(record, change)
    assert next(iter(delta.values()))[1]["value"] == {
        "name": "HELLO",
        "associated_fields": [{"name": "two"}],
    }
    change["value"]["associated_fields"] = [{"name": "x" * 80 + str(n)} for n in range(4000)]
    with pytest.raises(ValueError, match="exceeds 8000 tokens"):
        edited(record, change)


def test_nested_field_schema_errors_identify_the_failing_location():
    from rsc.workflow.model import validate_record

    with pytest.raises(ValueError, match=r"associated_fields/0: .*not of type"):
        validate_record("messages", {"name": "HELLO", "associated_fields": ["pathname"]})
    with pytest.raises(ValueError, match=r"associated_fields: .*not of type"):
        validate_record("messages", {"name": "HELLO", "associated_fields": {"name": "pathname"}})
