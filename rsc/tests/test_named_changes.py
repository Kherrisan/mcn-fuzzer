"""Named targets/rows retain exact identity, rollback and legacy retry semantics."""

from copy import deepcopy

import pytest
from test_evidence import draft
from test_workflow import call

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.change_format import decode_changes
from rsc.workflow.sources import digest


def party(tmp_path):
    task = draft(tmp_path)
    b = task.data["batches"][0]
    cite = call(task, "read_batch", batch_id=b["batch_id"])["batch"]["segments"][0]["citation"]
    result = call(
        task,
        "apply_changes",
        expected_revision=0,
        operation_id="initial",
        changes={
            "groups": [
                {
                    "op": "add",
                    "kind": "parties",
                    "defaults": {"description": "sender"},
                    "rows": [{"value": {"name": "client"}, "evidence": [cite]}],
                }
            ],
        },
    )
    return task, cite, result["record_ids"][0]


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


def test_named_row_schema_exposes_metadata_and_identifies_misplaced_fields():
    from rsc.workflow.tools import schema, validate_arguments

    choices = schema("apply_changes")["inputSchema"]["properties"]["changes"]["oneOf"]
    row_schema = choices[1]["properties"]["groups"]["items"]["properties"]["rows"]["items"]
    named = row_schema["anyOf"][0]
    assert named["additionalProperties"] is False
    assert {"value", "evidence", "target", "record_id"} <= named["properties"].keys()
    args = {
        "expected_revision": 0,
        "operation_id": "named-schema",
        "changes": {
            "evidence": [{"batch_id": "b", "block_id": "a"}],
            "groups": [
                {
                    "op": "add",
                    "kind": "parties",
                    "defaults": {"description": "actor"},
                    "rows": [
                        {"value": {"name": "client"}, "evidence": [0, "e1"]},
                        {"value": {"name": "server"}, "evidence": ["e1"]},
                    ],
                }
            ],
        },
    }
    validate_arguments("apply_changes", args)
    assert len(decode_changes(args["changes"], {"e1": {"batch_id": "b", "block_id": "a"}})) == 2
    for row in args["changes"]["groups"][0]["rows"]:
        row["field_relations"] = []
    with pytest.raises(WorkflowError) as error:
        validate_arguments("apply_changes", args)
    diagnostic = str(error.value)
    assert "changes/groups/0/rows/0" in diagnostic
    assert "changes/groups/0/rows/1" in diagnostic
    assert "field_relations" in diagnostic
    assert "not valid under any" not in diagnostic
    assert "description" not in diagnostic  # Do not dump the entire proposed transaction.


def test_target_retries_survive_rename_delete_and_id_form(tmp_path):
    task, cite, rid = party(tmp_path)
    edit = {
        "op": "patch",
        "kind": "parties",
        "target": {"name": "client"},
        "value": {"description": "HELLO sender"},
        "evidence": [cite],
    }
    result = call(task, "apply_changes", expected_revision=1, operation_id="patch", changes=[edit])
    legacy = {k: v for k, v in edit.items() if k != "target"}
    legacy["record_id"] = rid
    assert (
        call(task, "apply_changes", expected_revision=1, operation_id="patch", changes=[legacy])
        == result
    )
    rename = {**edit, "value": {"name": "user"}}
    result = call(
        task, "apply_changes", expected_revision=2, operation_id="rename", changes=[rename]
    )
    assert (
        call(task, "apply_changes", expected_revision=2, operation_id="rename", changes=[rename])
        == result
    )
    delete = {
        "op": "delete",
        "kind": "parties",
        "target": {"name": "user"},
        "reason": "source alias",
    }
    result = call(
        task, "apply_changes", expected_revision=3, operation_id="delete", changes=[delete]
    )
    assert rid not in task.data["records"]
    assert (
        call(task, "apply_changes", expected_revision=3, operation_id="delete", changes=[delete])
        == result
    )
    assert (
        call(
            task,
            "apply_changes",
            expected_revision=3,
            operation_id="delete",
            changes=[
                {
                    "op": "delete",
                    "record_id": rid,
                    "reason": "source alias",
                }
            ],
        )
        == result
    )
    with pytest.raises(WorkflowError, match="stale model revision"):
        call(task, "apply_changes", expected_revision=3, operation_id="new", changes=[edit])


