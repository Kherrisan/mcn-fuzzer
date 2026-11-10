"""Phase measurements distinguish source deliveries, report counts and observers."""

import json

import pytest
import test_parallel_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.metrics import parallel_metrics

run = h.run


def test_exported_metrics_use_coordinator_phase_ends_and_delivery_metadata(run):
    h.merged(run)
    run.call(h.who(run, "coordinator"), "workflow_status", origin="health")
    for a in run.store.assignments():
        if a["phase"] == "review":
            h.report_review(run, a["worker"])
            h.read(run, a["worker"])
            h.call(run, a["worker"], "advance_phase")
    h.advance(run)
    report = json.loads((run.root / "phases.json").read_text())
    assert [p["phase"] for p in report["phases"]] == ["preprocess", "parse", "merge", "review"]
    assert all(p["finished_at"] is not None for p in report["phases"])
    review = report["phases"][-1]
    assert review["batch_reports"] == 3
    assert review["calls_by_origin"]["health"] == 1
    assert review["repeated_delivered_items"] >= 2
    assert review["fresh_conclusions"] == 3 and review["carried_conclusions"] == 0
    assert review["delivered_tokens"] > 0
    assert 0 <= review["workers"]["reviewer-1"]["phase_fraction"] <= 1
    events = [json.loads(row[0]) for row in run.db.execute("SELECT body FROM events ORDER BY seq")]
    assert parallel_metrics(run.store, events) == report
    assert report["token_usage"] is None  # No inference from tool payload sizes.
    assert (run.root / "events.jsonl").read_text().count("\n") == len(events)
    # Observer traffic after publication is not part of the frozen export prefix.
    run.call(h.who(run, "coordinator"), "workflow_status", origin="health")
    assert audit_run(run.root)["verified"]


@pytest.mark.parametrize(
    "filename",
    [
        "batches.json",
        "assignments.json",
        "source-reports.json",
        "sources.json",
        "review.json",
        "coverage.json",
        "changes.json",
        "report.json",
        "phases.json",
        "protocol.json",
        "events.jsonl",
    ],
)
def test_audit_rejects_changed_export_even_when_database_is_intact(run, filename):
    h.merged(run)
    for a in run.store.assignments():
        if a["phase"] == "review":
            h.report_review(run, a["worker"])
            h.call(run, a["worker"], "advance_phase")
    h.advance(run)
    assert audit_run(run.root)["verified"]
    path = run.root / filename
    if filename == "report.json":
        report = json.loads(path.read_text())
        report["export_event_seq"] = 1
        path.write_text(json.dumps(report))
    else:
        path.write_text('{"tampered": true}\n')
    with pytest.raises(WorkflowError, match="export"):
        audit_run(run.root)
