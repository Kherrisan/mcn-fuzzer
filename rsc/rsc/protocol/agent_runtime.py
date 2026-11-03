"""PydanticAI execution with request-level audit, cache, and shared run limits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic_ai import Agent, ModelRetry, PromptedOutput, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability, Instrumentation
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    RetryFeedbackPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import UsageLimits

from rsc.llm.budget import RequestBudgetError, RequestLimits, estimate_tokens

from .options import AgentOptions
from .sessions import SessionState, resolve_context_capacity
from .telemetry import agent_instrumentation
from .wire import CURRENT_WIRE_REQUEST, WireRequest

RESPONSE_ADAPTER = TypeAdapter(ModelResponse)
PARAMETERS_ADAPTER = TypeAdapter(ModelRequestParameters)
CACHE_VERSION = "rsc-agent-request-v8"


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OutputTruncatedError(RuntimeError):
    """The provider stopped before a complete extraction could be trusted."""


class RunLimitError(RuntimeError):
    """An execution exhausted a shared request, tool, retry, or time limit."""


@dataclass
class RunBudget:
    options: AgentOptions
    model_requests: int = 0
    tool_calls: int = 0
    output_retries: int = 0
    started: float = field(default_factory=time.monotonic)

    def remaining_seconds(self) -> float:
        remaining = self.options.max_run_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise RunLimitError("Agent run time limit reached")
        return remaining

    def consume_request(self) -> None:
        self.remaining_seconds()
        if self.model_requests >= self.options.max_model_requests:
            raise RunLimitError("Agent model request limit reached")
        self.model_requests += 1

    def consume_tool(self) -> None:
        self.remaining_seconds()
        if self.tool_calls >= self.options.max_tool_calls:
            raise RunLimitError("Agent tool call limit reached")
        self.tool_calls += 1


class EventWriter:
    """Single-event-loop writer for immutable JSONL audit records."""

    def __init__(self, root: Path | None = None, *, capture: bool = False) -> None:
        self.root = root
        self.capture = capture or root is None
        self.events: dict[str, list[dict]] = {}
        self.counts: dict[str, int] = {}
        self.tool_origins: dict[str, dict[str, dict]] = {}
        self.metrics: dict[str, dict] = {}
        self.started = time.monotonic()
        self._timings: dict[str, dict] = {}
        self._audit_writes: dict[str, dict] = {}
        self.totals = {
            "calls": 0,
            "failures": 0,
            "skipped": 0,
            "cache_hits": 0,
            "agent_runs": 0,
            "run_failures": 0,
            "tool_calls": 0,
            "output_retries": 0,
            "tool_failures": 0,
            "context_snapshots": 0,
            "context_omissions": 0,
            "max_request_bytes": 0,
        }

    def append(self, name: str, record: dict) -> None:
        started = time.monotonic()
        saved = deepcopy(record)
        saved["logged_at"] = utc_timestamp()
        self.counts[name] = self.counts.get(name, 0) + 1
        if name == "model-requests.jsonl":
            self.totals["calls"] += int(record["called"])
            self.totals["skipped"] += int(not record["called"])
        elif name == "model-responses.jsonl":
            self.totals["failures"] += int(record["error"] is not None)
            self.totals["cache_hits"] += int(record["cached"])
            for part in (record.get("response") or {}).get("parts", []):
                if part.get("part_kind") == "tool-call":
                    self.tool_origins.setdefault(record["run_id"], {})[part["tool_call_id"]] = {
                        "request_index": record["request_index"],
                        "arguments": deepcopy(part.get("args")),
                    }
        run_id = record.get("run_id")
        if run_id is not None and name in {
            "model-requests.jsonl",
            "provider-requests.jsonl",
            "tool-calls.jsonl",
            "retrieval-contexts.jsonl",
        }:
            metric = self.metrics.setdefault(
                run_id,
                {
                    "max_request_bytes": 0,
                    "tool_failures": 0,
                    "context_snapshots": 0,
                    "context_omissions": 0,
                },
            )
            if name in {"model-requests.jsonl", "provider-requests.jsonl"}:
                metric["max_request_bytes"] = max(
                    metric["max_request_bytes"], record["request_bytes"]
                )
                self.totals["max_request_bytes"] = max(
                    self.totals["max_request_bytes"], record["request_bytes"]
                )
            elif name == "tool-calls.jsonl":
                failures = int(record.get("status") in {"error", "invalid", "failed", "missing"})
                metric["tool_failures"] += failures
                self.totals["tool_failures"] += failures
            elif name == "retrieval-contexts.jsonl":
                metric["context_snapshots"] += 1
                omitted = len(record.get("omitted", []))
                metric["context_omissions"] += omitted
                self.totals["context_snapshots"] += 1
                self.totals["context_omissions"] += omitted
        encoded = encode(saved) + "\n"
        encoded_bytes = len(encoded.encode("utf-8"))
        try:
            if self.capture:
                self.events.setdefault(name, []).append(saved)
            if self.root is not None:
                self.root.mkdir(parents=True, exist_ok=True)
                with (self.root / name).open("a", encoding="utf-8") as handle:
                    handle.write(encoded)
        finally:
            metric = self._audit_writes.setdefault(
                name, {"count": 0, "bytes": 0, "total_seconds": 0.0, "max_seconds": 0.0}
            )
            duration = time.monotonic() - started
            metric["count"] += 1
            metric["bytes"] += encoded_bytes
            metric["total_seconds"] += duration
            metric["max_seconds"] = max(metric["max_seconds"], duration)

    @contextmanager
    def span(self, operation: str, **metadata: Any) -> Iterator[dict]:
        """Record bounded timing metadata, including interrupted asynchronous work.

        The yielded dictionary accepts result metrics and a status. Identity and
        clock fields are generated here and cannot be replaced by result metrics.
        Callers must supply identifiers and counts, never prompts or credentials.
        """

        started = time.monotonic()
        span_id = uuid.uuid4().hex
        identity = {"span_id": span_id, "operation": operation}
        self.append(
            "timings.jsonl",
            {
                **metadata,
                **identity,
                "event": "start",
                "timestamp": utc_timestamp(),
                "elapsed_seconds": started - self.started,
            },
        )
        result: dict = {}
        try:
            yield result
        except BaseException as exc:
            result["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            # Detailed errors live in the existing audit logs. Timing records only
            # retain the type, avoiding provider error bodies and source text.
            result["error"] = type(exc).__name__
            raise
        finally:
            ended = time.monotonic()
            duration = ended - started
            status = result.get("status", "ok")
            metric = self._timings.setdefault(
                operation,
                {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0, "failures": 0},
            )
            metric["count"] += 1
            metric["total_seconds"] += duration
            metric["max_seconds"] = max(metric["max_seconds"], duration)
            metric["failures"] += int(status != "ok")
            self.append(
                "timings.jsonl",
                {
                    **metadata,
                    **result,
                    **identity,
                    "event": "end",
                    "timestamp": utc_timestamp(),
                    "elapsed_seconds": ended - self.started,
                    "duration_seconds": duration,
                    "status": status,
                },
            )

    def timing_summary(self) -> dict:
        """Return independent operation and audit-write timing aggregates."""

        return deepcopy({"operations": self._timings, "audit_writes": self._audit_writes})


class RequestCache:
    """Content-addressed model responses with per-key coordination and atomic writes."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self._memory: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_call(self, key: str, call) -> tuple[ModelResponse, bool]:
        async with self._locks.setdefault(key, asyncio.Lock()):
            value = self._memory.get(key)
            path = self.root / f"{key}.json" if self.root is not None else None
            if value is None and path is not None and path.is_file():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    RESPONSE_ADAPTER.validate_python(value)
                except (ValueError, OSError):
                    value = None
            if value is not None:
                return RESPONSE_ADAPTER.validate_python(deepcopy(value)), True
            response = await call()
            value = RESPONSE_ADAPTER.dump_python(response, mode="json")
            self._memory[key] = deepcopy(value)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=path.parent,
                        delete=False,
                        prefix=f".{key}.",
                        suffix=".tmp",
                    ) as handle:
                        temporary = Path(handle.name)
                        handle.write(encode(value))
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return response, False