def test_targets_require_exact_unique_fields_and_batch_errors_roll_back(tmp_path):
    task, cite, rid = party(tmp_path)
    before = deepcopy(task.data)
    base = {
        "op": "patch",
        "kind": "parties",
        "value": {"description": "changed"},
        "evidence": [cite],
    }
    for target in ({"name": "clien"}, {"absent_key": None}, {}):
        with pytest.raises(WorkflowError):
            call(
                task,
                "apply_changes",
                expected_revision=1,
                operation_id="bad",
                changes=[
                    {**base, "target": {"name": "client"}},
                    {**base, "target": target},
                ],
            )
        assert task.data["records"] == before["records"]
        assert task.data["operations"] == before["operations"]
        assert task.data["revision"] == before["revision"]
    with pytest.raises(WorkflowError):
        call(
            task,
            "apply_changes",
            expected_revision=1,
            operation_id="both",
            changes=[
                {**base, "target": {"name": "client"}, "record_id": rid},
            ],
        )
    call(
        task,
        "apply_changes",
        expected_revision=1,
        operation_id="duplicate",
        changes=[
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": "client", "description": "other"},
                "evidence": [cite],
            }
        ],
    )
    with pytest.raises(WorkflowError, match="2 matches"):
        call(
            task,
            "apply_changes",
            expected_revision=2,
            operation_id="ambiguous",
            changes=[{**base, "target": {"name": "client"}}],
        )
    saved = deepcopy(task.data["records"])
    with pytest.raises(WorkflowError):
        call(
            task,
            "apply_changes",
            expected_revision=2,
            operation_id="bad defaults",
            changes={
                "groups": [
                    {
                        "op": "add",
                        "kind": "messages",
                        "defaults": {"condition": "wrong field"},
                        "rows": [
                            {
                                "value": {"name": "HELLO", "associated_fields": []},
                                "evidence": [cite],
                            }
                        ],
                    }
                ],
            },
        )
    assert task.data["records"] == saved


def test_dependency_selectors_and_step_names_stay_strict(tmp_path):
    task, cite, _ = party(tmp_path)
    call(
        task,
        "apply_changes",
        expected_revision=1,
        operation_id="steps",
        changes={
            "groups": [
                {
                    "op": "add",
                    "kind": "steps",
                    "defaults": {
                        "action": "listen",
                        "channel_name": "control",
                        "party_name": "client",
                        "condition": "",
                    },
                    "rows": [
                        {"value": {"name": name}, "evidence": [cite]} for name in ("s1", "s2")
                    ],
                }
            ]
        },
    )
    with pytest.raises(WorkflowError, match="step name cannot change"):
        call(
            task,
            "apply_changes",
            expected_revision=2,
            operation_id="rename",
            changes=[
                {
                    "op": "patch",
                    "kind": "steps",
                    "target": {"name": "s1"},
                    "value": {"name": "new"},
                    "evidence": [cite],
                }
            ],
        )
    result = call(
        task,
        "apply_changes",
        expected_revision=2,
        operation_id="edges",
        changes={
            "groups": [
                {
                    "op": "add",
                    "kind": "step_dependencies",
                    "defaults": {"prior_step": "s1", "dependent_step": "s2", "field_relations": []},
                    "rows": [
                        {"value": {"condition": c}, "evidence": [cite]} for c in ("first", "second")
                    ],
                }
            ]
        },
    )
    base = {
        "op": "patch",
        "kind": "step_dependencies",
        "value": {"condition": "corrected"},
        "evidence": [cite],
    }
    target = {"prior_step": "s1", "dependent_step": "s2"}
    with pytest.raises(WorkflowError, match="2 matches"):
        call(
            task,
            "apply_changes",
            expected_revision=3,
            operation_id="edge",
            changes=[{**base, "target": target}],
        )
    edit = {**base, "target": {**target, "condition": "first"}}
    applied = call(task, "apply_changes", expected_revision=3, operation_id="edge", changes=[edit])
    assert (
        call(task, "apply_changes", expected_revision=3, operation_id="edge", changes=[edit])
        == applied
    )
    assert task.data["records"][result["record_ids"][0]]["value"]["condition"] == "corrected"


def test_old_kind_bearing_signatures_survive_upgrade_and_named_retry(tmp_path):
    task, _, rid = party(tmp_path)
    pair = task.data["records"][rid]["evidence"][0]
    change = {
        "op": "update",
        "kind": "parties",
        "record_id": rid,
        "value": {"name": "client", "description": "updated"},
        "evidence": [pair],
    }
    result = call(task, "apply_changes", expected_revision=1, operation_id="old", changes=[change])
    old = task.data["operations"]["old"]
    old["signature"] = digest({"revision": 1, "changes": [change]})
    old.pop("target_bindings", None)
    with task.db:
        task._save()
    task.db.close()
    reopened = Workflow(task.root)
    assert (
        call(reopened, "apply_changes", expected_revision=1, operation_id="old", changes=[change])
        == result
    )
    named = {k: v for k, v in change.items() if k != "record_id"}
    named["target"] = {"name": "client"}
    assert (
        call(reopened, "apply_changes", expected_revision=1, operation_id="old", changes=[named])
        == result
    )
