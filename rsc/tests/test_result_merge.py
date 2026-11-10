"""Canonical references, owned updates and cross-group conflicts."""

from copy import deepcopy

import pytest
from protocol_samples import base, interactions

from rsc.workflow import merge


def records():
    model = base()
    model.update(interactions(model))
    rows = {}
    for owner in ("parser-1", "parser-2"):
        for kind, values in model.items():
            if kind == "unresolved":
                continue
            for value in values:
                key = f"i{len(rows) + 1}"
                rows[key] = {
                    "id": key,
                    "owner": owner,
                    "kind": kind,
                    "value": deepcopy(value),
                    "evidence": [{"batch_id": owner}],
                }
    for row in rows.values():
        merge.bind(row, rows)
    bins = {}
    for key, row in rows.items():
        if row["kind"] != "step_dependencies":
            bins.setdefault((row["kind"], row["value"]["name"]), []).append(key)
    groups = {
        f"g{i}": {
            "members": members,
            "result": {
                "value": deepcopy(rows[members[0]]["value"]),
                "evidence": rows[members[0]]["evidence"],
            },
        }
        for i, members in enumerate(bins.values(), 1)
    }
    return rows, groups


def group_for(rows, groups, kind, name=None):
    return next(
        (n, g)
        for n, g in groups.items()
        if rows[g["members"][0]]["kind"] == kind
        and (name is None or rows[g["members"][0]]["value"]["name"] == name)
    )


def test_ambiguous_foreign_reference_collapses_and_dependency_has_owner():
    rows, groups = records()
    dep = next(r for r in rows.values() if r["kind"] == "step_dependencies")
    dep.update(owner="parser-3", refs={})
    merge.bind(dep, rows)
    assert dep["refs"]['["dependent_step"]']["id"] is None
    name, g = group_for(rows, groups, "steps", "send response")
    g["result"]["dependencies"] = [
        {"id": dep["id"], "value": deepcopy(dep["value"]), "evidence": dep["evidence"]}
    ]
    g["result"]["dependencies"][0]["value"]["condition"] = "reply with the original token"
    candidate, table, errors = merge.integrate(rows, groups)
    assert not errors
    assert candidate[dep["id"]]["value"]["dependent_step"] == "send response"
    assert any(u["id"] == dep["id"] and u["group"] == name for u in table["updates"])


def test_rename_rewrites_references_and_preserves_distinct_dependency_conditions():
    rows, groups = records()
    for g in groups.values():
        g["result"]["value"]["name"] += " canonical"
    candidate, table, errors = merge.integrate(rows, groups)
    assert not errors
    deps = [r for r in candidate.values() if r["kind"] == "step_dependencies"]
    assert len(deps) == 4
    assert {r["value"]["dependent_step"] for r in deps} == {
        "send request canonical",
        "send response canonical",
    }
    assert len(table["deletions"]) == len(rows) // 2


def test_prose_patch_conflict_and_evidence_are_preserved():
    rows, groups = records()
    party_name, party = group_for(rows, groups, "parties", "requester")
    other_name, other = group_for(rows, groups, "messages", "request")
    key = party["members"][0]
    patch = {
        "id": key,
        "path": ["description"],
        "before": rows[key]["value"]["description"],
        "after": "new description",
        "evidence": [{"batch_id": "supplementary"}],
    }
    other["result"]["text_updates"] = [patch]
    candidate, _, errors = merge.integrate(rows, groups)
    assert not errors
    assert patch["evidence"][0] in candidate[key]["evidence"]
    party["result"]["value"]["description"] = "incompatible canonical description"
    _, _, errors = merge.integrate(rows, groups)
    conflict = next(e for e in errors if e["type"] == "write_conflict")
    assert set(conflict["groups"]) == {party_name, other_name}


def test_foreign_dependency_and_bad_prose_paths_rejected():
    rows, groups = records()
    name, group = group_for(rows, groups, "parties")
    dep = next(r for r in rows.values() if r["kind"] == "step_dependencies")
    with pytest.raises(ValueError, match="owned"):
        merge.validate_proposals(rows, groups, name, {"dependencies": [dep]})
    with pytest.raises(ValueError, match="path"):
        merge.validate_proposals(
            rows,
            groups,
            name,
            {
                "text_updates": [
                    {
                        "id": group["members"][0],
                        "path": ["missing", "description"],
                        "before": "",
                        "after": "bad",
                    }
                ]
            },
        )


def test_separate_same_name_groups_remain_a_conflict():
    rows, groups = records()
    _, group = group_for(rows, groups, "parties", "requester")
    member = group["members"].pop()
    groups["extra"] = {"members": [member], "result": deepcopy(group["result"])}
    _, _, errors = merge.integrate(rows, groups)
    assert any(e["type"] == "same_name" and "extra" in e["groups"] for e in errors)


def test_qualified_fields_follow_ambiguous_step_and_message_merges():
    rows, groups = records()
    for row in rows.values():
        if row["kind"] == "step_dependencies":
            row.update(owner="parser-3", refs={})
            merge.bind(row, rows)
            for side, endpoint in [
                ("prior_field", "prior_step"),
                ("dependent_field", "dependent_step"),
            ]:
                prefix = row["value"][endpoint].removeprefix("send ") + "."
                for relation in row["value"]["field_relations"]:
                    relation[side] = prefix + relation[side]
    for group in groups.values():
        if rows[group["members"][0]]["kind"] == "messages":
            group["result"]["value"]["name"] += " canonical"
    candidate, _, errors = merge.integrate(rows, groups)
    assert not errors
    assert all(
        " canonical." in relation["prior_field"]
        for row in candidate.values()
        if row["kind"] == "step_dependencies"
        for relation in row["value"]["field_relations"]
    )
