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