def cache_messages(messages: list[dict]) -> list[dict]:
    """Exclude framework bookkeeping, without stripping semantic tool-result fields."""

    result = deepcopy(messages)
    for message in result:
        for key in ("timestamp", "run_id", "conversation_id", "metadata"):
            message.pop(key, None)
        for part in message.get("parts", []):
            part.pop("timestamp", None)
            part.pop("metadata", None)
    return result


def request_envelope(messages: list, parameters: dict, settings: dict) -> dict:
    """Serialize every semantic request component for budgeting, caching and audit."""

    return {
        "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
        "model_settings": deepcopy(settings),
        "tool_definitions": parameters["function_tools"],
        "output_schema": {
            "output_object": parameters.get("output_object"),
            "output_tools": parameters.get("output_tools"),
        },
        "request_parameters": {
            key: value
            for key, value in parameters.items()
            if key not in {"function_tools", "output_object", "output_tools"}
        },
    }


class AuditedModel(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        *,
        run_id: str,
        stage: str,
        phase: str,
        budget: RunBudget,
        limits: RequestLimits,
        events: EventWriter,
        cache: RequestCache | None = None,
        cache_identity: dict | None = None,
        before_request: Callable[[list, dict], None] | None = None,
        prepare_messages: Callable | None = None,
        session: SessionState | None = None,
    ) -> None:
        super().__init__(wrapped)
        self.run_id = run_id
        self.stage = stage
        self.phase = phase
        self.budget = budget
        self.limits = limits
        self.events = events
        self.cache = cache
        self.cache_identity = (
            cache_identity
            or getattr(wrapped, "cache_identity", None)
            or {
                "system": wrapped.system,
                "model": wrapped.model_name,
                "base_url": str(getattr(wrapped, "base_url", "") or ""),
            }
        )
        self.before_request = before_request
        self._prepare_messages = prepare_messages
        self.session = session
        self.request_index = 0
        self.history_output_retries = 0
        self.prepared_output_retries = 0
        self.undelivered_response: ModelResponse | None = None
        self.context_preparation_error: Exception | None = None
        self.calls = self.failures = self.skipped = self.cache_hits = 0
        self.preparation_ms = 0.0
        self.provider_latency_ms = 0.0
        self.cache_overhead_ms = 0.0
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def prepare_messages(self, messages, model_request_parameters=None):
        # Providers render typed retry feedback as user/system text. Count it before
        # conversion; matching text would confuse source material with real retries.
        self.prepared_output_retries = max(
            self.prepared_output_retries,
            sum(
                isinstance(part, RetryFeedbackPart)
                for message in messages[self.current_turn_start(messages) :]
                for part in message.parts
            ),
        )
        return super().prepare_messages(messages, model_request_parameters)

    def current_turn_start(self, messages) -> int:
        if self.session is not None and self.session.active_turn is not None:
            return self.session._prefix_end(messages)
        return 0

    async def request(self, messages, model_settings, model_request_parameters):
        retry_limit = getattr(self.wrapped, "retry_limit", 0)
        for attempt in range(retry_limit + 1):
            try:
                return await self._request_once(messages, model_settings, model_request_parameters)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                transient = (
                    status in {408, 409, 429}
                    or (isinstance(status, int) and status >= 500)
                    or type(exc).__name__
                    in {
                        "APIConnectionError",
                        "APITimeoutError",
                        "RateLimitError",
                    }
                )
                if not transient or attempt >= retry_limit:
                    raise
                self.budget.remaining_seconds()
                await asyncio.sleep(min(2**attempt, 4))
        raise AssertionError("unreachable request retry state")

    async def _request_once(self, messages, model_settings, model_request_parameters):
        preparation_started = time.monotonic()
        self.request_index += 1
        parameters = PARAMETERS_ADAPTER.dump_python(model_request_parameters, mode="json")
        settings = deepcopy(model_settings or {})
        preparation_error = self.context_preparation_error
        try:
            # Provider credentials belong on the SDK client, never in an Agent request.
            if "extra_headers" in settings:
                raise ValueError("configure request headers on the provider client")
            if self._prepare_messages is not None:
                messages = self._prepare_messages(messages, parameters, settings, self.limits)
        except Exception as exc:
            preparation_error = exc
        payload = request_envelope(messages, parameters, settings)
        size = len(encode(payload).encode("utf-8")) + 1024
        input_tokens, counting_method = estimate_tokens(payload, self.model_name)
        record = {
            "run_id": self.run_id,
            "request_index": self.request_index,
            "stage": self.stage,
            "phase": self.phase,
            **payload,
            "request_bytes": size,
            "input_token_estimate": input_tokens,
            "counting_method": counting_method,
            "turn_message_start": self.current_turn_start(messages),
            "called": False,
            "skipped_reason": None,
        }
        try:
            self.budget.consume_request()
            if preparation_error is not None:
                raise preparation_error
            retry_count = self.prepared_output_retries
            additional_retries = max(0, retry_count - self.history_output_retries)
            if (
                self.budget.output_retries + additional_retries
                > self.budget.options.max_output_retries
            ):
                raise RunLimitError("Agent output retry limit reached")
            self.budget.output_retries += additional_retries
            self.history_output_retries = retry_count
            self.limits.check(size, settings.get("max_tokens", 8192), input_tokens=input_tokens)
        except Exception as exc:
            self.skipped += 1
            record["skipped_reason"] = f"{type(exc).__name__}: {exc}"
            record["preparation_ms"] = (time.monotonic() - preparation_started) * 1000
            self.preparation_ms += record["preparation_ms"]
            self.events.append("model-requests.jsonl", record)
            raise
        logged = False

        def delivered():
            nonlocal logged
            if logged:
                return
            if self.before_request is not None:
                self.before_request(messages, record)
            record["called"] = True
            self.calls += 1
            self.events.append("model-requests.jsonl", record)
            logged = True

        key = hashlib.sha256(
            encode(
                {
                    "version": CACHE_VERSION,
                    "provider": self.cache_identity,
                    "messages": cache_messages(payload["messages"]),
                    "settings": settings,
                    "parameters": parameters,
                }
            ).encode()
        ).hexdigest()
        preparation_ms = (time.monotonic() - preparation_started) * 1000
        self.preparation_ms += preparation_ms
        started = time.monotonic()
        response = None
        cached = False
        provider_latency_ms = 0.0
        cache_overhead_ms = 0.0
        try:

            async def call():
                nonlocal provider_latency_ms
                scope = WireRequest(
                    self.limits,
                    self.events,
                    self.run_id,
                    self.request_index,
                    settings.get("max_tokens", 8192),
                    delivered,
                    model_name=self.model_name,
                )
                token = CURRENT_WIRE_REQUEST.set(scope)
                try:
                    if not getattr(self.wrapped, "wire_guard_enabled", False):
                        delivered()
                    provider_started = time.monotonic()
                    try:
                        result = await self.wrapped.request(
                            messages,
                            model_settings,
                            model_request_parameters,
                        )
                    except Exception as exc:
                        # SDKs wrap hook exceptions as connection failures. Local budget
                        # failures must not become transport retries or count as sent.
                        if scope.error is not None:
                            raise scope.error from exc
                        raise
                    finally:
                        duration = (time.monotonic() - provider_started) * 1000
                        provider_latency_ms += duration
                        self.provider_latency_ms += duration
                    if not logged:
                        raise ValueError(
                            "provider returned without checking its final request body"
                        )
                    return result
                finally:
                    CURRENT_WIRE_REQUEST.reset(token)

            with self.events.span(
                "model_request",
                run_id=self.run_id,
                request_index=self.request_index,
                stage=self.stage,
                phase=self.phase,
            ) as timing:
                cache_started = time.monotonic()
                try:
                    if self.cache is None:
                        response, cached = await call(), False
                    else:
                        response, cached = await self.cache.get_or_call(key, call)
                finally:
                    if self.cache is not None:
                        cache_overhead_ms = max(
                            0.0,
                            (time.monotonic() - cache_started) * 1000 - provider_latency_ms,
                        )
                        self.cache_overhead_ms += cache_overhead_ms
                    timing.update(
                        cached=cached,
                        preparation_ms=preparation_ms,
                        provider_latency_ms=provider_latency_ms,
                        cache_overhead_ms=cache_overhead_ms,
                    )
            if cached:
                delivered()
            self.cache_hits += int(cached)
            for name in self.usage:
                self.usage[name] += getattr(response.usage, name, 0)
            for part in response.parts:
                if isinstance(part, ToolCallPart):
                    self.budget.consume_tool()
            if response.finish_reason == "length":
                raise OutputTruncatedError("model response reached its output limit")
            self.events.append(
                "model-responses.jsonl",
                {
                    "run_id": self.run_id,
                    "request_index": self.request_index,
                    "stage": self.stage,
                    "phase": self.phase,
                    "cached": cached,
                    "response": RESPONSE_ADAPTER.dump_python(response, mode="json"),
                    "error": None,
                    "latency_ms": (time.monotonic() - started) * 1000,
                    "preparation_ms": preparation_ms,
                    "provider_latency_ms": provider_latency_ms,
                    "cache_overhead_ms": cache_overhead_ms,
                },
            )
            return response
        except BaseException as exc:
            if response is not None:
                self.undelivered_response = response
            if not logged:
                self.skipped += 1
                record["skipped_reason"] = f"{type(exc).__name__}: {exc}"
                self.events.append("model-requests.jsonl", record)
                raise
            self.failures += 1
            self.events.append(
                "model-responses.jsonl",
                {
                    "run_id": self.run_id,
                    "request_index": self.request_index,
                    "stage": self.stage,
                    "phase": self.phase,
                    "cached": cached,
                    "response": RESPONSE_ADAPTER.dump_python(response, mode="json")
                    if response is not None
                    else None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": (time.monotonic() - started) * 1000,
                    "preparation_ms": preparation_ms,
                    "provider_latency_ms": provider_latency_ms,
                    "cache_overhead_ms": cache_overhead_ms,
                },
            )
            raise


