"""Partial edits retain full-record validation, provenance and transaction semantics."""

from copy import deepcopy

import pytest
from test_workflow import call, claim, parsed, reviewed

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.change_format import decode_changes, encode_changes


def test_patch_retains_untouched_values_and_evidence_across_dispatches(tmp_path):
    sources = []
    for number in (9998, 9999):
        source = tmp_path / f"rfc{number}.txt"
        source.write_text(f"RFC {number}\n\n1. Role\n\nThe client sends HELLO to the server.\n")
        sources.append(source)
    task = Workflow.create(tmp_path / "task", sources, "Example")
    rid = parsed(task)
    reviewed(task)
    batch = task.data["batches"][-1]
    evidence = [{"batch_id": batch["batch_id"], "block_id": batch["segments"][0]["block_id"]}]
    call(
        task,
        "submit_findings",
        reviewed_revision=1,
        findings=[
            {
                "description": "Clarify the sender role",
                "record_ids": [rid],
                "evidence": evidence,
            }
        ],
    )
    call(task, "advance_phase")
    claim(task)
    before = deepcopy(task.data["records"][rid])
    changes = [
        {
            "op": "patch",
            "record_id": rid,
            "value": {"description": "HELLO sender"},
            "evidence": evidence,
        }
    ]
    assert decode_changes(encode_changes(changes)) == changes
    with pytest.raises(WorkflowError, match="read evidence source"):
        call(task, "apply_changes", expected_revision=1, operation_id="patch", changes=changes)
    assert task.data["records"][rid] == before
    call(task, "read_batch", batch_id=batch["batch_id"])
    result = call(task, "apply_changes", expected_revision=1, operation_id="patch", changes=changes)
    assert (
        call(
            task,
            "apply_changes",
            expected_revision=1,
            operation_id="patch",
            changes=encode_changes(changes),
        )
        == result
    )
    after = task.data["records"][rid]
    assert after["value"] == {"name": "client", "description": "HELLO sender"}
    assert after["evidence"] == before["evidence"] + evidence
    assert task.data["history"][-1]["changes"][0]["before"] == before
    assert task.data["history"][-1]["changes"][0]["after"] == after
    with pytest.raises(WorkflowError, match="resolution"):
        call(task, "advance_phase")
    saved = deepcopy(task.data)
    with pytest.raises(WorkflowError):
        call(
            task,
            "apply_changes",
            expected_revision=2,
            operation_id="invalid",
            changes=[
                {
                    "op": "patch",
                    "record_id": rid,
                    "value": {"description": "temporary"},
                    "evidence": evidence,
                },
                {
                    "op": "patch",
                    "record_id": rid,
                    "value": {"invented_key": "unsupported"},
                    "evidence": evidence,
                },
            ],
        )
    assert task.data["records"] == saved["records"]
    assert task.data["revision"] == saved["revision"]


def test_patch_cannot_rename_steps_or_bypass_schema(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Role\n\nThe server listens for HELLO.\n")
    task = Workflow.create(tmp_path / "task", [source], "Example")
    claim(task)
    call(task, "prepare_sources")
    call(task, "advance_phase")
    claim(task)
    batch = task.data["batches"][0]
    call(task, "read_batch", batch_id=batch["batch_id"])
    evidence = [{"batch_id": batch["batch_id"], "block_id": batch["segments"][0]["block_id"]}]
    value = {
        "name": "listen",
        "action": "listen",
        "channel_name": "control",
        "party_name": "server",
        "condition": "",
    }
    rid = call(
        task,
        "apply_changes",
        expected_revision=0,
        operation_id="add",
        changes=[
            {
                "op": "add",
                "kind": "steps",
                "value": value,
                "evidence": evidence,
            }
        ],
    )["record_ids"][0]
    for patch in ({"name": "renamed"}, {"action": "send"}, {"condition": None}, {}):
        with pytest.raises(WorkflowError):
            call(
                task,
                "apply_changes",
                expected_revision=1,
                operation_id="bad",
                changes=[
                    {
                        "op": "patch",
                        "record_id": rid,
                        "value": patch,
                        "evidence": evidence,
                    }
                ],
            )
        assert task.data["records"][rid]["value"] == value
    call(
        task,
        "apply_changes",
        expected_revision=1,
        operation_id="condition",
        changes=[
            {
                "op": "patch",
                "record_id": rid,
                "value": {"condition": "When enabled"},
                "evidence": evidence,
            }
        ],
    )
    assert task.data["records"][rid]["evidence"] == evidence
    assert task.data["records"][rid]["value"] == {**value, "condition": "When enabled"}


def test_patch_replaces_arrays_and_legacy_update_still_requires_full_value(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Message\n\nHELLO includes a correlation field.\n")
    task = Workflow.create(tmp_path / "task", [source], "Example")
    claim(task)
    call(task, "prepare_sources")
    call(task, "advance_phase")
    claim(task)
    batch = task.data["batches"][0]
    call(task, "read_batch", batch_id=batch["batch_id"])
    evidence = [{"batch_id": batch["batch_id"], "block_id": batch["segments"][0]["block_id"]}]
    rid = call(
        task,
        "apply_changes",
        expected_revision=0,
        operation_id="message",
        changes=[
            {
                "op": "add",
                "kind": "messages",
                "value": {"name": "HELLO", "associated_fields": [{"name": "one"}]},
                "evidence": evidence,
            }
        ],
    )["record_ids"][0]
    change = {
        "op": "update",
        "record_id": rid,
        "value": {"associated_fields": [{"name": "two"}]},
        "evidence": evidence,
    }
    with pytest.raises(WorkflowError):
        call(task, "apply_changes", expected_revision=1, operation_id="replace", changes=[change])
    change["op"] = "patch"
    call(
        task,
        "apply_changes",
        expected_revision=1,
        operation_id="patch",
        changes=encode_changes([change]),
    )
    assert task.data["records"][rid]["value"] == {
        "name": "HELLO",
        "associated_fields": [{"name": "two"}],
    }
    change["value"]["associated_fields"] = [{"name": "x" * 80 + str(n)} for n in range(4000)]
    with pytest.raises(WorkflowError, match="exceeds 8000 tokens"):
        call(task, "apply_changes", expected_revision=2, operation_id="oversize", changes=[change])


def test_nested_field_schema_errors_identify_the_failing_location():
    from rsc.workflow.model import validate_record

    with pytest.raises(ValueError, match=r'associated_fields/0: .*not of type'):
        validate_record('messages', {'name': 'HELLO', 'associated_fields': ['pathname']})
    with pytest.raises(ValueError, match=r'associated_fields: .*not of type'):
        validate_record('messages', {'name': 'HELLO', 'associated_fields': {'name': 'pathname'}})
