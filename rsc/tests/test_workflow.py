"""Workflow invariants: no phase bypass, no unreviewed publication, bounded repair."""

import copy
import json

import pytest

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.sources import build_batches, token_count, validate_batches


def claim(w):
    a = w.data["assignment"]
    return w.call(w.actor, "claim_assignment", {"dispatch_id": a["dispatch_id"]})


def call(w, tool, **args):
    return w.call(w.actor, tool, args)


@pytest.fixture
def task(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Introduction\n\nThe client sends HELLO to the server.\n")
    return Workflow.create(tmp_path / "task", [source], "Example")


def parsed(w):
    claim(w)
    call(w, "prepare_sources")
    call(w, "advance_phase")
    claim(w)
    rid = None
    for b in w.data["batches"]:
        read = call(w, "read_batch", batch_id=b["batch_id"])
        evidence = [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}]
        if rid is None:
            result = call(
                w,
                "apply_changes",
                expected_revision=0,
                operation_id="initial",
                changes=[
                    {
                        "op": "add",
                        "kind": "parties",
                        "value": {"name": "client", "description": "sender"},
                        "evidence": evidence,
                    }
                ],
            )
            rid = result["record_ids"][0]
        call(
            w,
            "complete_batch",
            batch_id=b["batch_id"],
            receipt_id=read["receipt_id"],
            report={"summary": "Extracted sender role", "record_ids": [rid]},
        )
    call(w, "advance_phase")
    claim(w)
    return rid


def reviewed(w):
    call(w, "query_model")
    for bid in list(w.pending_batches()):
        read = call(w, "read_batch", batch_id=bid)
        call(
            w,
            "complete_review_batch",
            batch_id=bid,
            receipt_id=read["receipt_id"],
            report={"summary": "Compared source and model"},
        )


def test_roles_and_claim_required(task):
    with pytest.raises(WorkflowError, match="forbidden"):
        task.call("reviewer", "prepare_sources")
    with pytest.raises(WorkflowError, match="claim"):
        call(task, "prepare_sources")
    claim(task)
    with pytest.raises(WorkflowError, match="forbidden"):
        call(task, "apply_changes", expected_revision=0, operation_id="wrong", changes=[])
    with pytest.raises(WorkflowError, match="gate"):
        call(task, "advance_phase")


def test_full_review_required_and_recovery(task):
    parsed(task)
    with pytest.raises(WorkflowError, match="review batches"):
        call(task, "advance_phase")
    reopened = Workflow(task.root)
    assert reopened.data == task.data
    reviewed(reopened)
    call(reopened, "advance_phase")
    model = json.loads((task.root / "protocol.json").read_text())
    assert model["meta"]["complete"] is True
    with pytest.raises(WorkflowError, match="forbidden"):
        reopened.call("parser", "apply_changes", {})


def test_mutation_atomic_and_idempotent(task):
    rid = parsed(task)
    with pytest.raises(WorkflowError, match="forbidden"):
        task.call("parser", "apply_changes", {})
    reviewed(task)
    b = task.data["batches"][0]
    evidence = [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}]
    fid = call(
        task,
        "submit_findings",
        reviewed_revision=1,
        findings=[
            {
                "description": "Role description needs clarification",
                "evidence": evidence,
                "record_ids": [rid],
            }
        ],
    )["finding_ids"][0]
    call(task, "advance_phase")
    claim(task)
    call(task, "read_batch", batch_id=b["batch_id"])
    args = dict(
        expected_revision=1,
        operation_id="fix",
        changes=[
            {
                "op": "update",
                "record_id": rid,
                "value": {"name": "client", "description": "HELLO sender"},
                "evidence": evidence,
            }
        ],
    )
    result = call(task, "apply_changes", **args)
    assert call(task, "apply_changes", **args) == result
    assert task.data["revision"] == 2
    before = copy.deepcopy(task.data)
    with pytest.raises(WorkflowError):
        call(
            task,
            "apply_changes",
            expected_revision=2,
            operation_id="bad",
            changes=[
                {"op": "delete", "record_id": rid, "reason": "test"},
                {"op": "delete", "record_id": "missing", "reason": "test"},
            ],
        )
    assert task.data["records"] == before["records"]
    with pytest.raises(WorkflowError, match="resolution"):
        call(task, "advance_phase")
    call(
        task,
        "submit_resolutions",
        expected_revision=2,
        resolutions=[
            {
                "finding_id": fid,
                "status": "applied",
                "reason": "Clarified from source",
                "evidence": evidence,
            }
        ],
    )
    call(task, "advance_phase")
    claim(task)
    assert task.data["round"] == 2
    reviewed(task)
    call(
        task,
        "review_resolutions",
        reviewed_revision=2,
        decisions=[{"finding_id": fid, "accepted": True, "reason": "Source supports correction"}],
    )
    with pytest.raises(WorkflowError, match="diff"):
        call(task, "advance_phase")
    call(task, "get_changes", from_revision=1, to_revision=2)
    call(task, "advance_phase")
    assert json.loads((task.root / "protocol.json").read_text())["meta"]["complete"]


