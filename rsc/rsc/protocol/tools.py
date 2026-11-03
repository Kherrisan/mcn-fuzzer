"""Typed RSC retrieval tools and request-scoped source visibility."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from .agent_runtime import EventWriter
from .context import Corpus
from .keywords import KeywordIndex, normalize_keyword
from .merge import canonical, semantic
from .models import BlockRef, NonEmpty, ObjectKind, StrictModel
from .options import AgentOptions
from .paging import paginate
from .projection import object_view
from .registry import FILTERS, Registry, terms
from .retrieval import RetrievalContext

Positive = Annotated[int, Field(strict=True, ge=1)]
Nonnegative = Annotated[int, Field(strict=True, ge=0)]
Context = RetrievalContext
CONTEXT_VERSION = "rsc-agent-context-v10"
TOOL_VERSION = "rsc-retrieval-tools-v9"
SOURCE_TOOLS = {"search_keywords", "search_text", "read_blocks", "get_neighbor_blocks"}


class TextRange(StrictModel):
    """A half-open range in the complete block, measured in Unicode characters."""

    start: Nonnegative = Field(description="Inclusive offset in the full normalized block.")
    end: Positive = Field(description="Exclusive offset in the full normalized block.")


@dataclass
class AgentDeps:
    """Private services and visibility for a single Agent execution."""

    protocol: str
    stage: str
    phase: str
    corpus: Corpus
    registry: Registry
    keyword_index: KeywordIndex
    context: Context
    events: EventWriter
    options: AgentOptions = field(default_factory=AgentOptions)
    delivered_object_ids: set[str] = field(default_factory=set)
    delivered_definition_ids: set[str] = field(default_factory=set)
    queries: list[dict] = field(default_factory=list)
    queried_object_ids: set[str] = field(default_factory=set)
    _logged_returns: set[str] = field(default_factory=set)
    _definition_registry: Registry | None = field(default=None, repr=False)
    _definition_index: dict[str, str] = field(default_factory=dict, repr=False)
    _definition_identity_index: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def payload(self, **values) -> dict:
        """Build only the new turn input; source and definitions arrive through history."""

        return {
            "protocol": self.protocol,
            "stage": self.stage,
            "phase": self.phase,
            "context_version": CONTEXT_VERSION,
            **deepcopy(values),
        }

    def prepare_messages(self, messages, parameters, settings, limits):
        """Preserve historical user messages and actual tool results unchanged."""

        return messages

    def _record_definitions(self, value: Any) -> None:
        if self._definition_registry is not self.registry:
            self._definition_registry = self.registry
            self._definition_index = {}
            self._definition_identity_index = {}
            for identity, current in self.registry.rows.items():
                kind = self.registry.kinds[identity]
                if kind == "steps":
                    token = current.get("id") or current.get("name")
                elif kind == "step_dependencies":
                    token = (
                        f"{current.get('prior_step', '')} -> {current.get('dependent_step', '')}"
                    )
                else:
                    token = current.get("name")
                if token:
                    self._definition_index[canonical(object_view(current))] = token
                    self._definition_identity_index.setdefault(
                        canonical(semantic(object_view(current))), set()
                    ).add(identity)
                    if kind == "steps":
                        public = object_view(current)
                        public.pop("id", None)
                        self._definition_index[canonical(public)] = token
        if isinstance(value, list):
            for item in value:
                self._record_definitions(item)
        elif isinstance(value, dict):
            view = object_view(value)
            # Accepted model-authored interactions omit Python IDs and may omit
            # empty schema defaults; they still contain the complete semantic fact.
            if {
                "name",
                "message_name",
                "channel_name",
                "sender_name",
                "receiver_name",
            } <= view.keys():
                view.setdefault("condition", "")
            elif {"prior_step", "dependent_step"} <= view.keys():
                view.setdefault("condition", "")
                view.setdefault("field_relations", [])
            token = self._definition_index.get(canonical(view))
            if token:
                self.delivered_object_ids.add(token)
            self.delivered_definition_ids.update(
                self._definition_identity_index.get(canonical(semantic(view)), ())
            )
            for key, item in value.items():
                if key not in {"details", "catalog"}:
                    self._record_definitions(item)

    def audit_tool_failures(self, messages, record) -> None:
        """Audit failed tool returns without treating them as delivered material."""

        for message in messages[record.get("turn_message_start", 0) :]:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if (
                    isinstance(part, ToolReturnPart)
                    and part.outcome != "success"
                    and part.tool_name in TOOL_NAMES
                    and part.tool_call_id not in self._logged_returns
                    and part.tool_call_id in self.events.tool_origins.get(record["run_id"], {})
                ):
                    origin = self.events.tool_origins.get(record["run_id"], {}).get(
                        part.tool_call_id, {}
                    )
                    self.events.append(
                        "tool-calls.jsonl",
                        {
                            "run_id": record["run_id"],
                            "request_index": origin.get("request_index", record["request_index"]),
                            "tool_call_id": part.tool_call_id,
                            "tool_name": part.tool_name,
                            "arguments": deepcopy(origin.get("arguments")),
                            "status": "invalid",
                            "result": deepcopy(part.content),
                            "context_revision_before": None,
                            "context_revision_after": None,
                            "omitted": [],
                            "elapsed_ms": None,
                        },
                    )
                    self._logged_returns.add(part.tool_call_id)

    def before_request(self, messages, record) -> None:
        """Recompute full-definition visibility from the actual effective session history."""

        self.audit_tool_failures(messages, record)
        self.delivered_object_ids.clear()
        self.delivered_definition_ids.clear()
        for message_index, message in enumerate(messages):
            for part in message.parts:
                value = None
                if isinstance(message, ModelRequest) and isinstance(part, UserPromptPart):
                    if isinstance(part.content, str):
                        try:
                            value = json.loads(part.content)
                        except ValueError:
                            continue
                elif isinstance(message, ModelRequest) and isinstance(part, ToolReturnPart):
                    if (
                        part.outcome == "success"
                        and part.tool_name == "search_objects"
                        and message_index >= record.get("turn_message_start", 0)
                    ):
                        for query in self.queries:
                            if query.get("tool_call_id") == part.tool_call_id:
                                query["delivered"] = True
                    if part.outcome == "success" and part.tool_name in SOURCE_TOOLS | {
                        "lookup_object"
                    }:
                        value = part.content
                elif (
                    isinstance(message, ModelResponse)
                    and isinstance(part, TextPart)
                    and message_index < record.get("turn_message_start", len(messages))
                ):
                    # Final structured outputs retained from completed turns are readable history.
                    try:
                        value = json.loads(part.content)
                    except ValueError:
                        continue
                if value is not None:
                    self._record_definitions(value)
        self.events.append(
            "retrieval-contexts.jsonl",
            {
                "run_id": record["run_id"],
                "request_index": record["request_index"],
                "stage": self.stage,
                "phase": self.phase,
                "context": self.context.snapshot(),
                "delivered_object_ids": sorted(self.delivered_object_ids),
                "delivered_definition_ids": sorted(self.delivered_definition_ids),
            },
        )

    def record_keyword_retrieval(
        self, run_id, keywords, result, *, request_index=None, tool_call_id=None
    ) -> None:
        """Audit explicit keyword retrieval without constructing automatic source context."""

        self.events.append(
            "keyword-retrieval.jsonl",
            {
                "run_id": run_id,
                "request_index": request_index,
                "tool_call_id": tool_call_id,
                "stage": self.stage,
                "phase": self.phase,
                "focus_block": self.context.focus.ref() if self.context.focus else None,
                "query_keys": sorted({normalize_keyword(value) for value in keywords}),
                "hits": [
                    {
                        key: deepcopy(row[key])
                        for key in ("doc_id", "block_id", "sequence", "matched_keywords")
                        if key in row
                    }
                    for row in result["items"]
                ],
                "next_cursor": result.get("next_cursor"),
            },
        )

    def resolve_page_size(self, value: int | None) -> int:
        """Use min(8, configured maximum) when the model omits the page size."""

        maximum = self.options.max_search_results
        if value is None:
            return min(8, maximum)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"page size must be an integer from 1 to {maximum}")
        return value

    def resolve_source_page_size(self, value: int | None) -> int:
        """Apply both search and per-call source-count bounds to source searches."""

        maximum = min(self.options.max_search_results, self.options.max_tool_blocks)
        if value is None:
            return min(8, maximum)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"source page size must be an integer from 1 to {maximum}")
        return value

    def execute(self, ctx, name: str, arguments: dict, call: Callable) -> dict:
        """Execute a deterministic tool and audit errors without converting them to empty hits."""

        started = time.monotonic()
        revision_before = self.context.revision
        omissions_before = len(self.context.omitted)
        failure = None
        try:
            result = call()
        except KeyError as exc:
            result = {"status": "missing", "error": f"Unknown source identity: {exc}"}
        except ValueError as exc:
            result = {"status": "error", "error": str(exc)}
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            failure = exc
        self.events.append(
            "tool-calls.jsonl",
            {
                "run_id": ctx.run_id,
                "request_index": getattr(ctx.model, "request_index", None),
                "tool_call_id": ctx.tool_call_id,
                "tool_name": name,
                "arguments": deepcopy(arguments),
                "status": result.get("status", "ok"),
                "result": deepcopy(result),
                "context_revision_before": revision_before,
                "context_revision_after": self.context.revision,
                "omitted": deepcopy(self.context.omitted[omissions_before:]),
                "elapsed_ms": (time.monotonic() - started) * 1000,
            },
        )
        self._logged_returns.add(ctx.tool_call_id)
        if failure is not None:
            raise failure
        return result

    def admit_hit(self, hit: dict, role: str) -> dict:
        """Attach an actual bounded source excerpt and permitted extraction history."""

        doc_id, block_id = hit["doc_id"], hit["block_id"]
        result = self.context.add(
            doc_id,
            block_id,
            roles=[role],
            text_end=min(240, len(self.corpus.blocks[doc_id, block_id]["text"])),
        )
        return {
            **hit,
            "source_status": result["status"],
            "artifact": result["artifact"],
            "source_omitted": result["omitted"],
        }


async def search_keywords(
    ctx: RunContext[AgentDeps],
    keywords: Annotated[list[NonEmpty], Field(min_length=1)],
    top_k: Positive | None = None,
    cursor: str | None = None,
) -> dict:
    """Search exact normalized keywords in the visible historical index.

    Args:
        keywords: Nonempty literal keywords. Case and whitespace are normalized.
        top_k: Page size from 1 to the configured search-result limit. Omit or null for the default.
        cursor: Opaque next-page cursor from this same keyword query and visible index.

    Returns:
        Stable locations, matched keywords, actual source excerpts, visible artifacts and a cursor.
    """

    deps = ctx.deps

    def run():
        page_size = deps.resolve_source_page_size(top_k)
        if len(keywords) > deps.options.max_keywords_per_search or any(
            not k.strip() for k in keywords
        ):
            raise ValueError("keyword count exceeds the limit or contains an empty keyword")
        focus = deps.context.focus
        result = deps.keyword_index.search(
            keywords,
            before_sequence=focus.sequence if focus else None,
            list_id=focus.list_id if focus else None,
            top_k=page_size,
            cursor=cursor,
        )
        result["items"] = [deps.admit_hit(row, "keyword") for row in result["items"]]
        deps.record_keyword_retrieval(
            ctx.run_id,
            keywords,
            result,
            request_index=getattr(ctx.model, "request_index", None),
            tool_call_id=ctx.tool_call_id,
        )
        return result

    return deps.execute(
        ctx, "search_keywords", {"keywords": keywords, "top_k": top_k, "cursor": cursor}, run
    )


async def search_text(
    ctx: RunContext[AgentDeps],
    query: NonEmpty,
    top_k: Positive | None = None,
    cursor: str | None = None,
) -> dict:
    """Search literal words and identifiers in selected source documents.

    Args:
        query: Nonempty words or identifiers, never a regular expression.
        top_k: Page size from 1 to the configured search-result limit. Omit or null for the default.
        cursor: Next-page cursor for this exact query and page size.

    Returns:
        Matching block locations in document order, bounded admitted excerpts and pagination.
        An empty page is distinct from rejected reads or an invalid query.
    """

    deps = ctx.deps

    def run():
        page_size = deps.resolve_source_page_size(top_k)
        wanted = terms(query)
        if not wanted:
            raise ValueError("query must contain at least one word or identifier")
        rows = []
        for (doc_id, block_id), block in sorted(
            deps.corpus.blocks.items(), key=lambda pair: (pair[0][0], pair[1]["order"], pair[0][1])
        ):
            if wanted & terms(block["text"]):
                rows.append({"doc_id": doc_id, "block_id": block_id, "order": block["order"]})
        result = paginate(rows, page_size, cursor, ["text", query, rows])
        result["items"] = [deps.admit_hit(row, "retrieved") for row in result["items"]]
        return result

    return deps.execute(ctx, "search_text", {"query": query, "top_k": top_k, "cursor": cursor}, run)


async def read_blocks(
    ctx: RunContext[AgentDeps],
    block_refs: Annotated[list[BlockRef], Field(min_length=1)],
    range: TextRange | None = None,
) -> dict:
    """Read selected blocks or excerpts with permitted committed block artifacts.

    Args:
        block_refs: Document-qualified block identities from this compilation batch.
        range: Optional half-open Unicode offsets in each full block. Each returned excerpt
            contains at most 2,400 characters; use next_start to continue longer blocks.

    Returns:
        Actual source excerpts and permitted block artifacts. Objects from the completed
        object stage are public during interactions; same-stage commits remain list-private.
        Long blocks include text_length and next_start for explicit continuation.
    """

    deps = ctx.deps

    def run():
        if len(block_refs) > deps.options.max_tool_blocks:
            raise ValueError("block_refs exceeds the per-call source-block limit")
        items, omitted, errors = [], [], []
        for ref in dict.fromkeys((ref.doc_id, ref.block_id) for ref in block_refs):
            try:
                result = deps.context.add(
                    *ref,
                    full=range is None,
                    text_start=range.start if range else None,
                    text_end=range.end if range else None,
                )
                if result["artifact"] is not None:
                    items.append(result["artifact"])
                omitted.extend(result["omitted"])
            except (KeyError, ValueError) as exc:
                errors.append(
                    {
                        "doc_id": ref[0],
                        "block_id": ref[1],
                        "error": str(exc),
                        "status": "missing" if isinstance(exc, KeyError) else "error",
                    }
                )
        return {
            "status": "partial"
            if items and (omitted or errors)
            else "ok"
            if items
            else "missing"
            if errors and all(item["status"] == "missing" for item in errors)
            else "error"
            if errors
            else "limited",
            "items": items,
            "omitted": omitted,
            "errors": errors,
        }

    return deps.execute(
        ctx,
        "read_blocks",
        {
            "block_refs": [ref.model_dump() for ref in block_refs],
            "range": range.model_dump() if range else None,
        },
        run,
    )


async def get_parent_section(
    ctx: RunContext[AgentDeps],
    block_ref: BlockRef,
    cursor: str | None = None,
) -> dict:
    """Locate the direct parent section and page its block directory without loading text.

    Args:
        block_ref: A document-qualified block identity.
        cursor: Opaque directory-page cursor from this exact section query.

    Returns:
        Parent section, ancestor headings and one page of the block directory. The page
        size is the configured search-result limit. A preamble returns no_parent. Unknown
        block identities return missing. Continue only with the cursor from this section.
    """

    deps = ctx.deps

    def run():
        section = deps.corpus.get_parent_section(block_ref.doc_id, block_ref.block_id)
        if section is None:
            if cursor is not None:
                raise ValueError("a block without a parent section has no page cursor")
            return {
                "status": "no_parent",
                "section": None,
                "ancestors": [],
                "items": [],
                "next_cursor": None,
            }
        block_ids = deps.corpus.section_blocks[block_ref.doc_id, section["section_id"]]
        rows = [
            {
                "doc_id": block_ref.doc_id,
                "block_id": identity,
                "order": deps.corpus.blocks[block_ref.doc_id, identity]["order"],
                "text_length": len(deps.corpus.blocks[block_ref.doc_id, identity]["text"]),
                "type": deps.corpus.blocks[block_ref.doc_id, identity]["block_type"],
            }
            for identity in block_ids
        ]
        result = paginate(
            rows, deps.options.max_search_results, cursor, ["section", block_ref.model_dump(), rows]
        )
        return {
            **result,
            "section": section,
            "ancestors": deps.corpus.queries[block_ref.doc_id].get_section_ancestors(
                section["section_id"]
            ),
        }

    return deps.execute(
        ctx, "get_parent_section", {"block_ref": block_ref.model_dump(), "cursor": cursor}, run
    )


async def get_neighbor_blocks(
    ctx: RunContext[AgentDeps],
    block_ref: BlockRef,
    before: Nonnegative = 1,
    after: Nonnegative = 1,
    scope: Literal["section", "document"] = "section",
) -> dict:
    """Read bounded excerpts from neighbors without crossing document boundaries.

    Args:
        block_ref: Document-qualified center block; neighbors do not include the center.
        before: Preceding count. before plus after must fit the per-call source-block limit.
        after: Following count. before plus after must fit the per-call source-block limit.
        scope: Stay in the same direct section, or explicitly allow other sections in the document.

    Returns:
        Actual neighbor excerpts and visible artifacts. Blocks over 2,400 characters
        include next_start for continuation with read_blocks.
    """

    deps = ctx.deps

    def run():
        if before + after > deps.options.max_tool_blocks:
            raise ValueError("neighbor count exceeds the per-call source-block limit")
        neighbors = deps.corpus.get_neighbor_blocks(
            block_ref.doc_id,
            block_ref.block_id,
            before=before,
            after=after,
            scope=scope,
        )
        items, omitted = [], []
        for direction, role in (("before", "previous"), ("after", "next")):
            for block in neighbors[direction]:
                result = deps.context.add(block["doc_id"], block["block_id"], roles=[role])
                if result["artifact"] is not None:
                    items.append(result["artifact"])
                omitted.extend(result["omitted"])
        return {
            "status": "partial"
            if items and omitted
            else "ok"
            if items
            else "limited"
            if omitted
            else "empty",
            "items": items,
            "omitted": omitted,
        }

    return deps.execute(
        ctx,
        "get_neighbor_blocks",
        {
            "block_ref": block_ref.model_dump(),
            "before": before,
            "after": after,
            "scope": scope,
        },
        run,
    )


async def search_objects(
    ctx: RunContext[AgentDeps],
    query: str | None = None,
    kind: ObjectKind | None = None,
    filters: dict[str, str | list[str]] | None = None,
    top_k: Positive | None = None,
    cursor: str | None = None,
) -> dict:
    """Search visible protocol definitions by name, field text, or structural filters.

    kind alone is not a query. Supply a nonempty query, or nonempty filters together with
    kind. parties and messages have no structural filters. channels accepts
    endpoint_party_names, initiator_party_name, acceptor_party_name, connection, and
    transport. Example: kind="channels" and filters={"connection": "connectionless"}.
    steps accepts name, message_name, channel_name, sender_name, receiver_name,
    and condition. step_dependencies accepts prior_step, dependent_step,
    and condition. Reuse the original arguments with only the cursor from that query.
    A page does not require you to read every later page.
    Queries match literal terms; they do not expand synonyms or decide semantic identity.
    Filters compare stored strings exactly. Results are candidates, and an empty result
    does not establish that no equivalent concept exists. Use definitions already in
    history or lookup_object when a candidate's identity needs clarification.

    Args:
        query: Nonempty literal words or protocol names. Omit only when filters are nonempty.
        kind: Root collection. Required when filters are supplied, and not sufficient alone.
        filters: Nonempty kind-specific equality or list-membership filters on exact stored strings.
        top_k: Page size from 1 to the configured search-result limit. Omit or null for the default.
        cursor: Opaque cursor returned for this same query, kind, filters, and page size.

    Returns:
        Name summaries, with a step ID only for steps. Several candidates can match one
        name. Summaries are not complete definitions; read the full definition from history
        or lookup_object before editing one.
    """

    deps = ctx.deps

    def run():
        if isinstance(query, str) and not query.strip():
            raise ValueError("query must be a nonempty string")
        if filters is not None and not filters:
            raise ValueError("filters must be nonempty when supplied")
        page_size = deps.resolve_page_size(top_k)
        arguments = {
            "query": query,
            "kind": kind,
            "filters": filters,
            "top_k": page_size,
            "cursor": cursor,
        }
        result = deps.registry.search(**arguments)
        complete_prefix = cursor is None or any(
            previous.get("next_cursor") == cursor
            and previous.get("complete_prefix")
            and previous.get("delivered")
            and all(
                previous.get(key) == arguments[key] for key in ("query", "kind", "filters", "top_k")
            )
            for previous in deps.queries
        )
        deps.queries.append(
            {
                **deepcopy(arguments),
                "tool_call_id": ctx.tool_call_id,
                "complete_prefix": complete_prefix,
                "delivered": False,
                "next_cursor": result.get("next_cursor"),
                "status": result["status"],
            }
        )
        deps.queried_object_ids.update(
            token for row in result["items"] if (token := row.get("id") or row.get("name"))
        )
        return result

    return deps.execute(
        ctx,
        "search_objects",
        {"query": query, "kind": kind, "filters": filters, "top_k": top_k, "cursor": cursor},
        run,
    )


async def lookup_object(
    ctx: RunContext[AgentDeps],
    name: NonEmpty,
    depth: Nonnegative = 1,
    limit: Positive | None = None,
) -> dict:
    """Read one visible definition and a bounded reference closure.

    Use when a complete definition is absent from the effective history or a name is unclear.
    A definition already present in full does not require another lookup.

    Args:
        name: Nonempty protocol name. A step may also be read by its step ID.
        depth: Closure depth from 0 through the configured search-result limit.
        limit: Maximum definitions to return. Omit or null for the default page size.

    Returns:
        The definition when the name selects one object. Several matches return candidate
        names and their definitions, without choosing one. A missing name is not evidence
        that nothing exists.
    """

    deps = ctx.deps

    def run():
        page_size = deps.resolve_page_size(limit)
        if type(depth) is not int or not 0 <= depth <= deps.options.max_search_results:
            raise ValueError(
                f"lookup depth must be an integer from 0 to {deps.options.max_search_results}"
            )
        if not name.strip():
            raise ValueError("name must be a nonempty string")
        return deps.registry.lookup(name, depth=depth, limit=page_size)

    return deps.execute(ctx, "lookup_object", {"name": name, "depth": depth, "limit": limit}, run)


TOOL_NAMES = {
    "search_keywords",
    "search_text",
    "read_blocks",
    "get_parent_section",
    "get_neighbor_blocks",
    "search_objects",
    "lookup_object",
}


def _page_property(maximum: int, default_page: int) -> dict:
    return {
        "anyOf": [
            {"type": "integer", "minimum": 1, "maximum": maximum},
            {"type": "null"},
        ],
        "description": (
            f"Page size from 1 to {maximum}. Omit this argument or pass null to use "
            f"{default_page}. A page is not the full result set; continue only with the "
            "cursor returned for the same query."
        ),
    }


def _filter_lines() -> str:
    lines = ["kind alone is not a query. Supply a nonempty query, or nonempty filters with kind."]
    for kind, fields in FILTERS.items():
        if fields:
            lines.append(f"{kind}: {', '.join(sorted(fields))}.")
        else:
            lines.append(f"{kind}: no structural filters; use query.")
    lines.append('Example: kind="channels", filters={"connection": "connectionless"}.')
    return " ".join(lines)


def prepare_retrieval_tool(ctx: RunContext[AgentDeps], tool_def: ToolDefinition) -> ToolDefinition:
    """Copy the tool definition and apply this run's configured limits."""

    prepared = deepcopy(tool_def)
    deps = getattr(ctx, "deps", None)
    options = getattr(deps, "options", None) if deps is not None else None
    if not isinstance(options, AgentOptions):
        options = AgentOptions()
    maximum = options.max_search_results
    if prepared.name in {"search_text", "search_keywords"}:
        maximum = min(maximum, options.max_tool_blocks)
    default_page = min(8, maximum)
    schema = prepared.parameters_json_schema
    properties = schema.setdefault("properties", {})
    required = [name for name in schema.get("required", []) if name not in {"top_k", "limit"}]
    schema["required"] = required
    if "top_k" in properties:
        properties["top_k"] = _page_property(maximum, default_page)
    if prepared.name == "lookup_object" and "limit" in properties:
        properties["limit"] = _page_property(maximum, default_page)
    if "depth" in properties:
        properties["depth"] = {
            "type": "integer",
            "minimum": 0,
            "maximum": maximum,
            "default": 1,
            "description": f"Closure depth from 0 to {maximum}.",
        }
    if "keywords" in properties:
        properties["keywords"]["minItems"] = 1
        properties["keywords"]["maxItems"] = options.max_keywords_per_search
        properties["keywords"]["description"] = (
            f"From 1 to {options.max_keywords_per_search} nonempty literal keywords."
        )
        items = properties["keywords"].setdefault("items", {"type": "string"})
        items["minLength"] = 1
    if "query" in properties and prepared.name == "search_text":
        properties["query"]["minLength"] = 1
        properties["query"]["description"] = "Nonempty literal words or identifiers."
    if "query" in properties and prepared.name == "search_objects":
        properties["query"] = {
            "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": (
                "Nonempty literal words or protocol names; no synonym expansion. "
                "Omit only when filters are nonempty."
            ),
        }
    if "name" in properties:
        properties["name"]["minLength"] = 1
    if "block_refs" in properties:
        properties["block_refs"]["maxItems"] = options.max_tool_blocks
    for side in ("before", "after"):
        if side in properties:
            properties[side]["maximum"] = options.max_tool_blocks
            properties[side]["description"] = (
                f"Neighbor count from 0 to {options.max_tool_blocks}. "
                "before plus after cannot exceed that limit."
            )
    note = f" Configured search-result limit is {maximum}; the default page size is {default_page}."
    if prepared.name == "search_objects":
        note = " " + _filter_lines() + note
    if prepared.name == "search_keywords":
        note += f" At most {options.max_keywords_per_search} keywords."
    prepared.description = ((prepared.description or "").rstrip() + note).strip()
    return prepared


def make_tools() -> list[Tool]:
    """Build retrieval tools whose visible schema follows the current run configuration."""

    return [
        Tool(
            function,
            sequential=True,
            require_parameter_descriptions=True,
            prepare=prepare_retrieval_tool,
        )
        for function in (
            search_keywords,
            search_text,
            read_blocks,
            get_parent_section,
            get_neighbor_blocks,
            search_objects,
            lookup_object,
        )
    ]
