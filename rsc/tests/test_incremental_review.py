"""Conclusion reuse must track positive dependencies and negative query scopes."""

from copy import deepcopy

from rsc.workflow.mutations import record_key
from rsc.workflow.review import fix_groups, invalidated


def record(kind, value, batch="b1"):
    return {"kind": kind, "value": value, "evidence": [{"batch_id": batch, "block_id": "one"}]}


def model(*records):
    return {record_key(r): r for r in records}


def conclusion(*records, batch="b9", queries=()):
    return {"records": [record_key(r) for r in records], "batch": batch, "queries": list(queries)}


def test_new_record_invalidates_matching_absence_scopes_only():
    new = record("messages", {"name": "HELLO", "associated_fields": []})
    reports = {
        "hello_missing": conclusion(queries=[{"kind": "messages", "filters": {"name": "HELLO"}}]),
        "all_messages": conclusion(queries=[{"kind": "messages", "filters": {}}]),
        "bye_missing": conclusion(queries=[{"kind": "messages", "filters": {"name": "BYE"}}]),
        "other_source": conclusion(),
    }
    assert invalidated({}, model(new), reports) == {"hello_missing", "all_messages"}


def test_shared_definition_change_propagates_through_steps_and_relationships():
    channel = record(
        "channels",
        {
            "name": "control",
            "transport": "TCP",
            "endpoint_party_names": [],
            "initiator_party_name": None,
            "acceptor_party_name": None,
            "endpoint_sources": [],
        },
    )
    start = record(
        "steps",
        {"name": "start", "action": "connect", "channel_name": "control", "party_name": "client"},
        batch="b2",
    )
    stop = record(
        "steps",
        {"name": "stop", "action": "close", "channel_name": "control", "party_name": "client"},
        batch="b3",
    )
    edge = record(
        "step_dependencies",
        {
            "prior_step": "start",
            "dependent_step": "stop",
            "condition": "success",
            "field_relations": [],
        },
        batch="b4",
    )
    unrelated = record("messages", {"name": "HELLO", "associated_fields": []}, batch="b5")
    before = model(channel, start, stop, edge, unrelated)
    after = deepcopy(before)
    after[record_key(channel)]["value"]["transport"] = "UDP"
    reports = {
        "connection": conclusion(start),
        "relationship": conclusion(edge),
        "other": conclusion(unrelated),
    }
    assert invalidated(before, after, reports) == {"connection", "relationship"}


def test_uncertain_prose_change_falls_back_to_full_review():
    party = record("parties", {"name": "client", "description": "Sender"})
    updated = deepcopy(party)
    updated["value"]["description"] = "Sender except during resumption"
    reports = {"owner": conclusion(party), "remote": conclusion()}
    assert invalidated(model(party), model(updated), reports) == set(reports)


def test_origin_bookkeeping_alone_does_not_force_review():
    message = record("messages", {"name": "HELLO", "associated_fields": []})
    after = deepcopy(message)
    after["origins"] = [
        {"scope": "fix-1/parser-1", "kind": "messages", "target": {"name": "HELLO"}}
    ]
    assert not invalidated(model(message), model(after), {"report": conclusion(message)})


def test_field_inventory_changes_recheck_untyped_remote_conditions():
    message = record("messages", {"name": "HELLO", "associated_fields": [{"name": "token"}]})
    after = deepcopy(message)
    after["value"]["associated_fields"] = [{"name": "nonce"}]
    reports = {"direct": conclusion(message), "remote_prose": conclusion()}
    assert invalidated(model(message), model(after), reports) == set(reports)
    assert invalidated(model(message), {}, reports) == set(reports)


def test_new_relationship_invalidates_its_previously_unconnected_endpoints():
    start = record(
        "steps",
        {"name": "start", "action": "connect", "channel_name": "control", "party_name": "client"},
    )
    stop = record(
        "steps",
        {"name": "stop", "action": "close", "channel_name": "control", "party_name": "client"},
    )
    edge = record(
        "step_dependencies",
        {"prior_step": "start", "dependent_step": "stop", "field_relations": [], "condition": ""},
    )
    # Empty ordering conditions add no unknown prose; invalidate through the relation itself.
    before = model(start, stop)
    after = model(start, stop, edge)
    reports = {"start": conclusion(start), "stop": conclusion(stop), "remote": conclusion()}
    assert invalidated(before, after, reports) == {"start", "stop"}
    assert invalidated(after, before, reports) == {"start", "stop"}


def test_unknown_fix_scope_stays_unbounded_after_absorbing_a_named_scope():
    findings = {
        "unspecified": {"objects": []},
        "one": {"objects": [{"kind": "messages", "target": {"name": "ONE"}}]},
        "two": {"objects": [{"kind": "messages", "target": {"name": "TWO"}}]},
    }
    groups = fix_groups(findings, 3)
    assert len(groups) == 1 and set(groups[0]) == set(findings)
