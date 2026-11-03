"""Anthropic Messages API adapter.

Structured output is implemented with a single tool whose ``input_schema`` is
the requested JSON Schema and ``tool_choice`` forced to that tool.  This works
with the native Anthropic API and with Anthropic-compatible endpoints (for
example DeepSeek's ``/anthropic`` endpoint).  If an endpoint ignores tools and
returns JSON text instead, the adapter recovers the object from the text.
"""

from __future__ import annotations

import time
from typing import Any

from rsc.config import ProviderConfig

from .base import BaseLLMClient
from .budget import check_provider_payload
from .errors import (
    LLMStructuredOutputError,
)
from .types import LLMRequest, LLMResponse, Usage, parse_json_object, validate_against_schema


class AnthropicClient(BaseLLMClient):
    api = "anthropic"
    _sdk_client_class = "Anthropic"

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
        system, messages = self._system_and_messages(request)
        schema = request.response_schema

        params: dict[str, Any] = {
            "model": self._model(request),
            "max_tokens": self._max_tokens(request),
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }
        temperature = self._temperature(request)
        if temperature is not None:
            params["temperature"] = temperature
        if system:
            params["system"] = system
        if request.stop:
            params["stop_sequences"] = list(request.stop)
        if schema is not None:
            params["tools"] = [
                {
                    "name": request.schema_name,
                    "description": (
                        "Return the final result. Call this tool exactly once "
                        "with an object matching the input schema."
                    ),
                    "input_schema": schema,
                }
            ]
            params["tool_choice"] = {"type": "tool", "name": request.schema_name}

        check_provider_payload(request, params)
        try:
            response = self._client.messages.create(**params)
        except Exception as exc:  # provider SDK exceptions are mapped below
            raise self._map_error(exc) from exc

        text_parts: list[str] = []
        structured: dict[str, Any] | None = None
        for block in getattr(response, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(str(getattr(block, "text", "")))
            elif block_type == "tool_use" and structured is None:
                raw_input = getattr(block, "input", None)
                if isinstance(raw_input, dict):
                    structured = raw_input
        text = "".join(text_parts).strip()

        if schema is not None and getattr(response, "stop_reason", None) != "max_tokens":
            if structured is None:
                structured = parse_json_object(text)
            if structured is None:
                raise LLMStructuredOutputError(
                    "Anthropic response contained neither a tool call nor a JSON "
                    f"object (text prefix: {text[:300]!r})"
                )
            validate_against_schema(structured, schema)

        usage = self._usage(getattr(response, "usage", None))
        return LLMResponse(
            text=text,
            structured=structured,
            usage=usage,
            model=str(getattr(response, "model", self._model(request))),
            provider=self.provider,
            finish_reason=getattr(response, "stop_reason", None),
            request_id=getattr(response, "id", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _usage(raw: Any) -> Usage:
        return Usage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
        )
