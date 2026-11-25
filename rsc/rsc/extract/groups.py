"""Group validation rows into the repair units a Fix round works through.

A group is one place to repair: usually the entity the rows point at. Coverage
checks group by their ``check_ref`` target, open gaps group by their ``U…`` id, and
structural rows with no clear target group by their rule. A row that shares its
target with another row joins that group instead of becoming its own, so the agent
sees one entry per repair site rather than one per symptom.
"""

import json

from .registry import identity


def issue_ref(issue):
    """Stable public id for one validation row, used for paging and deltas."""
    if issue.get("check_ref"):
        return f"issue:{issue['check_ref']}"
    records = issue.get("records") or []
    path = ".".join(str(part) for part in issue.get("path") or ())
    owner = records[0] if records else "-"
    return f"issue:{issue['type']}:{owner}:{path}" if path else f"issue:{issue['type']}:{owner}"


def _target(issue):
    """The entity a row is about, when the row names one."""
    check = issue.get("check_ref")
    if check:
        tail = check.rsplit(":", 1)[-1]
        if tail:
            return tail
    records = issue.get("records")
    if records:
        return records[0]
    return None


def group_key(issue):
    """Repair site for one row: its target entity, else its rule and owner."""
    target = _target(issue)
    if target:
        return target
    return f"{issue['type']}:{issue_ref(issue).rsplit(':', 1)[-1]}"


def issue_rows(issues, records):
    """Compact, paged rows: id, description, type, owners and path.

    ``check_ref`` is kept because it is the handle an unresolved item already carries.
    Rows that would render the same id get an occurrence suffix so an id still names
    exactly one row in one reply.
    """
    rows = []
    used = {}
    for issue in issues:
        base = issue_ref(issue)
        used[base] = used.get(base, 0) + 1
        rows.append(
            {
                "id": base if used[base] == 1 else f"{base}#{used[base]}",
                "check_ref": issue.get("check_ref") or "",
                "group_id": group_key(issue),
                "type": issue["type"],
                "description": issue["description"],
                "records": list(issue.get("records") or ()),
                "path": list(issue.get("path") or ()),
                "advisory": bool(issue.get("advisory")),
            }
        )
    return rows


def ordered_issue_rows(issues, unresolved, records, sources=None, deferred=None):
    """Groups and rows in one order: largest group first, then source order.

    ``deferred`` holds the groups one unchanged blocked signature already stalled at.
    They keep their relative rank but move behind every eligible group, and an entry
    whose signature changed is dropped here so the group is selectable again.
    """
    groups = fix_groups(issues, unresolved, records)
    if deferred is not None:
        groups = defer_groups(groups, unresolved, sources, deferred)
    rank = {group["group_id"]: index for index, group in enumerate(groups)}
    rows = issue_rows(issues, records)
    rows.sort(key=lambda row: rank.get(row["group_id"], len(rank)))
    return groups, rows


def head_group(groups, deferred=None):
    """The repair site a round should open: the first eligible group with blocking rows.

    A deferred group is offered again only after its signature changes, so a round does
    not lead with it. With every group deferred the round gets the catalog instead.
    """
    for group in groups:
        if not group["count"]:
            continue
        if deferred is not None and group["group_id"] in deferred:
            continue
        return group
    if deferred:
        return None
    return groups[0] if groups else None


def located(sources, document_id, block_id):
    """Whether a frozen-source citation can still be read back."""
    return sources is not None and (document_id, block_id) in sources.blocks


