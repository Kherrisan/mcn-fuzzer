"""The opt-in paid runner reports real API usage separately from tool payloads."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
import test_parallel_workflow as h
from test_parallel_supervisor import supervisor

from scripts.benchmark_parallel_rsc import cleanup, codex_usage, summarize
from scripts.verify_hc_recovery import interrupt_claimed_parser

run = h.run


@pytest.mark.parametrize("saved_id", [False, True])
def test_codex_usage_filters_campaign_and_native_session_and_uses_final_counters(
    tmp_path, monkeypatch, saved_id
):
    home, root = tmp_path / "codex", tmp_path / "campaign"
    home.mkdir()
    (root / "agents/parser-1").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    rollout = home / "session.jsonl"
    usage = {"input_tokens": 70, "cached_input_tokens": 40, "output_tokens": 10, "total_tokens": 80}
    rollout.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": counts},
                    },
                }
            )
            for counts in ({"total_tokens": 30}, usage)
        )
    )
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id,name,rollout_path,model,reasoning_effort,cwd)")
    db.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?)",
        ("thread", "rsc-campaign-parser-1", str(rollout), "gpt-6-luna", "high", str(root)),
    )
    db.execute(
        "INSERT INTO threads VALUES (?,?,?,?,?,?)",
        ("other", "rsc-campaign-parser-1", "missing", "other", "max", "/another-campaign"),
    )
    if saved_id:
        db.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?)",
            ("old", "rsc-campaign-parser-1", "missing", "other", "max", str(root)),
        )
    db.commit()
    db.close()
    (root / "supervisor.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "parser-1": {
                        "name": "rsc-campaign-parser-1",
                        **({"id": "thread"} if saved_id else {}),
                    },
                }
            }
        )
    )
    result = codex_usage(root)
    assert result["available"] and result["total"] == usage
    assert result["workers"]["parser-1"]["model"] == "gpt-6-luna"
    assert (root / "agents/parser-1/codex-session.jsonl").read_text() == rollout.read_text()


def test_diagnostic_cleanup_refuses_another_campaign_namespace(tmp_path):
    root, runtime = tmp_path / "root", tmp_path / "runtime"
    root.mkdir()
    runtime.mkdir()
    (runtime / "owner.json").write_text(json.dumps({"root": "/somebody-else"}))
    (root / "supervisor.json").write_text(json.dumps({"hc_env": {"XDG_RUNTIME_DIR": str(runtime)}}))
    with pytest.raises(RuntimeError, match="another root"):
        cleanup(root)


@pytest.mark.parametrize("changed_identity", [False, True])
def test_live_recovery_probe_targets_only_its_claimed_worker(run, monkeypatch, changed_identity):
    s = supervisor(run)
    s.state["hc_env"] = {"HC_SESSION": "test-private"}
    s.step()
    assert interrupt_claimed_parser(run.root) is None
    h.claim(run, "parser-1")
    assert interrupt_claimed_parser(run.root) is None  # No mutation has committed yet.
    source = next(iter(h.read(run, "parser-1").values()))
    h.call(
        run,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="progress",
        changes=[
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": "client", "description": "sender"},
                "evidence": [source["batch"]["segments"][0]["citation"]],
            }
        ],
    )
    h.call(
        run,
        "parser-1",
        "complete_batch",
        revision=1,
        reports=[
            {
                "batch_id": source["batch"]["batch_id"],
                "receipt": source["receipt"],
                "report": {"summary": "Extracted sender", "objects": [h.CLIENT]},
            }
        ],
    )
    old = s.state["terminals"]["parser-1"]
    peer = s.state["terminals"]["parser-2"]
    if changed_identity:
        s.hc_call.terminals[old["address"]]["name"] = "another-owner"

    def fake_run(argv, **kwargs):
        assert kwargs["env"]["HC_SESSION"] == "test-private"
        assert "HC_TERMINAL_ID" not in kwargs["env"]
        args = [arg for arg in argv[1:] if arg != "--json"]
        return SimpleNamespace(stdout=json.dumps(s.hc_call(*args)))

    monkeypatch.setattr("scripts.verify_hc_recovery.subprocess.run", fake_run)
    if changed_identity:
        with pytest.raises(RuntimeError, match="changed identity"):
            interrupt_claimed_parser(run.root)
        assert s.hc_call.terminals[old["address"]]["state"] == "live"
    else:
        result = interrupt_claimed_parser(run.root)
        assert result["assignment"] == "parse-0/parser-1" and result["draft_revision"] == 1
        assert result["completed_reports"] == 1
        s.step()
        assert run.store.assignment(result["assignment"])["attempt"] == 2
        assert run.store.draft(result["assignment"])[0]["revision"] == 1
        assert run.store.completed(result["assignment"]) == {source["batch"]["batch_id"]}
    assert s.hc_call.terminals[peer["address"]]["state"] == "live"


def test_timing_distinguishes_unpublished_audit_failure_and_pending_semantic_acceptance(run):
    result = summarize(run.root, 0, False, 10)
    assert result["outcome"] == "incomplete_workflow"
    assert result["semantic_audit"] == "not_run" and not result["under_20_minutes"]
    h.merged(run)
    for worker in ("reviewer-1", "reviewer-2"):
        h.report_review(run, worker)
        h.call(run, worker, "advance_phase")
    h.advance(run)
    result = summarize(run.root, 0, False, 10)
    assert result["outcome"] == "published" and result["under_20_minutes"]
    assert result["performance_acceptance"] == "pending_semantic_audit"
    assert result["semantic_audit"] == "pending"
    assert summarize(run.root, 0, False, 1200)["performance_acceptance"] == "failed"
    assert summarize(run.root, -15, True, 2400)["outcome"] == "timeout"
    (run.root / "protocol.json").write_text("{}")
    result = summarize(run.root, 0, False, 10)
    assert result["outcome"] == "audit_failed" and result["performance_acceptance"] == "failed"
    assert not result["under_20_minutes"]


def test_timing_records_partial_publication_separately(run):
    from test_workflow import finding

    run.db.execute("UPDATE workflow SET body=json_set(body,'$.max_review_rounds',1)")
    h.merged(run)
    finding(run)
    h.advance(run)
    result = summarize(run.root, 0, False, 10)
    assert result["outcome"] == "partial" and not result["under_20_minutes"]
    assert result["performance_acceptance"] == "failed" and result["audit"]["verified"]
