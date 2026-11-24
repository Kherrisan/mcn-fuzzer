"""MCP surface for one active extraction workspace.

Every read reply is bounded: rows are chosen by a token budget, a page reports where
to continue, and a page of mutable data carries the registry snapshot it came from so
a cursor that predates a write is rejected instead of returning stale rows.
"""

import asyncio
from collections import Counter
from typing import Annotated

from pydantic import Field

from rsc.workflow.store import dumps

from .groups import group_rows, issue_ref, ordered_issue_rows
from .guidance import with_suggestions
from .paging import (
    DETAIL_PAGE_TOKENS,
    INDEX_PAGE_TOKENS,
    MINIMUM_PAGE_TOKENS,
    TurnBudget,
    bounded_page,
    count_tokens,
    page_limit,
)
from .registry import (
    MAX_PAGE,
    ORDER,
    EntityAddition,
    EntityUpdate,
    Evidence,
    Kind,
    Unresolved,
    identity,
    summary,
)

# One server keeps the issue ids it served per snapshot, so a later read can report
# what appeared and what disappeared without re-running the old validation.
SEEN_SNAPSHOTS = 8


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
    """Index-level row: identity plus evidence locations, never evidence text."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": identity(row),
        "aliases": row["aliases"],
        "evidence": [
            {
                "document_id": cite["document_id"],
                "block_id": cite["block_id"],
                "field": cite.get("field") or "",
            }
            for cite in row["evidence"][:3]
        ],
        "field_count": len(row["evidence"]),
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


def citation_location(item):
    """The values a scan prompt needs to re-read one cited block.

    Returns ``None`` for an old citation without a usable location: the caller passes
    that item through unchanged rather than dropping or rewriting it.
    """
    document_id = item.get("document_id")
    block_id = item.get("block_id")
    if not document_id or not block_id:
        return None
    location = {"document_id": document_id, "block_id": block_id}
    field = (item.get("field") or "").strip()
    if field:
        location["field"] = field
    return location


def projected_unresolved(items):
    """Open unresolved items with only the re-read locations kept in their evidence.

    Every open item is kept, in order, with its id, description, missing information,
    entities and check refs untouched. Within one item the evidence is reduced to
    ``document_id``, ``block_id`` and a non-empty ``field``, deduplicated on those three
    values in first-seen order; the same block under another field, or cited by another
    item, stays a separate entry. ``note``, ``section_path`` and offsets are dropped
    because the block can be re-read instead.
    """
    projected = []
    for item in items:
        evidence = []
        seen = set()
        for citation in item.get("evidence") or ():
            location = citation_location(citation)
            if location is None:
                evidence.append(citation)
                continue
            key = (location["document_id"], location["block_id"], location.get("field", ""))
            if key in seen:
                continue
            seen.add(key)
            evidence.append(location)
        projected.append({**item, "evidence": evidence})
    return projected


def evidence_locations(row):
    return [
        {
            "document_id": cite["document_id"],
            "block_id": cite["block_id"],
            "section_path": cite.get("section_path") or [],
            "field": cite.get("field") or "",
            "note": cite.get("note") or "",
        }
        for cite in row["evidence"]
    ]


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
# text is one read_source call away, so a wide entity cannot blow up the page.
DETAIL_LIST_PAGE = 60


def references(work, row):
    key = row["id"]
    return work.index.incoming[key] + [
        edge for edge in work.index.outgoing[key] if edge["to"] != key
    ]


def detail_body(work, row, fields=None, lists=True):
    """Entity detail: projected fields, evidence locations, references and mentions.

    Evidence text is not included; read_source returns it for a block. Long lists are
    capped and report their full count so the reply stays inside one page.
    """
    body = {
        "id": row["id"],
        "kind": row["kind"],
        "aliases": row["aliases"],
        "value": select_fields(row["value"], fields),
        "evidence": evidence_locations(row)[:DETAIL_LIST_PAGE],
    }
    if len(row["evidence"]) > DETAIL_LIST_PAGE:
        body["evidence_count"] = len(row["evidence"])
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
    """Index-level definition: projected fields and evidence locations."""
    return detail_body(work, row, fields, lists=False)


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


def make_server(current, *, readonly=False, turn_budget=None, context_tokens=None):
    """MCP server for one workspace; readonly exposes the reviewer's seven tools."""
    from fastmcp import FastMCP

    # Read-only tools may run concurrently; registry mutations are serialized so a
    # parallel tool turn cannot interleave two writes.
    write_lock = asyncio.Lock()
    in_flight = 0
    budget = turn_budget or TurnBudget(page_limit(context_tokens or 258400))
    seen = {}

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

    def page_tokens(serialize, rows, requested, offset, limit=None):
        """Page one reply against the call budget and the turn's remaining budget."""
        allowed = budget.reserve(requested)
        paged = bounded_page(rows, serialize, allowed, offset, limit)
        budget.settle(allowed, paged["tokens"])
        return paged, allowed

    def clipped_full(work, row, fields, allowed):
        """Full definition, or a bounded stand-in when one row cannot fit a page."""
        text = dumps(projected(work, row, fields))
        cost = count_tokens(text) + 4
        if cost <= allowed:
            return projected(work, row, fields), cost
        compact = compact_row(work, row)
        compact.update(
            {
                "truncated": True,
                "field_count": len(field_paths(row["value"])),
                "hint": (
                    f"get_entity(id='{row['id']}', field_offset=0) pages this definition "
                    "by field"
                ),
            }
        )
        return compact, count_tokens(dumps(compact)) + 4

    def clip_rows(work, paged, fields, allowed):
        """Replace over-budget full rows so one entity cannot bypass the page limit."""
        clipped = []
        used = 0
        for row in paged["rows"]:
            body, cost = clipped_full(work, row, fields, allowed)
            clipped.append(body)
            used += cost
        budget.settle(allowed, used)
        paged["rows"] = clipped
        paged["tokens"] = used
        return paged

    def stale(work, snapshot):
        if snapshot is not None and snapshot != work.snapshot:
            return {
                "stale": True,
                "snapshot": work.snapshot,
                "note": (
                    "The registry changed after this cursor was issued. Re-read from "
                    "offset 0 with the same filters and the new snapshot."
                ),
            }
        return None

    def remember(work, issues):
        seen[work.snapshot] = [issue_ref(issue) for issue in issues]
        while len(seen) > SEEN_SNAPSHOTS:
            seen.pop(next(iter(seen)))

    def validation(work):
        issues = work.issues()
        advisory = work.advisory_issues()
        unresolved = work.open_unresolved()
        groups, rows = ordered_issue_rows(issues, unresolved, work.records)
        return issues, advisory, unresolved, groups, rows

    server = FastMCP("rsc-extract")

    @server.tool
    def list_entities(
        kind: Kind | None = None,
        batch_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
        max_tokens: int = INDEX_PAGE_TOKENS,
        full: bool = False,
        snapshot: int | None = None,
    ) -> dict:
        """Page through entity indexes, optionally only those linked to one batch.

        Rows carry the ID, category, name, aliases and the first evidence locations.
        Set full=true for whole definitions of the returned rows. With batch_id the page
        also reports how many steps relate to the same messages. Pass the snapshot from
        the previous page to be told when the registry changed under the cursor.
        """
        work = current()
        if batch_id is not None and batch_id not in work.sources.batches:
            raise ValueError(f"unknown batch {batch_id}")
        if (changed := stale(work, snapshot)) is not None:
            return changed
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
                    for cite in row["evidence"]
                )
            ]
        if full:
            paged, allowed = page_tokens(
                lambda row: dumps(projected(work, row)),
                candidates,
                max_tokens,
                offset,
                limit,
            )
            clip_rows(work, paged, None, allowed)
        else:
            paged, allowed = page_tokens(
                lambda row: dumps(compact_row(work, row)),
                candidates,
                max_tokens,
                offset,
                limit,
            )
            paged["rows"] = [compact_row(work, row) for row in paged["rows"]]
        result = {
            "snapshot": work.snapshot,
            "rows": paged["rows"],
            "next_offset": paged["next_offset"],
            "count": paged["count"],
            "page_tokens": paged["tokens"],
            "page_limit": allowed,
        }
        if batch_id is not None:
            covered = {row["id"] for row in candidates}
            related_steps = step_scenarios(work, candidates, covered)
            result["related_step_count"] = len(related_steps)
            result["related_steps"] = related_steps[:5]
        return result

    @server.tool
    def search_entities(
        query: str | None = None,
        kind: Kind | None = None,
        offset: int = 0,
        limit: int = 20,
        queries: list[str] | None = None,
        max_tokens: int = INDEX_PAGE_TOKENS,
        full: bool = False,
        fields: list[str] | None = None,
        snapshot: int | None = None,
    ) -> dict:
        """Search names and aliases; rows are indexes unless full=true.

        Supply exactly one of query or queries. With full=true each row carries the whole
        definition, or only the dotted fields you pass. Evidence is returned as locations;
        read the text with read_source.
        """
        if query is not None and queries is None:
            queries = [query]
        elif query is not None or queries is None:
            raise ValueError("provide either query or queries")
        work = current()
        if (changed := stale(work, snapshot)) is not None:
            return changed
        candidates = search(work.records, queries, kind)

        def serialize(row):
            return dumps(projected(work, row, fields) if full else compact_row(work, row))

        paged, allowed = page_tokens(serialize, candidates, max_tokens, offset, limit)
        if full:
            clip_rows(work, paged, fields, allowed)
        else:
            paged["rows"] = [compact_row(work, row) for row in paged["rows"]]
        return {
            "snapshot": work.snapshot,
            "rows": paged["rows"],
            "next_offset": paged["next_offset"],
            "count": paged["count"],
            "page_tokens": paged["tokens"],
            "page_limit": allowed,
        }

    @server.tool
    def get_entity(
        id: str | None = None,
        ids: list[str] | None = None,
        fields: list[str] | None = None,
        offset: int = 0,
        field_offset: int = 0,
        max_tokens: int | None = None,
        snapshot: int | None = None,
    ) -> dict:
        """Read one or more entities by id, optionally only the dotted fields you name.

        Evidence is returned as locations and notes; use read_source for its text. A page
        too large for the token budget reports next_offset and next_ids. Follow a
        next_offset with the same ids and fields to continue. When a single entity is
        larger than one page, the reply sets fields_truncated and next_field_offset:
        continue with the same id and field_offset to read the rest of its fields.
        """
        if (id is None) == (ids is None):
            raise ValueError("provide either id or ids")
        work = current()
        if (changed := stale(work, snapshot)) is not None:
            return changed
        requested = [id] if id is not None else list(dict.fromkeys(ids or []))
        rows = []
        errors = {}
        for entity_id in requested:
            try:
                rows.append(work.records[work.canonical(entity_id)])
            except ValueError as exc:
                errors[entity_id] = str(exc)
        if max_tokens is None:
            # One batch read of N entities is worth N detail pages, still capped by the
            # turn's remaining budget.
            max_tokens = DETAIL_PAGE_TOKENS * max(1, len(rows))
        allowed = budget.reserve(max_tokens)
        base = {"snapshot": work.snapshot, "errors": errors, "page_limit": allowed}
        if len(rows) == 1:
            row = rows[0]
            body = detail_body(work, row, fields)
            cost = count_tokens(dumps(body)) + 4
            if cost > allowed:
                # The skeleton alone may be too large for a small page; drop the link
                # lists first and tell the caller they exist.
                body = detail_body(work, row, fields, lists=False)
                cost = count_tokens(dumps(body)) + 4
            paths = list(fields) if fields else field_paths(row["value"])
            if cost > allowed and paths:
                # One entity alone exceeds the page: split its fields into pages rather
                # than returning a page that is silently over budget.
                pages = []
                for path in paths:
                    size = count_tokens(dumps(select_fields(row["value"], [path]))) + 4
                    pages.append((path, size))
                selected = []
                used = 0
                index = max(0, int(field_offset))
                while index < len(pages):
                    path, size = pages[index]
                    if selected and used + size > allowed:
                        break
                    selected.append(path)
                    used += size
                    index += 1
                    if used >= allowed:
                        break
                page_body = detail_body(work, row, selected, lists=False)
                used = count_tokens(dumps(page_body)) + 4
                # The skeleton is part of the page, so trim fields until the whole
                # reply fits rather than trusting the sum of the field sizes.
                while used > allowed and len(selected) > 1:
                    selected.pop()
                    index -= 1
                    page_body = detail_body(work, row, selected, lists=False)
                    used = count_tokens(dumps(page_body)) + 4
                budget.settle(allowed, used)
                return {
                    **base,
                    "entities": {row["id"]: page_body},
                    "fields_truncated": index < len(pages),
                    "next_field_offset": index if index < len(pages) else None,
                    "field_count": len(pages),
                    "remaining_fields": [path for path, _ in pages[index:]][:20],
                    "next_offset": None,
                    "count": 1,
                    "page_tokens": used,
                }
        paged, allowed = page_tokens(
            lambda item: dumps(detail_body(work, item, fields)),
            rows,
            max_tokens,
            offset,
        )
        result = {
            **base,
            "entities": {row["id"]: detail_body(work, row, fields) for row in paged["rows"]},
            "next_offset": paged["next_offset"],
            "count": paged["count"],
            "page_tokens": paged["tokens"],
        }
        if paged["next_offset"] is not None:
            result["next_ids"] = [row["id"] for row in rows[paged["next_offset"] :][:5]]
        return result

    @server.tool
    def read_source(
        batch_id: str | None = None,
        document_id: str | None = None,
        section_path: list[str] | None = None,
        block_id: str | None = None,
        blocks: list[dict] | None = None,
        offset: int = 0,
        limit_chars: int = 10000,
        max_tokens: int = DETAIL_PAGE_TOKENS,
        original: bool = False,
    ) -> dict:
        """Read a batch, a document section (including subsections), or document blocks.

        For RFC section 4.2, section_path may be ["4", "2"] or ["4.2"].
        Offsets page through the returned text or block list. Use blocks=[{document_id,
        block_id}, ...] to read several evidence blocks at once; each block is delivered
        once and next_offset continues the list. original=true returns the frozen source
        span for a single block.
        """
        sources = current().sources
        if offset < 0 or limit_chars < 1:
            raise ValueError("offset must be non-negative and limit_chars must be positive")
        if blocks is not None:
            if batch_id is not None or section_path is not None or block_id is not None:
                raise ValueError("use blocks on its own")
            malformed = [
                item
                for item in blocks
                if not isinstance(item, dict)
                or "document_id" not in item
                or "block_id" not in item
            ]
            if malformed:
                raise ValueError(
                    "each blocks item needs document_id and block_id, e.g. "
                    '{"document_id": "rfc2326", "block_id": "blk-000012"}'
                )
            wanted = list(
                dict.fromkeys((item["document_id"], item["block_id"]) for item in blocks)
            )
            allowed = budget.reserve(max_tokens)
            selected = []
            used = 0
            index = offset
            while index < len(wanted):
                document, block = wanted[index]
                if original:
                    location = sources.evidence(document, block)
                    text = sources.originals[document][location["start"] : location["end"]]
                else:
                    location = sources.evidence(document, block)
                    text = sources.blocks[document, location["block_id"]]["text"]
                cost = count_tokens(text) + 16
                if selected and used + cost > allowed:
                    break
                selected.append(
                    {"document_id": document, "block_id": location["block_id"], "text": text}
                )
                used += cost
                index += 1
                if used >= allowed:
                    break
            budget.settle(allowed, used)
            return {
                "blocks": selected,
                "next_offset": index if index < len(wanted) else None,
                "count": len(wanted),
                "page_tokens": used,
                "page_limit": allowed,
            }
        location = None
        if batch_id and document_id is None and section_path is None and block_id is None:
            if batch_id not in sources.batches or original:
                raise ValueError("unknown batch or unsupported original view for batch")
            batch = sources.batch_context(batch_id)
            keys = [(batch["doc_id"], part["block"]) for part in batch["segments"]]
            body = "\n".join(f"[{part['block']}] {part['text']}" for part in batch["segments"])
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
                body = "\n".join(f"[{key[1]}] {sources.blocks[key]['text']}" for key in keys)
        else:
            raise ValueError("specify one batch or one document locator")
        # The char window is capped by the call budget as well, so one section cannot
        # spend the whole turn. The estimate is corrected against the real token count
        # so a dense block cannot overshoot the budget by a fixed ratio.
        allowed = budget.reserve(max_tokens)
        cap = min(limit_chars, 10000, max(MINIMUM_PAGE_TOKENS * 4, allowed * 4))
        text = body[offset : offset + cap]
        for _ in range(3):
            spent = count_tokens(text)
            if spent <= allowed or len(text) <= MINIMUM_PAGE_TOKENS * 4:
                break
            text = text[: max(MINIMUM_PAGE_TOKENS * 4, len(text) * allowed // spent)]
        spent = count_tokens(text)
        end = offset + len(text)
        budget.settle(allowed, spent)
        return {
            "text": text,
            "original_span": location,
            "next_offset": end if end < len(body) else None,
            "page_tokens": spent,
            "page_limit": allowed,
            "batch_ids": list(
                dict.fromkeys(batch for key in keys for batch in sources.block_batches.get(key, []))
            ),
        }

    async def add_entities(
        entities: Annotated[list[EntityAddition], Field(min_length=1, max_length=MAX_PAGE)],
    ) -> list[dict]:
        """Add up to 20 entities atomically with block evidence for every entity.

        Query existing names and aliases first. Enrich an existing concept with update_entities.
        """
        return await mutate(lambda: current().add_entities(entities))

    async def update_entities(
        entities: Annotated[list[EntityUpdate], Field(min_length=1, max_length=MAX_PAGE)],
    ) -> list[dict]:
        """Update entities by ID, citing each changed top-level field.

        Read current entities first. Each patched list is a full replacement: combine new
        details with all still-valid existing fields, operations and evidence. Use stable names.
        When exactly one field changes, unqualified citations apply to that field.
        For multiple changed fields, cite every field explicitly. Submit at most 20 entities.
        """
        return await mutate(lambda: current().update_entities(entities))

    async def merge_entities(
        keep_id: str,
        ids: list[str],
        reason: str,
        evidence: list[Evidence],
        value: dict | None = None,
    ) -> dict:
        """Merge equivalent entities, providing a final value when attributes differ."""
        return await mutate(lambda: current().merge_entities(keep_id, ids, reason, evidence, value))

    async def record_unresolved(issue: Unresolved) -> dict:
        """Record a new unresolved issue with source evidence.

        For an issue reported by validation, include the exact check_refs and all listed
        entity IDs. Recording an issue does not remove it from validation.
        """
        return await mutate(lambda: current().record_unresolved(issue))

    async def solve_unresolved(
        unresolved_id: str,
        reason: Annotated[str, Field(min_length=1)],
        evidence: Annotated[list[Evidence], Field(min_length=1)],
    ) -> dict:
        """Mark an existing unresolved ID as solved with a reason and source evidence.

        Any linked coverage checks must already pass. Use the unresolved ID even if
        entities listed on the original issue have since been withdrawn.
        """
        return await mutate(lambda: current().solve_unresolved(unresolved_id, reason, evidence))

    @server.tool
    def validate_entities(
        view: str = "summary",
        group_id: str | None = None,
        issue_id: str | None = None,
        issue_type: str | None = None,
        entity_id: str | None = None,
        offset: int = 0,
        max_tokens: int = INDEX_PAGE_TOKENS,
        snapshot: int | None = None,
        include_advisory: bool = False,
    ) -> dict:
        """Read validation state in bounded pages.

        view=summary (default) returns the global counts, the open-gap count, the first
        page of the current repair group and what changed since an earlier snapshot.
        view=groups pages the group catalog: one entry per repair site, largest first.
        view=group with group_id pages that group's rows. view=issues pages all rows,
        filtered by issue_type or entity_id. view=issue with issue_id returns one row's
        full description, suggestion and evidence notes.

        Row ids are stable and can be passed back as issue_id. error_count counts
        blocking rows only; advisory rows appear only with include_advisory.
        """
        work = current()
        require(work, "scan", "category", "gap", "global", "review")
        issues, advisory, unresolved, groups, rows = validation(work)
        remembered = seen.get(snapshot) if snapshot is not None else None
        remember(work, issues)
        advisory_rows = ordered_issue_rows(advisory, (), work.records)[1] if advisory else []
        # An advisory row is never in a blocking page, but its detail is still
        # addressable by id once the caller has seen it.
        by_id = {row["id"]: row for row in rows + advisory_rows}
        if include_advisory:
            rows = rows + advisory_rows
        catalog = group_rows(groups)
        counts = {
            "snapshot": work.snapshot,
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "deferred_checks": work.deferred_checks(),
            "groups": catalog[:5],
        }
        if snapshot is not None and snapshot != work.snapshot:
            counts["stale"] = True
            counts["note"] = (
                "The registry changed after that snapshot; rows below are current and "
                "group ids stay stable, so re-read the group you were fixing."
            )
        if remembered is not None:
            current_ids = [row["id"] for row in rows]
            counts["changed_since"] = {
                "snapshot": snapshot,
                "new": [item for item in current_ids if item not in remembered][:20],
                "gone": [item for item in remembered if item not in current_ids][:20],
            }
        if view == "summary":
            head = groups[0] if groups else None
            paged, allowed = page_tokens(dumps, rows, max_tokens, offset, None)
            head_rows = [
                row for row in paged["rows"] if head and row["group_id"] == head["group_id"]
            ]
            unresolved_page = bounded_page(
                unresolved,
                lambda item: dumps(
                    {
                        "id": item["id"],
                        "description": item["description"][:200],
                        "check_refs": item.get("check_refs") or [],
                    }
                ),
                max(MINIMUM_PAGE_TOKENS, allowed // 2),
            )
            counts.update(
                {
                    "rows": paged["rows"],
                    "next_offset": paged["next_offset"],
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
                    "unresolved": [
                        {
                            "id": item["id"],
                            "description": item["description"][:200],
                            "check_refs": item.get("check_refs") or [],
                        }
                        for item in unresolved_page["rows"]
                    ],
                    "unresolved_next_offset": unresolved_page["next_offset"],
                    "page_tokens": paged["tokens"],
                    "page_limit": allowed,
                    "hint": (
                        "view=groups pages the catalog; view=group with group_id pages one "
                        "group; view=issues filters by type or entity; view=issue with "
                        "issue_id returns suggestions and evidence; view=unresolved pages "
                        "open items."
                    ),
                }
            )
            return counts
        if view == "groups":
            paged, allowed = page_tokens(dumps, catalog, max_tokens, offset, None)
            counts.update(
                {
                    "groups": paged["rows"],
                    "next_offset": paged["next_offset"],
                    "page_tokens": paged["tokens"],
                    "page_limit": allowed,
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
                        "note": "this group is clean at the current snapshot",
                    }
                )
                return counts
            paged, allowed = page_tokens(dumps, selected, max_tokens, offset, None)
            counts.update(
                {
                    "group_id": group_id,
                    "issues": paged["rows"],
                    "next_offset": paged["next_offset"],
                    "page_tokens": paged["tokens"],
                    "page_limit": allowed,
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
            paged, allowed = page_tokens(dumps, selected, max_tokens, offset, None)
            counts.update(
                {
                    "issues": paged["rows"],
                    "next_offset": paged["next_offset"],
                    "page_tokens": paged["tokens"],
                    "page_limit": allowed,
                }
            )
            return counts
        if view == "unresolved":
            paged, allowed = page_tokens(
                lambda item: dumps(
                    {
                        "id": item["id"],
                        "description": item["description"],
                        "entity_ids": item.get("entity_ids") or [],
                        "check_refs": item.get("check_refs") or [],
                        "evidence": [
                            {"document_id": cite["document_id"], "block_id": cite["block_id"]}
                            for cite in (item.get("evidence") or [])[:3]
                        ],
                    }
                ),
                unresolved,
                max_tokens,
                offset,
                None,
            )
            counts.update(
                {
                    "unresolved_rows": paged["rows"],
                    "unresolved_next_offset": paged["next_offset"],
                    "page_tokens": paged["tokens"],
                    "page_limit": allowed,
                }
            )
            return counts
        if view == "issue":
            if issue_id is None:
                raise ValueError("view=issue requires issue_id")
            row = by_id.get(issue_id)
            if row is None:
                raise ValueError(f"unknown or resolved issue {issue_id}")
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
                        "evidence": [
                            {
                                "document_id": cite["document_id"],
                                "block_id": cite["block_id"],
                                "path": cite.get("path") or [],
                                "note": cite.get("note") or "",
                            }
                            for cite in (issue.get("evidence") or [])[:6]
                        ],
                    }
                }
            )
            return counts
        raise ValueError("view must be summary, groups, group, issues, issue or unresolved")

    async def complete(summary: str, phases: tuple[str, ...]) -> dict:
        work = current()
        phase = work.assignment.phase
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
            issues = work.issues()
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
                        "evidence."
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

    def rejection(work, issues, start, scope):
        """Compact diagnostics: counts, the group catalog head and one page of rows."""
        _, advisory, unresolved, groups, rows = validation(work)
        paged, allowed = page_tokens(dumps, rows, INDEX_PAGE_TOKENS, 0, None)
        return {
            "accepted": False,
            "error_count": len(issues),
            "repair_start_count": start,
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "groups": group_rows(groups)[:5],
            "errors": paged["rows"],
            "next_offset": paged["next_offset"],
            "page_tokens": paged["tokens"],
            "page_limit": allowed,
            "hint": (
                "Call validate_entities(view='group', group_id=...) for one group's rows, "
                "or view='issue', issue_id=... for suggestions and evidence."
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
        remember(work, issues)
        return {
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "unresolved_count": len(unresolved),
            "group_count": len(groups),
            "groups": group_rows(groups)[:5],
            "deferred_checks": work.deferred_checks(),
        }

    async def complete_batch(summary: str) -> dict:
        """Submit the current scan batch with a short summary after all writes finish.

        The response includes repairable_categories for the current and earlier categories.
        """
        return await complete(summary, ("scan",))

    async def complete_fix(summary: str) -> dict:
        """Submit the current category or global Fix with a short summary after all writes finish.

        A category Fix is accepted when validation is clean or the issue count drops.
        The response reports error_count before and after and the current group catalog
        head. When rejected it lists the first page of rows; fetch suggestions and
        evidence with validate_entities(view="issue", issue_id=...).
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
            cite["document_id"] == doc_id and cite["block_id"] in blocks for cite in row["evidence"]
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
