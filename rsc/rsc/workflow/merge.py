"""Scoped candidates and deterministic reference-aware merging for Parse and Fix."""

import json
from copy import deepcopy

from rsc.protocol.references import reference_issues

from .model import validate_record
from .mutations import plan_changes, record_key, target
from .store import dumps


def reference_paths(record):
    """Yield every typed structured reference, retaining its exact value location."""
    kind, value = record["kind"], record["value"]
    if kind == "channels":
        for i, name in enumerate(value["endpoint_party_names"]):
            yield ("endpoint_party_names", i), "parties", name
        for field in ("initiator_party_name", "acceptor_party_name"):
            if value[field] is not None:
                yield (field,), "parties", value[field]
        for i, source in enumerate(value["endpoint_sources"]):
            yield ("endpoint_sources", i, "party_name"), "parties", source["party_name"]
    elif kind == "steps":
        yield ("channel_name",), "channels", value["channel_name"]
        fields = (
            (("message_name", "messages"), ("sender_name", "parties"), ("receiver_name", "parties"))
            if value["action"] == "send"
            else (("party_name", "parties"),)
        )
        for field, ref_kind in fields:
            yield (field,), ref_kind, value[field]
    elif kind == "step_dependencies":
        for field in ("prior_step", "dependent_step"):
            yield (field,), "steps", value[field]


def assign_path(value, path, replacement):
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = replacement


def ref(candidate):
    return {"scope": candidate["scope"], "kind": candidate["kind"], "target": target(candidate)}


def display(record, candidates=None):
    """Agent pages use named references; storage keys and merge bookkeeping stay internal."""
    result = {key: record[key] for key in ("kind", "value", "evidence", "scope") if key in record}
    if candidates is not None and "links" in record:
        result["references"] = [
            {"path": json.loads(path), "targets": [ref(candidates[k]) for k in keys]}
            for path, keys in record["links"].items()
        ]
    return result


def bind(candidates):
    """Local definitions win. Unresolved foreign matches never choose by arrival."""
    for candidate in candidates.values():
        previous = candidate.get("links", {})
        links = {}
        for path, kind, name in reference_paths(candidate):
            pointer = dumps(path)
            old = previous.get(pointer, [])
            if len(old) > 1 and all(key in candidates for key in old):
                links[pointer] = old
                continue
            # Retain established bindings after a scoped rename/merge.
            if (
                len(old) == 1
                and old[0] in candidates
                and candidates[old[0]]["value"].get("name") == name
            ):
                links[pointer] = old
                continue
            matches = [
                key
                for key, value in candidates.items()
                if value["kind"] == kind and value["value"].get("name") == name
            ]
            scopes = set(candidate["local_scopes"])
            local = [key for key in matches if scopes & set(candidates[key]["local_scopes"])]
            links[pointer] = local or matches
        candidate["links"] = links
    return candidates


def combine(drafts):
    candidates = {}
    for scope, records in drafts:
        for record in records.values():
            key = dumps([scope, record_key(record)])
            candidates[key] = {
                **deepcopy(record),
                "scope": scope,
                "local_scopes": [scope],
                "origins": deepcopy(record.get("origins", [])),
            }
            origin = {"scope": scope, "kind": record["kind"], "target": target(record)}
            if origin not in candidates[key]["origins"]:
                candidates[key]["origins"].append(origin)
    return automatic(bind(candidates))


def redirect(candidates, old_keys, final):
    for candidate in candidates.values():
        for pointer, targets in candidate.get("links", {}).items():
            candidate["links"][pointer] = list(
                dict.fromkeys(final if k in old_keys else k for k in targets)
            )


def automatic(candidates):
    """Deduplicate only equal definitions with already-unique, equal bindings."""
    while True:
        bind(candidates)
        seen, changed = {}, False
        for key, record in list(candidates.items()):
            if any(len(targets) != 1 for targets in record["links"].values()):
                continue
            signature = dumps([record["kind"], record["value"], record["links"]])
            if signature not in seen:
                seen[signature] = key
                continue
            first = seen[signature]
            for field in ("origins", "evidence", "local_scopes"):
                candidates[first][field].extend(
                    x for x in record[field] if x not in candidates[first][field]
                )
            del candidates[key]
            redirect(candidates, {key}, first)
            changed = True
        if not changed:
            return candidates


