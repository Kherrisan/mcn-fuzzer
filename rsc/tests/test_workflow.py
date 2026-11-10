"""Publication limits and shared invariants; historical decoding stays audit-only.

The worker/merge/receipt invariants formerly exercised through the removed singleton
API now live in test_parallel_workflow, test_workflow_store and test_named_mutations.
"""

import copy
import json

import pytest
import test_parallel_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.sources import build_batches, token_count, validate_batches

run = h.run


def test_bounded_unicode_ranges():
    doc = {
        "blocks": [
            {"block_id": "b", "order": 0, "section_path": ["1"], "text": "协议消息😀\n" * 900}
        ]
    }
    batches = build_batches({"doc": doc}, 150, 200)
    assert len(batches) > 1
    assert all(token_count(b) <= 200 for b in batches)
    validate_batches({"doc": doc}, batches, 200)
    broken = copy.deepcopy(batches)
    broken[0]["segments"][0]["start"] += 1
    with pytest.raises(ValueError):
        validate_batches({"doc": doc}, broken, 200)


def test_reference_gate_rejects_missing_and_ambiguous_names():
    from protocol_samples import base, interactions

    from rsc.protocol.contract import COLLECTIONS
    from rsc.workflow.model import reference_errors

    model = base()
    model.update(interactions(model))
    records = {
        str(i): {"kind": kind, "value": value}
        for i, (kind, value) in enumerate(
            (kind, value) for kind in COLLECTIONS for value in model.get(kind, [])
        )
    }
    step = next(r["value"] for r in records.values() if r["kind"] == "steps")
    step["receiver_name"] = "ghost"
    assert any("ghost" in e for e in reference_errors(records))
    party = next(copy.deepcopy(r) for r in records.values() if r["kind"] == "parties")
    records["duplicate"] = party
    assert any("ambiguous" in e for e in reference_errors(records))


def test_subsections_merge_but_appendices_and_documents_keep_boundaries():
    doc = {
        "blocks": [
            {"block_id": "intro", "order": 0, "section_path": ["1"], "text": "Introduction"},
            {
                "block_id": "sub1",
                "order": 1,
                "section_path": ["1", "1"],
                "text": "Client sends HELLO.",
            },
            {
                "block_id": "table",
                "order": 2,
                "section_path": ["1", "2"],
                "block_type": "table",
                "text": "Code | Meaning\n1 | HELLO\n2 | WELCOME",
            },
            {
                "block_id": "appendix",
                "order": 3,
                "section_path": ["A"],
                "text": "Appendix requirements",
            },
        ]
    }
    documents = {"first": doc, "second": copy.deepcopy(doc)}
    batches = build_batches(documents, 600, 1000)
    assert len(batches) == 4
    assert {part["block_id"] for part in batches[0]["segments"]} == {"intro", "sub1", "table"}
    assert batches[0]["segments"][-1]["section_path"] == ["1", "2"]
    assert batches[1]["section_path"] == ["A"]
    assert batches[2]["doc_id"] == "second"
    validate_batches(documents, batches, 1000)


def test_audit_labels_and_bounds_legacy_pages():
    from rsc.workflow.audit import delivery

    rows = [{"record_id": str(i), "value": "read"} for i in range(51)]
    with pytest.raises(WorkflowError, match="legacy page"):
        delivery(rows, [0], [])
    assert delivery(rows, [0, 50], [])["tracking"] == "legacy_offsets"
    with pytest.raises(WorkflowError, match="budget"):
        delivery([{"value": "large output " * 20000}], [0], [])


def test_legacy_phase_metrics_are_explicit_observed_timings():
    from rsc.workflow.metrics import phase_metrics

    events = [
        {"dispatch_id": "d", "tool": "read_batch", "time": 10, "elapsed_ms": 2},
        {
            "dispatch_id": "d",
            "tool": "advance_phase",
            "time": 13,
            "elapsed_ms": 4,
            "phase": "parse",
            "round": 0,
            "revision": 1,
            "role": "parser",
        },
    ]
    report = phase_metrics({}, events)[0]
    assert report["elapsed_seconds"] == 3
    assert report["timing_origin"] == "first_observed_tool"
    assert report["tool_elapsed_ms"] == 6


def test_phase_metrics_separate_observers_and_repeated_reads():
    from rsc.workflow.metrics import phase_metrics

    events = []
    for i, (tool, origin, ids) in enumerate(
        [
            ("workflow_status", "supervisor", []),
            ("read_batches", "agent", ["batch-1", "batch-3"]),
            ("read_batch", "agent", ["batch-1"]),
            ("advance_phase", "agent", []),
        ]
    ):
        event = {
            "dispatch_id": "d",
            "tool": tool,
            "origin": origin,
            "phase": "parse",
            "role": "parser",
            "round": 0,
            "revision": 1,
            "time": i + 1,
            "elapsed_ms": 100,
            "status": "ok",
        }
        if ids:
            event["delivery"] = {"delivered_ids": ids, "tokens": 20}
        events.append(event)
    report = phase_metrics({}, events)[0]
    assert report["calls_by_origin"] == {"supervisor": 1, "agent": 3}
    assert report["delivered_tokens"] == 40
    assert report["delivered_items"] == 3
    assert report["repeated_delivered_items"] == 1
    assert report["non_tool_wall_seconds"] == pytest.approx(2.6)


