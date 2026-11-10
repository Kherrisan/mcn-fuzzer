"""The opt-in paid runner reports real API usage separately from tool payloads."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
import test_parallel_workflow as h
from test_parallel_supervisor import supervisor

from scripts.benchmark_parallel_rsc import cleanup, codex_usage
from scripts.verify_hc_recovery import interrupt_claimed_parser

run = h.run


def test_codex_usage_filters_campaign_and_named_session_and_uses_final_counters(
    tmp_path, monkeypatch
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
    db.commit()
    db.close()
    (root / "supervisor.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "parser-1": {"name": "rsc-campaign-parser-1"},
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
        assert result["assignment"] == "parse-0/parser-1" and result["draft_revision"] == 0
        s.step()
        assert run.store.assignment(result["assignment"])["attempt"] == 2
    assert s.hc_call.terminals[peer["address"]]["state"] == "live"