def test_limit_publishes_explicit_partial(task):
    task.data["max_review_rounds"] = 1
    with task.db:
        task._save()
    parsed(task)
    reviewed(task)
    b = task.data["batches"][0]
    call(
        task,
        "submit_findings",
        reviewed_revision=1,
        findings=[
            {
                "description": "Unresolved protocol ambiguity",
                "evidence": [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}],
            }
        ],
    )
    call(task, "advance_phase")
    model = json.loads((task.root / "protocol.json").read_text())
    assert model["meta"]["complete"] is False
    assert model["unresolved"][0]["description"] == "Unresolved protocol ambiguity"
    assert json.loads((task.root / "report.json").read_text())["review_limit_reached"]


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


def test_three_reviews_reuse_state_and_preserve_rejections(task):
    task.data["max_review_rounds"] = 3
    with task.db:
        task._save()
    parsed(task)
    reviewed(task)
    b = task.data["batches"][0]
    evidence = [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}]
    fid = call(
        task,
        "submit_findings",
        reviewed_revision=1,
        findings=[{"description": "Missing prerequisite", "evidence": evidence}],
    )["finding_ids"][0]
    call(task, "advance_phase")
    for expected_round in (2, 3):
        claim(task)
        call(task, "read_batch", batch_id=b["batch_id"])
        call(
            task,
            "submit_resolutions",
            expected_revision=1,
            resolutions=[
                {
                    "finding_id": fid,
                    "status": "not_applicable",
                    "reason": "No supporting text found",
                    "evidence": evidence,
                }
            ],
        )
        call(task, "advance_phase")
        assert task.data["round"] == expected_round
        claim(task)
        reviewed(task)
        call(task, "get_changes", from_revision=1, to_revision=1)
        call(
            task,
            "review_resolutions",
            reviewed_revision=1,
            decisions=[
                {
                    "finding_id": fid,
                    "accepted": False,
                    "reason": "Still missing the stated prerequisite",
                }
            ],
        )
        call(task, "advance_phase")
    assert task.data["phase"] == "published"
    assert task.data["round"] == 3
    assert task.data["findings"][fid]["status"] == "open"
    assert len(task.data["findings"][fid]["history"]) == 4
    assert not json.loads((task.root / "protocol.json").read_text())["meta"]["complete"]


def test_last_round_cannot_bypass_source_gate(task):
    parsed(task)
    reviewed(task)
    source = next((task.root / "snapshots").glob("*.json"))
    source.write_text("{}")
    with pytest.raises(WorkflowError, match="snapshot"):
        call(task, "advance_phase")
    assert task.data["phase"] == "review"
    assert not (task.root / "protocol.json").exists()


def test_missing_evidence_and_stale_revision_are_rejected(task):
    claim(task)
    call(task, "prepare_sources")
    call(task, "advance_phase")
    claim(task)
    with pytest.raises(WorkflowError, match="stale"):
        call(
            task,
            "apply_changes",
            expected_revision=7,
            operation_id="stale",
            changes=[{"op": "delete", "record_id": "missing", "reason": "stale request"}],
        )
    with pytest.raises(WorkflowError, match="evidence"):
        call(
            task,
            "apply_changes",
            expected_revision=0,
            operation_id="no-source",
            changes=[
                {
                    "op": "add",
                    "kind": "parties",
                    "value": {"name": "client", "description": "sender"},
                    "evidence": [],
                }
            ],
        )
    assert task.data["revision"] == 0 and not task.data["records"]


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


def test_token_bounded_pages_require_every_model_record(task):
    parsed(task)
    template = next(iter(task.data["records"].values()))
    for index in range(60):
        record = copy.deepcopy(template)
        record["value"] = {"name": f"role-{index}", "description": "source-supported role " * 100}
        task.data["records"][f"extra-{index}"] = record
    with task.db:
        task._save()
    first = call(task, "query_model", format="records", max_tokens=2000)
    assert first["next_cursor"]
    assert not call(task, "check_gate")["ready"]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = call(
            task,
            "query_model",
            format="records",
            max_tokens=2000,
            cursor=cursor,
            revision=first["revision"],
        )
        cursor = page["next_cursor"]
    for bid in list(task.pending_batches()):
        read = call(task, "read_batch", batch_id=bid)
        call(
            task,
            "complete_review_batch",
            batch_id=bid,
            receipt_id=read["receipt_id"],
            report={"summary": "Compared source and model"},
        )
    assert call(task, "check_gate")["ready"]


def test_failed_tools_are_audited_without_committing_changes(task):
    claim(task)
    with pytest.raises(WorkflowError):
        call(task, "apply_changes", expected_revision=0, operation_id="wrong-phase", changes=[])
    events = [json.loads(row[0]) for row in task.db.execute("select body from events")]
    assert events[-1]["tool"] == "apply_changes"
    assert events[-1]["status"] == "rejected"
    assert events[-1]["task_id"] == task.data["task_id"]
    assert task.data["revision"] == 0


