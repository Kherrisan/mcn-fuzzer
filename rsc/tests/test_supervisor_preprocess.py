"""Deterministic preprocessing preserves source gates and crash-safe dispatch ownership."""

import json
from copy import deepcopy

import pytest

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.supervisor import Supervisor


def setup(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Role\n\nThe client connects and sends HELLO.\n")
    w = Workflow.create(tmp_path / "task", [source], "Example")
    s = Supervisor.__new__(Supervisor)
    s.root = w.root
    s.path = w.root / "supervisor.json"
    s.state = {"terminals": {}}
    calls = []

    def rpc(name, arguments=None):
        calls.append(name)
        return w.call("parser", name, arguments, origin="supervisor")

    s.workflow_call = rpc
    return w, s, calls


def test_preprocess_performs_same_deterministic_tools_without_reading_or_extracting(tmp_path):
    w, s, calls = setup(tmp_path)
    assert s.preprocess()
    assert calls == ["workflow_status", "claim_assignment", "prepare_sources", "advance_phase"]
    assert w.data["phase"] == "parse" and w.data["documents"] and w.data["batches"]
    assert not w.data["records"] and not w.data["receipts"] and not w.data["processed"]
    assert not w.data["assignment"]["claimed"]
    assert not w.workflow_status()["gate"]["ready"]
    events = [json.loads(x[0]) for x in w.db.execute("select body from events")]
    assert all(e["origin"] == "supervisor" for e in events)


@pytest.mark.parametrize("lost", ["claim_assignment", "prepare_sources", "advance_phase"])
def test_lost_reply_resumes_without_repreparing_or_advancing_twice(tmp_path, lost):
    w, s, calls = setup(tmp_path)
    rpc = s.workflow_call

    def lose(name, arguments=None):
        result = rpc(name, arguments)
        if name == lost:
            raise OSError("reply lost after commit")
        return result

    s.workflow_call = lose
    with pytest.raises(OSError, match="reply lost"):
        s.preprocess()
    s.state = json.loads(s.path.read_text())
    s.workflow_call = rpc
    s.preprocess()
    assert w.data["phase"] == "parse"
    assert calls.count("claim_assignment") == 1
    assert calls.count("prepare_sources") == 1
    assert calls.count("advance_phase") == 1
    assert len(w.data["phase_history"]) == 1


@pytest.mark.parametrize("claimed", [False, True])
def test_existing_or_uncertain_agent_dispatch_is_not_taken_over(tmp_path, claimed):
    w, s, calls = setup(tmp_path)
    if claimed:
        w.call("parser", "claim_assignment", {"dispatch_id": w.data["assignment"]["dispatch_id"]})
    else:
        s.state["dispatch_id"] = w.data["assignment"]["dispatch_id"]
        s.state["delivery"] = "sending"
    before = deepcopy(w.data)
    assert not s.preprocess()
    assert calls == ["workflow_status"] and w.data == before


def test_prepared_source_tampering_still_blocks_advance(tmp_path):
    w, s, calls = setup(tmp_path)
    dispatch = w.data["assignment"]["dispatch_id"]
    s.state["preprocess_dispatch_id"] = dispatch
    s.workflow_call("claim_assignment", {"dispatch_id": dispatch})
    s.workflow_call("prepare_sources")
    snapshot = next((w.root / "snapshots").glob("*.json"))
    snapshot.write_text("{}")
    with pytest.raises(WorkflowError, match="snapshot file changed"):
        s.preprocess()
    assert w.data["phase"] == "preprocess"
    assert calls.count("prepare_sources") == 1


def test_preprocess_uses_authenticated_socket_and_preserves_role_permissions(tmp_path):
    import threading

    from rsc.workflow.server import TaskServer, request

    w, s, _ = setup(tmp_path)
    del s.workflow_call
    s.socket = tmp_path / "s.sock"
    s.token = (w.root / "parser.token").read_text().strip()
    reviewer_token = (w.root / "reviewer.token").read_text().strip()
    with TaskServer(s.socket, w) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(WorkflowError, match="forbidden"):
                request(
                    s.socket,
                    {
                        "op": "call",
                        "token": reviewer_token,
                        "name": "prepare_sources",
                        "origin": "supervisor",
                    },
                )
            assert s.preprocess()
            assert w.data["phase"] == "parse" and not w.data["receipts"]
            events = [json.loads(row[0]) for row in w.db.execute("select body from events")]
            assert all(e["origin"] == "supervisor" for e in events if e["status"] == "ok")
        finally:
            service.shutdown()
            thread.join()