def finding(w):
    status = h.claim(w, "reviewer-1")
    sources = h.read(w, "reviewer-1")
    citation = sources[status["assignment"]["batches"][0]]["batch"]["segments"][0]["citation"]
    name = h.call(
        w,
        "reviewer-1",
        "submit_findings",
        revision=1,
        findings=[
            {
                "name": "prerequisite",
                "requirement": "Client sends HELLO",
                "description": "Prerequisite is unclear",
                "objects": [h.CLIENT],
                "evidence": [citation],
                "acceptance": "Compare the prerequisite with the source",
            }
        ],
    )["names"][0]
    for a in w.store.assignments():
        if a["phase"] == "review":
            h.report_review(w, a["worker"], finding=name if a["worker"] == "reviewer-1" else None)
            h.call(w, a["worker"], "advance_phase")
    return name, citation


def test_last_round_source_gate_and_explicit_partial_publication(run):
    run.db.execute("UPDATE workflow SET body=json_set(body,'$.max_review_rounds',1)")
    h.merged(run)
    finding(run)
    path = next((run.root / "snapshots").glob("*.json"))
    original = path.read_text()
    path.write_text("{}")
    with pytest.raises(WorkflowError, match="snapshot"):
        h.advance(run)
    assert not (run.root / "protocol.json").exists()
    path.write_text(original)
    h.advance(run)
    assert run.data["phase"] == "published" and run.data["review_limit_reached"]
    assert not audit_run(run.root)["complete"]


def test_three_reviews_preserve_rejections_and_unchanged_conclusions(run):
    run.db.execute("UPDATE workflow SET body=json_set(body,'$.max_review_rounds',3)")
    h.merged(run)
    name, citation = finding(run)
    h.advance(run)
    for round_ in (2, 3):
        h.claim(run, "parser-1")
        h.read(run, "parser-1")
        h.call(
            run,
            "parser-1",
            "submit_resolutions",
            revision=0,
            resolutions=[
                {
                    "name": name,
                    "status": "not_applicable",
                    "description": "No extra prerequisite found",
                    "objects": [h.CLIENT],
                    "evidence": [citation],
                }
            ],
        )
        h.call(run, "parser-1", "advance_phase")
        h.advance(run)
        h.claim(run, "parser-1")
        h.call(run, "parser-1", "advance_phase")
        h.advance(run)
        status = h.claim(run, "reviewer-1")
        assert not status["pending_batches"] and not status["pending_checks"]
        h.read(run, "reviewer-1")
        h.call(run, "reviewer-1", "query_model")
        args = {
            "revision": round_,
            "reviews": [
                {
                    "name": name,
                    "accepted": False,
                    "reason": "The prerequisite remains unclear",
                    "evidence": [citation],
                }
            ],
        }
        result = h.call(run, "reviewer-1", "review_resolutions", **args)
        assert h.call(run, "reviewer-1", "review_resolutions", **args) == result
        h.call(run, "reviewer-1", "advance_phase")
        h.advance(run)
    f = run.findings()[name]
    assert f["status"] == "open" and len(f["history"]) == 2
    report = audit_run(run.root)
    assert not report["complete"] and [r["carried"] for r in report["reviews"]] == [0, 3, 3]


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_resolution_tracks_merge_rename_or_deletion_without_rewriting_fix(run, changed, operation):
    h.merged(run)
    name, citation = finding(run)
    h.advance(run)
    h.claim(run, "parser-1")
    h.read(run, "parser-1")
    changes = [
        {
            "op": "add",
            "kind": "parties",
            "value": {"name": "peer", "description": "receiver"},
            "evidence": [citation],
        }
    ]
    if changed:
        changes.append(
            {
                "op": "patch",
                **h.CLIENT,
                "value": {"description": "request sender"},
                "evidence": [citation],
            }
        )
    h.call(
        run, "parser-1", "apply_changes", expected_revision=0, operation_id="fix", changes=changes
    )
    h.call(
        run,
        "parser-1",
        "submit_resolutions",
        revision=1,
        resolutions=[
            {
                "name": name,
                "status": "fixed",
                "description": "Clarified role",
                "objects": [h.CLIENT],
                "evidence": [citation],
            }
        ],
    )
    original = copy.deepcopy(run.findings()[name]["resolution"])
    h.call(run, "parser-1", "advance_phase")
    h.advance(run)
    h.claim(run, "parser-1")
    h.read(run, "parser-1")
    scope = "fix-1/parser-1" if changed else "base"
    change = (
        {
            "op": "rename",
            "source": {"scope": scope, **h.CLIENT},
            "name": "user",
            "evidence": [citation],
        }
        if operation == "rename"
        else {"op": "delete", "scope": scope, **h.CLIENT, "reason": "Superseded role"}
    )
    h.call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="merge",
        changes=[change],
    )
    h.call(run, "parser-1", "advance_phase")
    h.advance(run)
    for a in run.store.assignments():
        if a["phase"] != "review" or a["round"] != 2:
            continue
        h.report_review(run, a["worker"])
        if a["worker"] == "reviewer-1":
            view = h.call(run, "reviewer-1", "list_findings")["items"][0]
            associations = view["resolution_associations"]
            assert associations[0]["source"] == {"scope": "fix-1/parser-1", **h.CLIENT}
            assert associations[0]["final"] == (
                {"kind": "parties", "target": {"name": "user"}} if operation == "rename" else None
            )
            h.call(
                run,
                "reviewer-1",
                "review_resolutions",
                revision=2,
                reviews=[
                    {
                        "name": name,
                        "accepted": True,
                        "reason": "Checked final role",
                        "evidence": [citation],
                    }
                ],
            )
        h.call(run, a["worker"], "advance_phase")
    h.advance(run)
    assert run.findings()[name]["resolution"] == original
    assert audit_run(run.root)["complete"]


