"""End-to-end coordinator/worker protocol, including independent Fix acceptance."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.review import review_errors
from rsc.workflow.tools import CONTRACTS, schema, validate_arguments


def who(w, worker):
    return w.authenticate((w.root / f"{worker}.token").read_text())


def call(w, worker, tool, **args):
    return w.call(who(w, worker), tool, args)


def claim(w, worker):
    a = w.store.assignment(who(w, worker).assignment)
    return call(w, worker, "claim_assignment", name=a["name"], attempt=a["attempt"])


def advance(w):
    return call(w, "coordinator", "advance_phase")


def read(w, worker, batches=None):
    remaining = batches or [b["batch_id"] for b in w.batches]
    values = {}
    while remaining:
        page = call(w, worker, "read_batches", batch_ids=remaining)
        values.update({b["batch"]["batch_id"]: b for b in page["batches"]})
        remaining = page["remaining"]
    return values


CLIENT = {"kind": "parties", "target": {"name": "client"}}


@pytest.fixture
def run(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text(
        "RFC 9999\n\n1. Introduction\n\nThe client sends HELLO.\n\n"
        "2. Message\n\nHELLO contains a username.\n"
    )
    w = Workflow.create(
        tmp_path / "parallel", [source], "Example", parse_workers=2, review_workers=2
    )
    call(w, "coordinator", "prepare_sources")
    advance(w)
    yield w
    w.db.close()


def parse(w, worker):
    status = claim(w, worker)
    sources = read(w, worker, status["assignment"]["batches"])
    citation = next(iter(sources.values()))["batch"]["segments"][0]["citation"]
    call(
        w,
        worker,
        "apply_changes",
        expected_revision=0,
        operation_id="role",
        changes=[
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": "client", "description": "sender"},
                "evidence": [citation],
            },
        ],
    )
    call(
        w,
        worker,
        "complete_batch",
        revision=1,
        reports=[
            {
                "batch_id": b,
                "receipt": sources[b]["receipt"],
                "report": {"summary": "Extracted sender role", "objects": [CLIENT]},
            }
            for b in status["assignment"]["batches"]
        ],
    )
    call(w, worker, "advance_phase")


def merged(w):
    workers = [a["worker"] for a in w.store.assignments() if a["phase"] == "parse"]
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        list(pool.map(lambda worker: parse(w, worker), workers))
    assert w.data["phase"] == "parse"  # Last parser cannot advance the global phase.
    advance(w)
    claim(w, "parser-1")
    result = call(w, "parser-1", "query_model", view="conflicts")
    assert result["items"] == []
    call(w, "parser-1", "advance_phase")
    advance(w)


def report_review(w, worker, *, finding=None):
    status = claim(w, worker)
    sources = read(w, worker)
    model = call(w, worker, "query_model")
    objects = [{"kind": r["kind"], "target": {"name": r["value"]["name"]}} for r in model["items"]]
    report = {
        "summary": "Compared source requirements with model",
        "objects": objects,
        "requirements": [
            {
                "requirement": "Client is the sending party",
                "objects": objects,
                "result": "finding" if finding else "pass",
                "findings": [finding] if finding else [],
            }
        ],
    }
    reports = [
        {"batch_id": b, "receipt": sources[b]["receipt"], "report": report}
        for b in status["pending_batches"]
    ]
    reports += [{"check_name": c, "report": report} for c in status["pending_checks"]]
    call(w, worker, "complete_batch", revision=status["revision"], reports=reports)
    return status, sources


def test_parallel_parse_merge_and_independent_review_publish(run):
    merged(run)
    assert run.data["phase"] == "review" and run.data["revision"] == 1
    for a in run.store.assignments():
        if a["phase"] == "review":
            report_review(run, a["worker"])
            call(run, a["worker"], "advance_phase")
    assert not (run.root / "protocol.json").exists()
    advance(run)
    assert run.data["phase"] == "published"
    protocol = json.loads((run.root / "protocol.json").read_text())
    assert protocol["meta"]["complete"] is True
    assert protocol["parties"] == [{"name": "client", "description": "sender"}]
    assert {a["worker"] for a in run.store.assignments() if a["phase"] == "parse"} == {
        "parser-1",
        "parser-2",
    }
    before = (run.root / "workflow.sqlite").stat().st_mtime_ns
    assert audit_run(run.root)["verified"]
    assert (run.root / "workflow.sqlite").stat().st_mtime_ns == before


def test_thirteen_tools_coordinator_privileges_and_no_legacy_entrypoints(run):
    assert len(CONTRACTS) == 13
    for name in CONTRACTS:
        Draft202012Validator.check_schema(schema(name)["inputSchema"])
    parser = who(run, "parser-1")
    assert run.allowed(parser) == {"claim_assignment"}
    for name in (
        "prepare_sources",
        "workflow_status",
        "check_gate",
        "read_batch",
        "complete_review_batch",
    ):
        with pytest.raises(WorkflowError):
            run.call(parser, name, {})
    claim(run, "parser-1")
    with pytest.raises(WorkflowError, match="different|exact|invalid|unknown"):
        validate_arguments(
            "apply_changes",
            {"expected_revision": 0, "operation_id": "bad", "changes": {"groups": []}},
        )


def test_reports_cannot_replace_model_delivery_or_independent_comparison(run):
    merged(run)
    status = claim(run, "reviewer-1")
    source = read(run, "reviewer-1")
    batch = status["assignment"]["batches"][0]
    args = {
        "revision": 1,
        "reports": [
            {
                "batch_id": batch,
                "receipt": source[batch]["receipt"],
                "report": {"summary": "looked", "objects": [CLIENT]},
            }
        ],
    }
    with pytest.raises(WorkflowError, match="model context"):
        call(run, "reviewer-1", "complete_batch", **args)
    call(run, "reviewer-1", "query_model", view="index")
    with pytest.raises(WorkflowError, match="model context"):
        call(run, "reviewer-1", "complete_batch", **args)
    call(run, "reviewer-1", "query_model")
    with pytest.raises(WorkflowError, match="actual source/model"):
        call(run, "reviewer-1", "complete_batch", **args)
    assert not run.store.completed(status["assignment"]["name"])
    with pytest.raises(WorkflowError, match="forbidden"):
        call(
            run, "reviewer-1", "apply_changes", expected_revision=1, operation_id="bad", changes=[]
        )


def test_sealed_pages_keep_the_same_drafts_as_other_workers_finish(run):
    claim(run, "parser-1")
    first = call(run, "parser-1", "query_model", view="sealed")
    assert first["items"] == []
    parse(run, "parser-2")
    resumed = Workflow(run.root)
    pinned = call(
        resumed,
        "parser-1",
        "query_model",
        view="sealed",
        revision=0,
        view_revision=first["view_revision"],
    )
    fresh = call(resumed, "parser-1", "query_model", view="sealed")
    resumed.db.close()
    assert pinned["items"] == []
    assert len(fresh["items"]) == 1
    assert fresh["items"][0]["scope"] == "parse-0/parser-2"
    assert fresh["view_revision"] > first["view_revision"]
    with pytest.raises(WorkflowError, match="view_revision"):
        call(run, "parser-1", "query_model", view="sealed", cursor=1, revision=0)


def test_merge_aliases_and_deleted_report_associations_are_atomic_and_auditable(run):
    parse(run, "parser-1")
    parse(run, "parser-2")
    originals = [tuple(row) for row in run.db.execute("SELECT * FROM reports")]
    advance(run)
    claim(run, "parser-1")
    sources = read(run, "parser-1")
    citation = next(iter(sources.values()))["batch"]["segments"][0]["citation"]
    changes = [
        {
            "op": "rename",
            "source": {"scope": "parse-0/parser-2", **CLIENT},
            "name": "endpoint",
            "evidence": [citation],
        }
    ]
    result = call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="rename",
        changes=changes,
    )
    assert (
        call(
            run,
            "parser-1",
            "apply_changes",
            expected_revision=0,
            operation_id="rename",
            changes=changes,
        )
        == result
    )
    reports = call(run, "parser-1", "query_model", view="reports")["items"]
    assert len(reports) == 2
    assert all(
        report["associations"][0]["final"]["target"]["name"] == "endpoint" for report in reports
    )
    # The original alias still selects the renamed candidate in a later transaction.
    call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=1,
        operation_id="rename-again",
        changes=[{**changes[0], "name": "peer"}],
    )
    scope = call(run, "parser-1", "query_model")["items"][0]["scope"]
    call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=2,
        operation_id="replace",
        changes=[
            {
                "op": "delete",
                "scope": scope,
                "kind": "parties",
                "target": {"name": "peer"},
                "reason": "Role is not independently required by the final model",
            },
            {
                "op": "add",
                "scope": "merge-0/parser-1",
                "kind": "messages",
                "value": {"name": "HELLO", "associated_fields": []},
                "evidence": [citation],
            },
        ],
    )
    reports = call(run, "parser-1", "query_model", view="reports")["items"]
    assert all(report["associations"][0]["final"] is None for report in reports)
    assert all(report["associations"][0]["removed_by"]["revision"] == 3 for report in reports)
    assert [tuple(row) for row in run.db.execute("SELECT * FROM reports")] == originals
    call(run, "parser-1", "advance_phase")
    advance(run)
    for a in run.store.assignments():
        if a["phase"] == "review":
            report_review(run, a["worker"])
            with pytest.raises(WorkflowError, match="only available during merge"):
                call(run, a["worker"], "query_model", view="reports")
            call(run, a["worker"], "advance_phase")
    advance(run)
    assert audit_run(run.root)["verified"]


def test_missing_message_can_be_fixed_but_only_reviewer_accepts(run):
    merged(run)
    status = claim(run, "reviewer-1")
    source = read(run, "reviewer-1")
    citation = source[status["assignment"]["batches"][0]]["batch"]["segments"][0]["citation"]
    call(run, "reviewer-1", "query_model")
    name = call(
        run,
        "reviewer-1",
        "submit_findings",
        revision=1,
        findings=[
            {
                "name": "missing-hello",
                "requirement": "The client sends HELLO",
                "description": "HELLO message missing",
                "objects": [{"kind": "messages", "target": {"name": "HELLO"}}],
                "evidence": [citation],
                "acceptance": "HELLO is represented with its username field",
            }
        ],
    )["names"][0]
    for a in run.store.assignments():
        if a["phase"] == "review":
            report_review(run, a["worker"], finding=name if a["worker"] == "reviewer-1" else None)
            call(run, a["worker"], "advance_phase")
    advance(run)
    assert run.data["phase"] == "fix"
    claim(run, "parser-1")
    read(run, "parser-1")
    hello = {"kind": "messages", "target": {"name": "HELLO"}}
    call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="hello",
        changes=[
            {
                "op": "add",
                "kind": "messages",
                "value": {"name": "HELLO", "associated_fields": [{"name": "username"}]},
                "evidence": [citation],
            }
        ],
    )
    call(
        run,
        "parser-1",
        "submit_resolutions",
        revision=1,
        resolutions=[
            {
                "name": name,
                "status": "fixed",
                "description": "Added HELLO and its field",
                "objects": [hello],
                "evidence": [citation],
            }
        ],
    )
    with pytest.raises(WorkflowError, match="forbidden"):
        call(run, "parser-1", "review_resolutions", revision=1, reviews=[])
    call(run, "parser-1", "advance_phase")
    advance(run)
    claim(run, "parser-1")
    call(run, "parser-1", "advance_phase")
    advance(run)
    assert run.data["phase"] == "review" and run.data["round"] == 2
    carried = run.store.assignment("review-2/reviewer-2")
    assert carried["status"] == "submitted"
    assert not run.db.execute(
        "SELECT 1 FROM receipts WHERE assignment=?", (carried["name"],)
    ).fetchone()
    assert not review_errors(run.store, carried, run.snapshot())
    # Restarting does not manufacture a new read or change the carried provenance.
    reopened = Workflow(run.root)
    assert not review_errors(reopened.store, carried, reopened.snapshot())
    reopened.db.close()
    old = run.db.execute(
        "SELECT * FROM receipts WHERE assignment='review-1/reviewer-2' AND batch=?",
        (carried["batches"][0],),
    ).fetchone()
    run.db.execute(
        "DELETE FROM receipts WHERE assignment=? AND attempt=? AND batch=?", tuple(old[:3])
    )
    assert any(
        "source was not fully delivered" in error
        for error in review_errors(run.store, carried, run.snapshot())
    )
    run.db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?)", tuple(old))
    for a in run.store.assignments():
        if a["phase"] == "review" and a["round"] == 2 and a["status"] != "submitted":
            report_review(run, a["worker"])
            if name in a["findings"]:
                with pytest.raises(WorkflowError, match="independent acceptance"):
                    call(run, a["worker"], "advance_phase")
                call(
                    run,
                    a["worker"],
                    "review_resolutions",
                    revision=2,
                    reviews=[
                        {
                            "name": name,
                            "accepted": True,
                            "reason": "Source and new HELLO fields match",
                            "evidence": [citation],
                        }
                    ],
                )
            call(run, a["worker"], "advance_phase")
    advance(run)
    assert run.publication_model()["meta"]["complete"]
    assert run.findings()[name]["history"][-1]["reviewer"] == "reviewer-1"
    assert audit_run(run.root)["reviews"][-1]["carried"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM history WHERE revision=0",
        "DELETE FROM operations",
        "DELETE FROM receipts WHERE assignment LIKE 'parse-%'",
        "DELETE FROM receipts WHERE assignment LIKE 'review-%'",
        "DELETE FROM deliveries WHERE family='model'",
        "DELETE FROM conclusions",
        "UPDATE assignments SET body=json_set(body,'$.checks',json('[]')) WHERE phase='review'",
        "UPDATE snapshots SET body=json_set(body,'$.name_map',json('[]'))",
        'UPDATE reports SET body=json_set(body,\'$.objects\',json(\'[{"kind":"parties",'
        '"target":{"name":"invented"}}]\')) WHERE phase=\'parse\'',
    ],
)
def test_parallel_audit_rejects_missing_or_changed_evidence(run, sql):
    merged(run)
    for a in run.store.assignments():
        if a["phase"] == "review":
            report_review(run, a["worker"])
            call(run, a["worker"], "advance_phase")
    advance(run)
    assert audit_run(run.root)["verified"]
    run.db.execute(sql)
    with pytest.raises(ValueError):
        audit_run(run.root)
