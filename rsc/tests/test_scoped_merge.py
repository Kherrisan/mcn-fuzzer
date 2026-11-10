"""Reference identity, strict deduplication, atomic scoped renames and evidence."""

from copy import deepcopy

import pytest

from rsc.workflow import merge
from rsc.workflow.mutations import record_key

EVIDENCE = [{"batch_id": "b1", "block_id": "one"}]


def record(kind, value, evidence=None):
    return {"kind": kind, "value": value, "evidence": evidence or EVIDENCE}


def draft(description="sender", suffix="", evidence=None):
    values = [
        record("parties", {"name": "client", "description": description}, evidence),
        record("parties", {"name": "server", "description": "receiver"}, evidence),
        record(
            "channels",
            {
                "name": "control",
                "description": "control transport",
                "transport": "TCP",
                "connection": "connection_oriented",
                "endpoint_party_names": ["client", "server"],
                "initiator_party_name": "client",
                "acceptor_party_name": "server",
                "endpoint_sources": [],
            },
            evidence,
        ),
        record(
            "steps",
            {
                "name": "connect" + suffix,
                "action": "connect",
                "channel_name": "control",
                "party_name": "client",
                "condition": "",
            },
            evidence,
        ),
        record(
            "steps",
            {
                "name": "close" + suffix,
                "action": "close",
                "channel_name": "control",
                "party_name": "client",
                "condition": "",
            },
            evidence,
        ),
        record(
            "step_dependencies",
            {
                "prior_step": "connect" + suffix,
                "dependent_step": "close" + suffix,
                "condition": "success",
                "field_relations": [],
            },
            evidence,
        ),
    ]
    return {record_key(r): r for r in values}


def reference(scope, kind, name):
    return {"scope": scope, "kind": kind, "target": {"name": name}}


def apply(candidates, changes, checks=()):
    def evidence(entries):
        assert entries

    return merge.apply(candidates, changes, list(checks), evidence_check=evidence)


def test_duplicates_merge_upstream_then_downstream_with_all_evidence():
    first = draft()
    second = draft(evidence=[{"batch_id": "b2", "block_id": "two"}])
    before = deepcopy((first, second))
    merged = merge.combine([("parser-1", first), ("parser-2", second)])
    assert len(merged) == 6
    assert merge.conflicts(merged) == []
    assert all(len(r["origins"]) == len(r["evidence"]) == 2 for r in merged.values())
    assert (first, second) == before


def test_identical_names_and_values_do_not_merge_different_local_bindings():
    candidates = merge.combine([("parser-1", draft("primary")), ("parser-2", draft("secondary"))])
    assert len([r for r in candidates.values() if r["kind"] == "steps"]) == 4
    conflicts = merge.conflicts(candidates)
    parties = next(c for c in conflicts if c["type"] == "same_name" and c["kind"] == "parties")
    assert parties["differences"] == ["description"]
    assert len(parties["candidates"]) == 2
    # Once the semantic decision unifies the upstream party, deterministic
    # merging can discover the previously distinct channels, steps and edges.
    result, checks, _ = apply(
        candidates,
        [
            {
                "op": "merge",
                "sources": [
                    reference("parser-1", "parties", "client"),
                    reference("parser-2", "parties", "client"),
                ],
                "value": {"name": "client", "description": "sender"},
                "evidence": EVIDENCE,
            }
        ],
    )
    assert len(result) == 6 and not checks and not merge.conflicts(result)