def test_failed_tools_are_audited_and_export_cannot_bypass_review(run):
    with pytest.raises(WorkflowError):
        h.call(
            run,
            "parser-1",
            "apply_changes",
            expected_revision=0,
            operation_id="before-claim",
            changes=[],
        )
    event = json.loads(
        run.db.execute("SELECT body FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
    )
    assert event["status"] == "rejected" and event["worker"] == "parser-1"
    assert not run.store.draft("parse-0/parser-1")[1]
    with pytest.raises(WorkflowError, match="independent review"):
        run.export()
    assert not (run.root / "protocol.json").exists()


def test_page_chain_requires_all_model_context_and_fixed_revision(run):
    status = h.claim(run, "parser-1")
    sources = h.read(run, "parser-1")
    citation = next(iter(sources.values()))["batch"]["segments"][0]["citation"]
    h.call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="many",
        changes=[
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": f"role-{i}", "description": "role " * 40},
                "evidence": [citation],
            }
            for i in range(40)
        ],
    )
    h.call(
        run,
        "parser-1",
        "complete_batch",
        revision=1,
        reports=[
            {
                "batch_id": b,
                "receipt": sources[b]["receipt"],
                "report": {"summary": "Extracted roles", "objects": []},
            }
            for b in status["assignment"]["batches"]
        ],
    )
    h.call(run, "parser-1", "advance_phase")
    h.parse(run, "parser-2")
    h.advance(run)
    h.claim(run, "parser-1")
    h.call(run, "parser-1", "advance_phase")
    h.advance(run)
    status = h.claim(run, "reviewer-1")
    sources = h.read(run, "reviewer-1")
    first = h.call(run, "reviewer-1", "query_model", max_tokens=1000)
    assert first["next_cursor"] is not None
    objects = [
        {"kind": r["kind"], "target": {"name": r["value"]["name"]}} for r in run.snapshot().values()
    ]
    report = {
        "summary": "Compared all roles",
        "objects": objects,
        "requirements": [
            {
                "requirement": "Roles have source definitions",
                "objects": objects,
                "result": "pass",
                "findings": [],
            }
        ],
    }
    reports = [
        {"batch_id": b, "receipt": sources[b]["receipt"], "report": report}
        for b in status["pending_batches"]
    ]
    reports += [{"check_name": c, "report": report} for c in status["pending_checks"]]
    with pytest.raises(WorkflowError, match="complete associated model"):
        h.call(run, "reviewer-1", "complete_batch", revision=1, reports=reports)
    with pytest.raises(WorkflowError, match="require their revision"):
        h.call(run, "reviewer-1", "query_model", cursor=first["next_cursor"])
    names = [r["value"]["name"] for r in first["items"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = h.call(run, "reviewer-1", "query_model", cursor=cursor, revision=1, max_tokens=1000)
        names += [r["value"]["name"] for r in page["items"]]
        cursor = page["next_cursor"]
    assert len(names) == len(set(names)) == len(run.snapshot())
    h.call(run, "reviewer-1", "complete_batch", revision=1, reports=reports)
    changes, cursor = [], 0
    while cursor is not None:
        page = h.call(
            run,
            "reviewer-1",
            "get_changes",
            from_revision=0,
            revision=1,
            cursor=cursor,
            max_tokens=1000,
        )
        changes.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(changes) == len(run.snapshot())
