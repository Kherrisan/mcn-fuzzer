"""MCP surface for one active extraction workspace.

Reads always return the current registry state; writes are never rejected for being
based on an older read. Replies are not clipped or budgeted: the caller pages rows with
offset and limit and projects detail with fields. Context control is history
compaction's job, not the tools'.
"""

import asyncio
from collections import Counter
from typing import Annotated

from pydantic import Field

from .groups import group_rows, head_group, issue_ref, ordered_issue_rows
from .guidance import with_suggestions
from .registry import (
    ORDER,
    EntityAddition,
    EntityUpdate,
    Kind,
    Unresolved,
    identity,
    summary,
)


def search(records, queries, kind):
    terms_by_query = {query: query.split() for query in map(str.casefold, queries)}
    ranked = []
    for row in records.values():
        if kind is not None and row["kind"] != kind:
            continue
        names = [identity(row), *row["aliases"], *row["value"].get("alias", [])]
        folded = [name.casefold() for name in names]
        if any(name in terms_by_query for name in folded):
            ranked.append((0, row))
        else:
            corpus = " ".join(folded)
            if any(all(term in corpus for term in terms) for terms in terms_by_query.values()):
                ranked.append((1, row))
    return [row for _, row in sorted(ranked, key=lambda item: item[0])]


def compact_row(work, row):
    """Index-level row: identity only, so a page stays small."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": identity(row),
        "aliases": row["aliases"],
    }


def select_fields(value, fields):
    """Project dotted field paths such as processing.on_receive from one value."""
    if not fields:
        return value
    result = {}
    for path in fields:
        current = value
        parts = [part for part in path.split(".") if part]
        target = result
        for depth, part in enumerate(parts):
            last = depth == len(parts) - 1
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                current = None
                break
            if last:
                target[part] = current
            else:
                target = target.setdefault(part, {})
                if not isinstance(target, dict):
                    break
    return result


def projected_unresolved(items):
    """Open unresolved items with their evidence reduced to duplicate-free block IDs.

    Every open item is kept, in order, with its id, description, missing information,
    entities and check refs untouched. Evidence is a list of integer block IDs, so the
    only projection left is removing repeats while keeping first-seen order: read any
    block once with read_source(blocks=[...]).
    """
    return [
        {**item, "evidence": list(dict.fromkeys(item.get("evidence") or ()))}
        for item in items
    ]


def paged_rows(rows, offset=0, limit=None):
    """Slice rows by the caller's offset and limit; every selected row comes back whole."""
    offset = max(0, int(offset))
    end = len(rows) if limit is None else offset + max(0, int(limit))
    selected = list(rows[offset:end])
    next_offset = offset + len(selected) if offset + len(selected) < len(rows) else None
    return {
        "rows": selected,
        "offset": offset,
        "count": len(rows),
        "next_offset": next_offset,
    }


