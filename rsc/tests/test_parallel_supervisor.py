"""Exercise the new dispatcher against durable assignments without launching models."""

import json

import pytest
import test_parallel_workflow as helpers

from rsc.workflow.supervisor import HcObservationTimeout, Supervisor

run = helpers.run


class FakeHc:
    def __init__(self):
        self.calls, self.terminals = [], {}

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        command = args[0]
        if command == "run":
            name = args[args.index("--name") + 1]
            address = f"t{len(self.terminals) + 1}"
            self.terminals[address] = {
                "address": address,
                "name": name,
                "state": "live",
                "status": {"state": "idle"},
                "turn": 0,
                "renamed": False,
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
                "envs": [],
                "args": [],
            },
        }
        for role in ("parser", "reviewer")
    }
    s.config = {"workflow": {"batch_max_tokens": 12000}, "resolved_agents": s.roles}
    s.retry_worker, s.wait_index = None, 0
    s.hc_call = FakeHc()
    s.workflow_call = lambda name, arguments=None: w.call(
        helpers.who(w, "coordinator"), name, arguments or {}, origin="supervisor"
    )
    return s


def test_serial_ready_and_same_thread_name_then_fair_dispatch(run):
    s = supervisor(run)
    assert s.step()
    calls = s.hc_call.calls
    launches = [i for i, c in enumerate(calls) if c[0] == "run"]
    renames = [i for i, c in enumerate(calls) if c[0] == "send" and c[2].startswith("/rename")]
    sends = [i for i, c in enumerate(calls) if c[0] == "send" and not c[2].startswith("/rename")]
    assert launches[0] < renames[0] < launches[1] < renames[1] < sends[0] < sends[1]
    assert calls[-1][0] == "wait" and all(c[0] != "wait" for c in calls[sends[0] : sends[-1]])
    assert all(a["status"] == "sent" for a in run.store.assignments())
    contexts = [s.context(a) for a in run.store.assignments()]
    assert len({c.directory for c in contexts}) == len({c.namespace for c in contexts}) == 2
    for context in contexts:
        launch = json.loads((context.directory / "launch.json").read_text())
        assert "check_for_update_on_startup=false" in launch["argv"]
        assert (
            json.loads((context.directory / "session.json").read_text())["name"]
            == context.terminal_name
        )
    previous = len(s.hc_call.calls)
    assert s.step()
    assert not any(c[0] in {"run", "send"} for c in s.hc_call.calls[previous:])


def test_observation_timeout_does_not_fence_or_resend(run):
    s = supervisor(run)
    s.step()
    observe = s.observe

    def transient(worker):
        if worker == "parser-1":
            raise HcObservationTimeout("observation expired")
        return observe(worker)

    s.observe = transient
    previous = len(s.hc_call.calls)
    assert s.step()
    assert all(a["attempt"] == 1 for a in run.store.assignments())
    assert not any(c[0] in {"kill", "run", "send"} for c in s.hc_call.calls[previous:])
    assert s.hc_call.calls[-1][0] == "wait"


def test_confirmed_exit_retries_and_resumes_only_that_worker(run):
    s = supervisor(run)
    s.step()
    failed = s.state["terminals"]["parser-1"]["address"]
    s.hc_call.terminals[failed]["state"] = "finished"
    s.step()
    a = run.store.assignment("parse-0/parser-1")
    assert a["attempt"] == 2 and a["status"] == "pending"
    s.step()
    spec = json.loads((run.root / "agents/parser-1/launch.json").read_text())
    assert "resume" in spec["argv"]
    assert run.store.assignment("parse-0/parser-2")["attempt"] == 1


def test_recovery_after_lost_launch_response_reuses_terminal(run):
    s = supervisor(run)
    a = run.store.assignments()[0]
    context = s.context(a)
    s.hc_call("run", "--name", context.terminal_name)
    s.state["launching"] = {"name": context.terminal_name, "assignment": a["name"], "attempt": 1}
    s.recover_launch(run.store.assignments())
    assert s.state["terminals"][a["worker"]]["address"] == "t1"
    assert len([c for c in s.hc_call.calls if c[0] == "run"]) == 1


def test_cleanup_rejects_changed_terminal_identity(run):
    s = supervisor(run)
    s.step()
    address = s.state["terminals"]["parser-1"]["address"]
    s.hc_call.terminals[address]["name"] = "someone-else"
    with pytest.raises(ValueError, match="another terminal"):
        s.retire("parser-1")
    assert s.hc_call.terminals[address]["state"] == "live"
