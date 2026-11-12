"""File provenance and declared review coverage replace historical receipts."""

import pytest
import test_result_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.store import atomic_json, read_json

run = h.run


def test_audit_rejects_changed_frozen_candidate(run):
    h.publish(run)
    path = run.root / run.data["reviewed_candidate"]
    model = read_json(path)
    model["items"][0]["value"]["description"] = "changed"
    atomic_json(path, model)
    with pytest.raises(WorkflowError, match="frozen candidate differs"):
        audit_run(run.root)


def test_audit_rejects_missing_range_and_wrong_reviewer(run):
    h.publish(run)
    a = next(a for a in run.store.assignments() if a["phase"] == "review")
    path = run.root / a["output_path"]
    original = read_json(path)
    atomic_json(path, {**original, "reviewed_batches": original["reviewed_batches"][:-1]})
    with pytest.raises(ValueError, match="every assigned source"):
        audit_run(run.root)
    atomic_json(path, original)
    run.store.update_assignment(a["name"], worker="merge-1")
    with pytest.raises(WorkflowError, match="not independent"):
        audit_run(run.root)


def test_audit_validates_full_source_text_and_evidence(run):
    h.publish(run)
    sources = read_json(run.root / "sources.json")
    sources["batches"][0]["segments"][0]["text"] = "altered source"
    atomic_json(run.root / "sources.json", sources)
    with pytest.raises(ValueError, match="source text mismatch"):
        audit_run(run.root)


def test_old_receipt_fields_and_model_mutations_are_not_tools(run):
    with pytest.raises(WorkflowError, match="forbidden"):
        h.call(run, "parser-1", "get_changes")
    with pytest.raises(WorkflowError, match="invalid content"):
        h.call(
            run,
            "parser-1",
            "read_source_batches",
            batch_ids=[run.batches[0]["batch_id"]],
            receipt="obsolete",
        )


def test_exported_snapshot_must_equal_official_source_without_hashes(run):
    h.publish(run)
    source = next((run.root / "snapshots").glob("*.json"))
    document = read_json(source)
    document["blocks"][0]["text"] += " altered"
    atomic_json(source, document)
    with pytest.raises(WorkflowError, match="source snapshot differs"):
        audit_run(run.root)
