"""Compile selected RFC batches with private extraction lists, link and Issue Agents."""

from __future__ import annotations

import asyncio
import platform
import shutil
import time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from rsc.artifacts import write_json, write_jsonl
from rsc.config import load_compile_options, load_llm_config
from rsc.schemas import resolve_schemas_dir
from rsc.version import __version__

from .agent_prompts import PROMPT_VERSION
from .agent_runtime import CACHE_VERSION, EventWriter, RequestCache
from .completion import complete_interactions, complete_objects
from .context import Corpus, document_digest, load_batch
from .contract import COLLECTIONS
from .extraction import ExtractionRuntime, extract_interactions, extract_objects
from .ids import GlobalIdAllocator
from .linking import link_interactions, link_objects
from .merge import issue, unique
from .models import PROTOCOL_VERSION
from .options import CompileOptions, compile_options
from .planning import plan_block_lists
from .proposals import current_issues, empty_model
from .providers import ModelFactory, adapt_client
from .sessions import resolve_context_capacity
from .tools import CONTEXT_VERSION, TOOL_VERSION
from .validate import prune_invalid, validate_protocol

JSONL_OUTPUTS = (
    "timings.jsonl",
    "retrieval-contexts.jsonl",
    "session-messages.jsonl",
    "context-compactions.jsonl",
    "model-requests.jsonl",
    "model-responses.jsonl",
    "provider-requests.jsonl",
    "user-messages.jsonl",
    "tool-calls.jsonl",
    "keywords.jsonl",
    "keyword-retrieval.jsonl",
    "block-extractions.jsonl",
    "objects.jsonl",
    "interactions.jsonl",
    "link.jsonl",
    "repair.jsonl",
)
JSON_OUTPUTS = (
    "timing-summary.json",
    "block-lists.json",
    "keyword-index.json",
    "unresolved.json",
    "compile-report.json",
    "protocol.json",
)
DIRECTORY_OUTPUTS = (
    "sources",
    "lists",
    "objects-link",
    "interactions-link",
    "objects-repair",
    "interactions-repair",
)
# These were managed compiler artifacts before persistent sessions replaced windows.
LEGACY_OUTPUTS = ("windows.jsonl", "link-contexts.jsonl", "repair-contexts.jsonl")


class CompileError(ValueError):
    """Invalid input, configuration or output destination before model requests."""

    exit_code = 2


def resolve_options(config=None, **overrides) -> CompileOptions:
    """Apply explicit arguments over configuration, preserving phase-specific workers."""

    values = asdict(load_compile_options(config) if config is not None else CompileOptions())
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "request_limits":
            values[key] = value
        elif key in ("context", "agent", "link", "repair"):
            item = value if isinstance(value, dict) else asdict(value)
            values[key] = {**values[key], **item}
        else:
            values[key] = value
    return compile_options(values).resolved()


def reset_outputs(out: Path) -> None:
    """Replace only named compiler artifacts; preserve unrelated top-level files."""

    for name in (*JSONL_OUTPUTS, *JSON_OUTPUTS, *DIRECTORY_OUTPUTS, *LEGACY_OUTPUTS):
        path = out / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            if name not in DIRECTORY_OUTPUTS:
                raise CompileError(f"compiler output is unexpectedly a directory: {path}")
            shutil.rmtree(path)
    for name in JSONL_OUTPUTS:
        write_jsonl(out / name, [])


def save_sources(out: Path, corpus: Corpus) -> list[dict]:
    (out / "sources").mkdir(exist_ok=True)
    sources = []
    for doc_id, document in sorted(corpus.documents.items()):
        digest = document_digest(document)
        relative = f"sources/{digest}.json"
        write_json(out / relative, document)
        write_json(out / "sources" / f"{digest}.index.json", corpus.indexes[doc_id])
        sources.append(
            {
                "doc_id": doc_id,
                "rfc": document["meta"]["rfc"],
                "input_sha256": document["meta"]["input_sha256"],
                "document_sha256": digest,
                "path": relative,
            }
        )
    return sources


