"""Declared cross-range relationships contribute independent source/model context."""

import copy
import json

import pytest
import test_parallel_workflow as h

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.review import object_ref


@pytest.fixture
def relationship(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text(
        "RFC 9999\n\n1. Client\n\nThe client uses HELLO state described in section 2.\n\n"
        "2. Greeting\n\nHELLO contains a token.\n\n3. Independent\n\nOTHER is unrelated.\n"
    )
    w = Workflow.create(
        tmp_path / "relations", [source], "Example", parse_workers=3, review_workers=3
    )
    h.call(w, "coordinator", "prepare_sources")
    h.advance(w)
    assert len(w.batches) == 3
    yield w
    w.db.close()


def prepare(w, missing=False):
    definitions = [
        ("parties", {"name": "client", "description": "sender"}),
        ("messages", {"name": "HELLO", "associated_fields": [{"name": "token"}]}),
        ("messages", {"name": "OTHER", "associated_fields": []}),
    ]
    for a, (kind, value) in zip(w.store.assignments(), definitions, strict=True):
        h.claim(w, a["worker"])
        batch = a["batches"][0]
        page = h.read(w, a["worker"], [batch])[batch]
        evidence = [page["batch"]["segments"][0]["citation"]]
        h.call(
            w,
            a["worker"],
            "apply_changes",
            expected_revision=0,
            operation_id="definition",
            changes=[{"op": "add", "kind": kind, "value": value, "evidence": evidence}],
        )
        report = {
            "summary": "Extracted source definition",
            "objects": [{"kind": kind, "target": {"name": value["name"]}}],
        }
        if a["worker"] == "parser-1":
            report["obligations"] = [
                {
                    "description": "PARSER_PRIVATE_EXPLANATION: integrate remote state",
                    "objects": [
                        {"kind": "messages", "target": {"name": "UNKNOWN" if missing else "HELLO"}}
                    ],
                    "evidence": evidence,
                }
            ]
        h.call(
            w,
            a["worker"],
            "complete_batch",
            revision=1,
            reports=[{"batch_id": batch, "receipt": page["receipt"], "report": report}],
        )
        h.call(w, a["worker"], "advance_phase")
    h.advance(w)
    h.claim(w, "parser-1")
    h.read(w, "parser-1")


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_foreign_obligation_survives_merge_and_requires_both_source_regions(
    relationship, operation
):
    w = relationship
    prepare(w)
    b1, b2, b3 = [b["batch_id"] for b in w.batches]
    evidence = [{"batch_id": b2, "block_id": w.batches[1]["segments"][0]["block_id"]}]
    source = {"scope": "parse-0/parser-2", "kind": "messages", "target": {"name": "HELLO"}}
    changes = [
        (
            {"op": "rename", "source": source, "name": "GREETING", "evidence": evidence}
            if operation == "rename"
            else {"op": "delete", **source, "reason": "No longer a model unit"}
        ),
        {
            "op": "resolve",
            "name": "obligation-1",
            "decision": "Checked against source",
            "evidence": evidence,
        },
    ]
    h.call(
        w,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="integrate",
        changes=changes,
    )
    association = h.call(w, "parser-1", "query_model", view="reports")["items"][0][
        "obligation_associations"
    ][0][0]["associations"][0]
    assert association["final"] == (
        {"kind": "messages", "target": {"name": "GREETING"}} if operation == "rename" else None
    )
    h.call(w, "parser-1", "advance_phase")
    h.advance(w)
    status = h.claim(w, "reviewer-1")
    assert "PARSER_PRIVATE_EXPLANATION" not in json.dumps(status)
    a = w.store.assignment("review-1/reviewer-1")
    assert a["contexts"][b1]["batches"] == [b1, b2]
    assert b3 not in a["contexts"][b1]["batches"]
    source = h.read(w, "reviewer-1", [b1])[b1]
    model = h.call(w, "reviewer-1", "query_model", batch_ids=[b1])
    names = {r["value"]["name"] for r in model["items"]}
    assert names == ({"client", "GREETING"} if operation == "rename" else {"client"})
    report = {
        "summary": "Compared original requirements",
        "objects": [object_ref(r) for r in model["items"]],
        "requirements": [
            {
                "requirement": "Client uses greeting state",
                "objects": [],
                "result": "pass",
                "findings": [],
            }
        ],
    }
    args = {
        "revision": 1,
        "reports": [{"batch_id": b1, "receipt": source["receipt"], "report": report}],
    }
    with pytest.raises(WorkflowError, match="source delivery"):
        h.call(w, "reviewer-1", "complete_batch", **args)
    h.read(w, "reviewer-1", [b2])
    h.call(w, "reviewer-1", "complete_batch", **args)
    for a in w.store.assignments():
        if a["phase"] == "review":
            h.report_review(w, a["worker"])
            h.call(w, a["worker"], "advance_phase")
    h.advance(w)
    assert audit_run(w.root)["verified"]
    w.db.execute("UPDATE assignments SET body=json_remove(body,'$.contexts') WHERE phase='review'")
    with pytest.raises(WorkflowError, match="ownership"):
        audit_run(w.root)


def test_unbound_relationship_expands_review_instead_of_claiming_independence(relationship):
    w = relationship
    prepare(w, missing=True)
    evidence = [
        {"batch_id": w.batches[0]["batch_id"], "block_id": w.batches[0]["segments"][0]["block_id"]}
    ]
    h.call(
        w,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="decision",
        changes=[
            {
                "op": "resolve",
                "name": "obligation-1",
                "decision": "Source underspecified",
                "evidence": evidence,
            }
        ],
    )
    h.call(w, "parser-1", "advance_phase")
    h.advance(w)
    for a in w.store.assignments():
        if a["phase"] == "review":
            for context in a["contexts"].values():
                assert len(context["records"]) == 3 and len(context["batches"]) == 3
            assert "PARSER_PRIVATE_EXPLANATION" not in json.dumps(a)


def test_obligation_and_resolution_provenance_survive_successive_fix_merges_and_restart(
    relationship,
):
    w = relationship
    prepare(w)
    b1, b2, b3 = [b["batch_id"] for b in w.batches]
    evidence = [{"batch_id": b2, "block_id": w.batches[1]["segments"][0]["block_id"]}]
    h.call(
        w,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="integrate",
        changes=[
            {
                "op": "resolve",
                "name": "obligation-1",
                "decision": "Verified remote state",
                "evidence": evidence,
            }
        ],
    )
    h.call(w, "parser-1", "advance_phase")
    h.advance(w)
    original_reports = list(w.db.execute("SELECT body FROM reports WHERE phase='parse'"))
    original_reports = [row[0] for row in original_reports]
    resolutions = {}
    try:
        for round_, operation, message in ((1, "rename", "HELLO"), (2, "delete", "GREETING")):
            h.claim(w, "reviewer-1")
            sources = h.read(w, "reviewer-1")
            h.call(w, "reviewer-1", "query_model")
            # Accept the previous Fix against the current snapshot before creating a new finding.
            if round_ == 2:
                previous = next(iter(resolutions))
                h.call(
                    w,
                    "reviewer-1",
                    "review_resolutions",
                    revision=round_,
                    reviews=[
                        {
                            "name": previous,
                            "accepted": True,
                            "reason": "Verified rename",
                            "evidence": evidence,
                        }
                    ],
                )
            ref = {"kind": "messages", "target": {"name": message}}
            name = h.call(
                w,
                "reviewer-1",
                "submit_findings",
                revision=round_,
                findings=[
                    {
                        "name": f"greeting-{round_}",
                        "requirement": "Compare remote state",
                        "description": "Greeting needs clarification",
                        "objects": [ref],
                        "evidence": [sources[b1]["batch"]["segments"][0]["citation"]],
                        "acceptance": "Clarify greeting against the source",
                    }
                ],
            )["names"][0]
            for worker in ("reviewer-1", "reviewer-2", "reviewer-3"):
                h.report_review(w, worker, finding=name if worker == "reviewer-1" else None)
                h.call(w, worker, "advance_phase")
            h.advance(w)
            h.claim(w, "parser-1")
            h.read(w, "parser-1")
            h.call(
                w,
                "parser-1",
                "apply_changes",
                expected_revision=0,
                operation_id="clarify",
                changes=[
                    {
                        "op": "patch",
                        **ref,
                        "value": {
                            "associated_fields": [{"name": "token"}, {"name": f"context{round_}"}]
                        },
                        "evidence": evidence,
                    }
                ],
            )
            h.call(
                w,
                "parser-1",
                "submit_resolutions",
                revision=1,
                resolutions=[
                    {
                        "name": name,
                        "status": "fixed",
                        "description": "Clarified",
                        "objects": [ref],
                        "evidence": evidence,
                    }
                ],
            )
            resolutions[name] = copy.deepcopy(w.findings()[name]["resolution"])
            h.call(w, "parser-1", "advance_phase")
            h.advance(w)
            h.claim(w, "parser-1")
            h.read(w, "parser-1")
            scoped = {"scope": f"fix-{round_}/parser-1", **ref}
            change = (
                {"op": "rename", "source": scoped, "name": "GREETING", "evidence": evidence}
                if operation == "rename"
                else {"op": "delete", **scoped, "reason": "Superseded greeting"}
            )
            args = {"expected_revision": 0, "operation_id": "integrate", "changes": [change]}
            applied = h.call(w, "parser-1", "apply_changes", **args)
            # Resume an interrupted merge and replay its acknowledged operation atomically.
            w.db.close()
            w = Workflow(w.root)
            assert h.call(w, "parser-1", "apply_changes", **args) == applied
            h.call(w, "parser-1", "advance_phase")
            h.advance(w)
            assignment = w.store.assignment(f"review-{round_ + 1}/reviewer-1")
            context = assignment["contexts"][b1]
            assert context["batches"] == [b1, b2] and b3 not in context["batches"]
            model = w.snapshot(round_ + 1)
            assert {model[key]["value"]["name"] for key in context["records"]} == (
                {"client", "GREETING"} if operation == "rename" else {"client"}
            )
            assert w.findings()[name]["resolution"] == resolutions[name]
            assert [
                row[0] for row in w.db.execute("SELECT body FROM reports WHERE phase='parse'")
            ] == original_reports
        for worker in ("reviewer-1", "reviewer-2", "reviewer-3"):
            h.report_review(w, worker)
            if worker == "reviewer-1":
                h.call(
                    w,
                    worker,
                    "review_resolutions",
                    revision=3,
                    reviews=[
                        {
                            "name": name,
                            "accepted": True,
                            "reason": "Verified deletion",
                            "evidence": evidence,
                        }
                    ],
                )
            h.call(w, worker, "advance_phase")
        h.advance(w)
        assert audit_run(w.root)["complete"]
        assert all(w.findings()[key]["resolution"] == value for key, value in resolutions.items())
    finally:
        w.db.close()
