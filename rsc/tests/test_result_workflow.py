"""Result-oriented lifecycle, parallel submissions and deterministic publication."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from protocol_samples import TEXT, base, interactions

from rsc.protocol.validate import validate_protocol_file
from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.tools import CONTRACTS


def who(w, worker):
    return w.authenticate((w.root / f"{worker}.token").read_text().strip())


def call(w, worker, tool, **args):
    return w.call(who(w, worker), tool, args)


def dispatch(w):
    for a in w.store.assignments():
        if a["status"] == "pending":
            call(w, "coordinator", "record_send", name=a["name"], attempt=a["attempt"])


@pytest.fixture
def run(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text(TEXT + "\n2. Reply\n\nThe responder replies to requests.\n")
    w = Workflow.create(tmp_path / "run", [str(source)], "Test", parse_workers=2, merge_workers=2)
    call(w, "coordinator", "tick")
    dispatch(w)
    yield w
    w.db.close()


def parsed(w):
    assignments = w.store.assignments()

    def extract(a):
        for i, batch_id in enumerate(a["batches"]):
            b = next(b for b in w.batches if b["batch_id"] == batch_id)
            ev = [{"batch_id": batch_id, "block_id": b["segments"][0]["block_id"]}]
            model = base()
            if i == 0:
                model.update(interactions(model))
            items = [
                {"kind": k, "value": v, "evidence": ev}
                for k, vs in model.items()
                if k != "unresolved"
                for v in vs
            ]
            call(w, a["worker"], "submit_parse_result", batch_id=batch_id, items=items)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(extract, assignments))
    assert w.data["phase"] == "cluster"
    dispatch(w)


def clustered(w):
    parsed(w)
    bins = {}
    for k, r in w.store.snapshot("parse").items():
        if r["kind"] != "step_dependencies":
            bins.setdefault((r["kind"], r["value"]["name"]), []).append(k)
    call(w, "parser-1", "submit_clustering_result", groups=list(bins.values()))
    assert w.data["phase"] == "merge"
    dispatch(w)


def merged(w):
    while w.data["phase"] == "merge":
        dispatch(w)
        active = [
            a for a in w.store.assignments() if a["phase"] == "merge" and a["status"] != "submitted"
        ]
        assert active
        for a in active:
            group = w.groups()[a["group_name"]]
            row = w.store.snapshot("parse")[group["members"][0]]
            call(
                w,
                a["worker"],
                "submit_merge_result",
                group_name=a["group_name"],
                value=row["value"],
                evidence=row["evidence"],
            )


def test_parallel_result_pipeline_exports_name_model_and_revision_table(run):
    clustered(run)
    original = deepcopy(run.store.snapshot("parse"))
    merged(run)
    assert run.data["phase"] == "published", run.data.get("errors")
    assert validate_protocol_file(run.root / "protocol.json") == []
    assert run.store.snapshot("parse") == original
    table = json.loads((run.root / "revision-table.json").read_text())
    assert table["deletions"]
    assert set(table["replacements"]) == set(original)
    assert not any(
        "id" in r
        for k, rows in run.publication_model().items()
        if isinstance(rows, list)
        for r in rows
    )
    assert len(CONTRACTS) == 6
    names = [r[0] for r in run.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert not set(names) & {"receipts", "findings", "reports", "conclusions"}


def test_atomic_parse_failure_and_idempotent_retry(run):
    a = run.store.assignments()[0]
    identity = who(run, a["worker"])
    b = next(b for b in run.batches if b["batch_id"] == a["batches"][0])
    item = {
        "kind": "parties",
        "value": {"name": "client", "description": "sender"},
        "evidence": [{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}],
    }
    args = {"batch_id": b["batch_id"], "items": [item, {**item, "value": {"name": "bad"}}]}
    with pytest.raises(WorkflowError):
        run.call(identity, "submit_parse_result", args)
    assert not run.store.items()
    args["items"] = [item]
    first = run.call(identity, "submit_parse_result", args)
    assert run.call(identity, "submit_parse_result", args) == first
    assert len(run.store.items()) == 1
    changed = deepcopy(args)
    changed["items"][0]["value"]["description"] = "different"
    with pytest.raises(WorkflowError, match="different content"):
        run.call(identity, "submit_parse_result", changed)


def test_old_attempt_and_removed_tools_cannot_mutate(run):
    a = run.store.assignments()[0]
    identity = who(run, a["worker"])
    for tool in (
        "claim_assignment",
        "apply_changes",
        "advance_phase",
        "complete_batch",
        "submit_findings",
    ):
        with pytest.raises(WorkflowError, match="forbidden"):
            run.call(identity, tool, {})
    call(
        run,
        "coordinator",
        "replace_attempt",
        name=a["name"],
        attempt=1,
        terminal={"name": "owned", "state": "exited"},
    )
    with pytest.raises(WorkflowError, match="stale"):
        run.call(identity, "query_model", {})
    assert run.store.assignment(a["name"])["attempt"] == 2


def test_cluster_partition_validation_and_restart(run):
    parsed(run)
    items = run.store.snapshot("parse")
    named = [k for k, r in items.items() if r["kind"] != "step_dependencies"]
    with pytest.raises(WorkflowError, match="ungrouped"):
        call(run, "parser-1", "submit_clustering_result", groups=[[named[0]]])
    before = run.store.assignments()
    reopened = Workflow(run.root)
    try:
        call(reopened, "coordinator", "tick")
        assert reopened.store.assignments() == before
    finally:
        reopened.db.close()


def test_audit_replays_submissions_and_rejects_tampered_revision_table(run):
    from rsc.workflow.audit import audit_run

    clustered(run)
    merged(run)
    assert audit_run(run.root)["verified"]
    table = run.store.snapshot("revision_table")
    table["deletions"] = []
    run.store.save_snapshot("revision_table", table)
    with pytest.raises(WorkflowError, match="revision table differs"):
        audit_run(run.root)


@pytest.mark.parametrize("feedback_rounds", [2, 5])
def test_deterministic_failure_retries_only_affected_groups_to_saved_limit(run, feedback_rounds):
    run.store.settings_update(max_feedback_rounds=feedback_rounds)
    clustered(run)
    while run.data["phase"] == "merge":
        dispatch(run)
        active = [
            a
            for a in run.store.assignments()
            if a["phase"] == "merge" and a["status"] != "submitted"
        ]
        for a in active:
            g = run.groups()[a["group_name"]]
            row = run.store.snapshot("parse")[g["members"][0]]
            value = deepcopy(row["value"])
            if row["kind"] == "channels":
                value["initiator_party_name"] = "missing-party"
            call(
                run,
                a["worker"],
                "submit_merge_result",
                group_name=a["group_name"],
                value=value,
                evidence=row["evidence"],
            )
    assert run.data["phase"] == "failed" and run.data["round"] == feedback_rounds
    assert not (run.root / "protocol.json").exists()
    later = [a for a in run.store.assignments() if a["round"] > 0]
    assert later and all(a["phase"] == "merge" for a in later)
    kinds = {
        run.store.snapshot("parse")[run.groups()[a["group_name"]]["members"][0]]["kind"]
        for a in later
    }
    assert kinds == {"channels"}


def test_merge_pool_reuses_assignments_across_groups(run):
    clustered(run)
    merged(run)
    assignments = [a for a in run.store.assignments() if a["phase"] == "merge"]
    assert len(assignments) == 2
    assert len(run.groups()) > len(assignments)
    assert all(
        run.db.execute(
            "SELECT count(*) FROM submissions WHERE assignment=?", (a["name"],)
        ).fetchone()[0]
        > 1
        for a in assignments
    )


def test_partial_parse_recovery_retains_completed_batches_and_owned_items(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text(TEXT + "\n2. Reply\n\nThe responder replies.\n")
    w = Workflow.create(tmp_path / "resume", [str(source)], "Test", parse_workers=1)
    try:
        call(w, "coordinator", "tick")
        dispatch(w)
        a = w.store.assignments()[0]
        assert len(a["batches"]) > 1
        b = w.batches[0]
        args = {
            "batch_id": b["batch_id"],
            "items": [
                {
                    "kind": "parties",
                    "value": {"name": "client", "description": "sender"},
                    "evidence": [
                        {"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}
                    ],
                }
            ],
        }
        response = call(w, "parser-1", "submit_parse_result", **args)
        saved = w.store.items()
        call(
            w,
            "coordinator",
            "replace_attempt",
            name=a["name"],
            attempt=1,
            terminal={"name": "owned", "state": "exited"},
        )
        dispatch(w)
        assert w.store.items() == saved
        assert w.progress(w.store.assignment(a["name"]))["pending_batches"] == a["batches"][1:]
        assert response["items"][0]["id"] in saved
        with pytest.raises(WorkflowError, match="expected next batch"):
            call(w, "parser-1", "submit_parse_result", **args)
        b = w.batches[1]
        args["batch_id"] = b["batch_id"]
        args["items"][0].update(
            id=response["items"][0]["id"],
            evidence=[{"batch_id": b["batch_id"], "block_id": b["segments"][0]["block_id"]}],
        )
        call(w, "parser-1", "submit_parse_result", **args)
        assert len(w.store.items()) == 1
    finally:
        w.db.close()


def test_index_excludes_dependencies_and_incoming_query_finds_them(run):
    clustered(run)
    response = call(run, "merge-1", "query_model", view="index")
    assert all(r["name"] and r["kind"] != "step_dependencies" for r in response["items"])
    step = next(r["id"] for r in response["items"] if r["kind"] == "steps")
    response = call(run, "merge-1", "query_model", references=[step])
    assert response["items"]
    assert all(r["kind"] == "step_dependencies" for r in response["items"])


def test_phase_metrics_include_final_submit_and_read_deliveries(run):
    parsed(run)
    call(run, "parser-1", "query_model", view="index")
    call(run, "parser-1", "query_model", view="index")
    bins = {}
    for k, r in run.store.snapshot("parse").items():
        if r["kind"] != "step_dependencies":
            bins.setdefault((r["kind"], r["value"]["name"]), []).append(k)
    call(run, "parser-1", "submit_clustering_result", groups=list(bins.values()))
    merged(run)
    metrics = json.loads((run.root / "phases.json").read_text())
    cluster = next(p for p in metrics["phases"] if p["phase"] == "cluster")
    assert cluster["calls"]["submit_clustering_result"] == 1
    assert cluster["repeated_delivered_items"] > 0 and cluster["delivered_tokens"] > 0
    stage = next(p for p in metrics["phases"] if p["phase"] == "merge")
    assert stage["calls"]["submit_merge_result"] == len(run.groups())


def test_group_reads_do_not_expose_other_workers_in_progress_results(run):
    clustered(run)
    before = call(run, "merge-2", "query_model", view="groups")
    a = next(a for a in run.store.assignments() if a["worker"] == "merge-1")
    g = run.groups()[a["group_name"]]
    row = run.store.snapshot("parse")[g["members"][0]]
    call(
        run,
        "merge-1",
        "submit_merge_result",
        group_name=a["group_name"],
        value=row["value"],
        evidence=row["evidence"],
    )
    after = call(run, "merge-2", "query_model", view="groups")
    assert before == after
    assert all(set(g) == {"name", "members"} for g in after["items"])