def test_large_revision_diff_is_split_without_skipping_changes(task):
    parsed(task)
    record = copy.deepcopy(next(iter(task.data["records"].values())))
    changes = [
        {"record_id": f"changed-{i}", "before": record, "after": copy.deepcopy(record)}
        for i in range(70)
    ]
    for change in changes:
        change["after"]["value"]["description"] = "corrected source-supported definition " * 80
    task.data["history"].append({"revision": 2, "changes": changes})
    task.data["revision"] = 2
    task.data["review_base"] = 1
    task.data["round"] = 2
    with task.db:
        task._save()
    reviewed(task)
    page = call(task, "get_changes", from_revision=1, to_revision=2, max_tokens=2000)
    assert page["next_cursor"] is not None
    assert not call(task, "check_gate")["ready"]
    ids = [r["change_id"] for r in page["items"]]
    cursor = page["next_cursor"]
    while cursor is not None:
        page = call(
            task,
            "get_changes",
            from_revision=1,
            to_revision=2,
            cursor=cursor,
            max_tokens=2000,
            revision=page["revision"],
        )
        ids.extend(r["change_id"] for r in page["items"])
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids)) == 70
    assert call(task, "check_gate")["ready"]


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


def test_export_cannot_bypass_review(task):
    with pytest.raises(WorkflowError, match="gate"):
        task.export()
    assert not (task.root / "protocol.json").exists()


def test_published_audit_detects_missing_receipts_and_model_changes(task):
    from rsc.workflow.audit import audit_run

    parsed(task)
    reviewed(task)
    call(task, "advance_phase")
    assert audit_run(task.root)["verified"]
    phases = json.loads((task.root / "phases.json").read_text())
    assert [p["phase"] for p in phases] == ["preprocess", "parse", "review"]
    assert all(p["timing_origin"] == "assignment_created" for p in phases)
    model = task.root / "protocol.json"
    original = model.read_text()
    model.write_text("{}")
    with pytest.raises(WorkflowError, match="differs"):
        audit_run(task.root)
    model.write_text(original)
    task.data["receipts"].clear()
    with task.db:
        task._save()
    with pytest.raises(WorkflowError, match="receipt"):
        audit_run(task.root)


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


def test_read_batches_stops_on_budget_without_a_receipt(task):
    claim(task)
    call(task, "prepare_sources")
    ids = [batch["batch_id"] for batch in task.data["batches"]]
    if len(ids) < 2:
        extra = copy.deepcopy(task.data["batches"][0])
        extra["batch_id"] = "batch-extra"
        task.data["batches"].append(extra)
        ids.append(extra["batch_id"])
        with task.db:
            task._save()
    from rsc.workflow.sources import token_count

    with pytest.raises(WorkflowError, match=r"requires (\d+)") as raised:
        call(task, "read_batches", batch_ids=ids, max_tokens=1)
    needed = int(str(raised.value).split("requires ", 1)[1].split(" ", 1)[0])
    tight = call(task, "read_batches", batch_ids=ids, max_tokens=needed)
    assert tight["tokens"] == token_count(tight)
    assert tight["tokens"] <= tight["max_tokens"] == needed
    assert tight["delivered"] == [ids[0]]
    assert ids[1] in tight["remaining"]
    assert tight["delivered_ids"] == tight["delivered"]
    assert tight["revision"] == task.data["revision"]
    assert task._key(ids[0]) in task.data["receipts"]
    assert task._key(ids[1]) not in task.data["receipts"]
    with pytest.raises(WorkflowError):
        call(task, "read_batches", batch_ids=ids, max_tokens=needed - 1)
    first = call(task, "read_batches", batch_ids=ids, max_tokens=24000)
    assert first["delivered"]
    assert first["tokens"] == token_count(first)
    assert first["tokens"] <= first["max_tokens"]
    assert set(first["delivered"]).isdisjoint(first["remaining"])
    for batch_id in first["delivered"]:
        assert task._key(batch_id) in task.data["receipts"]
    for batch_id in first["remaining"]:
        assert task._key(batch_id) not in task.data["receipts"]
    with pytest.raises(WorkflowError, match="assignment order"):
        call(task, "read_batches", batch_ids=list(reversed(ids)))
    with pytest.raises(WorkflowError, match="requires"):
        call(task, "read_batches", batch_ids=[ids[0]], max_tokens=1)


def test_query_model_compact_matches_records(task):
    from rsc.workflow.paging import decode_compact

    parsed(task)
    compact = call(task, "query_model", format="compact")
    records = call(task, "query_model", format="records")
    assert decode_compact(compact) == records["items"]
    if compact["next_cursor"] is not None:
        with pytest.raises(WorkflowError, match="revision"):
            call(
                task,
                "query_model",
                cursor=compact["next_cursor"],
                revision=compact["revision"] + 1,
            )
