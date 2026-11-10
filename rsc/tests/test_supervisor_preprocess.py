"""Coordinator preprocessing is deterministic, resumable and inaccessible to workers."""

import json

import pytest
from test_parallel_supervisor import supervisor

from rsc.workflow import Workflow, WorkflowError


def setup(tmp_path):
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Role\n\nThe client connects and sends HELLO.\n")
    w = Workflow.create(tmp_path / "task", [source], "Example")
    s = supervisor(w)
    calls, rpc = [], s.workflow_call

    def tracked(name, arguments=None):
        calls.append(name)
        return rpc(name, arguments)

    s.workflow_call = tracked
    return w, s, calls


def test_preprocess_needs_no_agent_claim_or_source_read(tmp_path):
    w, s, calls = setup(tmp_path)
    assert s.step()
    assert calls == ["workflow_status", "prepare_sources", "advance_phase"]
    assert w.data["phase"] == "parse" and w.documents and w.batches
    assert not w.db.execute("SELECT 1 FROM records").fetchone()
    assert not w.db.execute("SELECT 1 FROM receipts").fetchone()
    assert all(a["status"] == "pending" for a in w.store.assignments())
    assert not s.hc_call.calls
    events = [json.loads(r[0]) for r in w.db.execute("SELECT body FROM events")]
    assert all(e["origin"] == "supervisor" for e in events)
    w.db.close()


@pytest.mark.parametrize("lost", ["prepare_sources", "advance_phase"])
def test_lost_reply_resumes_immutable_preparation_and_advances_only_once(
    tmp_path, lost, monkeypatch
):
    from rsc.workflow import state

    prepare, count = state.prepare, []

    def counted(*args):
        count.append(1)
        return prepare(*args)

    monkeypatch.setattr(state, "prepare", counted)
    w, s, calls = setup(tmp_path)
    rpc = s.workflow_call

    def lose(name, arguments=None):
        result = rpc(name, arguments)
        if name == lost:
            raise OSError("reply lost after commit")
        return result

    s.workflow_call = lose
    with pytest.raises(OSError, match="reply lost"):
        s.step()
    s.workflow_call = rpc
    assert s.step()
    assert w.data["phase"] == "parse" and len(count) == 1
    assert calls.count("advance_phase") == 1
    assert len(w.store.assignments()) == 1
    w.db.close()


def test_tampered_source_snapshot_blocks_preprocessing_resume(tmp_path):
    w, s, _ = setup(tmp_path)
    s.workflow_call("prepare_sources")
    snapshot = next((w.root / "snapshots").glob("*.json"))
    snapshot.write_text("{}")
    with pytest.raises(WorkflowError, match="source snapshot changed"):
        s.step()
    assert w.data["phase"] == "preprocess"
    w.db.close()


def test_workers_cannot_impersonate_the_coordinator_over_socket(tmp_path):
    import threading

    from rsc.workflow.server import TaskServer, request

    w, s, _ = setup(tmp_path)
    s.step()
    path = tmp_path / "test.sock"
    token = (w.root / "parser-1.token").read_text()
    with TaskServer(path, w) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(WorkflowError, match="forbidden"):
                request(
                    path,
                    {
                        "op": "call",
                        "token": token,
                        "name": "prepare_sources",
                        "origin": "supervisor",
                    },
                )
            request(
                path,
                {
                    "op": "call",
                    "token": (w.root / "coordinator.token").read_text(),
                    "name": "workflow_status",
                    "origin": "health",
                },
            )
            events = [json.loads(r[0]) for r in w.db.execute("SELECT body FROM events")]
            assert events[-1]["origin"] == "health"
            assert events[-2]["origin"] == "agent"
        finally:
            service.shutdown()
            thread.join()
    w.db.close()
