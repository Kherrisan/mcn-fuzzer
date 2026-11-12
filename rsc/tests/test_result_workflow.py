"""Complete file checkpoints, independent review, Fix and crash recovery."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from protocol_samples import TEXT, base, interactions

from rsc.protocol.validate import validate_protocol_file
from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.store import atomic_json, read_json
from rsc.workflow.tools import CONTRACTS


def who(w, worker):
    return w.authenticate((w.root / f"{worker}.token").read_text().strip())


def call(w, worker, tool, **args):
    return w.call(who(w, worker), tool, args)


def active(w, phase=None):
    return [
        a
        for a in w.store.assignments()
        if a["phase"] == (phase or w.data["phase"])
        and a["round"] == w.data["round"]
        and a["status"] != "submitted"
    ]


def dispatch(w):
    for a in active(w):
        if a["status"] == "pending":
            call(w, "coordinator", "record_send", name=a["name"])


def create(tmp_path, **settings):
    source = tmp_path / "rfc9999.txt"
    source.write_text(TEXT + "\n2. Reply\n\nThe responder replies to requests.\n")
    w = Workflow.create(tmp_path / "run", [str(source)], "Test", **settings)
    call(w, "coordinator", "tick")
    dispatch(w)
    return w


@pytest.fixture
def run(tmp_path):
    w = create(tmp_path, parse_workers=2)
    yield w
    w.store.close()


def checkpoint(w, a=None):
    a = a or active(w)[0]
    b = next(b for b in w.batches if b["batch_id"] == a["batches"][0])
    evidence = [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}]
    model = base()
    model.update(interactions(model))
    return {
        "items": [
            {"kind": k, "value": deepcopy(v), "evidence": deepcopy(evidence)}
            for k, vs in model.items()
            if k != "unresolved"
            for v in vs
        ],
        "unresolved": [],
    }


def submit(w, worker, payload, tool=None):
    if tool is None:
        tool = {
            "parse": "submit_parse_result",
            "merge": "submit_model_result",
            "fix": "submit_model_result",
            "review": "submit_review_result",
        }[w.data["phase"]]
    path = w.root / "work" / worker / "result.json"
    atomic_json(path, payload)
    return call(w, worker, tool, path=str(path))


def parsed(w):
    assignments = active(w)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda a: submit(w, a["worker"], checkpoint(w, a)), assignments))
    assert w.data["phase"] == ("review" if len(assignments) == 1 else "merge"), w.data
    dispatch(w)


def merged(w):
    rows = []
    for a in w.store.assignments():
        if a["phase"] == "parse":
            for item in w.checkpoint(a["output_path"])["items"]:
                match = next(
                    (r for r in rows if r["kind"] == item["kind"] and r["value"] == item["value"]),
                    None,
                )
                if match:
                    match["evidence"] += [e for e in item["evidence"] if e not in match["evidence"]]
                else:
                    rows.append(item)
    submit(w, "merge-1", {"items": rows, "unresolved": []})
    assert w.data["phase"] == "review", w.data
    dispatch(w)


def review(w, findings=()):
    a = active(w)[0]
    return submit(w, a["worker"], {"reviewed_batches": a["batches"], "findings": list(findings)})


def test_partial_review_survives_restart_and_cannot_publish(tmp_path):
    w = create(tmp_path, parse_workers=1)
    parsed(w)
    a = active(w)[0]
    assert len(a["batches"]) > 1
    payload = {"complete": False, "reviewed_batches": a["batches"][:1], "findings": []}
    result = submit(w, a["worker"], payload)
    assert not result["stop_turn"] and result["pending_batches"] == a["batches"][1:]
    assert w.data["phase"] == "review" and not (w.root / "protocol.json").exists()
    assert submit(w, a["worker"], payload) == result
    w.store.close()
    w = Workflow(w.root)
    call(w, "coordinator", "tick")
    assert w.data["phase"] == "review"
    with pytest.raises(WorkflowError, match="discard"):
        submit(w, a["worker"], {**payload, "reviewed_batches": a["batches"][1:]})
    review(w)
    assert w.data["phase"] == "published"
    assert audit_run(w.root)["verified"]
    w.store.close()


def finding(w, *, category="correctable", global_=False):
    return {
        "category": category,
        "description": "source needs clarification",
        "batch_ids": [w.batches[0]["batch_id"]],
        "global": global_,
    }


def publish(w):
    parsed(w)
    if w.data["phase"] == "merge":
        merged(w)
    review(w)
    assert w.data["phase"] == "published", w.data


def test_multi_parser_one_whole_input_merger_independent_review(run):
    parsed(run)
    merges = active(run)
    assert len(merges) == 1 and merges[0]["worker"] == "merge-1"
    assert merges[0]["inputs"] == [
        a["output_path"] for a in run.store.assignments() if a["phase"] == "parse"
    ]
    merged(run)
    assert not (run.root / "protocol.json").exists()
    assert active(run)[0]["role"] == "reviewer"
    review(run)
    assert validate_protocol_file(run.root / "protocol.json") == []
    assert audit_run(run.root)["verified"]
    assert len(CONTRACTS) == 5
    assert not list(run.root.glob("*.sqlite*"))
    assert not (run.root / "revision-table.json").exists()


def test_single_parser_bypasses_merge(tmp_path):
    w = create(tmp_path, parse_workers=1)
    publish(w)
    assert not any(a["phase"] == "merge" for a in w.store.assignments())
    assert w.data["fix_owner"] == "parser-1"
    assert audit_run(w.root)["semantic_review"]


def test_partial_checkpoints_replace_same_file_and_recover(tmp_path):
    w = create(tmp_path, parse_workers=1)
    a = active(w)[0]
    payload = checkpoint(w, a)
    payload.update(complete=False, completed_batches=[a["batches"][0]])
    assert not submit(w, a["worker"], payload)["stop_turn"]
    assert len(list((w.root / "parsers").glob("*.json"))) == 1
    reopened = Workflow(w.root)
    call(reopened, "coordinator", "tick")
    assert reopened.data["phase"] == "parse"
    assert active(reopened)[0]["completed_batches"] == payload["completed_batches"]
    payload["items"][0]["value"]["description"] += " updated"
    submit(reopened, a["worker"], payload)
    assert read_json(w.root / a["output_path"])["items"] == payload["items"]
    bad = {**payload, "completed_batches": []}
    with pytest.raises(WorkflowError, match="discard"):
        submit(reopened, a["worker"], bad)
    payload.update(complete=True, completed_batches=a["batches"])
    assert submit(reopened, a["worker"], payload)["stop_turn"]
    assert reopened.data["phase"] == "review"


def test_atomic_validation_and_content_idempotency(run):
    a = active(run)[0]
    payload = checkpoint(run, a)
    invalid = deepcopy(payload)
    invalid["items"][-1]["evidence"] = [{"batch_id": "bad", "block_id": "missing"}]
    with pytest.raises(WorkflowError, match="invalid source pair"):
        submit(run, a["worker"], invalid)
    assert not (run.root / a["output_path"]).exists()
    answer = submit(run, a["worker"], payload)
    assert submit(run, a["worker"], payload, "submit_parse_result") == answer
    payload["items"][0]["value"]["description"] += " different"
    with pytest.raises(WorkflowError, match="different content"):
        submit(run, a["worker"], payload, "submit_parse_result")


def test_saved_file_survives_failure_before_state_advancement(run, monkeypatch):
    a = active(run)[0]

    def crash():
        raise OSError("simulated service crash")

    monkeypatch.setattr(run, "advance", crash)
    with pytest.raises(WorkflowError, match="simulated"):
        submit(run, a["worker"], checkpoint(run, a))
    assert read_json(run.root / a["output_path"])["complete"]
    reopened = Workflow(run.root)
    call(reopened, "coordinator", "tick")
    assert reopened.store.assignment(a["name"])["status"] == "submitted"
    assert len(reopened.store.assignments()) == 2


def test_stale_session_and_removed_tools_rejected(run):
    a = active(run)[0]
    old = who(run, a["worker"])
    with pytest.raises(WorkflowError, match="forbidden"):
        run.call(old, "search_source", {"query": "hello"})
    call(
        run,
        "coordinator",
        "restart_worker",
        name=a["name"],
        terminal={"state": "exited", "name": "owned"},
    )
    with pytest.raises(WorkflowError, match="stale"):
        run.call(old, "query_model", {})
    with pytest.raises(WorkflowError, match="revoked"):
        run.authenticate(old.token)
    assert call(run, a["worker"], "query_model")["items"] == []


def test_only_declared_complete_review_coverage_required(run):
    parsed(run)
    merged(run)
    a = active(run)[0]
    with pytest.raises(WorkflowError, match="every assigned"):
        submit(run, a["worker"], {"reviewed_batches": a["batches"][:1], "findings": []})
    # Full shell reads are legitimate: no MCP receipts are required.
    assert review(run)["stop_turn"]


def test_review_cannot_edit_model_and_fix_owner_is_original_parser(tmp_path):
    w = create(tmp_path, parse_workers=1)
    parsed(w)
    with pytest.raises(WorkflowError, match="forbidden"):
        call(w, "reviewer-1", "submit_model_result", path="ignored")
    review(w, [finding(w)])
    assert w.data["phase"] == "fix"
    a = active(w)[0]
    assert a["worker"] == "parser-1" and a["role"] == "parser"
    frozen = read_json(w.root / "reviews/round-0/candidate.json")
    submit(w, a["worker"], frozen)
    assert w.data["phase"] == "review" and w.data["round"] == 1
    assert active(w)[0]["batches"] == finding(w)["batch_ids"]
    assert read_json(w.root / "reviews/round-0/candidate.json") == frozen
    review(w)
    assert w.data["phase"] == "published"


def test_global_fix_requires_full_source_review(run):
    parsed(run)
    merged(run)
    review(run, [finding(run)])
    a = active(run)[0]
    assert a["worker"] == "merge-1"
    fixed = run.checkpoint(a["candidate_path"])
    fixed["items"][0]["value"]["description"] += " corrected"
    submit(run, "merge-1", fixed)
    assert set(active(run)[0]["batches"]) == {b["batch_id"] for b in run.batches}


@pytest.mark.parametrize("limit", [1, 3])
def test_correctable_errors_never_publish_and_feedback_limit_persists(tmp_path, limit):
    w = create(tmp_path, parse_workers=1, max_feedback_rounds=limit)
    parsed(w)
    for n in range(limit):
        review(w, [finding(w)])
        assert w.data["phase"] == "fix" and w.data["round"] == n + 1
        w = Workflow(w.root)
        submit(w, "parser-1", w.checkpoint(active(w)[0]["candidate_path"]))
    review(w, [finding(w)])
    assert w.data["phase"] == "failed" and not (w.root / "protocol.json").exists()


def test_whole_model_reference_errors_return_to_same_turn_without_consuming_feedback(tmp_path):
    w = create(tmp_path, parse_workers=1)
    payload = checkpoint(w)
    step = next(i for i in payload["items"] if i["kind"] == "steps")
    step["value"]["channel_name"] = "missing channel"
    with pytest.raises(WorkflowError, match="missing channel"):
        submit(w, "parser-1", payload)
    assert w.data["phase"] == "parse" and w.data["round"] == 0
    assert not (w.root / "parsers/parser-1.json").exists()
    assert not any(a["phase"] == "review" for a in w.store.assignments())
    submit(w, "parser-1", checkpoint(w))
    assert w.data["phase"] == "review"


def test_invalid_fix_remains_editable_at_feedback_limit(tmp_path):
    w = create(tmp_path, parse_workers=1, max_feedback_rounds=1)
    parsed(w)
    review(w, [finding(w)])
    candidate = w.checkpoint(active(w)[0]["candidate_path"])
    invalid = deepcopy(candidate)
    step = next(i for i in invalid["items"] if i["kind"] == "steps")
    step["value"]["channel_name"] = "missing channel"
    with pytest.raises(WorkflowError, match="candidate validation failed"):
        submit(w, "parser-1", invalid)
    assert w.data["phase"] == "fix" and w.data["round"] == 1
    assert not (w.root / active(w)[0]["output_path"]).exists()
    submit(w, "parser-1", candidate)
    review(w)
    assert w.data["phase"] == "published"
    assert audit_run(w.root)["verified"]
    w.store.close()


def test_source_ambiguity_publishes_incomplete_with_evidence(run):
    parsed(run)
    merged(run)
    review(run, [finding(run, category="source_ambiguity")])
    model = read_json(run.root / "protocol.json")
    assert model["meta"]["complete"] is False
    assert "source needs clarification" in model["unresolved"][0]["description"]
    assert run.batches[0]["segments"][0]["block_id"] in model["unresolved"][0]["description"]
    assert read_json(run.root / "reviews/round-0/review.json")["findings"][0]["batch_ids"]
    assert audit_run(run.root)["complete"] is False


def test_merge_budget_failure_preserves_all_parser_results(run):
    run.store.settings_update(merge_context_tokens=1000)
    for a in active(run):
        submit(run, a["worker"], checkpoint(run, a))
    assert run.data["phase"] == "failed"
    assert "whole-input merge" in run.data["errors"][0]
    assert len(list((run.root / "parsers").glob("*.json"))) == 2
    assert not (run.root / "protocol.json").exists()


def test_audit_rejects_changed_export_and_is_readonly(run):
    publish(run)
    before = (run.root / "state.json").read_bytes()
    assert audit_run(run.root)["verified"]
    assert (run.root / "state.json").read_bytes() == before
    exported = read_json(run.root / "protocol.json")
    exported["parties"][0]["description"] = "tampered"
    atomic_json(run.root / "protocol.json", exported)
    with pytest.raises(WorkflowError, match="published protocol differs"):
        audit_run(run.root)


def test_no_object_ids_or_revision_in_query_and_same_name_inputs(run):
    parsed(run)
    page = call(run, "merge-1", "query_model", view="index")
    names = [(r["kind"], r["name"]) for r in page["items"]]
    assert len(names) > len(set(names))
    assert all("id" not in r and "record_id" not in r for r in page["items"])
    assert "revision" not in page
    with pytest.raises(WorkflowError, match="invalid content"):
        call(run, "merge-1", "query_model", revision=1)


def test_metrics_include_last_submission(run):
    publish(run)
    metrics = read_json(run.root / "phases.json")
    assert metrics["semantic_review"]
    stage = next(p for p in metrics["phases"] if p["phase"] == "review")
    assert stage["calls"]["submit_review_result"] == 1


def test_old_database_requires_archived_implementation(tmp_path):
    (tmp_path / "workflow.sqlite").touch()
    with pytest.raises(WorkflowError, match="archived"):
        Workflow(tmp_path)


def test_official_filename_without_service_acceptance_never_completes_review(tmp_path):
    w = create(tmp_path, parse_workers=1)
    parsed(w)
    a = active(w)[0]
    forged = {"reviewed_batches": a["batches"], "findings": []}
    atomic_json(w.root / a["output_path"], forged)
    call(w, "coordinator", "tick")
    assert w.data["phase"] == "review"
    assert not (w.root / "protocol.json").exists()
    # A legitimate submit validates and atomically installs the authoritative file.
    review(w)
    assert w.data["phase"] == "published"


def test_stray_review_cannot_be_adopted_if_validated_install_crashes(tmp_path, monkeypatch):
    from rsc.workflow import state

    w = create(tmp_path, parse_workers=1)
    parsed(w)
    a = active(w)[0]
    official = w.root / a["output_path"]
    atomic_json(official, {"reviewed_batches": a["batches"], "findings": []})
    original = state.atomic_json

    def crash(path, value, **kwargs):
        if path == official:
            raise OSError("accepted output install interrupted")
        return original(path, value, **kwargs)

    monkeypatch.setattr(state, "atomic_json", crash)
    with pytest.raises(WorkflowError, match="install interrupted"):
        review(w, [finding(w)])
    monkeypatch.setattr(state, "atomic_json", original)
    assert official.exists()
    assert not w.store.assignment(a["name"]).get("accepted_file")
    reopened = Workflow(w.root)
    call(reopened, "coordinator", "tick")
    assert reopened.data["phase"] == "review"
    assert not (w.root / "protocol.json").exists()
    review(reopened, [finding(reopened)])
    assert reopened.data["phase"] == "fix"


def test_crash_after_install_before_acceptance_requires_validated_resubmit(run, monkeypatch):
    a = active(run)[0]
    payload = checkpoint(run, a)
    original = run.store.update_assignment

    def crash(name, **changes):
        if changes.get("accepted_file"):
            raise OSError("acceptance persistence interrupted")
        return original(name, **changes)

    monkeypatch.setattr(run.store, "update_assignment", crash)
    with pytest.raises(WorkflowError, match="acceptance persistence interrupted"):
        submit(run, a["worker"], payload)
    assert (run.root / a["output_path"]).exists()
    reopened = Workflow(run.root)
    call(reopened, "coordinator", "tick")
    assert reopened.store.assignment(a["name"])["status"] != "submitted"
    assert not reopened.store.assignment(a["name"]).get("accepted_file")
    submit(reopened, a["worker"], payload)
    assert reopened.store.assignment(a["name"])["status"] == "submitted"
