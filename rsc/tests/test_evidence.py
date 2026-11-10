"""Short citations preserve exact source ownership, receipts and canonical storage."""

from copy import deepcopy

import pytest
from test_workflow import call, claim, reviewed

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.evidence import expand_evidence, source_citations
from rsc.workflow.source_format import decode_source_batch


def draft(tmp_path):
    sources = []
    for number in (9998, 9999):
        source = tmp_path / f"rfc{number}.txt"
        source.write_text(f"RFC {number}\n\n1. Role\n\nThe client sends HELLO to the server.\n")
        sources.append(source)
    task = Workflow.create(tmp_path / "task", sources, "Example")
    claim(task)
    call(task, "prepare_sources")
    call(task, "advance_phase")
    claim(task)
    return task


def test_citations_disambiguate_identical_block_names_without_mutating_sources():
    batches = [
        {"batch_id": name, "segments": [{"block_id": "blk-1"}, {"block_id": "blk-1"}]}
        for name in ("a", "b")
    ]
    before = deepcopy(batches)
    refs = source_citations(batches)
    assert refs == {
        "e1": {"batch_id": "a", "block_id": "blk-1"},
        "e2": {"batch_id": "b", "block_id": "blk-1"},
    }
    assert expand_evidence(["e1", refs["e2"]], refs) == list(refs.values())
    assert batches == before
    for bad in ("unknown", True, 0, {}, {"batch_id": "a"}):
        with pytest.raises(ValueError):
            expand_evidence([bad], refs)


def test_aliases_require_receipts_and_replay_equivalently_across_wire_formats(tmp_path):
    task = draft(tmp_path)
    first, second = task.data["batches"]
    page = call(task, "read_batches", batch_ids=[first["batch_id"]], format="compact")
    segment = decode_source_batch(page["batches"][0]["batch"])["segments"][0]
    alias, pair = segment["citation"], segment["evidence"]
    change = {
        "op": "add",
        "kind": "parties",
        "value": {"name": "client", "description": "sender"},
        "evidence": [alias],
    }
    result = call(task, "apply_changes", expected_revision=0, operation_id="add", changes=[change])
    rid = result["record_ids"][0]
    legacy = {**change, "evidence": [pair]}
    assert (
        call(task, "apply_changes", expected_revision=0, operation_id="add", changes=[legacy])
        == result
    )
    table = {
        "groups": [
            {
                "op": "add",
                "kind": "parties",
                "columns": ["name", "description", "@evidence"],
                "rows": [["client", "sender", [alias]]],
            }
        ]
    }
    assert (
        call(task, "apply_changes", expected_revision=0, operation_id="add", changes=table)
        == result
    )
    assert task.data["records"][rid]["evidence"] == [pair]
    unread = next(
        name
        for name, ref in source_citations(task.data["batches"]).items()
        if ref["batch_id"] == second["batch_id"]
    )
    with pytest.raises(WorkflowError, match="read evidence source"):
        call(
            task,
            "apply_changes",
            expected_revision=1,
            operation_id="unread",
            changes=[
                {
                    "op": "patch",
                    "record_id": rid,
                    "value": {"description": "changed"},
                    "evidence": [unread],
                }
            ],
        )
    for batch in task.data["batches"]:
        read = call(task, "read_batch", batch_id=batch["batch_id"])
        call(
            task,
            "complete_batch",
            batch_id=batch["batch_id"],
            receipt_id=read["receipt_id"],
            report={"summary": "Extracted sender", "record_ids": [rid]},
        )
    call(task, "advance_phase")
    claim(task)
    finding = {"description": "Check sender", "record_ids": [rid], "evidence": [alias]}
    with pytest.raises(WorkflowError, match="read evidence source"):
        call(task, "submit_findings", reviewed_revision=1, findings=[finding])
    call(task, "read_batch", batch_id=first["batch_id"])
    fid = call(task, "submit_findings", reviewed_revision=1, findings=[finding])["finding_ids"][0]
    assert task.data["findings"][fid]["evidence"] == [pair]
    reviewed(task)
    call(task, "advance_phase")
    claim(task)
    resolution = {
        "finding_id": fid,
        "status": "not_applicable",
        "reason": "Source names this sender",
        "evidence": [alias],
    }
    with pytest.raises(WorkflowError, match="read evidence source"):
        call(task, "submit_resolutions", expected_revision=1, resolutions=[resolution])
    call(task, "read_batch", batch_id=first["batch_id"])
    call(task, "submit_resolutions", expected_revision=1, resolutions=[resolution])
    assert task.data["findings"][fid]["resolution"]["evidence"] == [pair]


def test_all_mutation_evidence_errors_are_returned_atomically(tmp_path):
    task = draft(tmp_path)
    batch = task.data["batches"][0]
    call(task, "read_batch", batch_id=batch["batch_id"])
    before = deepcopy(task.data)
    with pytest.raises(WorkflowError) as error:
        call(
            task,
            "apply_changes",
            expected_revision=0,
            operation_id="bad",
            changes=[
                {
                    "op": "add",
                    "kind": "parties",
                    "value": {"name": name, "description": "peer"},
                    "evidence": [{"batch_id": batch["batch_id"], "block_id": "missing-" + name}],
                }
                for name in ("client", "server")
            ],
        )
    for fragment in ("changes[0]", "changes[1]", "missing-client", "missing-server"):
        assert fragment in str(error.value)
    assert task.data["records"] == before["records"]
    assert task.data["operations"] == before["operations"]
    assert task.data["revision"] == before["revision"]