def list_statuses(result, phase):
    output = []
    for group in result.lists:
        failed = sum(row["status"] != "ok" for row in group.records)
        partial = failed or any(row["block_status"] == "partial" for row in group.records)
        output.append(
            {
                "stage": phase,
                "list_id": group.list_id,
                "status": "failed"
                if failed == len(group.records)
                else "partial"
                if partial
                else "ok",
                "processed_blocks": len(group.records),
                "failed_blocks": failed,
            }
        )
    return output


def final_issues(model, persistent, rejected, documents):
    """Keep semantic reports. Name misses are not removed or rewritten."""

    items = current_issues(model, persistent + rejected, documents)
    if not any(model[key] for key in COLLECTIONS):
        items.append(issue("No extracted objects survived validation"))
    return unique(items)


def compile_protocol(input_dir, protocol, out_dir, **options) -> dict:
    """Compile synchronously; use compile_protocol_async inside an existing event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(compile_protocol_async(input_dir, protocol, out_dir, **options))
    raise CompileError("use await compile_protocol_async() inside an active event loop")


async def compile_protocol_async(
    input_dir,
    protocol,
    out_dir,
    *,
    model_factory=None,
    client_factory=None,
    config=None,
    workers=None,
    objects_workers=None,
    interactions_workers=None,
    block_lists=None,
    force=False,
    schemas_dir=None,
    context_options=None,
    agent_options=None,
    link_options=None,
    repair_options=None,
    request_limits=None,
    cache=None,
    **unsupported_options,
) -> dict:
    """Compile all parsed documents; return exit_code 0 for complete, or 1 for partial/failed.

    model_factory(stage, phase) must supply a PydanticAI Model. The compatibility
    client_factory(stage) may return a Model or a full as_pydantic_model adapter.
    Input, overwrite and capability errors raise CompileError before any request.
    Injected models remain caller-owned. Only the default factory is closed here.
    """

    compile_started = time.perf_counter()
    root = Path(input_dir).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    owned_factory = None
    try:
        if unsupported_options:
            old = {"window_options", "context_blocks_before", "context_blocks_after",
                   "context_keyword_blocks"}
            if old.intersection(unsupported_options):
                raise ValueError("window options were removed; use context_options instead")
            raise ValueError(f"unknown compile options: {sorted(unsupported_options)}")
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        if not isinstance(protocol, str) or not protocol.strip():
            raise ValueError("protocol must be a nonempty string")
        if model_factory is not None and client_factory is not None:
            raise ValueError("choose model_factory or client_factory, not both")
        options = resolve_options(
            config,
            workers=workers,
            objects_workers=objects_workers,
            interactions_workers=interactions_workers,
            block_lists=block_lists,
            context=context_options,
            agent=agent_options,
            link=link_options,
            repair=repair_options,
            request_limits=request_limits,
        )
        if cache is not None and not isinstance(cache, RequestCache):
            raise ValueError("cache must be a RequestCache")
        if out == root or root.is_relative_to(out) or out.is_relative_to(root):
            raise ValueError("input and output directories must not overlap")
        if out.exists() and not out.is_dir():
            raise ValueError("output must be a directory")
        if out.exists() and any(out.iterdir()) and not force:
            raise ValueError(f"{out} exists and is not empty; use --force to overwrite")
        for name in (*JSONL_OUTPUTS, *JSON_OUTPUTS):
            path = out / name
            if path.is_dir() and not path.is_symlink():
                raise ValueError(f"compiler output is unexpectedly a directory: {path}")
        schemas = resolve_schemas_dir(schemas_dir)
        documents = load_batch(root, schemas)
        corpus = Corpus(documents)
        plan = plan_block_lists(corpus, options.block_lists)
    except (ValueError, TypeError, OSError, KeyError) as exc:
        raise CompileError(f"invalid compile input/options: {exc}") from exc

    # All active model profiles are checked after input preflight and before requests.
    try:
        if model_factory is None and client_factory is None:
            if config is None:
                raise ValueError("provide config or a tool-capable model_factory")
            llm_config = load_llm_config(config)
            owned_factory = ModelFactory(llm_config)
            model_factory = owned_factory
            if cache is None and llm_config.cache_dir is not None:
                cache = RequestCache(llm_config.cache_dir / "agent-v6")
        elif client_factory is not None:
            legacy_models = {}

            def model_factory(stage, phase):
                if stage not in legacy_models:
                    legacy_models[stage] = adapt_client(client_factory(stage))
                return legacy_models[stage]

        keys = [
            ("objects", "objects"),
            ("link_partition", "objects"),
            ("link_merge", "objects"),
            ("interactions", "interactions"),
            ("link_partition", "interactions"),
            ("link_merge", "interactions"),
        ]
        if options.repair.enabled:
            keys.extend([("repair", "objects"), ("repair", "interactions")])
        models = {}
        capacities = {}
        for key in keys:
            model = adapt_client(model_factory(*key))
            if model.profile.get("supports_tools") is False:
                raise ValueError(f"{key}: model does not support structured tool calls")
            stage, phase = key
            limits = options.request_limits.get(f"{phase}_{stage}") or options.request_limits[stage]
            capacities[key] = resolve_context_capacity(model, limits, options.context)
            models[key] = model
    except Exception as exc:
        if owned_factory is not None:
            await owned_factory.aclose()
        raise CompileError(f"invalid Agent model configuration: {exc}") from exc

    report = {
        "protocol": protocol,
        "documents": len(documents),
        "processed_blocks": 0,
        "path": str(out),
        "stages": {},
        "list_statuses": [],
        "workers": options.workers,
        "objects_workers": options.objects_workers,
        "interactions_workers": options.interactions_workers,
        "block_lists": len(plan.lists),
        "context_options": asdict(options.context),
        "agent_options": asdict(options.agent),
        "link_options": asdict(options.link),
        "repair_options": asdict(options.repair),
        "request_limits": {key: asdict(value) for key, value in options.request_limits.items()},
        "versions": {
            "rsc": __version__,
            "python": platform.python_version(),
            "protocol_schema": PROTOCOL_VERSION,
            "prompt_contract": PROMPT_VERSION,
            "tool_contract": TOOL_VERSION,
            "context_contract": CONTEXT_VERSION,
            "cache_format": CACHE_VERSION,
            "packages": {
                package: version(package)
                for package in (
                    "pydantic", "pydantic-ai-slim", "openai", "anthropic", "httpx2", "logfire"
                )
            },
        },
        "models": [
            {"stage": stage, "phase": phase, "system": model.system, "model": model.model_name,
             "context_window_tokens": capacities[stage, phase]}
            for (stage, phase), model in models.items()
        ],
    }
    preflight_seconds = time.perf_counter() - compile_started
    report["preflight_seconds"] = preflight_seconds
    events = EventWriter(out)
    sources, issues, candidate = [], [], empty_model()
    fatal = None
    try:
        out.mkdir(parents=True, exist_ok=True)
        reset_outputs(out)
        with events.span("stage.sources", stage="prepare"):
            sources = save_sources(out, corpus)
            write_json(out / "block-lists.json", plan.snapshot())
        runtime = ExtractionRuntime(
            protocol,
            corpus,
            plan,
            lambda stage, phase: models[stage, phase],
            GlobalIdAllocator(protocol),
            events,
            options.request_limits,
            out=out,
            cache=cache,
            agent_options=options.agent,
            context_options=options.context,
        )
        with events.span("stage.objects", stage="objects", workers=options.objects_workers):
            objects = await extract_objects(runtime, options.objects_workers)
        report["stages"]["objects"] = objects.report
        report["list_statuses"].extend(list_statuses(objects, "objects"))
        report["processed_blocks"] += sum(len(group.records) for group in objects.lists)
        runtime.coordinator.handoff(
            "objects_link", objects.state.model, objects.state.issues, objects.state.revision
        )
        with events.span("stage.objects_link", stage="link", phase="objects"):
            state, report["stages"]["objects_link"] = await link_objects(
                runtime,
                objects.state,
                keywords=objects.keywords,
                store=objects.store,
                options=options.link,
                workers=options.workers,
            )
        runtime.coordinator.handoff("objects_complete", state.model, state.issues, state.revision)
        with events.span("stage.objects_complete", stage="repair", phase="objects"):
            state, report["stages"]["objects_repair"], _ = await complete_objects(
                runtime,
                state,
                keywords=objects.keywords,
                store=objects.store,
                options=options.repair,
            )
        for group in objects.lists:
            group.session.handoff("interactions", state.model, state.issues, state.revision)
        with events.span(
            "stage.interactions", stage="interactions", workers=options.interactions_workers
        ):
            relations = await extract_interactions(
                runtime,
                options.interactions_workers,
                base=state,
                keywords=objects.keywords,
                store=objects.store,
            )
        report["stages"]["interactions"] = relations.report
        report["list_statuses"].extend(list_statuses(relations, "interactions"))
        report["processed_blocks"] += sum(len(group.records) for group in relations.lists)
        runtime.coordinator.handoff(
            "interactions_link", relations.state.model, relations.state.issues,
            relations.state.revision,
        )
        with events.span("stage.interactions_link", stage="link", phase="interactions"):
            state, report["stages"]["interactions_link"] = await link_interactions(
                runtime,
                relations.state,
                keywords=relations.keywords,
                store=relations.store,
                options=options.link,
                workers=options.workers,
            )
        runtime.coordinator.handoff(
            "interactions_complete", state.model, state.issues, state.revision
        )
        with events.span("stage.interactions_complete", stage="repair", phase="interactions"):
            state, report["stages"]["interactions_repair"], _ = await complete_interactions(
                runtime,
                state,
                keywords=relations.keywords,
                store=relations.store,
                options=options.repair,
            )
        with events.span("stage.finalize", stage="finalize"):
            candidate, rejected = prune_invalid(state.model, documents, schemas)
            issues = final_issues(candidate, state.issues, rejected, documents)
    except Exception as exc:
        # ID consistency errors (including TaskGroup ExceptionGroups) prevent all publication.
        def describe(error):
            if isinstance(error, BaseExceptionGroup):
                return "; ".join(describe(nested) for nested in error.exceptions)
            return f"{type(error).__name__}: {error}"

        fatal = describe(exc)
        issues = [issue("Compiler could not safely finish")]
        candidate = empty_model()
    finally:
        try:
            if owned_factory is not None:
                await owned_factory.aclose()
        finally:
            if out.is_dir():
                write_json(
                    out / "timing-summary.json",
                    {"preflight_seconds": preflight_seconds, **events.timing_summary()},
                )
    complete = not issues and fatal is None
    model = {
        "meta": {
            "schema_version": PROTOCOL_VERSION,
            "protocol": protocol,
            "complete": complete,
            "documents": sources,
        },
        **candidate,
        "unresolved": issues,
    }
    report.update(
        complete=complete,
        exit_code=0 if complete else 1,
        model_written=False,
        counts={kind: len(candidate[kind]) for kind in COLLECTIONS},
        unresolved=len(issues),
        context_snapshots=events.counts.get("retrieval-contexts.jsonl", 0),
    )
    for key in (
        "calls",
        "failures",
        "skipped",
        "cache_hits",
        "agent_runs",
        "run_failures",
        "request_count",
        "tool_call_count",
        "output_retry_count",
        "tool_calls",
        "tool_failures",
        "output_retries",
        "context_omissions",
    ):
        report[key] = sum(stage.get(key, 0) for stage in report["stages"].values())
    report.update(events.totals)
    report["timings"] = events.timing_summary()
    if fatal is not None:
        report["fatal_error"] = fatal
    if fatal is None and any(candidate.values()):
        errors = validate_protocol(model, documents, schemas)
        if errors:
            report.update(complete=False, exit_code=1, validation_errors=errors)
        else:
            write_json(out / "protocol.json", model)
            report["model_written"] = True
    write_json(out / "unresolved.json", issues)
    write_json(out / "compile-report.json", report)
    return report
