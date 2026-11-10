"""Protect assignment delivery across busy agents, crashes and workflow handoffs."""

import pytest

from rsc.workflow import WorkflowError
from rsc.workflow.supervisor import Supervisor


def supervisor(tmp_path, *, claimed=False, agent="idle"):
    s = Supervisor.__new__(Supervisor)
    s.root = tmp_path
    s.path = tmp_path / "supervisor.json"
    s.state = {"terminals": {"parser": "t7", "reviewer": "t8"}}
    s.status = lambda: {
        "phase": "repair",
        "assignment": {
            "dispatch_id": "repair-2",
            "role": "parser",
            "claimed": claimed,
            "phase": "repair",
            "round": 1,
            "revision": 2,
        },
    }
    calls = []

    def hc(*args, **kwargs):
        calls.append(args)
        if args[0] == "get":
            return {"state": "live", "status": {"state": agent}}
        return {}

    s.hc_call = hc
    return s, calls


def test_busy_agent_is_waited_without_sending(tmp_path):
    s, calls = supervisor(tmp_path, agent="working")
    assert s.step()
    assert [c[0] for c in calls] == ["get", "wait"]


def test_idle_claimed_assignment_does_not_advance(tmp_path):
    s, calls = supervisor(tmp_path, claimed=True)
    with pytest.raises(WorkflowError, match="without completing"):
        s.step()
    assert all(c[0] != "send" for c in calls)


def test_send_is_recorded_and_never_blindly_repeated(tmp_path):
    s, calls = supervisor(tmp_path)
    assert s.step()
    sent = [c for c in calls if c[0] == "send"]
    assert len(sent) == 1 and sent[0][1] == "t7"
    assert "repair-2" in sent[0][2]
    assert s.state["delivery"] == "sent"
    with pytest.raises(WorkflowError, match="uncertain"):
        s.step()
    assert len([c for c in calls if c[0] == "send"]) == 1


def test_blocked_agent_does_not_receive_forced_input(tmp_path):
    s, calls = supervisor(tmp_path, agent="blocked")
    with pytest.raises(WorkflowError, match="attention"):
        s.step()
    assert all(c[0] != "send" for c in calls)


def test_review_repair_reuses_original_terminal_addresses(tmp_path):
    s, calls = supervisor(tmp_path)
    assignment = {
        "dispatch_id": "review-1",
        "role": "reviewer",
        "claimed": False,
        "phase": "review",
        "round": 1,
        "revision": 2,
    }
    s.status = lambda: {"phase": assignment["phase"], "assignment": assignment.copy()}
    assert s.step()
    assignment.update(dispatch_id="repair-1", role="parser", phase="repair")
    assert s.step()
    assignment.update(dispatch_id="review-2", role="reviewer", phase="review", round=2)
    assert s.step()
    assert [c[1] for c in calls if c[0] == "send"] == ["t8", "t7", "t8"]
    messages = [c[2] for c in calls if c[0] == "send"]
    assert "Round: 1." in messages[0] and "This is the first review" in messages[0]
    assert "do not request historical diffs" in messages[0]
    assert "Round: 2." in messages[2] and "EVERY get_changes page" in messages[2]
    assert all(c[0] != "run" for c in calls)


def test_explicit_retry_allows_exactly_one_redelivery(tmp_path):
    s, calls = supervisor(tmp_path, claimed=True)
    s.state["dispatch_id"] = "repair-2"
    s.retry_dispatch = "repair-2"
    assert s.step()
    assert s.retry_dispatch is None
    with pytest.raises(WorkflowError, match="without completing"):
        s.step()
    assert len([c for c in calls if c[0] == "send"]) == 1


def test_recovered_supervisor_waits_on_claimed_work_without_resending(tmp_path):
    s, _ = supervisor(tmp_path)
    s.state.update(dispatch_id="repair-2", delivery="sent")
    s.save()
    restored, calls = supervisor(tmp_path, claimed=True, agent="working")
    import json

    restored.state = json.loads(s.path.read_text())
    assert restored.step()
    assert [c[0] for c in calls] == ["get", "wait"]


def test_cleanup_uses_owned_terminals_and_is_idempotent(tmp_path):
    s, _ = supervisor(tmp_path)
    s.task_id = "test1234"
    calls = []

    def hc(*args, **kwargs):
        calls.append(args)
        if args[0] == "get":
            role = "parser" if args[1] == "t7" else "reviewer"
            return {"name": f"rsc-{role}-test1234", "state": "live"}
        return {}

    s.hc_call = hc
    s.cleanup()
    assert [c[1] for c in calls if c[0] == "kill"] == ["t7", "t8"]
    assert s.state["cleanup_complete"]
    count = len(calls)
    s.cleanup()
    assert len(calls) == count


def test_cleanup_refuses_changed_terminal_identity(tmp_path):
    s, _ = supervisor(tmp_path)
    s.task_id = "test1234"
    calls = []

    def hc(*args, **kwargs):
        calls.append(args)
        return {"name": "unrelated-terminal", "state": "live"}

    s.hc_call = hc
    with pytest.raises(WorkflowError, match="identity"):
        s.cleanup()
    assert not any(c[0] == "kill" for c in calls)


def test_observation_timeout_reobserves_without_resend(tmp_path):
    from rsc.workflow.supervisor import HcObservationTimeout

    s, calls = supervisor(tmp_path, claimed=True)
    original = s.hc_call

    def transient(*args, **kwargs):
        s.hc_call = original
        raise HcObservationTimeout("observer timeout")

    s.hc_call = transient
    assert s.step()
    # A later observation, not a timeout, establishes the actual idle state.
    with pytest.raises(WorkflowError, match="without completing"):
        s.step()
    assert not any(c[0] in {"run", "send", "kill"} for c in calls)


def test_pi_waits_for_adapter_ready_before_sending(tmp_path):
    import json

    s, calls = supervisor(tmp_path)
    s.socket = "/tmp/test.sock"
    s.task_id = "test1234"
    s.state["agents"] = {"parser": {"provider": "pi"}}
    assert s.step()
    assert not any(c[0] == "send" for c in calls)
    assert calls[-1][:4] == ("wait", "t7", "--output", "RSC adapter ready")
    directory = tmp_path / "agents/parser"
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text(json.dumps({"namespace": "rsc_test1234_parser"}))
    assert s.step()
    assert len([c for c in calls if c[0] == "send"]) == 1


def test_adapter_fatal_error_stops_before_further_input(tmp_path):
    s, calls = supervisor(tmp_path)
    directory = tmp_path / "agents/parser"
    directory.mkdir(parents=True)
    (directory / "fatal.json").write_text('{"error":"cannot restore complete response"}')
    with pytest.raises(WorkflowError, match="fatal delivery"):
        s.step()
    assert not calls