def field_paths(value, prefix=""):
    """Leaf paths of one value, so an oversized entity can be read field by field."""
    paths = []
    if isinstance(value, dict) and value:
        for key, item in value.items():
            paths.extend(field_paths(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            paths.extend(field_paths(item, f"{prefix}.{index}" if prefix else str(index)))
    elif prefix:
        paths.append(prefix)
    return paths


# One detail reply never carries more than this many locators of one list. The full
# text is one read_source call away, so a wide entity's link lists stay readable.
DETAIL_LIST_PAGE = 60


def references(work, row):
    key = row["id"]
    return work.index.incoming[key] + [
        edge for edge in work.index.outgoing[key] if edge["to"] != key
    ]


def detail_body(work, row, fields=None, lists=True):
    """Entity detail: projected fields, references and mentions.

    Entity evidence is gone, so the body carries no source locators of its own. Link
    lists report at most ``DETAIL_LIST_PAGE`` entries plus their full count.
    """
    body = {
        "id": row["id"],
        "kind": row["kind"],
        "aliases": row["aliases"],
        "value": select_fields(row["value"], fields),
    }
    if lists:
        mentions = row.get("mentions") or []
        edges = references(work, row)
        body["mentions"] = mentions[:DETAIL_LIST_PAGE]
        body["references"] = edges[:DETAIL_LIST_PAGE]
        if len(mentions) > DETAIL_LIST_PAGE:
            body["mentions_count"] = len(mentions)
        if len(edges) > DETAIL_LIST_PAGE:
            body["references_count"] = len(edges)
    return body


def projected(work, row, fields=None):
    """Index-level definition: projected fields, references and mentions."""
    return detail_body(work, row, fields, lists=False)


def changed_entities(work, rows):
    """Mutation reply: each changed entity rendered exactly like ``get_entity``.

    ``rows`` are the registry results (id plus the value paths the call changed), so a
    write reply and a later detail read of the same entity are identical. A large batch
    produces a large reply; history compaction, not the tools, controls context.
    """
    entities = {}
    changed_fields = {}
    for item in rows:
        row = work.records.get(item["id"])
        if row is None:
            continue
        entities[row["id"]] = detail_body(work, row)
        changed_fields[row["id"]] = list(item.get("changed_fields") or [])
    return {"entities": entities, "changed_fields": changed_fields}


def step_scenarios(work, rows, covered):
    """Summarize steps for the same message in other batches and scenarios.

    Only identity, action, channel and which hooks are non-empty; never the hook text.
    """
    messages = {
        row["value"].get("name")
        for row in rows
        if row["kind"] == "messages" and row["value"].get("name")
    }
    if not messages:
        return []
    result = []
    for row in work.records.values():
        if row["kind"] != "steps" or row["id"] in covered:
            continue
        value = row["value"]
        if value.get("message_name") not in messages:
            continue
        processing = value.get("processing") or {}
        result.append(
            {
                "id": row["id"],
                "name": value.get("name"),
                "action": value.get("action"),
                "channel": value.get("channel_name"),
                "condition": value.get("condition") or "",
                "hooks": [hook for hook, ops in processing.items() if ops],
            }
        )
    return result[:20]


def make_server(
    current,
    *,
    readonly=False,
    checked_basics=False,
    compact_rejection=False,
):
    """MCP server for one workspace; readonly exposes the reviewer's seven tools.

    ``checked_basics`` marks a run whose scan layout is the two-pass one, so its basics
    batches may ask ``complete_batch(check_first=true)`` to validate and seal in one call.
    ``compact_rejection`` shortens what that refused call returns: the counts, the cursor
    and one queryable line per row instead of a copy of the head group and the open-item
    text. Detail reads still go through ``validate_entities``, so callers that leave the
    flag alone keep the full reply.
    """
    from fastmcp import FastMCP

    # Read-only tools may run concurrently; registry mutations are serialized so a
    # parallel tool turn cannot interleave two writes.
    write_lock = asyncio.Lock()
    in_flight = 0

    def require(work, *phases):
        if work.assignment.phase not in phases:
            expected = "/".join(phases)
            raise ValueError(f"this tool requires a {expected} assignment")

    async def mutate(apply):
        nonlocal in_flight
        work = current()
        if work.assignment.phase == "review":
            raise ValueError("the reviewer cannot modify entities")
        in_flight += 1
        try:
            async with write_lock:
                return apply()
        finally:
            in_flight -= 1

    def validation(work):
        issues = work.issues()
        advisory = work.advisory_issues()
        unresolved = work.open_unresolved()
        # Deferred repair sites are listed last and cleared here as soon as their
        # blocked signature changes, so every view shows the same order.
        groups, rows = ordered_issue_rows(
            issues, unresolved, work.records, work.sources, work.deferred_groups
        )
        return issues, advisory, unresolved, groups, rows

    server = FastMCP("rsc-extract")

    @server.tool
    def list_entities(
        kind: Kind | None = None,
        batch_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
        full: bool = False,
    ) -> dict:
        """Page through entity indexes, optionally only those linked to one batch.

        Rows carry the ID, category, name and aliases. Set full=true for whole
        definitions of the returned rows. With batch_id the page also reports how many
        steps relate to the same messages.
        """
        work = current()
        if batch_id is not None and batch_id not in work.sources.batches:
            raise ValueError(f"unknown batch {batch_id}")
        if batch_id is None:
            candidates = search(work.records, [""], kind)
        else:
            batch = work.sources.batches[batch_id]
            blocks = {part["block_id"] for part in batch["segments"]}
            candidates = [
                row
                for row in work.records.values()
                if kind is None or row["kind"] == kind
                if any(
                    cite["document_id"] == batch["doc_id"] and cite["block_id"] in blocks
                    for cite in row.get("mentions") or ()
                )
            ]
        page = paged_rows(candidates, offset, limit)
        if full:
            page["rows"] = [projected(work, row) for row in page["rows"]]
        else:
            page["rows"] = [compact_row(work, row) for row in page["rows"]]
        if batch_id is not None:
            covered = {row["id"] for row in candidates}
            related_steps = step_scenarios(work, candidates, covered)
            page["related_step_count"] = len(related_steps)
            page["related_steps"] = related_steps[:5]
        return page

    @server.tool
    def search_entities(
        query: str | None = None,
        kind: Kind | None = None,
        offset: int = 0,
        limit: int = 20,
        queries: list[str] | None = None,
        full: bool = False,
        fields: list[str] | None = None,
    ) -> dict:
        """Search names and aliases; rows are indexes unless full=true.

        Supply exactly one of query or queries. With full=true each row carries the whole
        definition, or only the dotted fields you pass. Read source text with read_source.
        """
        if query is not None and queries is None:
            queries = [query]
        elif query is not None or queries is None:
            raise ValueError("provide either query or queries")
        work = current()
        candidates = search(work.records, queries, kind)
        page = paged_rows(candidates, offset, limit)
        if full:
            page["rows"] = [projected(work, row, fields) for row in page["rows"]]
        else:
            page["rows"] = [compact_row(work, row) for row in page["rows"]]
        return page

    @server.tool
    def get_entity(
        id: str | None = None,
        ids: list[str] | None = None,
        fields: list[str] | None = None,
        offset: int = 0,
        field_offset: int = 0,
    ) -> dict:
        """Read one or more entities by id, optionally only the dotted fields you name.

        Rows are paged with offset; every selected entity comes back whole. Pass fields to
        project named dotted paths, and field_offset to start that field list at an index
        (the remaining fields are returned). Follow a next_offset with the same ids and
        fields to continue.
        """
        if (id is None) == (ids is None):
            raise ValueError("provide either id or ids")
        work = current()
        requested = [id] if id is not None else list(dict.fromkeys(ids or []))
        rows = []
        errors = {}
        for entity_id in requested:
            try:
                rows.append(work.records[work.canonical(entity_id)])
            except ValueError as exc:
                errors[entity_id] = str(exc)
        paths = None
        if fields or field_offset:
            for row in rows[:1]:
                listed = list(fields) if fields else field_paths(row["value"])
                paths = listed[max(0, int(field_offset)) :]
        page = paged_rows(rows, offset)
        entities = {}
        for row in page["rows"]:
            selected = paths if paths is not None and row is rows[0] else fields
            entities[row["id"]] = detail_body(work, row, selected)
        result = {
            "errors": errors,
            "entities": entities,
            "next_offset": page["next_offset"],
            "count": page["count"],
        }
        if page["next_offset"] is not None:
            result["next_ids"] = [row["id"] for row in page["rows"][:5]]
        if paths is not None:
            result["field_count"] = len(paths) + max(0, int(field_offset))
        return result

    @server.tool
    def read_source(
        batch_id: str | None = None,
        document_id: str | None = None,
        section_path: list[str] | None = None,
        block_id: str | None = None,
        blocks: list[int] | None = None,
        offset: int = 0,
        limit_chars: int = 10000,
        original: bool = False,
    ) -> dict:
        """Read a batch, a document section (including subsections), or document blocks.

        For RFC section 4.2, section_path may be ["4", "2"] or ["4.2"].
        offsets page through the returned text or block list. Batch and section text is
        prefixed with each block's integer ID. Use blocks=[1042, 1043] to read several
        blocks at once; each block is delivered once with its integer ID. limit_chars is
        the only text window. original=true returns the frozen source span for a single
        block.
        """
        sources = current().sources
        if offset < 0 or limit_chars < 1:
            raise ValueError("offset must be non-negative and limit_chars must be positive")
        if blocks is not None:
            if batch_id is not None or section_path is not None or block_id is not None:
                raise ValueError("use blocks on its own")
            malformed = [
                item for item in blocks if isinstance(item, bool) or not isinstance(item, int)
            ]
            if malformed:
                raise ValueError("each blocks item is an integer block ID, e.g. blocks=[1042]")
            wanted = list(dict.fromkeys(blocks))
            selected = []
            for number in wanted[offset:]:
                location = sources.block_locator(number)
                document = location["document_id"]
                if original:
                    text = sources.originals[document][location["start"] : location["end"]]
                else:
                    text = sources.blocks[document, location["block_id"]]["text"]
                selected.append(
                    {
                        "id": number,
                        "document_id": document,
                        "block_id": location["block_id"],
                        "text": text,
                    }
                )
            return {
                "blocks": selected,
                "next_offset": None,
                "count": len(wanted),
            }
        location = None
        if batch_id and document_id is None and section_path is None and block_id is None:
            if batch_id not in sources.batches or original:
                raise ValueError("unknown batch or unsupported original view for batch")
            batch = sources.batch_context(batch_id)
            keys = [(batch["doc_id"], part["block"]) for part in batch["segments"]]
            body = "\n".join(f"[{part['id']}] {part['text']}" for part in batch["segments"])
        elif batch_id is None and document_id in sources.documents:
            if block_id is not None and section_path is None:
                locator = sources.evidence(document_id, block_id)
                keys = [(document_id, locator["block_id"])]
            elif block_id is None and section_path is not None:
                path = tuple(section_path)
                keys = sources.sections.get((document_id, path), [])
                if not keys and path and "." in path[-1]:
                    dotted = tuple(path[-1].split("."))
                    if dotted[: len(path) - 1] == path[:-1]:
                        path = dotted
                        keys = sources.sections.get((document_id, path), [])
                if not keys:
                    candidates = [
                        key for doc, key in sources.sections if doc == document_id and key
                    ]
                    for length in range(len(path) - 1, -1, -1):
                        nearby = [key for key in candidates if key[:length] == path[:length]]
                        if nearby:
                            break
                    else:
                        nearby = []
                    return {
                        "error": "unknown or empty section path",
                        "document_id": document_id,
                        "nearby_section_paths": nearby[:12],
                    }
            else:
                raise ValueError("specify one section_path or block_id")
            if original:
                if block_id is None:
                    raise ValueError("the original view requires a block_id")
                location = sources.evidence(*keys[0])
                body = sources.originals[document_id][location["start"] : location["end"]]
            else:
                body = "\n".join(
                    f"[{sources.block_numbers[key]}] {sources.blocks[key]['text']}"
                    for key in keys
                )
        else:
            raise ValueError("specify one batch or one document locator")
        text = body[offset : offset + limit_chars]
        end = offset + len(text)
        return {
            "text": text,
            "original_span": location,
            "next_offset": end if end < len(body) else None,
            "batch_ids": list(
                dict.fromkeys(batch for key in keys for batch in sources.block_batches.get(key, []))
            ),
        }

    async def add_entities(
        entities: Annotated[list[EntityAddition], Field(min_length=1)],
    ) -> dict:
        """Add entities atomically; every created entity is returned in full.

        Query existing names and aliases first. Enrich an existing concept with
        update_entities. The reply carries each created entity exactly as get_entity
        renders it, plus changed_fields with the value paths this call wrote.
        """
        result = await mutate(lambda: current().add_entities(entities))
        return changed_entities(current(), result)

    async def update_entities(
        entities: Annotated[list[EntityUpdate], Field(min_length=1)],
    ) -> dict:
        """Update entities by ID; every changed entity is returned in full.

        Read current entities first. Each patched list is a full replacement: combine new
        details with all still-valid existing fields and operations. Use stable names.
        associated_fields is an array of field-name strings. For a small change to one
        message's associated_fields, send associated_fields_delta instead of the whole
        list: add appends new names, replace maps {old_name: new_name}, remove drops names.
        The reply carries each changed entity
        exactly as get_entity renders it, plus changed_fields with the value paths this
        call changed.
        """
        result = await mutate(lambda: current().update_entities(entities))
        return changed_entities(current(), result)

    async def merge_entities(
        keep_id: str,
        ids: list[str],
        reason: str,
        value: dict | None = None,
    ) -> dict:
        """Merge equivalent entities, returning the survivor in full and absorbed IDs."""
        result = await mutate(
            lambda: current().merge_entities(keep_id, ids, reason, value)
        )
        reply = changed_entities(current(), [result])
        reply["absorbed_ids"] = list(result.get("absorbed_ids") or [])
        return reply

    async def record_unresolved(issue: Unresolved) -> dict:
        """Record a new unresolved issue with the integer block IDs that support it.

        For an issue reported by validation, include the exact check_refs and all listed
        entity IDs. Recording an issue does not remove it from validation. The basics pass
        only accepts gaps in parties, channels and messages.
        """
        return await mutate(lambda: current().record_unresolved(issue))

    async def solve_unresolved(
        unresolved_id: str,
        reason: Annotated[str, Field(min_length=1)],
        evidence: Annotated[list[int], Field(min_length=1)],
    ) -> dict:
        """Mark an existing unresolved ID as solved with a reason and integer block IDs.

        Any linked coverage checks must already pass. Use the unresolved ID even if
        entities listed on the original issue have since been withdrawn.
        """
        return await mutate(lambda: current().solve_unresolved(unresolved_id, reason, evidence))

    def unresolved_page(unresolved):
        """Open items in the shape the prompt carries; no entry is dropped or clipped."""
        return {
            "unresolved": [
                {
                    "id": item["id"],
                    "description": item["description"],
                    "check_refs": item.get("check_refs") or [],
                }
                for item in unresolved
            ],
            "unresolved_next_offset": None,
        }

    def summary_page(work, unresolved, groups, rows, offset):
        """Counts, head group and one page: the summary view and a checked submission
        describe the same state the same way, from sets that were computed once."""
        head = head_group(groups, work.deferred_groups)
        page = paged_rows(rows, offset)
        head_rows = [
            row for row in page["rows"] if head and row["group_id"] == head["group_id"]
        ]
        return {
            "rows": page["rows"],
            "next_offset": page["next_offset"],
            "current_group": (
                {
                    "group_id": head["group_id"],
                    "count": head["count"],
                    "unresolved_ids": head["unresolved_ids"][:10],
                    "issues": head_rows,
                    "issues_in_page": len(head_rows),
                }
                if head
                else None
            ),
            **unresolved_page(unresolved),
            "hint": (
                "view=groups pages the catalog; view=group with group_id pages one "
                "group; view=issues filters by type or entity; view=issue with "
                "issue_id returns its suggestion; view=unresolved pages open items."
            ),
        }

    @server.tool
    def validate_entities(
        view: str = "summary",
        group_id: str | None = None,
        issue_id: str | None = None,
        issue_type: str | None = None,
        entity_id: str | None = None,
        offset: int = 0,
        include_advisory: bool = False,
    ) -> dict:
        """Read validation state; nothing is clipped.

        view=summary (default) returns the global counts, the open-gap count and the rows
        from offset, starting with the current repair group.
        view=groups pages the group catalog: one entry per repair site, largest first.
        view=group with group_id returns that group's rows. view=issues returns all rows,
        filtered by issue_type or entity_id. view=issue with issue_id returns one row's
        full description and suggestion.

        Row ids are stable and can be passed back as issue_id. error_count counts
        blocking rows only; advisory rows appear only with include_advisory.
        """
        work = current()
        require(work, "scan", "category", "gap", "global", "review")
        issues, advisory, unresolved, groups, rows = validation(work)
        advisory_rows = ordered_issue_rows(advisory, (), work.records)[1] if advisory else []
        # An advisory row is never in a blocking page, but its detail is still
        # addressable by id once the caller has seen it.
        by_id = {row["id"]: row for row in rows + advisory_rows}
        if include_advisory:
            rows = rows + advisory_rows
        catalog = group_rows(groups)
        counts = {
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "deferred_checks": work.deferred_checks(),
            "groups": catalog[:5],
        }
        if view == "summary":
            counts.update(summary_page(work, unresolved, groups, rows, offset))
            return counts
        if view == "groups":
            page = paged_rows(catalog, offset)
            counts.update(
                {
                    "groups": page["rows"],
                    "next_offset": page["next_offset"],
                }
            )
            return counts
        if view == "group":
            if group_id is None:
                raise ValueError("view=group requires group_id")
            selected = [row for row in rows if row["group_id"] == group_id]
            if not selected:
                counts.update(
                    {
                        "group_id": group_id,
                        "issues": [],
                        "note": "this group is clean in the current registry",
                    }
                )
                return counts
            # Reading the group is what a stalled attempt is judged on, so record it
            # only when the reply actually carries the group's rows.
            work.read_groups.add(("group", group_id))
            page = paged_rows(selected, offset)
            counts.update(
                {
                    "group_id": group_id,
                    "issues": page["rows"],
                    "next_offset": page["next_offset"],
                }
            )
            return counts
        if view == "issues":
            selected = [
                row
                for row in rows
                if (issue_type is None or row["type"] == issue_type)
                and (entity_id is None or entity_id in row["records"])
            ]
            page = paged_rows(selected, offset)
            counts.update(
                {
                    "issues": page["rows"],
                    "next_offset": page["next_offset"],
                }
            )
            return counts
        if view == "unresolved":
            page = paged_rows(unresolved, offset)
            counts.update(
                {
                    "unresolved_rows": [
                        {
                            "id": item["id"],
                            "description": item["description"],
                            "entity_ids": item.get("entity_ids") or [],
                            "check_refs": item.get("check_refs") or [],
                            "evidence": list(item.get("evidence") or []),
                        }
                        for item in page["rows"]
                    ],
                    "unresolved_next_offset": page["next_offset"],
                }
            )
            return counts
        if view == "issue":
            if issue_id is None:
                raise ValueError("view=issue requires issue_id")
            row = by_id.get(issue_id)
            if row is None:
                raise ValueError(f"unknown or resolved issue {issue_id}")
            work.read_groups.add(("issue", issue_id))
            issue = next(
                item
                for item in issues + advisory
                if issue_ref(item) == issue_id
                or issue_id.startswith(issue_ref(item) + "#")
            )
            counts.update(
                {
                    "issue": {
                        **row,
                        "suggestion": with_suggestions([issue])[0].get("suggestion", ""),
                    }
                }
            )
            return counts
        raise ValueError("view must be summary, groups, group, issues, issue or unresolved")

    async def complete(summary: str, phases: tuple[str, ...], *, check_first=False) -> dict:
        work = current()
        phase = work.assignment.phase
        if check_first and (
            phase != "scan" or work.assignment.kind != "basics" or not checked_basics
        ):
            raise ValueError(
                "check_first is for a basics batch of the two-pass scan layout; "
                "submit this assignment with a plain summary"
            )
        if phase not in phases:
            expected_tool = "complete_batch" if phase == "scan" else "complete_fix"
            raise ValueError(f"use {expected_tool} for {phase} assignments")
        scope = {
            "repairable_categories": list(work.write_scope),
        }
        # Wait for mutations emitted in the same parallel turn before checking the
        # receipt, then validate inside the lock.
        while in_flight:
            await asyncio.sleep(0.01)
        async with write_lock:
            unresolved = work.open_unresolved()
            if phase == "global":
                scope.update(compact_validation(work))
            if work.completion is not None:
                if summary != work.completion.get("summary"):
                    raise ValueError("assignment already sealed with another completion")
                return {"accepted": True, **scope}
            if not summary.strip() or len(summary) > 2000:
                raise ValueError("provide a nonempty summary of at most 2000 characters")
            if not check_first and not work.reconsidered:
                reminder = steps_reminder(work)
                if reminder is not None:
                    return reminder
            issues = work.issues()
            if check_first:
                # The same call validates the whole registry: a batch is sealed only when
                # nothing blocks and nothing is advisory, never on a clean-looking page.
                validated = validation(work)
                blocking, advisory = validated[0], validated[1]
                if blocking or advisory:
                    return checked_rejection(work, validated)
            expected = sorted(
                issue["id"]
                for issue in unresolved
                if phase == "global" or issue["id"] in work.created_issues
            )
            receipt = {
                "status": "completed_with_unresolved" if expected else "completed",
                "summary": summary,
                "unresolved_ids": expected,
            }
            start = work.repair_start_count
            if phase == "category":
                if issues and len(issues) >= start:
                    return rejection(work, issues, start, scope)
            if phase == "global":
                # Reject a Fix that changed nothing and reduced nothing, once per
                # assignment: a second no-op submission is accepted so the run cannot
                # stall on the same scope until the request budget is gone.
                if issues and len(issues) >= start and not work.mutations and not work.reject_count:
                    work.reject_count += 1
                    payload = rejection(work, issues, start, scope)
                    payload["message"] = (
                        "This Fix made no entity change and did not reduce the error "
                        "count. Read the current groups, make a source-supported change, "
                        "or explain why these errors cannot be fixed with the available "
                        "source text."
                    )
                    return payload
            work.completion = receipt
            return {
                "accepted": True,
                "assignment": work.assignment.id,
                "error_count": len(issues),
                "repair_start_count": start,
                **scope,
            }

    def steps_reminder(work):
        """One reminder that an empty steps batch still has this batch's open items.

        A reminder is not a validation result: it writes no mutation, no open item and
        nothing else, and the next submission seals as usual. It is reported separately
        from a ``checked`` rejection so the two can be counted apart.
        """
        rows = work.steps_recheck()
        if rows is None:
            return None
        work.reconsidered = True
        return {
            "accepted": False,
            "review": "steps-recheck",
            "assignment": work.assignment.id,
            "kind": "steps",
            "open_items": rows,
            "message": (
                "This steps batch made no change. The basics pass for this same batch "
                "left open items that cite its text, and no step refers to them yet. "
                "The wording there describes earlier work: if the batch does describe "
                "behaviour, write it with the existing tools; if an existing step "
                "already covers it, cite that step in the summary; if the passage truly "
                "carries nothing to model, say why and submit again. This is a reminder, "
                "not a validation failure: submit again to seal the batch."
            ),
        }

    def checked_rejection(work, validated):
        """The summary view of the state that just stopped a checked submission."""
        if compact_rejection:
            return compact_checked_rejection(work, validated)
        issues, advisory, unresolved, groups, rows = validated
        payload = {
            "accepted": False,
            "checked": True,
            "assignment": work.assignment.id,
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "deferred_checks": work.deferred_checks(),
            "groups": group_rows(groups)[:5],
            **summary_page(work, unresolved, groups, rows, 0),
        }
        if advisory and not issues:
            # Blocking is empty, so the advisory rows are what stopped the seal: give
            # their ids and the detail path rather than only a count.
            advisory_rows = ordered_issue_rows(advisory, (), work.records)[1]
            payload["advisory"] = [
                {
                    "id": row["id"],
                    "type": row["type"],
                    "records": row["records"],
                    "description": row["description"],
                }
                for row in advisory_rows[:5]
            ]
            payload["advisory_hint"] = (
                "Read them with validate_entities(view='issues', include_advisory=true) "
                "or view='issue', issue_id=..."
            )
        payload["message"] = (
            "This call validated the batch and did not seal it: "
            f"{len(issues)} blocking and {len(advisory)} advisory row(s). "
            "Repair them, then submit again with complete_batch(summary). That check "
            "already counted as this batch's validation, so do not repeat check_first "
            "on rows you have already seen."
        )
        return payload

    def compact_checked_rejection(work, validated):
        """A refused checked submission as counts and one line per row.

        The row order is the one the full reply uses, so every ``id`` means the same
        thing in both. What is dropped is only what a row-level detail read can
        reproduce: the head group's problems, the group catalog and the open-item
        descriptions.
        """
        issues, advisory, unresolved, _, rows = validated
        payload = {
            "accepted": False,
            "checked": True,
            "assignment": work.assignment.id,
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "deferred_checks": work.deferred_checks(),
            "next_offset": None,
            "rows": [
                {
                    "id": row["id"],
                    "check_ref": row["check_ref"],
                    "group_id": row["group_id"],
                }
                for row in rows
            ],
        }
        if advisory and not issues:
            # Nothing blocking, so the advisory rows are what stopped the seal: name the
            # ones in this page by id and type and point at the detail read.
            advisory_rows = ordered_issue_rows(advisory, (), work.records)[1]
            payload["advisory"] = [
                {"id": row["id"], "type": row["type"]} for row in advisory_rows[:5]
            ]
            payload["advisory_hint"] = (
                "Read one with validate_entities(view='issue', issue_id=...), or all of "
                "them with view='issues', include_advisory=true."
            )
        counts = f"{len(issues)} blocking and {len(advisory)} advisory row(s)"
        advisory_hint = (
            " Add include_advisory=true to view='issues' to see the advisory rows."
            if advisory and issues
            else ""
        )
        payload["message"] = (
            f"This call validated the whole registry and did not seal the batch: {counts}."
            f"{advisory_hint} Fix the rows you can support, then submit again with "
            "complete_batch(summary). Details: view='issue', issue_id=<row id>; more rows: "
            "view='issues', offset=next_offset; open items: view='unresolved'; the old "
            "full first page: view='summary'."
        )
        return payload

    def rejection(work, issues, start, scope):
        """Compact diagnostics: counts, the group catalog head and all rows."""
        _, advisory, unresolved, groups, rows = validation(work)
        return {
            "accepted": False,
            "error_count": len(issues),
            "repair_start_count": start,
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "groups": group_rows(groups)[:5],
            "errors": rows,
            "next_offset": None,
            "hint": (
                "Call validate_entities(view='group', group_id=...) for one group's rows, "
                "or view='issue', issue_id=... for suggestions and details."
            ),
            "message": (
                "Close at least one more issue than this repair introduces."
                if work.assignment.phase != "global"
                else "The error count did not drop."
            ),
            **scope,
        }

    def compact_validation(work):
        """Small validation summary attached to accepted and rejected Fix receipts."""
        issues, advisory, unresolved, groups, _ = validation(work)
        return {
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "groups": group_rows(groups)[:5],
            "deferred_checks": work.deferred_checks(),
        }

    async def complete_batch(summary: str, check_first: bool = False) -> dict:
        """Submit the current scan batch with a short summary after all writes finish.

        check_first asks the host to validate the whole registry in this same call. A
        basics batch of the two-pass layout is sealed only when nothing blocks and nothing
        is advisory; otherwise the reply carries the summary view, nothing is sealed, and
        the next attempt submits with a plain summary. The response includes
        repairable_categories for the current and earlier categories.
        """
        return await complete(summary, ("scan",), check_first=check_first)

    async def complete_fix(summary: str) -> dict:
        """Submit the current category or global Fix with a short summary after all writes finish.

        A category Fix is accepted when validation is clean or the issue count drops.
        The response reports error_count before and after and the current group catalog
        head. When rejected it lists the first page of rows; fetch a row's suggestion
        with validate_entities(view="issue", issue_id=...).
        """
        return await complete(summary, ("category", "global"))

    @server.tool
    async def record_gap(
        kind: Kind,
        name: Annotated[str, Field(min_length=1, max_length=300)],
        block_id: str,
        evidence_text: str | None = None,
        missing_attributes: list[str] | None = None,
    ) -> dict:
        """Record one source-supported gap in the current batch review.

        kind is the entity category that should carry the missing content, name is the
        object or interaction, and block_id is the block in this batch that shows the gap;
        copy the id shown in front of the paragraph. evidence_text optionally quotes the
        supporting sentence and is stored as given without verification. missing_attributes
        optionally names the missing attributes. The host adds the document, batch and
        section path from the block.
        """
        work = current()
        require(work, "review")
        batch_id = work.assignment.id
        batch = work.sources.batches.get(batch_id)
        if batch is None:
            raise ValueError("the review assignment is not a batch")
        segments = {part["block_id"]: part for part in batch["segments"]}
        block = segments.get(block_id)
        if block is None:
            known = ", ".join(sorted(segments)[:12])
            raise ValueError(
                f"{block_id} is not a block of {batch_id}; use one of: {known}"
            )
        quote = (evidence_text or "").strip()
        if len(quote) > 4000:
            raise ValueError("evidence_text is too long")
        attributes = [item.strip() for item in (missing_attributes or []) if item.strip()]
        for gap in work.gaps:
            # The quote is a note for the fixer, so a reworded quote is still the same gap.
            if (
                gap["kind"] == kind
                and gap["name"] == name.strip()
                and gap["block_id"] == block_id
                and gap.get("missing_attributes") == attributes
            ):
                return {"recorded": False, "gap": gap}
        gap = {
            "kind": kind,
            "name": name.strip(),
            "evidence_text": quote,
            "missing_attributes": attributes,
            "document_id": batch["doc_id"],
            "block_id": block_id,
            "section_path": list(block.get("section_path") or []),
            "batch_id": batch_id,
            "note": f"cited from {batch_id}",
        }
        work.gaps.append(gap)
        return {"recorded": True, "gap": gap}

    @server.tool
    async def complete_review() -> dict:
        """Finish reviewing the current batch; zero gaps is allowed."""
        work = current()
        require(work, "review")
        while in_flight:
            await asyncio.sleep(0.01)
        async with write_lock:
            if work.completion is not None:
                return {"accepted": True, "assignment": work.assignment.id}
            work.completion = {"status": "reviewed", "summary": "review complete"}
            return {
                "accepted": True,
                "assignment": work.assignment.id,
                "gap_count": len(work.gaps),
            }

    async def complete_gaps(
        summary: str = "",
        unresolved_gap_ids: list[str] | None = None,
    ) -> dict:
        """Finish the current gap fix after all writes.

        summary is a short note on the additions and on what remains. unresolved_gap_ids
        lists the gap IDs from the assignment prompt whose cited block does not support the
        gap after re-reading; the host keeps those gaps open for the global Fix.
        """
        work = current()
        require(work, "gap")
        unresolved = list(dict.fromkeys(unresolved_gap_ids or []))
        known = {gap.get("gap_id") for gap in work.gaps}
        unknown = [gap_id for gap_id in unresolved if gap_id not in known]
        if unknown:
            raise ValueError(f"unknown gap ids: {', '.join(unknown)}")
        if len(summary) > 2000:
            raise ValueError("summary must be at most 2000 characters")
        while in_flight:
            await asyncio.sleep(0.01)
        async with write_lock:
            if work.completion is not None:
                return {"accepted": True, "assignment": work.assignment.id}
            work.completion = {
                "status": "gaps_completed",
                "summary": summary.strip(),
                "unresolved_gap_ids": unresolved,
            }
            return {"accepted": True, "assignment": work.assignment.id}

    if not readonly:
        for tool in (
            add_entities,
            update_entities,
            merge_entities,
            record_unresolved,
            solve_unresolved,
            complete_batch,
            complete_fix,
            complete_gaps,
        ):
            server.tool(tool)

    return server


def related(work):
    assignment = work.assignment
    batches = work.sources.batches
    if assignment.id not in batches:
        return {}
    batch = batches[assignment.id]
    doc_id = batch["doc_id"]
    blocks = {part["block_id"] for part in batch["segments"]}
    counts = Counter(row["kind"] for row in work.records.values())
    candidates = [
        summary(row)
        for row in work.records.values()
        if any(
            cite["document_id"] == doc_id and cite["block_id"] in blocks
            for cite in row.get("mentions") or ()
        )
    ]
    references = [
        {key: link[key] for key in ("raw", "target_section_id", "target_rfc", "from_block")}
        for link in work.sources.references[doc_id]
        if link["from_block"] in blocks
    ]
    return {
        "entity_counts": {kind: counts[kind] for kind in ORDER},
        "candidates": candidates,
        # Only the citations are projected: every open item and its full description
        # still reaches the prompt, and the model can re-read any block it needs.
        "unresolved": projected_unresolved(work.open_unresolved()),
        "references": references[:20],
    }
