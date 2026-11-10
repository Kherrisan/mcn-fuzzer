"""Review ownership, dependency checks and conservative conclusion reuse."""

import json
from copy import deepcopy

from .merge import reference_paths
from .mutations import select, target
from .store import dumps


def object_ref(record):
    return {"kind": record["kind"], "target": target(record)}


def semantic(record):
    return {k: record[k] for k in ("kind", "value", "evidence")} if record else None


def adjacency(records):
    names = {
        (r["kind"], r["value"].get("name")): k for k, r in records.items() if "name" in r["value"]
    }
    return {
        key: {names[(kind, name)] for _, kind, name in reference_paths(r) if (kind, name) in names}
        for key, r in records.items()
    }


def closure(records, keys):
    graph, found = adjacency(records), set(keys)
    todo = list(found)
    while todo:
        for target_ in graph.get(todo.pop(), set()) - found:
            found.add(target_)
            todo.append(target_)
    return found


def source_context(records, batches):
    return closure(
        records,
        {key for key, r in records.items() if any(e["batch_id"] in batches for e in r["evidence"])},
    )


def assign_checks(records, assignments, batches):
    """Group related model checks per primary reviewer, retaining explicit ownership."""
    positions = {b["batch_id"]: i for i, b in enumerate(batches)}
    owners = {b: a for a in assignments for b in a["batches"]}
    groups = {a["worker"]: [] for a in assignments}
    for key, record in records.items():
        evidence_batches = {e["batch_id"] for e in record["evidence"]}
        available = evidence_batches & set(owners)
        if available:
            worker = owners[min(available, key=positions.get)]["worker"]
        else:
            worker = min(groups, key=lambda w: (len(groups[w]), w))
        groups[worker].append(key)
    for a in assignments:
        keys = groups[a["worker"]]
        if keys:
            a["checks"] = [
                {
                    "name": f"cross-range-{a['worker']}",
                    "owned": keys,
                    "records": sorted(closure(records, keys)),
                    "batches": sorted(
                        {
                            e["batch_id"]
                            for k in closure(records, keys)
                            for e in records[k]["evidence"]
                        },
                        key=positions.get,
                    ),
                }
            ]
    return assignments


def invalidated(before, after, conclusions):
    """Propagate shared/reference changes and invalidate negative query assertions.

    A change to unconstrained prose can affect remote conditions without a typed
    graph edge. Expand to all conclusions instead of claiming semantic isolation.
    """
    changed = {k for k in set(before) | set(after)
               if semantic(before.get(k)) != semantic(after.get(k))}
    graph = adjacency(before)
    for key, targets in adjacency(after).items():
        graph.setdefault(key, set()).update(targets)
    affected = set(changed)
    source_changes = {
        e["batch_id"]
        for key in changed
        for r in (before.get(key), after.get(key))
        if r
        for e in r["evidence"]
    }
    while True:
        expanded = affected | {k for k, refs in graph.items() if refs & affected}
        if expanded == affected:
            break
        affected = expanded
    prose_fields = {"condition", "description", "endpoint_sources", "object_names"}
    uncertain = any(
        any(
            (before.get(k, {}).get("value", {}).get(f) != after.get(k, {}).get("value", {}).get(f))
            for f in prose_fields
        )
        for k in changed
    )
    invalid = set()
    for name, conclusion in conclusions.items():
        if (
            uncertain
            or set(conclusion["records"]) & affected
            or conclusion.get("batch") in source_changes
        ):
            invalid.add(name)
            continue
        for query in conclusion.get("queries", []):
            if any(
                r
                and r["kind"] == query["kind"]
                and all(r["value"].get(f) == v for f, v in query["filters"].items())
                for key in changed
                for r in (before.get(key), after.get(key))
            ):
                invalid.add(name)
    return invalid


def fix_groups(findings, workers):
    groups = []
    for name, finding in findings.items():
        scope = {dumps([r["kind"], r["target"]]) for r in finding["objects"]}
        overlaps = [g for g in groups if not scope or not g["scope"] or g["scope"] & scope]
        item = {"findings": [name], "scope": scope}
        for group in overlaps:
            item["findings"].extend(group["findings"])
            item["scope"].update(group["scope"])
            groups.remove(group)
        groups.append(item)
    bins = [[] for _ in range(min(workers, len(groups)))]
    for group in sorted(groups, key=lambda g: (-len(g["findings"]), g["findings"])):
        min(bins, key=len).extend(group["findings"])
    return bins


def required_context(assignment, item, records):
    if item in assignment["batches"]:
        return source_context(records, [item]), [item]
    check = next(c for c in assignment["checks"] if c["name"] == item)
    return set(check["records"]), check["batches"]


def carry_forward(before, after, previous, assignments, previous_revision):
    """Carry only conclusions whose dependencies and current ownership still agree."""
    invalid = invalidated(before, after, previous)
    lookup = {(c["batch"] or c["check"]): (name, c) for name, c in previous.items()}
    for assignment in assignments:
        carried = []
        items = [*assignment["batches"], *(c["name"] for c in assignment["checks"])]
        for item in items:
            if item not in lookup:
                continue
            name, old = lookup[item]
            needed, sources = required_context(assignment, item, after)
            if (
                name in invalid
                or not needed <= set(old["records"])
                or not set(sources) <= set(old["source_batches"])
            ):
                continue
            check = next((c for c in assignment["checks"] if c["name"] == item), None)
            if check:
                compared = {select(after, r["kind"], r["target"]) for r in old["report"]["objects"]}
                if not set(check["owned"]) <= compared:
                    continue
            body = {
                **deepcopy(old),
                "assignment": assignment["name"],
                "attempt": 0,
                "carried_from": {"name": name, "revision": previous_revision},
            }
            carried.append((f"{assignment['name']}/{item}", body))
        assignment["carried"] = carried
    return assignments


