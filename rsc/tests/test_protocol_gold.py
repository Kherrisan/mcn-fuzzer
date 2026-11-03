"""Hand annotations check protocol representation, not an LLM's extraction accuracy."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rsc.artifacts import read_json
from rsc.ingest import parse_html
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import object_errors

FIXTURES = Path(__file__).parent / "fixtures"


def gold_model(name):
    gold = read_json(FIXTURES / "protocol" / f"{name}.json")
    source = FIXTURES / "profuzzbench-rfc" / gold["rfc_fixture"]
    doc = parse_html(
        source.read_text(),
        source_path=gold["rfc_fixture"],
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        doc_id=source.stem,
    ).document
    values = {**gold["objects"], **gold["interactions"]}
    model = {kind: values.get(kind, []) for kind in COLLECTIONS}
    assert object_errors(model, {source.stem: doc}) == {}
    return model


@pytest.mark.parametrize("name", ["ftp", "rtsp"])
def test_annotations_match_the_protocol_contract(name):
    gold_model(name)


def test_ftp_connection_direction_and_endpoint_sources():
    model = gold_model("ftp")
    channels = {channel["name"]: channel for channel in model["channels"]}
    messages = {message["name"]: message for message in model["messages"]}
    assert messages["PASV"]["associated_fields"] == []
    assert {field["name"] for field in messages["227"]["associated_fields"]} == {"host", "port"}
    assert {party["name"] for party in model["parties"]} == {
        "user-PI",
        "server-PI",
        "user-DTP",
        "server-DTP",
    }
    for name, initiator, acceptor in [
        ("control", "user-PI", "server-PI"),
        ("active data", "server-DTP", "user-DTP"),
        ("passive data", "user-DTP", "server-DTP"),
    ]:
        assert channels[name]["initiator_party_name"] == initiator
        assert channels[name]["acceptor_party_name"] == acceptor
    sources = [source for channel in model["channels"] for source in channel["endpoint_sources"]]
    assert sources
    assert all(set(source) == {"party_name", "parameter", "from"} for source in sources)
    assert any("PORT" in source["from"] for source in sources)
    assert {step["sender_name"] for step in model["steps"]} == {"user-PI", "server-PI"}
    assert all(step["name"] and step["id"] for step in model["steps"])
    assert all("effects" not in step for step in model["steps"])
    assert all(
        set(field) == {"name"}
        for message in model["messages"]
        for field in message["associated_fields"]
    )


def test_rtsp_cseq_retransmissions_and_session_reuse():
    model = gold_model("rtsp")
    relations = [
        relation for edge in model["step_dependencies"] for relation in edge["field_relations"]
    ]
    assert sum(relation["prior_field"] == "CSeq" for relation in relations) == 4
    assert any(relation["prior_field"] == "Session" for relation in relations)
    assert all(set(relation) == {"prior_field", "dependent_field"} for relation in relations)
    retry = next(
        edge for edge in model["step_dependencies"] if "acknowledgement" in edge["condition"]
    )
    assert retry["prior_step"] == retry["dependent_step"]
    assert retry["field_relations"][0]["prior_field"] == "CSeq"
    increase = next(
        edge
        for edge in model["step_dependencies"]
        if "excluding retransmissions" in edge["condition"]
    )
    assert increase["prior_step"] != increase["dependent_step"]
    assert increase["field_relations"]
    channel = model["channels"][0]
    assert channel["connection"] == "connectionless"
    assert channel["initiator_party_name"] is None and channel["acceptor_party_name"] is None
    assert any("subsequent request" in edge["condition"] for edge in model["step_dependencies"])
    assert all("id" not in edge and "strength" not in edge for edge in model["step_dependencies"])
