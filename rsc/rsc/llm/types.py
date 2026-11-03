"""Provider-neutral request/response types for LLM calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import LLMStructuredOutputError

Role = Literal["system", "user", "assistant"]
_ROLES = frozenset({"system", "user", "assistant"})


@dataclass(slots=True)
class Message:
    """One chat message.  Tool messages are intentionally not modelled yet."""

    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"unsupported message role: {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("message content must be a string")


@dataclass(slots=True)
class Usage:
    """Token accounting, normalised across providers."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_read_tokens=int(data.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(data.get("cache_write_tokens", 0) or 0),
        )


@dataclass(slots=True)
class LLMRequest:
    """A single completion request.

    ``response_schema`` can be supplied directly or through
    :meth:`BaseLLMClient.complete_structured`.  Provider adapters choose the
    appropriate native structured-output mechanism while keeping this type
    unchanged.
    """

    messages: list[Message] = field(default_factory=list)
    system: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    response_schema: dict[str, Any] | None = None
    schema_name: str = "result"
    extra_body: dict[str, Any] | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages and not self.system:
            raise ValueError("LLMRequest requires at least one message or a system prompt")
        for message in self.messages:
            if not isinstance(message, Message):
                raise TypeError("messages must contain Message instances")
        if self.response_schema is not None:
            if not isinstance(self.response_schema, dict):
                raise TypeError("response_schema must be a JSON Schema object")
            if not self.schema_name or not self.schema_name.strip():
                raise ValueError("schema_name must not be empty")
        if self.stop is not None and not all(isinstance(item, str) for item in self.stop):
            raise TypeError("stop must be a list of strings")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.extra_body is not None and not isinstance(self.extra_body, dict):
            raise TypeError("extra_body must be a dict")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")


@dataclass(slots=True)
class LLMResponse:
    """A normalised completion response."""

    text: str = ""
    structured: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    finish_reason: str | None = None
    request_id: str | None = None
    latency_ms: int = 0
    cached: bool = False

    def to_cache_dict(self) -> dict[str, Any]:
        """JSON-friendly representation; ``raw``/latency are not cached."""

        return {
            "text": self.text,
            "structured": self.structured,
            "usage": self.usage.to_dict(),
            "model": self.model,
            "provider": self.provider,
            "finish_reason": self.finish_reason,
            "request_id": self.request_id,
        }

    @classmethod
    def from_cache_dict(cls, data: dict[str, Any]) -> LLMResponse:
        return cls(
            text=str(data.get("text", "")),
            structured=data.get("structured"),
            usage=Usage.from_dict(data.get("usage")),
            model=str(data.get("model", "")),
            provider=str(data.get("provider", "")),
            finish_reason=data.get("finish_reason"),
            request_id=data.get("request_id"),
            cached=True,
        )


def response_object(response: LLMResponse) -> dict[str, Any] | None:
    """Prefer a structured response, otherwise recover JSON from its text."""
    return response.structured or parse_json_object(response.text)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of one JSON object from model text.

    Handles plain JSON, markdown fences, and a leading/trailing explanation.
    Returns ``None`` when no object can be recovered.
    """

    if not text:
        return None
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def validate_against_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate ``instance`` when ``jsonschema`` is installed.

    ``jsonschema`` is a declared dependency of the RSC package.  The lazy
    import keeps the client importable in minimal environments and in tests
    that only exercise provider translation.
    """

    try:
        import jsonschema
    except ImportError:  # pragma: no cover - dependency is declared
        return
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        raise LLMStructuredOutputError(
            f"structured output does not match schema: {exc.message}"
        ) from exc
    except jsonschema.SchemaError as exc:
        raise LLMStructuredOutputError(f"invalid response schema: {exc.message}") from exc
