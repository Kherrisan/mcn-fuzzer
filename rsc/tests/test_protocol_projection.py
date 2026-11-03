"""Compact context delivery retains identities and semantics without duplicate source bodies."""

import json
from copy import deepcopy

import pytest
from protocol_samples import base, document, interactions
from pydantic_ai.messages import ModelRequest, UserPromptPart
from test_protocol_tools import environment

from rsc.protocol.agent_runtime import encode
from rsc.protocol.projection import artifact_views, object_view


def test_duplicate_blocks_and_objects_are_compacted_without_changing_saved_artifacts():
    doc = document()
    objects = base(doc)
    steps = interactions(objects, doc)
    source = {"doc_id": doc["meta"]["source_id"], **doc["blocks"][0]}
    record = {"status": "ok", "items": objects, "record_id": "kept-in-audit", "unresolved": []}
    pair = {"block": source, "roles": ["previous"], "compile_result": {"objects": record}}
    duplicate = deepcopy(pair)
    duplicate["roles"] = ["keyword"]
    duplicate["compile_result"]["interactions"] = {"status": "ok", "items": steps}
    another = deepcopy(pair)
    another["block"]["block_id"] = "other"
    pairs = [pair, duplicate, another]
    saved = deepcopy(pairs)
    views = artifact_views(pairs)
    assert pairs == saved
    assert len(views) == 2 and set(views[0]["roles"]) == {"previous", "keyword"}
    assert set(views[0]["block"]) == {"doc_id", "block_id", "block_type", "text"}
    assert views[0]["block"]["text"] == source["text"]
    history = views[0]["compile_result"]
    assert history["objects"]["items"]["messages"] == object_view(objects["messages"])
    assert history["interactions"]["items"]["step_dependencies"] == object_view(
        steps["step_dependencies"]
    )
    assert "record_id" not in json.dumps(views)
    assert views[1]["compile_result"]["objects"]["items"] == {}
    assert "requester" in views[1]["compile_result"]["objects"]["reused_names"]
    assert len(encode(views)) < len(encode(pairs)) * 0.6


def test_block_ids_are_scoped_by_document_and_excerpt_conflicts_are_not_hidden():
    pair = {
        "block": {"doc_id": "one", "block_id": "same", "text": "PASV"},
        "roles": ["keyword"],
        "compile_result": None,
    }
    other = deepcopy(pair)
    other["block"]["doc_id"] = "two"
    assert len(artifact_views([pair, other])) == 2
    other["block"].update(doc_id="one", text="different excerpt")
    with pytest.raises(ValueError, match="coalesced"):
        artifact_views([pair, other])


def test_only_complete_definitions_in_effective_history_count_as_read():
    from pydantic_ai.messages import ModelResponse, TextPart

    deps, _, _ = environment()
    root = deps.registry.root("requester")
    payload = deps.payload(model={"parties": [object_view(root)]})
    assert not deps.delivered_object_ids
    messages = [ModelRequest([UserPromptPart(encode(payload))])]
    deps.before_request(messages, {"run_id": "compact", "request_index": 1})
    assert "requester" in deps.delivered_object_ids
    # A compression replacement can retain only names. Removed definitions stop counting.
    deps.before_request(
        [ModelRequest([UserPromptPart(encode({"parties": [root["name"]]}))])],
        {"run_id": "compact", "request_index": 2},
    )
    assert not deps.delivered_object_ids
    deps.before_request(
        [ModelResponse([TextPart(encode({"parties": [object_view(root)]}))])],
        {"run_id": "compact", "request_index": 3},
    )
    assert deps.delivered_object_ids == {"requester"}


def test_message_preparation_preserves_historical_inputs_and_tool_results():
    from pydantic_ai.messages import ToolReturnPart

    deps, target, _ = environment()
    result = deps.context.add(*target.key)
    old = deps.payload(block={"text": "the previous focus"})
    messages = [
        ModelRequest([UserPromptPart(encode(old))]),
        ModelRequest([ToolReturnPart("read_blocks", result, "read")]),
        ModelRequest([UserPromptPart(encode(deps.payload(block={"text": "new focus"})))]),
    ]
    saved = deepcopy(messages)
    prepared = deps.prepare_messages(messages, None, None, None)
    assert prepared is messages
    assert messages == saved
    assert prepared[1].parts[0].content["artifact"]["block"]["text"]
    assert "window" not in deps.payload()
    assert "blocks" not in deps.payload()


def test_name_catalog_never_grants_full_definition_edit_permission():
    deps, _, _ = environment()
    root = deps.registry.root("requester")
    # Parties are already small: a name+description catalog may happen to match
    # the complete party shape, but the catalog is explicitly not a full read.
    message = ModelRequest(
        [
            UserPromptPart(
                encode({"event": "name_catalog", "catalog": {"parties": [object_view(root)]}})
            )
        ]
    )
    deps.before_request([message], {"run_id": "catalog", "request_index": 1})
    assert not deps.delivered_object_ids


def test_retained_interactions_allow_python_ids_and_empty_defaults_to_be_omitted():
    from pydantic_ai.messages import ModelResponse, TextPart

    from rsc.protocol.registry import Registry

    deps, _, _ = environment()
    full = interactions(base())
    deps.registry = Registry({**base(), **full})
    response = deepcopy(full)
    for step in response["steps"]:
        step.pop("id")
        step.pop("condition")
    for dependency in response["step_dependencies"]:
        if not dependency["condition"]:
            dependency.pop("condition")
        if not dependency["field_relations"]:
            dependency.pop("field_relations")
    deps.before_request(
        [ModelResponse([TextPart(encode(response))])],
        {"run_id": "next", "request_index": 1, "turn_message_start": 1},
    )
    assert {"send-request", "send-response"} <= deps.delivered_object_ids
