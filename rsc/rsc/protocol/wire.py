"""Final provider-body budgets, isolated by the current asynchronous model request."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx2

from rsc.llm.budget import RequestLimits, estimate_tokens


@dataclass
class WireRequest:
    limits: RequestLimits
    events: Any
    run_id: str
    request_index: int
    output_tokens: int
    delivered: Callable[[], None]
    model_name: str = ""
    error: Exception | None = None


CURRENT_WIRE_REQUEST: ContextVar[WireRequest | None] = ContextVar("rsc_wire_request", default=None)


async def check_wire_request(request: httpx2.Request) -> None:
    """Check the fully serialized SDK JSON body before the HTTP transport is invoked."""

    scope = CURRENT_WIRE_REQUEST.get()
    if scope is None:
        return
    body = await request.aread()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        scope.error = ValueError("RSC Agent providers must send a JSON request body")
        raise scope.error from exc
    record = {
        "run_id": scope.run_id,
        "request_index": scope.request_index,
        "request_bytes": len(body),
        "payload": payload,
        "called": False,
        "error": None,
    }
    try:
        output_tokens = payload.get(
            "max_tokens", payload.get("max_completion_tokens", scope.output_tokens)
        )
        input_tokens, method = estimate_tokens(payload, payload.get("model", scope.model_name))
        record.update(input_token_estimate=input_tokens, counting_method=method)
        scope.limits.check(len(body), output_tokens, input_tokens=input_tokens)
        scope.delivered()
    except Exception as exc:
        scope.error = exc
        record["error"] = f"{type(exc).__name__}: {exc}"
        scope.events.append("provider-requests.jsonl", record)
        raise
    record["called"] = True
    scope.events.append("provider-requests.jsonl", record)
    scope.events.append(
        "user-messages.jsonl",
        {
            "run_id": scope.run_id,
            "request_index": scope.request_index,
            "model": payload.get("model"),
            "user_messages": [
                {"message_index": index, "message": message}
                for index, message in enumerate(payload.get("messages", []))
                if message.get("role") == "user"
            ],
        },
    )


def install_wire_check(client: httpx2.AsyncClient) -> None:
    """Preserve other hooks while making the RSC check the last pre-transport hook."""

    hooks = client.event_hooks.setdefault("request", [])
    if check_wire_request not in hooks:
        hooks.append(check_wire_request)
