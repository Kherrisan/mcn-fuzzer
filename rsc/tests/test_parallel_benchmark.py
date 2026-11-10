"""Native API usage remains distinct from workflow payloads."""
import json
import sqlite3
import pytest
from scripts.benchmark_parallel_rsc import cleanup, codex_usage

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
