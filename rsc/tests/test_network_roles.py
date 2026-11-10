"""Known channel connection roles constrain network actions, with unknown roles retained."""

from copy import deepcopy

import pytest

from rsc.protocol.references import reference_errors


def records(action, actor, initiator="client", acceptor="server"):
    return {
        "client": {"kind": "parties", "value": {"name": "client", "description": "peer"}},
        "server": {"kind": "parties", "value": {"name": "server", "description": "peer"}},
        "channel": {
            "kind": "channels",
            "value": {
                "name": "transport",
                "description": "lifecycle",
                "transport": "TCP",
                "connection": "connection_oriented",
                "endpoint_party_names": ["client", "server"],
                "initiator_party_name": initiator,
                "acceptor_party_name": acceptor,
                "endpoint_sources": [],
            },
        },
        "operation": {
            "kind": "steps",
            "value": {
                "name": "operation",
                "action": action,
                "channel_name": "transport",
                "party_name": actor,
                "condition": "",
            },
        },
    }


@pytest.mark.parametrize(
    "action,correct,wrong",
    [
        ("connect", "client", "server"),
        ("listen", "server", "client"),
    ],
)
def test_network_actor_must_match_its_known_channel_role(action, correct, wrong):
    assert reference_errors(records(action, correct)) == []
    errors = reference_errors(records(action, wrong))
    assert len(errors) == 1
    assert "operation" in errors[0] and "transport" in errors[0]
    role = "initiator_party_name" if action == "connect" else "acceptor_party_name"
    assert role in errors[0]


@pytest.mark.parametrize("actor", ["client", "server"])
def test_unknown_connection_roles_and_either_endpoint_close_remain_valid(actor):
    for action in ("listen", "connect"):
        assert reference_errors(records(action, actor, None, None)) == []
    assert reference_errors(records("close", actor)) == []


def test_reversed_source_roles_use_a_separate_lifecycle_without_duplicate_network_nodes():
    rows = records("connect", "client")
    reverse = deepcopy(rows["channel"])
    reverse["value"].update(
        name="reverse", initiator_party_name="server", acceptor_party_name="client"
    )
    rows["reverse"] = reverse
    rows["reverse_connect"] = {
        "kind": "steps",
        "value": {
            "name": "server callback",
            "action": "connect",
            "channel_name": "reverse",
            "party_name": "server",
            "condition": "",
        },
    }
    rows["reverse_listen"] = {
        "kind": "steps",
        "value": {
            "name": "client callback listener",
            "action": "listen",
            "channel_name": "reverse",
            "party_name": "client",
            "condition": "",
        },
    }
    assert reference_errors(rows) == []


def test_wrong_roles_are_draft_feedback_and_block_merge_until_corrected(tmp_path):
    import test_parallel_workflow as h

    from rsc.workflow import Workflow, WorkflowError

    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Example\n\nThe client connects to the server.\n")
    w = Workflow.create(tmp_path / "task", [source], "Example", parse_workers=1)
    h.call(w, "coordinator", "prepare_sources")
    h.advance(w)
    h.claim(w, "parser-1")
    sources = h.read(w, "parser-1")
    source = next(iter(sources.values()))
    evidence = [source["batch"]["segments"][0]["citation"]]
    result = h.call(
        w,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="draft",
        changes=[
            {"op": "add", "kind": r["kind"], "value": r["value"], "evidence": evidence}
            for r in records("connect", "server").values()
        ],
    )
    assert any("initiator_party_name" in e for e in result["draft_reference_errors"])
    h.call(
        w,
        "parser-1",
        "complete_batch",
        revision=1,
        reports=[
            {
                "batch_id": source["batch"]["batch_id"],
                "receipt": source["receipt"],
                "report": {"summary": "Extracted connection", "objects": []},
            }
        ],
    )
    h.call(w, "parser-1", "advance_phase")
    h.advance(w)
    h.claim(w, "parser-1")
    with pytest.raises(WorkflowError, match="initiator_party_name"):
        h.call(w, "parser-1", "advance_phase")
    h.read(w, "parser-1")
    fixed = h.call(
        w,
        "parser-1",
        "apply_changes",
        expected_revision=0,
        operation_id="fix",
        changes=[
            {
                "op": "patch",
                "scope": "parse-0/parser-1",
                "kind": "steps",
                "target": {"name": "operation"},
                "value": {"party_name": "client"},
                "evidence": evidence,
            }
        ],
    )
    assert fixed["draft_reference_errors"] == []
    h.call(w, "parser-1", "advance_phase")
    h.advance(w)
    assert w.data["phase"] == "review"
    w.db.close()