def test_scoped_rename_rewrites_only_bound_local_references_and_dependency_endpoints():
    candidates = merge.combine(
        [("parser-1", draft("primary", "A")), ("parser-2", draft("secondary", "B"))]
    )
    before = deepcopy(candidates)
    result, checks, _ = apply(
        candidates,
        [
            {
                "op": "rename",
                "source": reference("parser-2", "parties", "client"),
                "name": "other_client",
                "evidence": EVIDENCE,
            },
            {
                "op": "rename",
                "source": reference("parser-2", "channels", "control"),
                "name": "other_control",
                "evidence": EVIDENCE,
            },
            {
                "op": "rename",
                "source": reference("parser-2", "steps", "connectB"),
                "name": "connect_other",
                "evidence": EVIDENCE,
            },
        ],
    )
    assert candidates == before
    channel = next(
        r for r in result.values() if r["kind"] == "channels" and r["scope"] == "parser-2"
    )
    assert channel["value"]["initiator_party_name"] == "other_client"
    a_step = next(r for r in result.values() if r["value"].get("name") == "connectA")
    b_step = next(r for r in result.values() if r["value"].get("name") == "connect_other")
    assert (a_step["value"]["channel_name"], a_step["value"]["party_name"]) == ("control", "client")
    assert (b_step["value"]["channel_name"], b_step["value"]["party_name"]) == (
        "other_control",
        "other_client",
    )
    assert any(
        r["kind"] == "step_dependencies" and r["value"]["prior_step"] == "connect_other"
        for r in result.values()
    )
    assert b_step["origins"][0]["target"] == {"name": "connectB"}
    assert all(c["type"] == "text_check" for c in checks)


def test_condition_differences_are_never_automatically_equated():
    a, b = draft(), draft()
    edge = next(r for r in b.values() if r["kind"] == "step_dependencies")
    edge["value"]["condition"] = "failure"
    merged = merge.combine([("parser-1", a), ("parser-2", b)])
    edges = [r for r in merged.values() if r["kind"] == "step_dependencies"]
    assert {r["value"]["condition"] for r in edges} == {"success", "failure"}
    assert not merge.conflicts(merged)


def test_rename_target_collision_and_later_error_leave_inputs_unchanged():
    candidates = merge.combine([("parser-1", draft())])
    before = deepcopy(candidates)
    with pytest.raises(ValueError, match="collides"):
        apply(
            candidates,
            [
                {
                    "op": "rename",
                    "source": reference("parser-1", "parties", "client"),
                    "name": "server",
                    "evidence": EVIDENCE,
                }
            ],
        )
    with pytest.raises(ValueError, match="matches"):
        apply(
            candidates,
            [
                {
                    "op": "rename",
                    "source": reference("parser-1", "parties", "client"),
                    "name": "new_client",
                    "evidence": EVIDENCE,
                },
                {
                    "op": "rename",
                    "source": reference("parser-1", "messages", "missing"),
                    "name": "message",
                    "evidence": EVIDENCE,
                },
            ],
        )
    assert candidates == before


def test_free_text_requires_explicit_source_supported_confirmation():
    model = draft()
    step = next(r for r in model.values() if r["value"].get("name") == "connect")
    step["value"]["condition"] = "client is authenticated"
    candidates = merge.combine([("parser-1", model)])
    result, checks, _ = apply(
        candidates,
        [
            {
                "op": "rename",
                "source": reference("parser-1", "parties", "client"),
                "name": "user",
                "evidence": EVIDENCE,
            }
        ],
    )
    assert any(c["type"] == "text_check" for c in checks)
    assert (
        next(r for r in result.values() if r["value"].get("name") == "connect")["value"][
            "condition"
        ]
        == "client is authenticated"
    )
    with pytest.raises(ValueError, match="unknown"):
        apply(
            result,
            [
                {
                    "op": "resolve",
                    "name": "invented-structural-waiver",
                    "decision": "ignore",
                    "evidence": EVIDENCE,
                }
            ],
            checks,
        )
    resolved, checks, _ = apply(
        result,
        [
            {
                "op": "resolve",
                "name": c["name"],
                "decision": "Source calls this peer client; retained wording is supported",
                "evidence": EVIDENCE,
            }
            for c in checks
        ],
        checks,
    )
    assert not merge.conflicts(resolved, checks)


