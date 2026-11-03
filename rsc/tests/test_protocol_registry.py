"""Visible object search and bounded name lookup for Agent tools."""

from copy import deepcopy

import pytest
from protocol_samples import base, interactions

from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.registry import Registry


def model():
    objects = base()
    related = interactions(objects)
    return {kind: deepcopy(objects.get(kind, related.get(kind, []))) for kind in COLLECTIONS}


def test_search_uses_names_fields_and_structural_filters():
    data = model()
    registry = Registry(data)
    found = registry.search("requester", kind="parties")["items"][0]
    assert found["name"] == "requester" and "id" not in found
    messages = registry.search("cseq", kind="messages")["items"]
    assert len(messages) == 2
    sender = data["steps"][0]["sender_name"]
    found = registry.search(kind="steps", filters={"sender_name": sender})["items"]
    assert found and all(item["identity"]["sender_name"] == sender for item in found)
    assert registry.search(".*", kind="parties")["status"] == "empty"
    with pytest.raises(ValueError, match="provide query"):
        registry.search()
    with pytest.raises(ValueError, match="unsupported filters"):
        registry.search(kind="parties", filters={"sender_name": sender})


def test_lookup_returns_candidates_for_shared_field_names():
    data = model()
    registry = Registry(data)
    found = registry.lookup("CSeq", depth=1, limit=8)
    assert found["status"] == "candidates"
    assert len(found["inventory"]["messages"]) == 2
    message = registry.lookup("request")
    assert message["status"] == "ok"
    assert message["object"]["name"] == "request"
    assert message["object"]["associated_fields"][0]["name"] == "CSeq"
    assert registry.lookup("absent")["status"] == "missing"
    assert registry.lookup("send request")["object"]["id"] == "send-request"
    assert "request" in {
        row["name"] for row in registry.lookup("send request", depth=1)["inventory"]["messages"]
    }


def test_duplicate_names_return_candidates_without_choosing():
    data = model()
    data["parties"].append(deepcopy(data["parties"][0]))
    data["parties"][1]["description"] = "another requester"
    registry = Registry(data)
    found = registry.lookup("requester")
    assert found["status"] == "candidates"
    assert found["candidate_names"] == ["requester", "requester"]
    assert len(found["inventory"]["parties"]) == 2
    assert "id" not in found["inventory"]["parties"][0]
    assert registry.root("requester") is None


def test_invalid_candidates_are_visible_only_as_nonreusable_leads():
    data = model()
    probe = Registry(data)
    identity = next(
        key
        for key, kind in probe.kinds.items()
        if kind == "messages" and probe.rows[key]["name"] == "request"
    )
    registry = Registry(data, invalid_ids={identity})
    found = registry.lookup("request")
    assert found["status"] == "ok" and not found["reusable"]
    summaries = registry.search("CSeq", kind="messages")["items"]
    request = next(row for row in summaries if row["name"] == "request")
    assert request["candidate_status"] == "invalid"


def test_search_pagination_is_stable_and_scoped_to_the_visible_revision():
    data = model()
    registry = Registry(data, revision=5)
    first = registry.search("CSeq", kind="messages", top_k=1)
    second = registry.search("CSeq", kind="messages", top_k=1, cursor=first["next_cursor"])
    assert first["items"][0]["name"] != second["items"][0]["name"]
    assert second["next_cursor"] is None
    with pytest.raises(ValueError, match="cursor"):
        Registry(data, revision=6).search("CSeq", kind="messages", cursor=first["next_cursor"])


def test_registry_views_and_lookup_results_do_not_share_mutable_definitions():
    data = model()
    registry = Registry(data)
    data["parties"][0]["name"] = "changed outside"
    result = registry.lookup("requester")
    result["object"]["name"] = "changed response"
    assert registry.lookup("requester")["object"]["name"] == "requester"
    assert registry.search("changed")["status"] == "empty"
