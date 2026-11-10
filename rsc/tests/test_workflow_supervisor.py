"""Dispatch recovery and phase handoffs use worker identity and durable claims."""

import json

import pytest
import test_parallel_workflow as h
from test_parallel_supervisor import supervisor

from rsc.workflow import WorkflowError
from rsc.workflow.supervisor import HcObservationTimeout

run = h.run


def test_busy_claimed_work_survives_supervisor_restart_without_resend(run):
    s = supervisor(run)
    s.step()
    h.claim(run, "parser-1")
    restored = supervisor(run)
    restored.state = json.loads(s.path.read_text())
    restored.hc_call = s.hc_call
    before = len(s.hc_call.calls)
    assert restored.step()
    assert not any(c[0] in {"send", "run", "kill"} for c in s.hc_call.calls[before:])
    assert run.data["phase"] == "parse"


def test_a_finished_turn_cannot_replace_the_assignment_submission(run):
    s = supervisor(run)
    s.step()
    h.claim(run, "parser-1")
    t = s.hc_call.terminals[s.state["terminals"]["parser-1"]["address"]]
    t.update(turn=1, status={"state": "done"})
    before = len(s.hc_call.calls)
    with pytest.raises(WorkflowError, match="without submitting"):
        s.step()
    assert not any(c[0] in {"run", "send"} for c in s.hc_call.calls[before:])


def test_lost_send_response_is_not_blindly_repeated(run):
    s = supervisor(run)
    hc = s.hc_call

    def lost(*args, **kwargs):
        result = hc(*args, **kwargs)
        if args[0] == "send" and not args[2].startswith("/rename"):
            raise HcObservationTimeout("send reply lost")
        return result

    s.hc_call = lost
    with pytest.raises(HcObservationTimeout):
        s.step()
    assert run.store.assignment("parse-0/parser-1")["status"] == "sent"
    s.hc_call = hc
    s.step()
    sends = [c for c in hc.calls if c[0] == "send" and not c[2].startswith("/rename")]
    assert len([c for c in sends if c[1] == s.state["terminals"]["parser-1"]["address"]]) == 1


@pytest.mark.parametrize("state", ["blocked", "error", "fatal"])
def test_blocked_or_fatal_workers_never_receive_forced_input(run, state):
    s = supervisor(run)
    s.step()
    address = s.state["terminals"]["parser-1"]["address"]
    if state == "fatal":
        (run.root / "agents/parser-1/fatal.json").write_text('{"error":"incomplete tool delivery"}')
    else:
        s.hc_call.terminals[address]["status"] = {"state": state}
    before = len(s.hc_call.calls)
    with pytest.raises(WorkflowError, match="attention"):
        s.step()
    assert not any(c[0] in {"send", "run", "kill"} for c in s.hc_call.calls[before:])


def test_live_worker_retry_is_rejected_and_cleanup_is_idempotent(run):
    s = supervisor(run)
    s.step()
    s.retry_worker = "parser-1"
    with pytest.raises(WorkflowError, match="cannot retry a live worker"):
        s.step()
    s.cleanup()
    before = len(s.hc_call.calls)
    s.cleanup()
    assert len(s.hc_call.calls) == before
    assert s.state["cleanup_complete"] and not s.state["terminals"]


def test_phase_change_releases_processes_and_resumes_the_same_named_conversation(run):
    s = supervisor(run)
    s.step()
    for worker in ("parser-1", "parser-2"):
        h.parse(run, worker)
    assert s.step() and run.data["phase"] == "merge"
    assert not s.state["terminals"]
    assert len(s.state["sessions"]) == 2
    assert s.step()
    launch = json.loads((run.root / "agents/parser-1/launch.json").read_text())
    assert launch["argv"][-2:] == [
        "resume",
        s.context(run.store.assignment("merge-0/parser-1")).terminal_name,
    ]
    assert len(s.state["terminals"]) == 1
