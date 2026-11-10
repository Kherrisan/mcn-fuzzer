"""Named edits preserve exact selection, local unresolved references and atomicity."""

from copy import deepcopy

import pytest
import test_workflow_store as helpers

from rsc.workflow.mutations import plan_changes, record_key
from rsc.workflow.store import StoreError

store = helpers.store
claim = helpers.claim


EVIDENCE = [{"batch_id": "b1", "block_id": "block-1"}]


def evidence_check(value):
    if not isinstance(value, list) or not value:
        raise ValueError("source evidence is required")
    if any(item not in EVIDENCE for item in value):
        raise ValueError("evidence was not delivered")


def record(kind, value):
    return {"kind": kind, "value": value, "evidence": EVIDENCE}


def rows(*records):
    return {record_key(r): deepcopy(r) for r in records}


def edit(op="add", kind="parties", value=None, **kwargs):
    return {
        "op": op,
        "kind": kind,
        "value": value if value is not None else {"name": "client", "description": "sender"},
        "evidence": EVIDENCE,
        **kwargs,
    }


def test_exact_name_changes_commit_through_independent_draft_store(store):
    who = claim(store)
    changes = [edit()]
    delta, ledger, blockers = plan_changes({}, changes, evidence_check=evidence_check)
    assert ledger == [{"kind": "parties", "target": {"name": "client"}, "deleted": False}]
    assert not blockers
    store.commit_records(
        who,
        revision=0,
        operation_id="extract-role",
        arguments={"changes": changes},
        changes=delta,
        result={"revision": 1, "records": ledger},
    )
    draft, model = store.draft(who.assignment)
    assert draft["revision"] == 1
    assert list(model.values())[0]["value"]["name"] == "client"


def test_aggregates_independent_errors_without_modifying_input():
    model = rows(record("parties", {"name": "client", "description": "sender"}))
    before = deepcopy(model)
    with pytest.raises(ValueError) as exc:
        plan_changes(
            model,
            [
                edit("patch", value={"description": "changed"}, target={"name": "client"}),
                edit(value={"name": "missing-description"}),
                edit("delete", value=None, target={"name": "unknown"}),
                edit(
                    value={"name": "server", "description": "receiver"},
                    evidence=[{"batch_id": "other"}],
                ),
            ],
            evidence_check=evidence_check,
        )
    assert all(f"changes[{i}]" in str(exc.value) for i in (1, 2, 3))
    assert model == before


def test_patch_replaces_arrays_and_keeps_original_evidence():
    original = record(
        "messages",
        {"name": "USER", "associated_fields": [{"name": "user"}]},
    )
    model = rows(original)
    delta, _, _ = plan_changes(
        model,
        [
            edit("patch", "messages", {"associated_fields": []}, target={"name": "USER"}),
        ],
        evidence_check=evidence_check,
    )
    assert next(iter(delta.values()))[1]["value"]["associated_fields"] == []
    assert next(iter(delta.values()))[1]["evidence"] == EVIDENCE
    assert original["value"]["associated_fields"] == [{"name": "user"}]


def test_ambiguous_dependency_selector_is_never_guessed():
    model = rows(
        *[
            record(
                "step_dependencies",
                {
                    "prior_step": "a",
                    "dependent_step": "b",
                    "condition": c,
                    "field_relations": [],
                },
            )
            for c in ("success", "failure")
        ]
    )
    change = {
        "op": "delete",
        "kind": "step_dependencies",
        "reason": "source correction",
        "target": {"prior_step": "a", "dependent_step": "b"},
    }
    with pytest.raises(ValueError, match="2 matches"):
        plan_changes(model, [change], evidence_check=evidence_check)
    change["target"]["condition"] = "failure"
    delta, _, blockers = plan_changes(model, [change], evidence_check=evidence_check)
    assert len(delta) == 1 and next(iter(delta.values()))[1] is None
    assert any("reference" in error for error in blockers)


def test_local_dangling_reference_is_retained_for_merge():
    delta, _, blockers = plan_changes(
        {},
        [
            edit(
                "add",
                "step_dependencies",
                {
                    "prior_step": "external",
                    "dependent_step": "local",
                    "condition": "",
                    "field_relations": [],
                },
            )
        ],
        evidence_check=evidence_check,
    )
    assert len(delta) == 1
    assert any("external" in error for error in blockers)


@pytest.mark.parametrize(
    "changes",
    [
        {"groups": []},
        ["row"],
        [edit(record_id="opaque")],
        [edit(), edit()],
        [edit("rename", target={"name": "client"})],
    ],
)
def test_rejects_legacy_encodings_ids_duplicates_and_privileged_operations(changes):
    with pytest.raises(ValueError):
        plan_changes({}, changes, evidence_check=evidence_check)


def test_ordinary_patch_cannot_silently_rename_referenced_objects():
    model = rows(record("parties", {"name": "client", "description": "sender"}))
    with pytest.raises(ValueError, match="merge-phase rename"):
        plan_changes(
            model,
            [edit("patch", value={"name": "server"}, target={"name": "client"})],
            evidence_check=evidence_check,
        )


def test_store_commit_detects_change_between_validation_and_commit(store):
    first, second = claim(store), claim(store, "parser-2")
    delta, ledger, _ = plan_changes({}, [edit()], evidence_check=evidence_check)
    for who in (first, second):
        store.commit_records(
            who,
            revision=0,
            operation_id="initial",
            arguments={"changes": [edit()]},
            changes=delta,
            result={"revision": 1, "records": ledger},
        )
    # A late validation from the same draft is stale, while the other parser's
    # independent commit above is valid despite having the same starting version.
    with pytest.raises(StoreError, match="revision changed"):
        store.commit_records(
            first,
            revision=0,
            operation_id="late",
            arguments={"changes": [edit()]},
            changes=delta,
            result={"revision": 1, "records": ledger},
        )