def conflicts(candidates, checks=()):
    result, names = [], {}
    bindings = {
        (key, tuple(json.loads(path))): targets
        for key, record in candidates.items()
        for path, targets in record["links"].items()
    }
    reverse = {key: set() for key in candidates}
    for (owner, _), targets in bindings.items():
        for key in targets:
            reverse[key].add(owner)

    def context(keys):
        affected, pending = set(keys), list(keys)
        while pending:
            for key in reverse[pending.pop()] - affected:
                affected.add(key)
                pending.append(key)
        evidence = {dumps(e): e for key in keys for e in candidates[key]["evidence"]}
        return {
            "affected_records": [ref(candidates[k]) for k in sorted(affected)],
            "evidence": list(evidence.values()),
            "status": "open",
        }

    for key, record in candidates.items():
        name = record["value"].get("name")
        if name is not None:
            names.setdefault((record["kind"], name), []).append(key)
        for path, kind, name in reference_paths(record):
            targets = record["links"].get(dumps(path), [])
            if len(targets) != 1:
                result.append(
                    {
                        "type": "missing_reference" if not targets else "ambiguous_reference",
                        "record": ref(record),
                        "path": list(path),
                        "kind": kind,
                        "name": name,
                        "candidates": [ref(candidates[k]) for k in targets],
                        "definitions": [display(candidates[k]) for k in targets],
                        **context([key, *targets]),
                    }
                )
    for (kind, name), keys in names.items():
        if len(keys) > 1:
            records = [candidates[key] for key in keys]
            fields = set().union(*(r["value"] for r in records))
            result.append(
                {
                    "type": "same_name",
                    "kind": kind,
                    "name": name,
                    "candidates": [
                        {
                            "ref": ref(r),
                            "value": r["value"],
                            "references": display(r, candidates).get("references", []),
                            "evidence": r["evidence"],
                        }
                        for r in records
                    ],
                    "differences": sorted(
                        f for f in fields if len({dumps(r["value"].get(f)) for r in records}) > 1
                    ),
                    **context(keys),
                }
            )
    # Name/reference conflicts already have scoped candidate detail above. The same
    # validator supplies role/field issues using those bindings, without choosing a
    # last-arriving global definition or repeating each global name ambiguity.
    for issue in reference_issues(candidates, bindings):
        if issue["type"] == "structure":
            keys = issue["records"]
            result.append(
                {
                    **issue,
                    "record": ref(candidates[keys[0]]),
                    "records": [display(candidates[k]) for k in keys],
                    **context(keys),
                }
            )
    result.extend(check for check in checks if check.get("status") != "resolved")
    return result


def scoped_select(candidates, selector):
    if not selector.get("scope"):
        raise ValueError("merge targets must specify their source scope")
    scope = selector["scope"]
    matches = []
    for key, r in candidates.items():
        if r["kind"] != selector["kind"]:
            continue
        aliases = [o["target"] for o in r["origins"] if o["scope"] == scope]
        if scope in r["local_scopes"]:
            aliases.append(r["value"])
        if any(
            all(
                field in value and dumps(value[field]) == dumps(expected)
                for field, expected in selector["target"].items()
            )
            for value in aliases
        ):
            matches.append(key)
    if len(matches) != 1:
        raise ValueError(f"scoped target {selector!r} has {len(matches)} matches")
    return matches[0]


def rewrite(candidates, selected, final, old_names, checks):
    redirect(candidates, selected, final)
    name = candidates[final]["value"].get("name")
    if name is None:
        return
    for record in candidates.values():
        for path, _, _ in list(reference_paths(record)):
            if record["links"].get(dumps(path)) == [final]:
                assign_path(record["value"], path, name)
        if record["kind"] == "step_dependencies" and candidates[final]["kind"] == "messages":
            for endpoint, field in (
                ("prior_step", "prior_field"),
                ("dependent_step", "dependent_field"),
            ):
                steps = record["links"].get(dumps([endpoint]), [])
                if len(steps) != 1:
                    continue
                step = candidates[steps[0]]
                if step["links"].get(dumps(["message_name"])) != [final]:
                    continue
                for relation in record["value"]["field_relations"]:
                    for old in old_names:
                        if relation[field].startswith(old + "."):
                            relation[field] = name + relation[field][len(old) :]
                            break
        # These fields are prose, not structured references. Never replace text.
        prose = {
            f: v
            for f, v in record["value"].items()
            if f in {"condition", "description", "endpoint_sources", "object_names"}
        }
        if any(old != name and old in dumps(prose) for old in old_names):
            checks.append(
                {
                    "name": f"text-{len(checks) + 1}",
                    "type": "text_check",
                    "record": ref(record),
                    "description": f"Check prose after renaming {old_names!r} to {name!r}",
                    "evidence": record["evidence"],
                    "status": "open",
                }
            )


