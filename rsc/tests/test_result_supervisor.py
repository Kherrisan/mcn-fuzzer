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
        for role in ("parser", "merge")
    }
    s.config = {"workflow": {"batch_max_tokens": 12000}, "resolved_agents": s.roles}
    s.retry_worker, s.wait_index = None, 0
    s.hc_call = FakeHc(w.root)
    s.workflow_call = lambda name, arguments=None: w.call(
        helpers.who(w, "coordinator"), name, arguments or {}, origin="supervisor"
    )
    return s


def test_serial_initialization_fair_dispatch_and_no_claim_tool(run):
    s = supervisor(run)
    # Fixture already recorded sends; create fake live assignments for this dispatcher.
    run.db.execute("UPDATE assignments SET status='pending'")
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


def test_confirmed_death_replaces_only_owned_attempt(run):
    s = supervisor(run)
    run.db.execute("UPDATE assignments SET status='pending'")
    s.step()
    saved = s.state["terminals"]["parser-1"]
    s.hc_call.terminals[saved["address"]]["state"] = "finished"
    s.step()
    a = run.store.assignment(saved["assignment"])
    assert a["attempt"] == 2 and a["status"] == "pending"
    assert run.store.assignments()[1]["attempt"] == 1


def test_cluster_restores_parser_native_session_and_does_not_redispatch_parse(run):
    s = supervisor(run)
    run.db.execute("UPDATE assignments SET status='pending'")
    s.step()
    original_thread = s.state["sessions"]["parser-1"]["id"]
    helpers.parsed(run)
    # The real supervisor owns send intent; the extraction helper recorded it for unit calls.
    run.db.execute("UPDATE assignments SET status='pending' WHERE phase='cluster'")
    s.step()
    assert s.state["sessions"]["parser-1"]["id"] == original_thread
    sent = [c for c in s.hc_call.calls if c[0] == "send" and not c[2].startswith("/rename")]
    assert len(sent) == 3
    assert "submit_clustering_result" in sent[-1][2]
    assert "parser-2" not in s.state["terminals"]
