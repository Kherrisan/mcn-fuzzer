"""OpenAI Chat Completions adapter.

This adapter targets the widely implemented Chat Completions shape, so it also
works with OpenAI-compatible endpoints (vLLM, Ollama, DeepSeek, OpenRouter,
and other services exposing the Chat Completions shape).

Structured output has three supported modes:

``json_schema``
    Native ``response_format={"type": "json_schema", ...}`` (strictest).
``json_object``
    ``response_format={"type": "json_object"}`` plus a JSON instruction.
``tool``
    Function calling with a forced tool whose parameters are the JSON Schema.

``structured_mode="auto"`` tries them in that order and falls back only when
the provider explicitly rejects the structured-output mechanism.
"""

from __future__ import annotations

import json
import time
from typing import Any

from rsc.config import ProviderConfig

from .base import BaseLLMClient
from .budget import check_provider_payload
from .errors import (
    LLMProviderError,
    LLMStructuredOutputError,
    StructuredOutputUnsupported,
)
from .types import LLMRequest, LLMResponse, Usage, parse_json_object, validate_against_schema

_UNSUPPORTED_STRUCTURED_MARKERS = (
    "response_format",
    "json_schema",
    "json schema",
    "json_object",
    "json mode",
    "structured output",
    "tool_choice",
    "function calling",
    "tools",
)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text"):
                    parts.append(str(part.get("text", "")))
            else:
                parts.append(str(getattr(part, "text", "")))
        return "".join(parts)
    return str(content)


class OpenAIClient(BaseLLMClient):
    api = "openai"
    _sdk_client_class = "OpenAI"
    _unsupported_structured_markers = _UNSUPPORTED_STRUCTURED_MARKERS

    def __init__(
        self,
        config: ProviderConfig,
        *,
        sdk_client: Any | None = None,
    ) -> None:
        super().__init__(config)
        self._sdk: Any | None = None
        if sdk_client is not None:
            self._client = sdk_client
        else:
            self._client = self._build_client()

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        schema = request.response_schema
        if schema is None:
            return self._complete_once(request, None, started)

        mode = self.config.structured_mode
        if mode != "auto":
            try:
                return self._complete_once(request, mode, started)
            except StructuredOutputUnsupported as exc:
                raise LLMProviderError(
                    f"provider rejected structured_mode={mode!r}: {exc}"
                ) from exc

        failures: list[str] = []
        for candidate in ("json_schema", "json_object", "tool"):
            try:
                return self._complete_once(request, candidate, started)
            except StructuredOutputUnsupported as exc:
                failures.append(f"{candidate}: {exc}")
        raise LLMStructuredOutputError(
            "provider rejected every structured-output mode; " + "; ".join(failures)
        )

    def _complete_once(
        self,
        request: LLMRequest,
        mode: str | None,
        started: float,
    ) -> LLMResponse:
        system, messages = self._system_and_messages(request)
        schema = request.response_schema

        if mode == "json_object":
            instruction = "Return one JSON object only. Do not wrap it in markdown."
            system = f"{system}\n\n{instruction}" if system else instruction

        payload: list[dict[str, str]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend({"role": message.role, "content": message.content} for message in messages)

        params: dict[str, Any] = {
            "model": self._model(request),
            "messages": payload,
        }
        temperature = self._temperature(request)
        if temperature is not None:
            params["temperature"] = temperature
        max_tokens = self._max_tokens(request)
        if self.config.max_tokens_param == "max_completion_tokens":
            params["max_completion_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens
        if request.stop:
            params["stop"] = list(request.stop)
        if request.extra_body:
            params["extra_body"] = request.extra_body

        if schema is not None and mode == "json_schema":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": schema,
                    "strict": True,
                },
            }
        elif schema is not None and mode == "json_object":
            params["response_format"] = {"type": "json_object"}
        elif schema is not None and mode == "tool":
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": request.schema_name,
                        "description": "Return the final result as a JSON object.",
                        "parameters": schema,
                        "strict": True,
                    },
                }
            ]
            params["tool_choice"] = {
                "type": "function",
                "function": {"name": request.schema_name},
            }

        check_provider_payload(request, params)
        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:
            raise self._map_error(exc) from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMProviderError("OpenAI response contained no choices")
        choice = choices[0]
        message = getattr(choice, "message", None)
        text = _content_text(getattr(message, "content", None))
        structured: dict[str, Any] | None = None

        if schema is not None and getattr(choice, "finish_reason", None) != "length":
            if mode == "tool":
                tool_calls = getattr(message, "tool_calls", None) or []
                if tool_calls:
                    function = getattr(tool_calls[0], "function", None)
                    arguments = getattr(function, "arguments", None)
                    if isinstance(arguments, dict):
                        structured = arguments
                    elif isinstance(arguments, str):
                        try:
                            parsed = json.loads(arguments)
                        except json.JSONDecodeError:
                            parsed = parse_json_object(arguments)
                        if isinstance(parsed, dict):
                            structured = parsed
            if structured is None:
                structured = parse_json_object(text)
            if structured is None:
                raise LLMStructuredOutputError(
                    "OpenAI response contained no parseable JSON object "
                    f"(text prefix: {text[:300]!r})"
                )
            validate_against_schema(structured, schema)

        usage = self._usage(getattr(response, "usage", None))
        return LLMResponse(
            text=text,
            structured=structured,
            usage=usage,
            model=str(getattr(response, "model", self._model(request))),
            provider=self.provider,
            finish_reason=getattr(choice, "finish_reason", None),
            request_id=getattr(response, "id", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _usage(raw: Any) -> Usage:
        details = getattr(raw, "prompt_tokens_details", None)
        return Usage(
            input_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
            cache_read_tokens=int(getattr(details, "cached_tokens", 0) or 0),
        )