def review_errors(store, assignment, records):
    """The same independent-delivery gate is used live and by offline audits.

    Carried reports have attempt=0, an explicit predecessor, and no new receipts.
    Every predecessor is recursively verified at its own immutable revision.
    """
    errors, checked = [], set()
    cache = {assignment["base_revision"]: records}

    def snapshot(revision):
        if revision not in cache:
            row = store.db.execute(
                "SELECT body FROM snapshots WHERE revision=?", (revision,)
            ).fetchone()
            if row is None:
                raise ValueError("missing frozen snapshot")
            cache[revision] = json.loads(row[0])["records"]
        return cache[revision]

    def check(name, a, item, model):
        if name in checked:
            return
        row = store.db.execute(
            "SELECT revision, body FROM conclusions WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise ValueError(f"review conclusion remains incomplete: {item}")
        revision, body = row[0], json.loads(row[1])
        if (
            revision != a["base_revision"]
            or body["assignment"] != a["name"]
            or (body.get("batch") or body.get("check")) != item
        ):
            raise ValueError("review conclusion belongs to a different assignment/version")
        needed, sources = required_context(a, item, model)
        compared = {select(model, r["kind"], r["target"]) for r in body["report"]["objects"]}
        needed |= closure(model, compared)
        if not needed <= set(body["records"]) or not set(sources) <= set(body["source_batches"]):
            raise ValueError("review dependency ledger omits required context")
        check_item = next((c for c in a["checks"] if c["name"] == item), None)
        if check_item and not set(check_item["owned"]) <= compared:
            raise ValueError("cross-range report omits owned model records")
        if not body["report"].get("requirements"):
            raise ValueError("review conclusion has no actual comparisons")
        if body.get("carried_from"):
            old_name, old_revision = body["carried_from"]["name"], body["carried_from"]["revision"]
            if body["attempt"] != 0 or not 0 < old_revision < revision:
                raise ValueError("invalid review carry provenance")
            old = store.db.execute(
                "SELECT revision, body FROM conclusions WHERE name=?", (old_name,)
            ).fetchone()
            if old is None or old[0] != old_revision:
                raise ValueError("missing carried review predecessor")
            old_body = json.loads(old[1])
            old_assignment = store.assignment(old_body["assignment"])
            check(
                old_name,
                old_assignment,
                old_body["batch"] or old_body["check"],
                snapshot(old_revision),
            )
            if old_name in invalidated(snapshot(old_revision), model, {old_name: old_body}):
                raise ValueError("carried review dependencies have changed")
            for field in ("records", "source_batches", "queries", "report"):
                if body[field] != old_body[field]:
                    raise ValueError("carried review differs from its predecessor")
        else:
            attempt = body["attempt"]
            if type(attempt) is not int or not 1 <= attempt <= a["attempt"]:
                raise ValueError("review conclusion has an invalid attempt")
            if not store.db.execute(
                "SELECT 1 FROM attempts WHERE assignment=? AND attempt=?", (a["name"], attempt)
            ).fetchone():
                raise ValueError("review attempt is missing")
            for batch in body["source_batches"]:
                receipt = store.db.execute(
                    "SELECT name FROM receipts WHERE assignment=? AND attempt=? AND batch=?",
                    (a["name"], attempt, batch),
                ).fetchone()
                if receipt is None or receipt[0] != f"{a['name']}/{attempt}/{batch}":
                    raise ValueError("review source was not fully delivered in its attempt")
            delivered = {
                json.loads(r[0])
                for r in store.db.execute(
                    "SELECT name FROM deliveries WHERE assignment=? AND attempt=? "
                    "AND revision=? AND family='model'",
                    (a["name"], attempt, revision),
                )
            }
            if not set(body["records"]) <= delivered:
                raise ValueError("review model context was not fully delivered")
            for query in body["queries"]:
                row = store.db.execute(
                    "SELECT body FROM deliveries WHERE assignment=? AND attempt=? "
                    "AND revision=? AND family='query' AND name=?",
                    (a["name"], attempt, revision, dumps(dumps(query))),
                ).fetchone()
                if row is None or json.loads(row[0]) != query:
                    raise ValueError("missing negative-query delivery proof")
        if body.get("batch"):
            report = store.db.execute(
                "SELECT phase, revision, body FROM reports "
                "WHERE assignment=? AND attempt=? AND batch=?",
                (a["name"], body["attempt"], item),
            ).fetchone()
            if (
                report is None
                or report[0] != "review"
                or report[1] != revision
                or json.loads(report[2]) != body["report"]
            ):
                raise ValueError("source review report and conclusion disagree")
        checked.add(name)

    for item in [*assignment["batches"], *(c["name"] for c in assignment["checks"])]:
        try:
            check(f"{assignment['name']}/{item}", assignment, item, records)
        except (ValueError, KeyError, TypeError, StopIteration) as error:
            errors.append(f"{item}: {error}")
    return errors
