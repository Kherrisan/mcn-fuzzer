"""Publication validation applies the coverage rules extraction enforces."""

import json

from protocol_samples import base, interactions

from rsc.artifacts import write_json
from rsc.cli import main
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import validate_protocol, validate_protocol_file


def sample():
    """The shared request/response sample as a publishable model."""
    model = {kind: [] for kind in COLLECTIONS}
    model.update({key: value for key, value in base().items() if key != "unresolved"})
    model.update(
        {key: value for key, value in interactions(model).items() if key != "unresolved"}
    )
    model["meta"] = {
        "protocol": "demo",
        "complete": True,
        "documents": [{"doc_id": "rfc9999", "rfc": "RFC 9999", "path": "sources/doc.json"}],
    }
    model["unresolved"] = []
    return model


def with_lifecycle(model):
    """Add the listen/connect/close steps and ordering edges coverage requires."""
    model = json.loads(json.dumps(model))
    model["steps"] += [
        {
            "name": action + " control",
            "action": action,
            "channel_name": "control",
            "party_name": party,
            "condition": "",
        }
        for action, party in [
            ("listen", "responder"),
            ("connect", "requester"),
            ("close", "requester"),
        ]
    ]
    model["step_dependencies"] += [
        {
            "prior_step": "listen control",
            "dependent_step": "connect control",
            "condition": "listen before connect",
            "field_relations": [],
        },
        {
            "prior_step": "connect control",
            "dependent_step": "close control",
            "condition": "close after the exchange",
            "field_relations": [],
        },
    ]
    return model


def test_connected_channel_without_lifecycle_fails_coverage():
    errors = validate_protocol(sample(), coverage=True)
    assert [error.split(" requires ")[0] for error in errors] == [
        "Channel 'control'",
        "Channel 'control'",
        "Channel 'control'",
    ]
    assert "requires a connect step" in errors[0]
    assert "requires a listen step" in errors[1]
    assert "requires an endpoint close step" in errors[2]


def test_lifecycle_steps_and_edges_satisfy_coverage():
    assert validate_protocol(with_lifecycle(sample()), coverage=True) == []


def test_advisory_rows_never_block_publication():
    errors = validate_protocol(sample(), coverage=True)
    assert all("with no processing" not in error for error in errors)


def test_each_coverage_rule_reports_its_own_gap():
    model = with_lifecycle(sample())
    model["parties"].append({"name": "observer", "description": "reads records"})
    model["messages"] += [
        {"name": "notice", "associated_fields": []},
        {"name": "alert", "associated_fields": []},
    ]
    model["states"].append(
        {"name": "cursor", "party_name": "requester", "source": "processing"}
    )
    model["channels"].append(
        {
            "name": "datagrams",
            "description": "one-way notifications",
            "transport": "UDP",
            "connection": "connectionless",
            "endpoint_party_names": ["requester", "responder"],
            "initiator_party_name": None,
            "acceptor_party_name": None,
            "endpoint_sources": [],
        }
    )
    model["steps"].append(
        {
            "name": "send notice",
            "action": "send",
            "message_name": "notice",
            "channel_name": "control",
            "sender_name": "requester",
            "receiver_name": "responder",
            "condition": "",
        }
    )
    errors = validate_protocol(model, coverage=True)
    expected = [
        "Party 'observer' is not a channel endpoint",
        "Channel 'datagrams' has no step",
        "Message 'alert' has no send step",
        "State 'cursor' has no hook reads or writes and no processing producer",
        "Step 'send notice' has no dependency links",
    ]
    assert errors == expected


def test_duplicate_network_actions_and_shape_references_still_run():
    model = with_lifecycle(sample())
    model["steps"][0]["receiver_name"] = "missing"
    errors = validate_protocol(model, coverage=True)
    assert "reference 'missing' has 0 matches" in errors[0]


def test_diagnostics_carry_coverage_check_refs():
    rows: list[dict] = []
    validate_protocol(sample(), coverage=True, diagnostics=rows)
    assert {row["check_ref"].split(":")[0] for row in rows} == {
        "channel_requires_connect",
        "channel_requires_listen",
        "channel_requires_close",
    }


def test_file_validation_requires_explicit_coverage(tmp_path):
    model = sample()
    path = tmp_path / "protocol.json"
    write_json(path, model)
    assert validate_protocol_file(path) == []
    errors = validate_protocol_file(path, coverage=True)
    assert "requires a connect step" in errors[0]


def test_validate_command_checks_coverage_and_can_skip_it(tmp_path, capsys):
    model = sample()
    path = tmp_path / "protocol.json"
    write_json(path, model)
    assert main(["validate", "protocol", str(path)]) == 1
    stderr = capsys.readouterr().err
    assert "requires a connect step" in stderr
    assert "FAILED: 3 validation error(s)" in stderr
    assert main(["validate", "protocol", str(path), "--no-coverage"]) == 0
    assert "schema and references verified" in capsys.readouterr().out


def test_coverage_defaults_to_off_for_library_callers():
    assert validate_protocol(sample()) == []


def test_duplicate_declarations_are_reported():
    model = with_lifecycle(sample())
    model["step_dependencies"].append(dict(model["step_dependencies"][-1]))
    model["messages"][0]["associated_fields"].append(
        model["messages"][0]["associated_fields"][0]
    )
    assert validate_protocol(model, coverage=True) == [
        "message field names must be unique",
        "duplicate dependency",
    ]