def apply(candidates, changes, checks, *, evidence_check):
    """Return a new candidate revision; caller commits it and its decisions atomically."""
    result, pending, decisions, errors = deepcopy(candidates), deepcopy(checks), [], []
    for index, change in enumerate(changes):
        try:
            op = change["op"]
            evidence_check(change.get("evidence", [])) if op != "delete" else None
            if op == "resolve":
                matches = [c for c in pending if c["name"] == change["name"]]
                if len(matches) != 1:
                    raise ValueError(
                        "unknown or ambiguous semantic check; structural conflicts require edits"
                    )
                matches[0].update(
                    status="resolved",
                    decision=change["decision"],
                    decision_evidence=change["evidence"],
                )
            elif op in {"rename", "merge"}:
                selectors = [change["source"]] if op == "rename" else change["sources"]
                keys = [scoped_select(result, s) for s in selectors]
                if len(set(keys)) != len(keys):
                    raise ValueError("source candidate selected more than once")
                first = keys[0]
                kind = result[first]["kind"]
                if any(result[key]["kind"] != kind for key in keys):
                    raise ValueError("cannot merge different record kinds")
                value = (
                    {**result[first]["value"], "name": change["name"]}
                    if op == "rename"
                    else deepcopy(change["value"])
                )
                validate_record(kind, value)
                if "name" in value and any(
                    k not in keys and r["kind"] == kind and r["value"].get("name") == value["name"]
                    for k, r in result.items()
                ):
                    raise ValueError(
                        "target name collides with another candidate; "
                        "include it in an explicit merge"
                    )
                old_names = list(dict.fromkeys(result[k]["value"].get("name") for k in keys))
                old_names = [n for n in old_names if n is not None]
                for key in keys[1:]:
                    for field in ("origins", "evidence", "local_scopes"):
                        result[first][field].extend(
                            x for x in result[key][field] if x not in result[first][field]
                        )
                    del result[key]
                result[first]["value"] = value
                result[first]["evidence"].extend(
                    e for e in change["evidence"] if e not in result[first]["evidence"]
                )
                rewrite(result, set(keys), first, old_names, pending)
            else:
                scope = change.get("scope")
                if not scope:
                    raise ValueError("merge edits must specify a scope")
                subset = {k: r for k, r in result.items() if r["scope"] == scope}
                plain = {
                    record_key(r): {f: r[f] for f in ("kind", "value", "evidence")}
                    for r in subset.values()
                }
                delta, _, _ = plan_changes(
                    plain,
                    [{k: v for k, v in change.items() if k != "scope"}],
                    evidence_check=evidence_check,
                )
                for old, new in delta.values():
                    old_key = next(
                        (k for k, r in subset.items() if old and record_key(r) == record_key(old)),
                        None,
                    )
                    if new is None:
                        del result[old_key]
                        redirect(result, {old_key}, "missing-deleted-candidate")
                    elif old_key:
                        # Explicit edits to a reference resolve its previous ambiguity.
                        old_refs = {dumps(p): n for p, _, n in reference_paths(result[old_key])}
                        new_refs = {dumps(p): n for p, _, n in reference_paths(new)}
                        for pointer in list(result[old_key].get("links", {})):
                            if old_refs.get(pointer) != new_refs.get(pointer):
                                del result[old_key]["links"][pointer]
                        result[old_key].update(new)
                    else:
                        key = dumps([scope, record_key(new)])
                        if key in result:
                            raise ValueError(
                                "local name already has an origin mapping; use a new scope"
                            )
                        result[key] = {
                            **new,
                            "scope": scope,
                            "local_scopes": [scope],
                            "origins": [
                                {"scope": scope, "kind": new["kind"], "target": target(new)}
                            ],
                        }
            bind(result)
            decisions.append(deepcopy(change))
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"changes[{index}]: {error}")
    if errors:
        raise ValueError("; ".join(errors))
    return automatic(result), pending, decisions


def final_records(candidates):
    errors = conflicts(candidates)
    if errors:
        raise ValueError("cannot freeze unresolved merge conflicts")
    return {
        record_key(r): {k: deepcopy(r[k]) for k in ("kind", "value", "evidence", "origins")}
        for r in candidates.values()
    }
