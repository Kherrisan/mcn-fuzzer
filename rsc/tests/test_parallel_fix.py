"""Independent Fix patches that discover shared writes must merge before re-review."""

from concurrent.futures import ThreadPoolExecutor

import pytest
import test_parallel_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.audit import audit_run

run = h.run


def test_parallel_fixes_detect_new_overlap_without_changing_their_shared_base(run):
    h.merged(run)
    base = run.snapshot(1)
    findings = {}
    citations = []
    for worker, message in (("reviewer-1", "HELLO"), ("reviewer-2", "BYE")):
        status = h.claim(run, worker)
        sources = h.read(run, worker)
        citation = sources[status["pending_batches"][0]]["batch"]["segments"][0]["citation"]
        citations.append(citation)
        h.call(run, worker, "query_model")
        name = h.call(
            run,
            worker,
            "submit_findings",
            revision=1,
            findings=[
                {
                    "name": "missing-message",
                    "requirement": f"Represent {message}",
                    "description": f"Missing {message}",
                    "acceptance": "Define the message",
                    "objects": [{"kind": "messages", "target": {"name": message}}],
                    "evidence": [citation],
                }
            ],
        )["names"][0]
        findings[name] = message
        h.report_review(run, worker, finding=name)
        h.call(run, worker, "advance_phase")
    h.advance(run)
    assignments = [a for a in run.store.assignments() if a["phase"] == "fix"]
    assert len(assignments) == 2 and all(a["base_revision"] == 1 for a in assignments)

    def fix(a):
        worker = a["worker"]
        h.claim(run, worker)
        h.read(run, worker)
        name = a["findings"][0]
        message = findings[name]
        h.call(
            run,
            worker,
            "apply_changes",
            expected_revision=0,
            operation_id="fix",
            changes=[
                {
                    "op": "add",
                    "kind": "messages",
                    "value": {"name": message, "associated_fields": []},
                    "evidence": citations,
                },
                {
                    "op": "patch",
                    **h.CLIENT,
                    "value": {"description": f"sender of {message}"},
                    "evidence": citations,
                },
            ],
        )
        h.call(
            run,
            worker,
            "submit_resolutions",
            revision=1,
            resolutions=[
                {
                    "name": name,
                    "status": "fixed",
                    "description": "Defined message",
                    "objects": [h.CLIENT, {"kind": "messages", "target": {"name": message}}],
                    "evidence": citations,
                }
            ],
        )
        h.call(run, worker, "advance_phase")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fix, assignments))
    assert run.snapshot(1) == base
    patches = {a["name"]: run.store.draft(a["draft"])[1] for a in assignments}
    assert all(run.store.draft(a["draft"])[0]["sealed"] for a in assignments)
    h.advance(run)
    h.claim(run, "parser-1")
    conflicts = h.call(run, "parser-1", "query_model", view="conflicts")["items"]
    overlap = next(c for c in conflicts if c["type"] == "patch_conflict")
    assert {write["scope"] for write in overlap["writes"]} == set(patches)
    assert all(write["before"]["value"]["name"] == "client" for write in overlap["writes"])
    assert {write["after"]["value"]["description"] for write in overlap["writes"]} == {
        "sender of HELLO",
        "sender of BYE",
    }
    with pytest.raises(WorkflowError, match="gate rejected.*patch_conflict"):
        h.call(run, "parser-1", "advance_phase")
    h.read(run, "parser-1")
    h.call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="integrate",
        changes=[
            {
                "op": "merge",
                "sources": [{"scope": a["name"], **h.CLIENT} for a in assignments],
                "value": {"name": "client", "description": "sender"},
                "evidence": citations,
            },
            {
                "op": "resolve",
                "name": overlap["name"],
                "decision": "One shared sender role",
                "evidence": citations,
            },
        ],
    )
    h.call(run, "parser-1", "advance_phase")
    h.advance(run)
    assert run.snapshot(1) == base
    assert {a["name"]: run.store.draft(a["draft"])[1] for a in assignments} == patches
    for worker in ("reviewer-1", "reviewer-2"):
        status, _ = h.report_review(run, worker)
        assigned = status["assignment"]["findings"]
        assert len(assigned) == 1
        h.call(
            run,
            worker,
            "review_resolutions",
            revision=2,
            reviews=[
                {
                    "name": assigned[0],
                    "accepted": True,
                    "reason": "Compared merged result",
                    "evidence": citations,
                }
            ],
        )
        h.call(run, worker, "advance_phase")
    h.advance(run)
    assert audit_run(run.root)["complete"]