def blocked_signature(group, unresolved, sources=None):
    """Signature of a group when one located open item explains all of its checks.

    Empty when the group cannot be deferred: a row without its own check, a repeated
    check, an open item that only covers part of the group, several candidate items,
    or an item whose frozen location no longer resolves. The signature holds the sorted
    checks and the covering item, and the evidence lists are sorted too, so a change in
    order alone does not invalidate it.
    """
    checks = []
    for issue in group["issues"]:
        ref = issue.get("check_ref")
        if not ref or ref in checks:
            return ""
        checks.append(ref)
    if not checks:
        return ""
    wanted = set(checks)
    covering = [
        item
        for item in unresolved
        if wanted <= set(item.get("check_refs") or ())
    ]
    if len(covering) != 1:
        return ""
    item = covering[0]
    locations = sorted(
        (cite["document_id"], cite["block_id"])
        for cite in item.get("evidence") or ()
        if cite.get("block_id")
    )
    if not locations or not all(located(sources, *pair) for pair in locations):
        return ""
    rows = sorted(
        (
            issue["check_ref"],
            issue["type"],
            tuple(sorted(issue.get("records") or ())),
            tuple(str(part) for part in issue.get("path") or ()),
            tuple(
                sorted(
                    (cite["document_id"], cite["block_id"])
                    for cite in issue.get("evidence") or ()
                    if cite.get("block_id")
                )
            ),
        )
        for issue in group["issues"]
    )
    listed = sorted(
        (
            cite["document_id"],
            cite["block_id"],
            cite.get("start") or 0,
            cite.get("end") or 0,
            cite.get("field") or "",
            cite.get("note") or "",
        )
        for cite in item.get("evidence") or ()
        if cite.get("block_id")
    )
    return json.dumps(
        {
            "checks": rows,
            "item": [
                item["id"],
                item.get("description") or "",
                item.get("missing_information") or "",
                tuple(sorted(item.get("entity_ids") or ())),
                listed,
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def defer_groups(groups, unresolved, sources, deferred):
    """Eligible groups first: deferred groups keep their rank but lose priority.

    Entries whose signature changed, or whose group is gone, are cleared here, so the
    next prompt or catalog query already offers those repair sites again.
    """
    live, waiting = [], []
    present = set()
    for group in groups:
        group_id = group["group_id"]
        present.add(group_id)
        if group_id in deferred:
            signature = blocked_signature(group, unresolved, sources)
            if signature and signature == deferred[group_id]:
                waiting.append(group)
                continue
            del deferred[group_id]
        live.append(group)
    for group_id in [key for key in deferred if key not in present]:
        del deferred[group_id]
    return live + waiting


def fix_groups(issues, unresolved, records):
    """Return the ordered repair groups for the current validation state."""
    groups = {}
    for issue in issues:
        key = group_key(issue)
        group = groups.setdefault(
            key,
            {
                "group_id": key,
                "issues": [],
                "unresolved_ids": [],
                "entity_ids": [],
                "kinds": set(),
                "titles": [],
            },
        )
        group["issues"].append(issue)
        group["titles"].append(issue["description"])
        for entity_id in issue.get("records") or ():
            if entity_id in records:
                group["entity_ids"].append(entity_id)
                group["kinds"].add(records[entity_id]["kind"])
    by_check = {}
    for key, group in groups.items():
        for issue in group["issues"]:
            if issue.get("check_ref"):
                by_check.setdefault(issue["check_ref"], key)
    for item in unresolved:
        linked = {
            by_check[check_ref]
            for check_ref in item.get("check_refs") or ()
            if check_ref in by_check
        }
        if not linked:
            owner = (item.get("entity_ids") or [None])[0]
            linked = {owner or item["id"]}
        # One open item can explain several groups; it keeps its own id in each.
        for key in linked:
            group = groups.setdefault(
                key,
                {
                    "group_id": key,
                    "issues": [],
                    "unresolved_ids": [],
                    "entity_ids": [],
                    "kinds": set(),
                    "titles": [],
                },
            )
            group["unresolved_ids"].append(item["id"])
            for entity_id in item.get("entity_ids") or ():
                if entity_id in records:
                    group["entity_ids"].append(entity_id)
                    group["kinds"].add(records[entity_id]["kind"])
    result = []
    for group in groups.values():
        titles = []
        for title in group.pop("titles"):
            text = " ".join(title.split())
            if text not in titles:
                titles.append(text[:160])
        result.append(
            {
                **group,
                "count": len(group["issues"]),
                "unresolved_count": len(group["unresolved_ids"]),
                "entity_ids": list(dict.fromkeys(group["entity_ids"])),
                "kinds": sorted(group["kinds"]),
                "titles": titles[:3],
            }
        )
    # Blocking rows first: a group that is only a set of open items repairs nothing
    # until its checks are due, so it never leads the Fix prompt.
    result.sort(
        key=lambda group: (
            -group["count"],
            -group["unresolved_count"],
            group["group_id"],
        )
    )
    return result


def group_rows(groups):
    """Catalog rows for the Fix prompt and the paged group view."""
    return [
        {
            "group_id": group["group_id"],
            "count": group["count"],
            "unresolved_count": group["unresolved_count"],
            "kinds": group["kinds"],
            "entity_ids": group["entity_ids"][:6],
            "titles": group["titles"],
        }
        for group in groups
    ]


def group_entities(groups, records, limit=3):
    """Names of the entities a group points at, so a head row stays readable."""
    names = []
    for group in groups:
        for entity_id in group["entity_ids"][:limit]:
            row = records.get(entity_id)
            if row is None:
                continue
            names.append({"group_id": group["group_id"], "id": entity_id, "name": identity(row)})
    return names
