"""Provider-neutral LLM client base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from importlib import import_module
from typing import Any

from rsc.config import ProviderConfig

from .errors import LLMConfigError, LLMError, map_provider_error
from .types import LLMRequest, LLMResponse, Message, validate_against_schema


class BaseLLMClient(ABC):
    """Common helpers shared by Anthropic and OpenAI adapters.

    The interface is deliberately small: ``complete`` is the only required
    method.  ``complete_structured`` is a convenience wrapper that asks the
    provider to return a JSON object matching a supplied JSON Schema.
    """

    api: str = "base"
    _sdk_client_class: str = ""
    _unsupported_structured_markers: tuple[str, ...] = ()

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.provider = config.name
        self.model = config.model

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion request."""

    def _build_client(self) -> Any:
        try:
            self._sdk = import_module(self.api)
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise LLMConfigError(
                f"the '{self.api}' package is required for the {self.api} adapter; "
                f"install it with `pip install rsc` or `pip install {self.api}`"
            ) from exc
        kwargs: dict[str, Any] = {
            "api_key": self.config.resolve_api_key(),
            "timeout": self.config.timeout_s,
            "max_retries": self.config.max_retries,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.default_headers:
            kwargs["default_headers"] = dict(self.config.default_headers)
        kwargs.update(self.config.extra_client_kwargs)
        return getattr(self._sdk, self._sdk_client_class)(**kwargs)

    def _map_error(self, error: Exception) -> LLMError:
        return map_provider_error(
            error, self._sdk, self.api, self._unsupported_structured_markers
        )

    def complete_structured(
        self,
        request: LLMRequest,
        schema: dict[str, Any],
        *,
        schema_name: str = "result",
    ) -> LLMResponse:
        """Run a completion that must return a JSON object matching ``schema``."""

        if not isinstance(schema, dict) or not schema:
            raise ValueError("schema must be a non-empty JSON Schema object")
        if request.response_schema is not None and request.response_schema != schema:
            raise ValueError("request already carries a different response_schema")
        response = self.complete(
            replace(request, response_schema=schema, schema_name=schema_name)
        )
        if response.structured is None:
            raise LLMError("provider returned no structured object")
        validate_against_schema(response.structured, schema)
        return response

    # -- request normalisation -------------------------------------------------

    def _model(self, request: LLMRequest) -> str:
        return request.model or self.config.model

    def _temperature(self, request: LLMRequest) -> float | None:
        if request.temperature is not None:
            return request.temperature
        return self.config.temperature

    def _max_tokens(self, request: LLMRequest) -> int:
        return request.max_tokens or self.config.max_tokens

    def _system_and_messages(
        self, request: LLMRequest
    ) -> tuple[str | None, list[Message]]:
        """Hoist system messages into one system prompt and return chat turns."""

        system_parts: list[str] = []
        if request.system:
            system_parts.append(request.system)
        chat: list[Message] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                chat.append(message)
        if not chat:
            raise LLMError("at least one non-system message is required")
        system = "\n\n".join(part for part in system_parts if part) or None
        return system, chat
