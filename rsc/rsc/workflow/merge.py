"""ID-bound references and deterministic integration of independent group results."""

import json
from copy import deepcopy

from rsc.protocol.references import reference_issues

from .model import validate_record
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


def number(key):
    return int(key.removeprefix("i"))


def bind(row, records):
    links = {}
    for path, kind, name in reference_paths(row):
        old = row.get("refs", {}).get(dumps(path), {})
        if name in records:
            matches = [name] if records[name]["kind"] == kind else []
        elif old.get("id") in records and old.get("name") == name:
            matches = [old["id"]]
        else:
            matches = [
                k
                for k, r in sorted(records.items(), key=lambda pair: number(pair[0]))
                if r["kind"] == kind and r["value"].get("name") == name
            ]
            local = [k for k in matches if records[k]["owner"] == row["owner"]]
            matches = local or matches
        links[dumps(path)] = {
            "kind": kind,
            "name": name,
            "id": matches[0] if len(matches) == 1 else None,
            "candidates": matches,
        }
    row["refs"] = links
    return row


def grouped(records, groups):
    expected = {
        k
        for k, r in sorted(records.items(), key=lambda pair: number(pair[0]))
        if r["kind"] != "step_dependencies"
    }
    seen = set()
    for members in groups:
        if not members or len(members) != len(set(members)):
            raise ValueError("groups must be nonempty with unique members")
        if not set(members) <= expected or seen.intersection(members):
            raise ValueError("unknown or multiply grouped item")
        if len({records[k]["kind"] for k in members}) != 1:
            raise ValueError("a group must contain a single item kind")
        seen.update(members)
    if seen != expected:
        raise ValueError(f"ungrouped items: {sorted(expected - seen)}")
    return [sorted(m, key=number) for m in groups]


def evidence(rows):
    return list({dumps(e): e for r in rows for e in r["evidence"]}.values())


def canonical_target(link, mapping):
    if link.get("id") in mapping:
        return mapping[link["id"]]
    alternatives = {mapping[k] for k in link.get("candidates", []) if k in mapping}
    return next(iter(alternatives)) if len(alternatives) == 1 else None


def group_owners(original, groups):
    owners = {key: name for name, g in groups.items() for key in g["members"]}
    for key, row in original.items():
        if row["kind"] == "step_dependencies":
            owners[key] = canonical_target(row["refs"][dumps(["dependent_step"])], owners)
    return owners


def at_path(value, path):
    for part in path:
        value = value[part]
    return value


def validate_proposals(original, groups, name, result):
    """Reject invalid owned edits before accepting a group's immutable submission."""
    owners = group_owners(original, groups)
    seen = set()
    for update in result.get("dependencies", []):
        key = update["id"]
        if (
            key not in original
            or original[key]["kind"] != "step_dependencies"
            or owners.get(key) != name
            or key in seen
        ):
            raise ValueError(f"dependency {key} must be unique and owned by {name}")
        seen.add(key)
    for patch in result.get("text_updates", []):
        key, path = patch["id"], patch["path"]
        if key not in original or not path or path[-1] not in {"description", "condition", "from"}:
            raise ValueError("text update requires a known item and a prose field path")
        try:
            before = at_path(original[key]["value"], path)
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("invalid text update path") from error
        if before != patch["before"]:
            raise ValueError("text update old value mismatch")