async def run_agent(
    model: Model,
    output_type: type[BaseModel],
    payload: dict,
    *,
    stage: str,
    phase: str,
    instructions: str,
    events: EventWriter,
    limits: RequestLimits,
    options: AgentOptions | None = None,
    budget: RunBudget | None = None,
    cache: RequestCache | None = None,
    cache_identity: dict | None = None,
    deps: Any = None,
    tools: list | None = None,
    output_check: Callable | None = None,
    before_request: Callable | None = None,
    prepare_messages: Callable | None = None,
    after_run: Callable | None = None,
    run_id: str | None = None,
    model_settings: dict | None = None,
    session: SessionState | None = None,
) -> dict:
    """Run a PydanticAI tool loop without committing the returned proposal."""

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    options = options or (budget.options if budget is not None else AgentOptions())
    budget = budget or RunBudget(options)
    run_id = run_id or uuid.uuid4().hex
    context_error = None
    if session is not None:
        from dataclasses import replace

        try:
            limits = replace(
                limits,
                context_window_tokens=resolve_context_capacity(
                    model, limits, session.context_options
                ),
            )
        except ValueError as exc:
            context_error = exc
    wrapped = AuditedModel(
        model,
        run_id=run_id,
        stage=stage,
        phase=phase,
        budget=budget,
        limits=limits,
        events=events,
        cache=cache,
        cache_identity=cache_identity,
        before_request=before_request,
        prepare_messages=prepare_messages,
        session=session,
    )

    class SessionHistory(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            # This hook updates BOTH the outgoing request and the graph's actual
            # history. Adapter-level processing alone only changes a temporary
            # copy and would reintroduce removed blocks on the next request.
            parameters = PARAMETERS_ADAPTER.dump_python(
                request_context.model_request_parameters, mode="json"
            )
            current = list(request_context.messages)
            try:
                session.prepare_messages(
                    current,
                    parameters,
                    deepcopy(request_context.model_settings or {}),
                    limits,
                    model,
                )
            except Exception as exc:
                # Let the audited model reject this locally. That preserves the
                # called:false request record and skipped count used by reports.
                wrapped.context_preparation_error = exc
            request_context.messages = current
            ctx.messages[:] = current
            return request_context

    agent = Agent(
        wrapped,
        # JSON text keeps the output schema out of the tool list and works with
        # compatible providers that do not implement native JSON Schema output.
        output_type=PromptedOutput(output_type),
        instructions=instructions,
        deps_type=type(deps),
        tools=tools or [],
        retries=options.max_output_retries,
        name=f"rsc_{stage}_{phase}",
        metadata={"rsc.run_id": run_id, "rsc.stage": stage, "rsc.phase": phase},
        capabilities=[
            Instrumentation(settings=agent_instrumentation()),
            *([SessionHistory()] if session is not None else []),
        ],
    )
    validation_ms = 0.0
    if output_check is not None:

        @agent.output_validator
        def validate_output(ctx, output):
            nonlocal validation_ms
            started = time.monotonic()
            try:
                with events.span(
                    "output_validation",
                    run_id=run_id,
                    request_index=wrapped.request_index,
                    stage=stage,
                    phase=phase,
                ) as timing:
                    errors = output_check(output)
                    timing["error_count"] = len(errors) if errors else 0
                    if errors:
                        raise ModelRetry("; ".join(errors))
            finally:
                validation_ms += (time.monotonic() - started) * 1000
            return output

    start_requests, start_tools = budget.model_requests, budget.tool_calls
    start_output_retries = budget.output_retries
    started = time.monotonic()
    record = {
        "run_id": run_id,
        "stage": stage,
        "phase": phase,
        "status": "failed",
        "end_reason": None,
        "data": None,
        "error": None,
    }
    with capture_run_messages() as messages:
        try:
            if context_error is not None:
                raise context_error
            if session is not None:
                session.begin_turn(run_id, stage, phase, payload)
            with events.span("agent_run", run_id=run_id, stage=stage, phase=phase) as timing:
                try:
                    async with asyncio.timeout(budget.remaining_seconds()):
                        result = await agent.run(
                            encode(payload),
                            deps=deps,
                            model_settings=model_settings,
                            run_id=run_id,
                            message_history=list(session.messages) if session is not None else None,
                            usage_limits=UsageLimits(
                                request_limit=options.max_model_requests,
                                tool_calls_limit=options.max_tool_calls,
                            ),
                        )
                finally:
                    timing.update(
                        request_count=budget.model_requests - start_requests,
                        tool_call_count=budget.tool_calls - start_tools,
                        output_retry_count=budget.output_retries - start_output_retries,
                        cache_hits=wrapped.cache_hits,
                        preparation_ms=wrapped.preparation_ms,
                        provider_latency_ms=wrapped.provider_latency_ms,
                        cache_overhead_ms=wrapped.cache_overhead_ms,
                        validation_ms=validation_ms,
                    )
            record.update(
                status="ok", end_reason="completed", data=result.output.model_dump(mode="json")
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            record.update(status="cancelled", end_reason="cancelled", error="Agent trial cancelled")
            raise
        except Exception as exc:
            reason = (
                "output_truncated"
                if isinstance(exc, OutputTruncatedError)
                else "request_budget"
                if isinstance(exc, RequestBudgetError)
                else "context_length"
                if any(
                    marker in str(exc).casefold()
                    for marker in (
                        "context_length_exceeded",
                        "maximum context length",
                        "prompt is too long",
                        "input tokens exceed",
                    )
                )
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "run_limit"
                if isinstance(exc, RunLimitError)
                else type(exc).__name__
            )
            record.update(end_reason=reason, error=f"{type(exc).__name__}: {exc}")
        finally:
            if session is not None and wrapped.undelivered_response is not None:
                response = wrapped.undelivered_response
                if not any(message is response for message in messages):
                    response.run_id = run_id
                    messages.append(response)
            if after_run is not None:
                after_run(
                    messages,
                    {
                        "run_id": run_id,
                        "request_index": wrapped.request_index,
                        "turn_message_start": wrapped.current_turn_start(messages),
                    },
                )
            if (
                session is not None
                and session.active_turn is not None
                and session.active_turn.run_id == run_id
            ):
                session.finish_turn(messages, status=record["status"], error=record["error"])
    record.update(
        request_count=budget.model_requests - start_requests,
        tool_call_count=budget.tool_calls - start_tools,
        output_retry_count=budget.output_retries - start_output_retries,
        calls=wrapped.calls,
        failures=wrapped.failures,
        skipped=wrapped.skipped,
        cache_hits=wrapped.cache_hits,
        usage=wrapped.usage,
        preparation_ms=wrapped.preparation_ms,
        provider_latency_ms=wrapped.provider_latency_ms,
        cache_overhead_ms=wrapped.cache_overhead_ms,
        validation_ms=validation_ms,
        elapsed_seconds=time.monotonic() - started,
        **events.metrics.pop(run_id, {}),
    )
    events.tool_origins.pop(run_id, None)
    events.totals["agent_runs"] += 1
    events.totals["run_failures"] += int(record["status"] != "ok")
    events.totals["tool_calls"] += record["tool_call_count"]
    events.totals["output_retries"] += record["output_retry_count"]
    return record
