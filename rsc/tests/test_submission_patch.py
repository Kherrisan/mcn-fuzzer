from copy import deepcopy

import pytest
import test_result_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.store import atomic_json, read_json


def test_preflight_and_patch_share_validation_and_commit_atomically(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    h.parsed(w)
    h.review(w, [h.finding(w)])
    assignment = h.active(w)[0]
    worker, base = assignment["worker"], assignment["candidate_path"]
    frozen = w.checkpoint(base)
    request = next(row for row in frozen["items"] if row["value"].get("name") == "send request")
    invalid = deepcopy(request["value"])
    invalid["channel_name"] = "unknown channel"
    invalid["message_name"] = "unknown message"
    note = {"finding": "F1", "note": "Added the missing guard on send request."}
    patch = {
        "base": base,
        "handled_findings": [note],
        "changes": [
            {
                "op": "replace",
                "kind": "steps",
                "match": {"name": "send request"},
                "value": invalid,
                "evidence": request["evidence"],
            }
        ],
    }
    path = w.root / "work" / worker / "patch.json"

    def preflight():
        atomic_json(path, patch)
        return h.call(w, worker, "validate_model", path=str(path))

    result = preflight()
    assert not result["valid"]
    assert {e["path"].rsplit(".", 1)[-1] for e in result["errors"]} == {
        "channel_name",
        "message_name",
    }
    assert w.store.assignment(assignment["name"]) == assignment
    assert not (w.root / assignment["output_path"]).exists()

    with pytest.raises(WorkflowError, match="unknown channel"):
        h.call(w, worker, "submit_model_patch", path=str(path))
    assert w.checkpoint(base) == frozen
    assert not (w.root / assignment["output_path"]).exists()

    patch["changes"][0]["value"] = {**request["value"], "condition": "corrected guard"}
    del patch["handled_findings"]
    assert "missing handling notes for findings: F1" in preflight()["errors"][0]["message"]
    patch["handled_findings"] = [note, note]
    assert "duplicate handled finding: F1" in preflight()["errors"][0]["message"]
    patch["handled_findings"] = [{**note, "finding": "F99"}]
    assert "unknown or duplicate handled finding: F99" in preflight()["errors"][0]["message"]
    patch["handled_findings"] = [note]
    patch["base"] = "stale.json"
    assert not preflight()["valid"]
    patch["base"] = base
    assert preflight() == {"valid": True, "errors": []}
    assert h.call(w, worker, "submit_model_patch", path=str(path))["stop_turn"]
    scope = read_json(w.root / h.active(w)[0]["review_scope_path"])
    assert scope["before"] == base and scope["full_review_reason"] is None
    assert scope["fix_notes"] == [note]
    assert read_json(w.root / assignment["output_path"])["handled_findings"] == [note]
    assert all(row["object"]["name"] == "send request" for row in scope["changed"])
    actual = w.checkpoint(assignment["output_path"])
    assert len(actual["items"]) == len(frozen["items"])
    assert all(row in actual["items"] for row in frozen["items"] if row != request)


def test_scope_uses_both_versions_without_walking_unrelated_order_edges():
    from rsc.workflow.review_scope import build_scope

    def row(kind, batch, **value):
        return {"kind": kind, "value": value, "evidence": [{"batch_id": batch, "block_id": batch}]}

    def step(name, batch, *, reads=(), writes=()):
        processing = (
            {"on_receive": [{"reads": reads, "writes": writes, "rule": "related processing"}]}
            if reads or writes
            else {}
        )
        return row("steps", batch, name=name, receiver_name="Peer", processing=processing)

    def edge(prior, dependent, batch):
        return row(
            "step_dependencies",
            batch,
            prior_step=prior,
            dependent_step=dependent,
            condition="",
            field_relations=[],
        )

    before = [
        row("states", "B1", name="old", party_name="Peer", source="processing"),
        step("write", "B2", writes=["state.old"]),
        step("read", "B3", reads=["state.old"]),
        step("neighbor", "B4"),
        step("unrelated", "B5"),
        edge("read", "neighbor", "B4"),
        edge("neighbor", "unrelated", "B5"),
    ]
    after = [
        row("states", "B1", name="new", party_name="Peer", source="processing"),
        step("write", "B2", writes=["state.new"]),
        step("read", "B3", reads=["state.new"]),
        *before[3:],
    ]

    def scope(old, new):
        return build_scope(
            {"items": old, "unresolved": []},
            {"items": new, "unresolved": []},
            ["B1", "B2", "B3", "B4", "B5"],
            [],
            before_path="old.json",
            after_path="new.json",
        )

    selected = scope(before, after)
    assert selected["batches"] == ["B1", "B2", "B3"]
    assert {
        r["object"]["name"] for r in selected["changed"] if r["object"]["kind"] == "states"
    } == {
        "old",
        "new",
    }
    after[-2] = edge("write", "unrelated", "B4")
    moved = scope(before, after)
    assert moved["batches"] == ["B1", "B2", "B3", "B4", "B5"]
    assert {a["object"].get("name") for a in moved["affected"]} >= {"neighbor", "unrelated"}


@pytest.mark.parametrize("edit", ["rule", "write"])
def test_processing_change_traverses_only_changed_outputs_and_aggregates_reasons(edit):
    from rsc.workflow.review_scope import build_scope

    def op(reads, writes, rule):
        return {"reads": reads, "writes": writes, "rule": rule}

    def step(name, *ops):
        return {
            "kind": "steps",
            "value": {"name": name, "receiver_name": "peer", "processing": {"on_receive": ops}},
            "evidence": [{"batch_id": name, "block_id": name}],
        }

    if edit == "rule":
        changed = op(["state.config"], ["state.selected"], "old")
        replacement = {**changed, "rule": "new"}
    else:
        changed = op(["state.config"], ["state.shared"], "old")
        replacement = {**changed, "writes": ["state.shared", "state.selected"]}
    sibling = op([], ["state.sibling"], "unrelated")
    source = step("source", changed, sibling)
    consumer = step("consumer", op(["state.selected"], ["state.derived"], "derive"))
    rows = [
        source,
        consumer,
        step("downstream", op(["state.derived"], [], "uses derived")),
        step("config", op([], ["state.config"], "generate config")),
        step("config reader", op(["state.config"], [], "also uses config")),
        step("shared reader", op(["state.shared"], [], "uses shared")),
        step("sibling reader", op(["state.sibling"], [], "uses sibling")),
    ]
    updated = step("source", replacement, sibling)
    scope = build_scope(
        {"items": rows, "unresolved": []},
        {"items": [updated, *rows[1:]], "unresolved": []},
        [r["value"]["name"] for r in rows],
        [],
        before_path="old.json",
        after_path="new.json",
    )
    assert scope["batches"] == ["source", "consumer", "downstream", "config"]
    assert len({r["object"]["name"] for r in scope["affected"]}) == len(scope["affected"])
    record = next(r for r in scope["affected"] if r["object"]["name"] == "consumer")
    assert len(record["reasons"]) == 2


def test_message_field_propagates_only_through_its_own_relations():
    from rsc.workflow.review_scope import build_scope

    def row(kind, batch, **value):
        return {"kind": kind, "value": value, "evidence": [{"batch_id": batch, "block_id": batch}]}

    message = row("messages", "definition", name="Offer", associated_fields=[{"name": "first"}])
    sender = row("steps", "offer", name="offer", message_name="Offer")
    # Only one of the two consumers uses the added field in the new snapshot.
    additions = [
        row("steps", "first", name="first", message_name="Answer"),
        row("steps", "second", name="second", message_name="Answer"),
    ]

    def edge(field):
        return row(
            "step_dependencies",
            field,
            prior_step="offer",
            dependent_step=field,
            condition="",
            field_relations=[{"prior_field": f"Offer.{field}", "dependent_field": "answer"}],
        )

    updated = deepcopy(message)
    updated["value"]["associated_fields"].append({"name": "second"})
    scope = build_scope(
        {"items": [message, sender, *additions, edge("first")], "unresolved": []},
        {"items": [updated, sender, *additions, edge("first"), edge("second")], "unresolved": []},
        ["definition", "offer", "first", "second"],
        [],
        before_path="old.json",
        after_path="new.json",
    )
    assert scope["batches"] == ["definition", "offer", "second"]