def integrate(original, groups):
    mapping, owners, candidate = {}, group_owners(original, groups), {}
    for name, g in groups.items():
        keep = min(g["members"], key=number)
        mapping.update({k: keep for k in g["members"]})
        owners[keep] = name
        row = deepcopy(original[keep])
        row.update(
            value=deepcopy(g["result"]["value"]),
            evidence=evidence([original[k] for k in g["members"]] + [g["result"]]),
        )
        candidate[keep] = bind(row, original)
    for key, row in sorted(original.items(), key=lambda pair: number(pair[0])):
        if row["kind"] == "step_dependencies":
            candidate[key] = deepcopy(row)
            mapping[key] = key
    errors, edits = [], {}
    for name, g in groups.items():
        validate_proposals(original, groups, name, g["result"])
        for update in g["result"].get("dependencies", []):
            key = update["id"]
            candidate[key] = bind({**deepcopy(original[key]), **deepcopy(update)}, original)
        for patch in g["result"].get("text_updates", []):
            key, path = patch["id"], patch["path"]
            target = mapping.get(key)
            pointer = (target, dumps(path))
            if pointer in edits and edits[pointer][0] != patch["after"]:
                errors.append(
                    {
                        "type": "write_conflict",
                        "records": [target],
                        "path": path,
                        "groups": [name, edits[pointer][1]],
                        "description": "conflicting text updates",
                    }
                )
            else:
                previous = edits.get(pointer)
                ev = evidence([patch, {"evidence": previous[2] if previous else []}])
                edits[pointer] = (patch["after"], name, ev)
    for (key, path), (after, name, ev) in edits.items():
        try:
            current = candidate[key]["value"]
            old = original[key]["value"]
            for part in json.loads(path):
                current, old = current[part], old[part]
            if current != old and current != after:
                errors.append(
                    {
                        "type": "write_conflict",
                        "records": [key],
                        "path": json.loads(path),
                        "groups": sorted({g for g in (name, owners.get(key)) if g}),
                        "description": "text update conflicts with canonical definition",
                    }
                )
                continue
            assign_path(candidate[key]["value"], json.loads(path), after)
            candidate[key]["evidence"] = evidence([candidate[key], {"evidence": ev}])
        except (KeyError, IndexError, TypeError):
            errors.append(
                {
                    "type": "write_conflict",
                    "records": [key],
                    "path": json.loads(path),
                    "groups": sorted({g for g in (name, owners.get(key)) if g}),
                    "description": "text update path disappeared in merged item",
                }
            )
    for key, row in candidate.items():
        for path, link in row.get("refs", {}).items():
            target = canonical_target(link, mapping)
            if target is None:
                matches = [
                    k
                    for k, r in candidate.items()
                    if r["kind"] == link["kind"] and r["value"].get("name") == link["name"]
                ]
                target = matches[0] if len(matches) == 1 else None
            if target not in candidate or candidate[target]["kind"] != link["kind"]:
                errors.append(
                    {
                        "type": "reference",
                        "records": [key],
                        "path": json.loads(path),
                        "description": f"unresolved {link['kind']} reference {link['name']!r}",
                    }
                )
                continue
            link["id"] = target
            assign_path(row["value"], json.loads(path), candidate[target]["value"]["name"])
        if row["kind"] == "step_dependencies":
            for side, field in (
                ("prior_field", "prior_step"),
                ("dependent_field", "dependent_step"),
            ):
                source_link = original[key].get("refs", {}).get(dumps([field]), {})
                source_steps = [source_link.get("id"), *source_link.get("candidates", [])]
                prefixes = {}
                for step in source_steps:
                    link = original.get(step, {}).get("refs", {}).get(dumps(["message_name"]), {})
                    target = canonical_target(link, mapping)
                    if target not in candidate:
                        continue
                    for msg in [link.get("id"), *link.get("candidates", [])]:
                        if msg in original and mapping.get(msg) == target:
                            prefixes[original[msg]["value"]["name"] + "."] = (
                                candidate[target]["value"]["name"] + "."
                            )
                for relation in row["value"]["field_relations"]:
                    for prefix in sorted(prefixes, key=len, reverse=True):
                        if relation[side].startswith(prefix):
                            relation[side] = prefixes[prefix] + relation[side][len(prefix) :]
                            break
    seen = {}
    for key, row in list(candidate.items()):
        try:
            validate_record(row["kind"], row["value"])
        except ValueError as error:
            errors.append({"type": "schema", "records": [key], "description": str(error)})
        if row["kind"] == "step_dependencies":
            signature = dumps(row["value"])
            if signature in seen:
                keep = seen[signature]
                candidate[keep]["evidence"] = evidence([candidate[keep], row])
                mapping[key] = keep
                del candidate[key]
            else:
                seen[signature] = key
    if not any(e["type"] == "schema" for e in errors):
        errors.extend(reference_issues(candidate))
    for error in errors:
        error.setdefault("groups", sorted({owners[k] for k in error["records"] if owners.get(k)}))
    updates = []

    def difference(key, before, after, path=()):
        if before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
            for field in before:
                difference(key, before[field], after[field], (*path, field))
        elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
            for index, (old, new) in enumerate(zip(before, after, strict=True)):
                difference(key, old, new, (*path, index))
        else:
            updates.append(
                {
                    "id": key,
                    "path": list(path),
                    "before": before,
                    "after": after,
                    "group": edits.get((key, dumps(path)), (None, owners.get(key)))[1],
                }
            )

    for key, row in candidate.items():
        difference(key, original[key]["value"], row["value"])
    return (
        candidate,
        {
            "replacements": mapping,
            "deletions": [k for k in original if k not in candidate],
            "updates": updates,
        },
        errors,
    )
