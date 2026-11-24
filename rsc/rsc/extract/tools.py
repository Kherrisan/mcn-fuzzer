"""MCP surface for one active extraction workspace."""

import asyncio
from collections import Counter
from typing import Annotated

from pydantic import Field

from .guidance import with_suggestions
from .registry import (
    MAX_PAGE,
    ORDER,
    EntityAddition,
    EntityUpdate,
    Evidence,
    Kind,
    Unresolved,
    identity,
    page,
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


def entity_details(work, row):
    key = row["id"]
    incoming = work.index.incoming[key]
    outgoing = work.index.outgoing[key]
    return {
        **row,
        "evidence": [
            {
                **cite,
                "text": work.sources.blocks[cite["document_id"], cite["block_id"]]["text"],
            }
            for cite in row["evidence"]
        ],
        "references": incoming + [edge for edge in outgoing if edge["to"] != key],
    }


def projected(work, row):
    """Full definition with evidence locations and notes, without evidence text."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "aliases": row["aliases"],
        "value": row["value"],
        "evidence": [
            {
                "document_id": cite["document_id"],
                "block_id": cite["block_id"],
                "section_path": cite.get("section_path") or [],
                "field": cite.get("field") or "",
                "note": cite.get("note") or "",
            }
            for cite in row["evidence"]
        ],
    }


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


def make_server(current, *, readonly=False):
    """MCP server for one workspace; readonly exposes the reviewer's seven tools."""
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

    server = FastMCP("rsc-extract")

    @server.tool
    def list_entities(
        kind: Kind | None = None,
        batch_id: str | None = None,
        offset: int = 0,
        limit: int = 12,
    ) -> dict:
        """Page through entities, optionally only those linked to one batch's evidence.

        Without batch_id the rows are name/alias summaries. With batch_id the rows contain
        the full definition and fields plus evidence locators and notes, for the entities
        whose evidence blocks belong to that batch.
        """
        work = current()
        if batch_id is None:
            result = page(search(work.records, [""], kind), offset, limit)
            result["rows"] = [summary(row) for row in result["rows"]]
            return result
        if batch_id not in work.sources.batches:
            raise ValueError(f"unknown batch {batch_id}")
        batch = work.sources.batches[batch_id]
        blocks = {part["block_id"] for part in batch["segments"]}
        rows = [
            row
            for row in work.records.values()
            if kind is None or row["kind"] == kind
            if any(
                cite["document_id"] == batch["doc_id"] and cite["block_id"] in blocks
                for cite in row["evidence"]
            )
        ]
        result = page(rows, offset, limit)
        covered = {row["id"] for row in rows}
        related_steps = step_scenarios(work, rows, covered)
        result["rows"] = [projected(work, row) for row in result["rows"]]
        if related_steps:
            result["related_steps"] = related_steps
        return result

    @server.tool
    def search_entities(
        query: str | None = None,
        kind: Kind | None = None,
        offset: int = 0,
        limit: int = 12,
        queries: list[str] | None = None,
    ) -> dict:
        """Search names and aliases with one query or a list of queries.

        Supply exactly one of query or queries. The result is the paged union of matches,
        with exact matches for any query first. Each row contains the full entity, evidence
        block text with locators, and all references.
        """
        if query is not None and queries is None:
            queries = [query]
        elif query is not None or queries is None:
            raise ValueError("provide either query or queries")
        work = current()
        result = page(search(work.records, queries, kind), offset, limit)
        result["rows"] = [entity_details(work, row) for row in result["rows"]]
        return result

    @server.tool
    def get_entity(id: str | None = None, ids: list[str] | None = None) -> dict:
        """Read one entity by id or several by ids, following redirects.

        Supply exactly one of id or ids. Each entity includes its full record, evidence block
        text with locators, and all references. For ids, the entities and errors maps are
        keyed by the requested IDs.
        """
        if (id is None) == (ids is None):
            raise ValueError("provide either id or ids")
        work = current()

        def read(entity_id):
            return entity_details(work, work.records[work.canonical(entity_id)])

        if id is not None:
            return read(id)
        entities = {}
        errors = {}
        for entity_id in dict.fromkeys(ids or []):
            try:
                entities[entity_id] = read(entity_id)
            except ValueError as exc:
                errors[entity_id] = str(exc)
        return {"entities": entities, "errors": errors}

    @server.tool
    def read_source(
        batch_id: str | None = None,
        document_id: str | None = None,
        section_path: list[str] | None = None,
        block_id: str | None = None,
        offset: int = 0,
        limit_chars: int = 10000,
        original: bool = False,
    ) -> dict:
        """Read a batch, a document section (including subsections), or a document block.

        For RFC section 4.2, section_path may be ["4", "2"] or ["4.2"].
        Offsets page through the returned text. Use document_id and block_id for evidence.
        original=true returns the frozen source span for a single block.
        """
        sources = current().sources
        if offset < 0 or limit_chars < 1:
            raise ValueError("offset must be non-negative and limit_chars must be positive")
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
        end = offset + min(limit_chars, 10000)
        return {
            "text": body[offset:end],
            "original_span": location,
            "next_offset": end if end < len(body) else None,
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

    def validation_report(work, unresolved=None):
        issues = work.issues()
        advisory = work.advisory_issues()
        result = {
            "error_count": len(issues),
            "advisory_count": len(advisory),
            "deferred_checks": work.deferred_checks(),
            "rows": with_suggestions(issues),
        }
        if advisory:
            result["advisory"] = with_suggestions(advisory)
        if unresolved is not None:
            result["unresolved"] = unresolved
        return result

    @server.tool
    def validate_entities() -> dict:
        """Return all structural and coverage issues for ready categories.

        error_count counts blocking issues only; the advisory list holds rows that
        describe missing work without blocking completion. During global Fix, rows also
        include active checks linked from unresolved items.
        The unresolved list contains every still-open item and its check_refs.
        """
        work = current()
        require(work, "scan", "category", "gap", "global", "review")
        unresolved = work.open_unresolved() if work.assignment.phase == "global" else None
        return validation_report(work, unresolved)

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
                scope.update(validation_report(work, unresolved))
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
                    return {
                        "accepted": False,
                        "error_count": len(issues),
                        "repair_start_count": start,
                        "errors": with_suggestions(issues),
                        "message": "Close at least one more issue than this repair introduces.",
                        **scope,
                    }
            if phase == "global":
                # Reject a Fix that changed nothing and reduced nothing, once per
                # assignment: a second no-op submission is accepted so the run cannot
                # stall on the same scope until the request budget is gone.
                if issues and len(issues) >= start and not work.mutations and not work.reject_count:
                    work.reject_count += 1
                    return {
                        "accepted": False,
                        "error_count": len(issues),
                        "repair_start_count": start,
                        "errors": with_suggestions(issues),
                        "message": (
                            "This Fix made no entity change and did not reduce the error "
                            "count. Run validate_entities, make a source-supported change, "
                            "or explain why these errors cannot be fixed with the available "
                            "evidence."
                        ),
                        **scope,
                    }
            work.completion = receipt
            return {"accepted": True, "assignment": work.assignment.id, **scope}

    async def complete_batch(summary: str) -> dict:
        """Submit the current scan batch with a short summary after all writes finish.

        The response includes repairable_categories for the current and earlier categories.
        """
        return await complete(summary, ("scan",))

    async def complete_fix(summary: str) -> dict:
        """Submit the current category or global Fix with a short summary after all writes finish.

        A category Fix is accepted when validation is clean or the issue count drops.
        The response includes repairable_categories. A global Fix response also includes
        validation rows, including active checks linked from unresolved, and the full
        unresolved list. The host runs global validation after acceptance.
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
        "unresolved": work.open_unresolved(),
        "references": references[:20],
    }
