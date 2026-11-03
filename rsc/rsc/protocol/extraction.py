"""Private sequential block Agents, with asynchronous concurrency between fixed lists."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.models import Model

from rsc.artifacts import write_json, write_jsonl
from rsc.llm.budget import RequestLimits

from . import agent_prompts
from .agent_runtime import EventWriter, RequestCache, RunBudget, encode, run_agent
from .context import Corpus
from .contract import BASE, COLLECTIONS, INTERACTIONS
from .ids import GlobalIdAllocator
from .keywords import KeywordIndex
from .merge import canonical, issue, semantic, sort_rows, unique
from .models import (
    InteractionExtractionResponse,
    ObjectExtractionResponse,
)
from .options import AgentOptions, ContextOptions
from .planning import BlockList, BlockListPlan, FocusBlock
from .proposals import (
    CandidateState,
    ProposalError,
    ProposalValidator,
    commit_proposal,
    empty_model,
)
from .providers import adapt_client
from .retrieval import RetrievalContext
from .sessions import SessionState
from .store import ExtractionStore
from .tools import AgentDeps, make_tools


def summarize_runs(records: list[dict]) -> dict:
    """Count Agent runs separately from actual requests and failed attempts."""

    return {
        "agent_runs": sum(row.get("attempt_count", 1) for row in records),
        "run_failures": sum(
            row.get("failed_attempt_count", int(row["status"] != "ok")) for row in records
        ),
        "block_retries": sum(row.get("retry_count", 0) for row in records),
        "max_request_bytes": max((row.get("max_request_bytes", 0) for row in records), default=0),
        "tool_calls": sum(row.get("tool_call_count", 0) for row in records),
        "output_retries": sum(row.get("output_retry_count", 0) for row in records),
        **{
            key: sum(row.get(key, 0) for row in records)
            for key in (
                "calls",
                "failures",
                "skipped",
                "cache_hits",
                "request_count",
                "tool_call_count",
                "output_retry_count",
                "tool_failures",
                "context_snapshots",
                "context_omissions",
            )
        },
        "usage": {
            key: sum(row.get("usage", {}).get(key, 0) for row in records)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        },
    }


@dataclass
class ExtractionRuntime:
    """Read-only services and the one shared allocation service used by list writers."""

    protocol: str
    corpus: Corpus
    plan: BlockListPlan
    model_factory: Callable[[str, str], Model]
    allocator: GlobalIdAllocator
    events: EventWriter
    limits: dict[str, RequestLimits]
    out: Path | None = None
    cache: RequestCache | None = None
    agent_options: AgentOptions = field(default_factory=AgentOptions)
    context_options: ContextOptions = field(default_factory=ContextOptions)
    sessions: dict[str, SessionState] = field(default_factory=dict)
    _coordinator: SessionState | None = field(default=None, init=False, repr=False)

    def session_for(self, list_id: str) -> SessionState:
        """Return the single-writer session retained across both extraction phases."""

        if list_id not in self.sessions:
            self.sessions[list_id] = SessionState(
                list_id, context_options=self.context_options, events=self.events
            )
        return self.sessions[list_id]

    @property
    def coordinator(self) -> SessionState:
        if self._coordinator is None:
            self._coordinator = SessionState(
                "coordinator", context_options=self.context_options, events=self.events
            )
        return self._coordinator

    async def run(
        self,
        stage,
        phase,
        output_type,
        payload,
        *,
        run_id,
        deps=None,
        output_check=None,
        instructions,
        budget=None,
        limits_override=None,
        session=None,
        new_input_max_bytes=None,
    ) -> dict:
        """Run a tool-capable stage model without silently adapting text completions."""

        if new_input_max_bytes is not None and len(encode(payload).encode()) > new_input_max_bytes:
            return {
                "run_id": run_id,
                "stage": stage,
                "phase": phase,
                "status": "failed",
                "end_reason": "request_budget",
                "error": "New group payload exceeds its byte limit",
                "data": None,
                "calls": 0,
                "failures": 0,
                "skipped": 1,
                "cache_hits": 0,
                "request_count": 0,
                "tool_call_count": 0,
                "output_retry_count": 0,
                "usage": {},
                "elapsed_seconds": 0,
            }
        try:
            model = adapt_client(self.model_factory(stage, phase))
            if model.profile.get("supports_tools") is False:
                raise ValueError(
                    "the selected model profile does not support structured tool calls"
                )
        except Exception as exc:
            return {
                "run_id": run_id,
                "stage": stage,
                "phase": phase,
                "status": "failed",
                "end_reason": "factory",
                "error": f"{type(exc).__name__}: {exc}",
                "data": None,
                "calls": 0,
                "failures": 0,
                "skipped": 1,
                "cache_hits": 0,
                "request_count": 0,
                "tool_call_count": 0,
                "output_retry_count": 0,
                "usage": {},
                "elapsed_seconds": 0,
            }
        return await run_agent(
            model,
            output_type,
            payload,
            stage=stage,
            phase=phase,
            instructions=instructions,
            events=self.events,
            limits=limits_override or self.limits.get(f"{phase}_{stage}") or self.limits[stage],
            options=self.agent_options,
            budget=budget,
            cache=self.cache,
            deps=deps,
            tools=make_tools() if deps is not None else [],
            output_check=output_check,
            prepare_messages=deps.prepare_messages if deps is not None else None,
            before_request=deps.before_request if deps is not None else None,
            after_run=deps.audit_tool_failures if deps is not None else None,
            run_id=run_id,
            session=session,
        )

    def save_list(
        self, list_id: str, phase: str, state: CandidateState, keywords: KeywordIndex
    ) -> None:
        if self.out is None:
            return
        root = self.out / "lists" / list_id
        root.mkdir(parents=True, exist_ok=True)
        write_json(root / f"{phase}-model.json", state.model)
        write_json(root / f"{phase}-issues.json", state.issues)
        write_json(root / "keyword-index.json", keywords.snapshot())


@dataclass
class ListResult:
    list_id: str
    state: CandidateState
    keywords: KeywordIndex
    store: ExtractionStore
    records: list[dict] = field(default_factory=list)
    session: SessionState | None = None


@dataclass
class ExtractionResult:
    state: CandidateState
    keywords: KeywordIndex
    store: ExtractionStore
    lists: list[ListResult]
    report: dict


def failure_issue(record: dict, focus: FocusBlock) -> dict:
    del focus
    return issue(record["error"] or "Agent extraction did not complete", [])


def tool_limit_exhausted(record: dict) -> bool:
    """Retry only the explicit per-run tool budget, not unrelated run limits."""

    return record.get("end_reason") == "run_limit" and "Agent tool call limit reached" in str(
        record.get("error") or ""
    )


def combine_block_attempts(attempts: list[dict]) -> dict:
    """Keep the final outcome while reporting every fresh-budget Agent run."""

    record = deepcopy(attempts[-1])
    additive = (
        "calls",
        "failures",
        "skipped",
        "cache_hits",
        "request_count",
        "tool_call_count",
        "output_retry_count",
        "tool_failures",
        "context_snapshots",
        "context_omissions",
        "elapsed_seconds",
    )
    for key in additive:
        record[key] = sum(row.get(key, 0) for row in attempts)
    record["usage"] = {
        key: sum(row.get("usage", {}).get(key, 0) for row in attempts)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }
    record["max_request_bytes"] = max(
        (row.get("max_request_bytes", 0) for row in attempts), default=0
    )
    record["attempt"] = len(attempts)
    record["attempt_count"] = len(attempts)
    record["retry_count"] = len(attempts) - 1
    record["failed_attempt_count"] = sum(row.get("status") != "ok" for row in attempts)
    record["attempts"] = [
        {
            key: deepcopy(row.get(key))
            for key in (
                "run_id",
                "status",
                "end_reason",
                "error",
                "request_count",
                "tool_call_count",
                "output_retry_count",
                "calls",
                "failures",
                "skipped",
                "cache_hits",
                "max_request_bytes",
                "usage",
                "elapsed_seconds",
            )
        }
        | {"attempt": index}
        for index, row in enumerate(attempts, 1)
    ]
    return record


async def extract_block(
    runtime: ExtractionRuntime,
    focus: FocusBlock,
    result: ListResult,
    phase: str,
) -> None:
    """Measure the complete block, including context, commit, and artifact persistence."""

    with runtime.events.span(
        "block",
        stage=phase,
        phase=phase,
        list_id=focus.list_id,
        sequence=focus.sequence,
        **focus.ref(),
    ) as timing:
        record = await _extract_block(runtime, focus, result, phase)
        timing.update(
            status=record["status"],
            block_status=record["block_status"],
            request_count=record["request_count"],
            cache_hits=record["cache_hits"],
            retry_count=record["retry_count"],
            tool_call_count=record["tool_call_count"],
            **record["timing"],
        )


async def _extract_block(
    runtime: ExtractionRuntime,
    focus: FocusBlock,
    result: ListResult,
    phase: str,
) -> dict:
    """Append the focus block to its retained session, then publish accepted facts."""

    prepare_started = time.perf_counter()
    state = result.state
    starting_roots = {
        (kind, row.get("id") or canonical(semantic(row))): row
        for kind in COLLECTIONS
        for row in state.model[kind]
    }
    registry = state.registry(runtime.corpus.documents)
    session = result.session or runtime.session_for(focus.list_id)
    result.session = session
    session.update_model(state.model, state.issues, state.revision)
    context = RetrievalContext(
        runtime.corpus,
        focus=focus,
        phase=phase,
        store=result.store,
        agent_options=runtime.agent_options,
        lookup=registry.root,
    )
    deps = AgentDeps(
        runtime.protocol,
        phase,
        phase,
        runtime.corpus,
        registry,
        result.keywords,
        context,
        runtime.events,
        options=runtime.agent_options,
    )
    validator = ProposalValidator(state, deps)
    output_type = ObjectExtractionResponse if phase == "objects" else InteractionExtractionResponse
    base_run_id = f"{phase}:{focus.doc_id}:{focus.block_id}"
    prepare_seconds = time.perf_counter() - prepare_started
    context_seconds = 0.0
    attempts = []
    for attempt in range(1, runtime.agent_options.max_block_retries + 2):
        retry_feedback = (
            []
            if attempt == 1
            else [
                {
                    "attempt": attempt,
                    "reason": "The previous Agent run exhausted its tool-call budget.",
                    "instruction": (
                        "Reuse material already present in this session, avoid repeating "
                        "completed searches, and return the extraction once sufficient."
                    ),
                }
            ]
        )
        run_id = base_run_id if attempt == 1 else f"{base_run_id}:retry-{attempt - 1}"
        context_started = time.perf_counter()
        payload_values = {
            "focus_block": {
                **focus.ref(),
                "block_type": runtime.corpus.blocks[focus.key]["block_type"],
                "text": runtime.corpus.blocks[focus.key]["text"],
            }
        }
        if retry_feedback:
            payload_values["retry_feedback"] = retry_feedback
        payload = deps.payload(**payload_values)
        context_seconds += time.perf_counter() - context_started
        current = await runtime.run(
            phase,
            phase,
            output_type,
            payload,
            run_id=run_id,
            deps=deps,
            output_check=validator.output_errors,
            instructions=(
                agent_prompts.OBJECTS if phase == "objects" else agent_prompts.INTERACTIONS
            ),
            budget=RunBudget(runtime.agent_options),
            session=session,
        )
        attempts.append(current)
        if current["status"] == "ok" or not tool_limit_exhausted(current):
            break
    record = combine_block_attempts(attempts)
    commit_started = time.perf_counter()
    committed = {"items": empty_model(), "reused_names": [], "unresolved": []}
    recovery = validator.recover() if record["status"] != "ok" else None
    data = record["data"] if record["status"] == "ok" else recovery["data"] if recovery else None
    commit_succeeded = False
    if data is not None:
        try:
            committed = await commit_proposal(
                validator,
                data,
                runtime.allocator,
                origin=f"{focus.doc_id}:{focus.block_id}",
                list_id=focus.list_id,
                sequence=focus.sequence,
            )
        except ProposalError as exc:
            record.update(status="failed", end_reason="commit", error=str(exc))
        else:
            commit_succeeded = True
    if record["status"] != "ok":
        problem = failure_issue(record, focus)
        if recovery is not None:
            record["recovery"] = recovery
        state.issues.append(problem)
        committed["unresolved"].append(problem)
    status = (
        "partial"
        if record["status"] != "ok"
        and (any(committed["items"].values()) or committed["reused_names"])
        else "failed"
        if record["status"] != "ok"
        else "partial"
        if committed["unresolved"]
        else "ok"
        if any(committed["items"].values()) or committed["reused_names"]
        else "empty"
    )
    commit_seconds = time.perf_counter() - commit_started
    persist_started = time.perf_counter()
    artifact = {
        "record_id": f"{phase}:{focus.doc_id}:{focus.block_id}",
        "run_id": record["run_id"],
        "stage": phase,
        **focus.ref(),
        "sequence": focus.sequence,
        "list_id": focus.list_id,
        "status": status,
        "items": committed["items"],
        "reused_names": committed["reused_names"],
        "unresolved": committed["unresolved"],
        "error": record["error"],
    }
    keyword = result.keywords.commit(
        focus,
        {"keywords": (data or {}).get("keywords", [])} if data is not None else None,
        error=record["error"] or "Block extraction failed" if data is None else None,
        phase=phase,
    )
    artifact["keywords"] = keyword["keywords"]
    runtime.events.append("keywords.jsonl", keyword)
    session.update_model(state.model, state.issues, state.revision)
    session.record_commit(
        record["run_id"],
        committed["items"],
        committed["unresolved"],
        accepted=commit_succeeded,
    )
    result.store.commit(artifact)
    runtime.events.append("block-extractions.jsonl", artifact)
    record.update(
        **focus.ref(),
        sequence=focus.sequence,
        list_id=focus.list_id,
        block_status=status,
        timing={
            "prepare_seconds": prepare_seconds,
            "context_seconds": context_seconds,
            "agent_seconds": record["elapsed_seconds"],
            "commit_seconds": commit_seconds,
        },
        commit={
            "id_map": deepcopy(committed.get("id_map", {})),
            "model_revision": state.revision,
            "new_roots": {
                kind: sum(
                    (kind, row.get("id") or canonical(semantic(row))) not in starting_roots
                    for row in committed["items"][kind]
                )
                for kind in COLLECTIONS
            },
            "reused_roots": len(committed["reused_names"]),
            "supplemented_roots": sum(
                (kind, row.get("id") or canonical(semantic(row))) in starting_roots
                and row != starting_roots[(kind, row.get("id") or canonical(semantic(row)))]
                for kind, rows in committed["items"].items()
                for row in rows
            ),
        },
    )
    result.records.append(record)
    runtime.events.append(f"{phase}.jsonl", record)
    runtime.save_list(focus.list_id, phase, state, result.keywords)
    record["timing"]["persist_seconds"] = time.perf_counter() - persist_started
    return record


async def extract_object_list(runtime: ExtractionRuntime, block_list: BlockList) -> ListResult:
    result = ListResult(
        block_list.list_id,
        CandidateState(),
        KeywordIndex(runtime.protocol, runtime.corpus, list_id=block_list.list_id),
        ExtractionStore(),
        session=runtime.session_for(block_list.list_id),
    )
    for focus in block_list.blocks:
        with runtime.events.span(
            "focus",
            stage="objects",
            phase="objects",
            list_id=focus.list_id,
            sequence=focus.sequence,
            **focus.ref(),
        ) as timing:
            await extract_block(runtime, focus, result, "objects")
            timing.update(
                status=result.records[-1]["status"],
                block_status=result.records[-1]["block_status"],
            )
    return result


async def extract_interaction_list(
    runtime: ExtractionRuntime,
    block_list: BlockList,
    base: CandidateState,
    keywords: KeywordIndex,
    store: ExtractionStore,
) -> ListResult:
    result = ListResult(
        block_list.list_id,
        CandidateState(
            model=deepcopy(base.model),
            redirects=deepcopy(base.redirects),
            origins=deepcopy(base.origins),
            revision=base.revision,
            history=deepcopy(base.history),
        ),
        keywords.fork(list_id=block_list.list_id),
        store.fork(),
        session=runtime.session_for(block_list.list_id),
    )
    for focus in block_list.blocks:
        await extract_block(runtime, focus, result, "interactions")
    return result


async def parallel_lists(block_lists, workers: int, call) -> list[ListResult]:
    if type(workers) is not int or workers < 1:
        raise ValueError("extraction workers must be a positive integer")
    semaphore = asyncio.Semaphore(workers)

    async def guarded(block_list):
        async with semaphore:
            return await call(block_list)

    # TaskGroup cancels siblings on a fatal consistency error, before publication.
    async with asyncio.TaskGroup() as group:
        pending = [group.create_task(guarded(block_list)) for block_list in block_lists]
    return [task.result() for task in pending]


def gather_extraction(
    runtime: ExtractionRuntime,
    lists: list[ListResult],
    phase: str,
    base: CandidateState | None = None,
) -> ExtractionResult:
    state = deepcopy(base) if base is not None else CandidateState()
    kinds = BASE if phase == "objects" else INTERACTIONS
    for result in lists:
        for kind in kinds:
            state.model[kind].extend(deepcopy(result.state.model[kind]))
        state.issues.extend(deepcopy(result.state.issues))
        state.origins.update(deepcopy(result.state.origins))
    for kind in COLLECTIONS:
        state.model[kind] = sort_rows(kind, state.model[kind])
    state.issues = unique(state.issues)
    state.revision = max([state.revision, *(result.state.revision for result in lists)]) + 1
    store = ExtractionStore()
    for result in lists:
        for record in result.store.snapshot():
            store.commit(record)
    keywords = KeywordIndex.merge([result.keywords for result in lists])
    records = sorted(
        [record for result in lists for record in result.records],
        key=lambda row: (row["sequence"], row["list_id"]),
    )
    if runtime.out is not None:
        write_jsonl(runtime.out / f"{phase}.jsonl", records)
        write_jsonl(runtime.out / "keywords.jsonl", keywords.snapshot()["records"])
        write_json(runtime.out / "keyword-index.json", keywords.snapshot())
    return ExtractionResult(
        state,
        keywords,
        store,
        lists,
        {
            **summarize_runs(records),
            "new_roots": {
                kind: sum(row["commit"]["new_roots"][kind] for row in records)
                for kind in COLLECTIONS
            },
            "reused_roots": sum(row["commit"]["reused_roots"] for row in records),
            "supplemented_roots": sum(row["commit"]["supplemented_roots"] for row in records),
            "unresolved_identity_issues": sum(
                bool(row.get("object_names")) for row in state.issues
            ),
        },
    )


async def extract_objects(runtime: ExtractionRuntime, workers: int) -> ExtractionResult:
    """Extract private object lists, then publish a frozen keyword index at the barrier."""

    lists = await parallel_lists(
        runtime.plan.lists, workers, lambda group: extract_object_list(runtime, group)
    )
    return gather_extraction(runtime, lists, "objects")


async def extract_interactions(
    runtime: ExtractionRuntime,
    workers: int,
    *,
    base: CandidateState,
    keywords: KeywordIndex,
    store: ExtractionStore,
) -> ExtractionResult:
    """Continue list sessions with public object keywords and private interaction additions."""

    lists = await parallel_lists(
        runtime.plan.lists,
        workers,
        lambda group: extract_interaction_list(runtime, group, base, keywords, store),
    )
    return gather_extraction(runtime, lists, "interactions", base)