def test_structural_conflicts_expose_location_sources_and_affected_records():
    values = draft()
    connect = next(r for r in values.values() if r["value"].get("name") == "connect")
    connect["value"]["party_name"] = "server"
    connect["evidence"] = [{"batch_id": "b2", "block_id": "connection"}]
    candidates = merge.combine([("parser-1", values)])
    conflicts = merge.conflicts(candidates)
    issue = next(c for c in conflicts if c["type"] == "structure")
    assert issue["record"] == reference("parser-1", "steps", "connect")
    assert issue["path"] == ["party_name"] and issue["status"] == "open"
    assert "initiator_party_name" in issue["description"]
    assert {r["value"]["name"] for r in issue["records"]} == {
        "connect",
        "control",
        "client",
        "server",
    }
    assert {e["batch_id"] for e in issue["evidence"]} == {"b1", "b2"}
    assert any(r["kind"] == "step_dependencies" for r in issue["affected_records"])
    assert all("scope" in r and "_key" not in r for r in issue["records"])


def test_local_channel_roles_never_use_another_same_named_candidate():
    first, second = draft("primary", "A"), draft("secondary", "B")
    channel = next(r["value"] for r in second.values() if r["kind"] == "channels")
    channel.update(initiator_party_name="server", acceptor_party_name="client")
    connect = next(r["value"] for r in second.values() if r["value"].get("name") == "connectB")
    connect["party_name"] = "server"
    for drafts in (
        [("parser-1", first), ("parser-2", second)],
        [("parser-2", second), ("parser-1", first)],
    ):
        candidates = merge.combine(drafts)
        conflicts = merge.conflicts(candidates)
        assert conflicts and {c["type"] for c in conflicts} == {"same_name"}
        channel_issue = next(c for c in conflicts if c["kind"] == "channels")
        assert {"initiator_party_name", "acceptor_party_name"} <= set(channel_issue["differences"])
        assert {
            r["target"]["name"] for r in channel_issue["affected_records"] if r["kind"] == "steps"
        } == {"connectA", "connectB", "closeA", "closeB"}


def test_field_carrier_conflict_includes_dependency_step_message_and_exact_field_location():
    values = draft()
    message = record("messages", {"name": "HELLO", "associated_fields": [{"name": "token"}]})
    send = record(
        "steps",
        {
            "name": "hello",
            "action": "send",
            "channel_name": "control",
            "sender_name": "client",
            "receiver_name": "server",
            "message_name": "HELLO",
            "condition": "",
        },
    )
    dependency = record(
        "step_dependencies",
        {
            "prior_step": "hello",
            "dependent_step": "hello",
            "condition": "next occurrence",
            "field_relations": [{"prior_field": "missing", "dependent_field": "token"}],
        },
    )
    for value in (message, send, dependency):
        values[record_key(value)] = value
    candidates = merge.combine([("parser-1", values)])
    issue = next(c for c in merge.conflicts(candidates) if c["type"] == "structure")
    assert issue["path"] == ["field_relations", 0, "prior_field"]
    assert issue["record"]["kind"] == "step_dependencies"
    assert {r["kind"] for r in issue["records"]} == {"step_dependencies", "steps", "messages"}
    assert "missing" in issue["description"] and issue["evidence"] == EVIDENCE
    with pytest.raises(ValueError, match="unresolved merge conflicts"):
        merge.final_records(candidates)


def test_ambiguous_foreign_references_keep_candidates_and_context_until_resolved():
    first, second = draft("primary"), draft("secondary")
    foreign = record(
        "channels",
        {
            "name": "foreign",
            "description": "foreign transport",
            "transport": "TCP",
            "connection": "connection_oriented",
            "endpoint_party_names": ["client", "server"],
            "initiator_party_name": "client",
            "acceptor_party_name": "server",
            "endpoint_sources": [],
        },
    )
    candidates = merge.combine(
        [
            ("parser-1", first),
            ("parser-2", second),
            ("parser-3", {record_key(foreign): foreign}),
        ]
    )
    ambiguous = [c for c in merge.conflicts(candidates) if c["type"] == "ambiguous_reference"]
    assert ambiguous and all(c["record"]["scope"] == "parser-3" for c in ambiguous)
    for issue in ambiguous:
        assert {r["scope"] for r in issue["candidates"]} == {"parser-1", "parser-2"}
        assert len(issue["definitions"]) == 2 and issue["evidence"] == EVIDENCE
        assert issue["status"] == "open" and issue["record"] in issue["affected_records"]
