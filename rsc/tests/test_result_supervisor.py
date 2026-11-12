"""Exercise the new dispatcher against durable assignments without launching models."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

import test_result_workflow as helpers

from rsc.workflow.supervisor import Supervisor

run = helpers.run


class FakeHc:
    def __init__(self, root):
        self.calls, self.terminals = [], {}
        self.root = root
        self.database = root / "codex-test-home/state_5.sqlite"
        self.database.parent.mkdir(exist_ok=True)
        with sqlite3.connect(self.database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, name, cwd, created_at)"
            )

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        command = args[0]
        if command == "run":
            name = args[args.index("--name") + 1]
            address = f"t{len(self.terminals) + 1}"
            launch = json.loads(Path(args[-1]).read_text()) if "--" in args else {}
            argv = launch.get("argv", [])
            thread = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
            with sqlite3.connect(self.database) as db:
                db.execute(
                    "INSERT OR IGNORE INTO threads VALUES (?,?,?,?)",
                    (thread, name, str(self.root), int(time.time())),
                )
            self.terminals[address] = {
                "address": address,
                "name": name,
                "state": "live",
                "status": {"state": "idle"},
                "turn": 0,
                "renamed": False,
                "thread": thread,
            }
            return self.terminals[address].copy()
        if command == "get":
            found = self.terminals.get(args[1]) or next(
                t for t in self.terminals.values() if t["name"] == args[1]
            )
            return found.copy()
        if command == "read":
            t = self.terminals[args[1]]
            return {"text": " · " + t["name"] if t["renamed"] else "Ask Codex to do anything"}
        if command == "send":
            t = self.terminals[args[1]]
            if args[2].startswith("/rename "):
                assert args[2] == "/rename " + t["name"]
                t["renamed"] = True
            else:
                t["status"] = {"state": "working"}
        if command == "kill":
            self.terminals[args[1]]["state"] = "finished"
        return {}


def supervisor(w):
    s = Supervisor.__new__(Supervisor)
    s.root, s.socket, s.task_name = w.root, "/tmp/rsc-test.sock", w.data["name"]
    s.path = w.root / "supervisor.json"
    s.state = {"terminals": {}, "sessions": {}}
    s.roles = {
        role: {
            "name": "test",
            "profile": {
                "provider": "codex",
                "executable": "/bin/true",
                "model": "gpt-6-luna",
                "effort": "high",
                "envs": [["CODEX_HOME", str(w.root / "codex-test-home")]],
                "args": [],
            },
        }
        for role in ("parser", "merge", "reviewer")
    }
    s.config = {"workflow": {"batch_max_tokens": 12000}, "resolved_agents": s.roles}
    s.retry_worker, s.wait_index = None, 0
    s.hc_call = FakeHc(w.root)
    s.workflow_call = lambda name, arguments=None: w.call(
        helpers.who(w, "coordinator"), name, arguments or {}, origin="supervisor"
    )
    return s


def pending(w):
    for a in helpers.active(w):
        w.store.update_assignment(a["name"], status="pending")


def test_serial_initialization_fair_dispatch_and_no_claim_tool(run):
    s = supervisor(run)
    pending(run)
    assert s.step()
    calls = s.hc_call.calls
    launches = [i for i, c in enumerate(calls) if c[0] == "run"]
    renames = [i for i, c in enumerate(calls) if c[0] == "send" and c[2].startswith("/rename")]
    sends = [i for i, c in enumerate(calls) if c[0] == "send" and not c[2].startswith("/rename")]
    assert launches[0] < renames[0] < launches[1] < renames[1] < sends[0] < sends[1]
    assert all(a["status"] == "sent" for a in run.store.assignments())
    assert "submit_parse_result" in calls[sends[0]][2]
    assert "claim_assignment" not in calls[sends[0]][2]
    assert s.step()
    assert len(
        [c for c in s.hc_call.calls if c[0] == "send" and not c[2].startswith("/rename")]
    ) == len(sends)


def test_confirmed_death_replaces_only_owned_session(run):
    s = supervisor(run)
    pending(run)
    s.step()
    old = helpers.who(run, "parser-1")
    saved = s.state["terminals"]["parser-1"]
    s.hc_call.terminals[saved["address"]]["state"] = "finished"
    s.step()
    a = run.store.assignment(saved["assignment"])
    assert len(a["failures"]) == 1 and a["status"] == "pending"
    assert not run.store.assignments()[1]["failures"]
    import pytest

    with pytest.raises(ValueError, match="stale"):
        run.call(old, "query_model", {})


def test_fix_restores_original_parser_native_session(tmp_path):
    w = helpers.create(tmp_path, parse_workers=1)
    s = supervisor(w)
    pending(w)
    s.step()
    original_thread = s.state["sessions"]["parser-1"]["id"]
    helpers.parsed(w)
    pending(w)
    s.step()
    assert s.state["sessions"]["reviewer-1"]["id"] != original_thread
    helpers.review(w, [helpers.finding(w)])
    pending(w)
    s.step()
    assert s.state["sessions"]["parser-1"]["id"] == original_thread
    sent = [c for c in s.hc_call.calls if c[0] == "send" and not c[2].startswith("/rename")]
    assert len(sent) == 3 and "submit_model_result" in sent[-1][2]
    assert "reviewer-1" not in s.state["terminals"]


def test_parser_launch_concurrency_is_bounded(run):
    s = supervisor(run)
    run.store.settings_update(parallel_agents=1)
    pending(run)
    s.step()
    assert len(s.state["terminals"]) == 1
    a = helpers.active(run)[0]
    helpers.submit(run, a["worker"], helpers.checkpoint(run, a))
    s.step()
    assert set(s.state["terminals"]) == {"parser-2"}


def test_early_reviewer_final_gets_bounded_same_session_continuations(tmp_path):
    w = helpers.create(tmp_path, parse_workers=1)
    helpers.parsed(w)
    s = supervisor(w)
    pending(w)
    s.step()
    saved = s.state["terminals"]["reviewer-1"]
    terminal = s.hc_call.terminals[saved["address"]]
    original_thread = s.state["sessions"]["reviewer-1"]["id"]
    for count in range(2):
        terminal.update(status={"state": "done"}, turn=terminal["turn"] + 1)
        s.step()
        assert len(helpers.active(w)[0]["continuations"]) == count + 1
        s.step()
        assert s.state["sessions"]["reviewer-1"]["id"] == original_thread
        sent = [c for c in s.hc_call.calls if c[0] == "send"][-1]
        assert "previous turn ended" in sent[2]
    assert len([c for c in s.hc_call.calls if c[0] == "run"]) == 1
    terminal.update(status={"state": "done"}, turn=terminal["turn"] + 1)
    s.step()
    assert w.data["phase"] == "failed"
    assert not (w.root / "protocol.json").exists()


def test_live_observation_failure_never_replaces_session(run):
    from rsc.workflow.supervisor import HcObservationTimeout

    s = supervisor(run)
    pending(run)
    s.step()
    original = s.state["terminals"].copy()
    s.observe = lambda worker: (_ for _ in ()).throw(HcObservationTimeout("observer unavailable"))
    assert s.step()
    assert s.state["terminals"] == original
    assert all(not a["failures"] for a in run.store.assignments())


def test_crash_after_record_send_recovers_without_duplicate_live_agent(run, monkeypatch):
    import pytest

    s = supervisor(run)
    pending(run)
    original_call = s.workflow_call

    def crash_after_send(name, arguments=None):
        result = original_call(name, arguments)
        if name == "record_send":
            raise OSError("coordinator crashed after recording send")
        return result

    s.workflow_call = crash_after_send
    with pytest.raises(OSError, match="coordinator crashed"):
        s.step()
    s.state = json.loads(s.path.read_text())
    assert s.state["terminals"]["parser-1"]["send_intent_at"]
    s.workflow_call = original_call
    monkeypatch.setattr(time, "time", lambda: 9999999999)
    s.step()
    assert helpers.active(run)[0]["status"] == "pending"
    s.step()
    assert len([c for c in s.hc_call.calls if c[0] == "run"]) == 2
    sends = [c for c in s.hc_call.calls if c[0] == "send" and not c[2].startswith("/rename")]
    assert len(sends) == 2


def test_definite_startup_nondelivery_retries_rename_and_assignment(run):
    from rsc.workflow.supervisor import HcFailure

    s = supervisor(run)
    pending(run)
    hc = s.hc_call
    rejected = set()

    def settle(*args, **kwargs):
        if args[0] == "send":
            kind = "rename" if args[2].startswith("/rename") else "assignment"
            if kind not in rejected:
                rejected.add(kind)
                raise HcFailure("send", 5, "still starting; nothing was typed; check hc read")
        return hc(*args, **kwargs)

    s.hc_call = settle
    s.step()
    assert rejected == {"rename", "assignment"}
    first = helpers.active(run)[0]
    assert first["status"] == "pending" and not first.get("continuations")
    s.step()
    assert helpers.active(run)[0]["status"] == "sent"
    assert len([c for c in hc.calls if c[0] == "run"]) == 2
    assert any(c[0] == "wait" for c in hc.calls)
    assert len([c for c in hc.calls if c[0] == "send" and not c[2].startswith("/rename")]) == 2


def test_uncertain_send_error_does_not_reset_dispatch_or_rename(run):
    import pytest

    from rsc.workflow.supervisor import HcFailure

    s = supervisor(run)
    pending(run)
    hc = s.hc_call

    def uncertain(*args, **kwargs):
        if args[0] == "send":
            raise HcFailure("send", 5, "connection lost; delivery is uncertain")
        return hc(*args, **kwargs)

    s.hc_call = uncertain
    with pytest.raises(HcFailure, match="uncertain"):
        s.step()
    assert s.state["terminals"]["parser-1"]["rename_sent"]
    assert len([c for c in hc.calls if c[0] == "run"]) == 1


def test_hard_failure_retires_owned_workers_but_preserves_core(run):
    import pytest

    s = supervisor(run)
    pending(run)
    s.step()
    run.store.settings_update(merge_context_tokens=1000)
    for a in helpers.active(run):
        helpers.submit(run, a["worker"], helpers.checkpoint(run, a))
    assert run.data["phase"] == "failed"
    with pytest.raises(ValueError, match="workflow failed"):
        s.step()
    assert s.state["cleanup_complete"]
    assert all(t["state"] == "finished" for t in s.hc_call.terminals.values())
    assert not any(c[:2] == ("server", "stop") for c in s.hc_call.calls)
