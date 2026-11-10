"""Short citations preserve source ownership, receipt fencing and canonical storage."""

from copy import deepcopy

import pytest
import test_parallel_workflow as h

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.evidence import expand_evidence, source_citations


def draft(tmp_path):
    sources = []
    for number in (9998, 9999):
        source = tmp_path / f"rfc{number}.txt"
        source.write_text(f"RFC {number}\n\n1. Role\n\nThe client sends HELLO to the server.\n")
        sources.append(source)
    task = Workflow.create(tmp_path / "task", sources, "Example", parse_workers=1, review_workers=1)
    h.call(task, "coordinator", "prepare_sources")
    h.advance(task)
    h.claim(task, "parser-1")
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


def test_aliases_require_receipts_and_store_canonical_evidence(tmp_path):
    task = draft(tmp_path)
    first, second = task.batches
    page = h.call(task, "parser-1", "read_batches", batch_ids=[first["batch_id"]])
    segment = page["batches"][0]["batch"]["segments"][0]
    pair = {"batch_id": first["batch_id"], "block_id": segment["block_id"]}
    change = {
        "op": "add",
        "kind": "parties",
        "value": {"name": "client", "description": "sender"},
        "evidence": [segment["citation"]],
    }
    args = dict(expected_revision=0, operation_id="add", changes=[change])
    result = h.call(task, "parser-1", "apply_changes", **args)
    assert h.call(task, "parser-1", "apply_changes", **args) == result
    model = task.store.draft("parse-0/parser-1")[1]
    assert next(iter(model.values()))["evidence"] == [pair]
    unread = next(
        name
        for name, ref in source_citations(task.batches).items()
        if ref["batch_id"] == second["batch_id"]
    )
    patch = {
        "op": "patch",
        "kind": "parties",
        "target": {"name": "client"},
        "value": {"description": "changed"},
        "evidence": [unread],
    }
    with pytest.raises(WorkflowError, match="complete source delivery"):
        h.call(
            task,
            "parser-1",
            "apply_changes",
            expected_revision=1,
            operation_id="patch",
            changes=[patch],
        )
    h.read(task, "parser-1", [second["batch_id"]])
    h.call(
        task,
        "parser-1",
        "apply_changes",
        expected_revision=1,
        operation_id="patch",
        changes=[patch],
    )
    model = task.store.draft("parse-0/parser-1")[1]
    assert len(next(iter(model.values()))["evidence"]) == 2
    task.db.close()


def test_all_mutation_evidence_errors_are_returned_atomically(tmp_path):
    task = draft(tmp_path)
    batch = task.batches[0]
    h.read(task, "parser-1", [batch["batch_id"]])
    before = task.store.draft("parse-0/parser-1")
    with pytest.raises(WorkflowError) as error:
        h.call(
            task,
            "parser-1",
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
    assert task.store.draft("parse-0/parser-1") == before
    assert task.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    task.db.close()
