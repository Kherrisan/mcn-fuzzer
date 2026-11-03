"""A deterministic in-memory client for tests and offline pipelines."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from rsc.config import ProviderConfig

from .base import BaseLLMClient
from .errors import LLMError
from .types import LLMRequest, LLMResponse, parse_json_object

MockItem = LLMResponse | str | dict[str, Any] | Callable[[LLMRequest], LLMResponse]


class MockLLMClient(BaseLLMClient):
    """Return queued responses and record requests.

    Items can be :class:`LLMResponse` objects, JSON strings, dicts (treated as
    structured objects), or callables receiving the request.
    """

    def __init__(
        self,
        responses: Iterable[MockItem] | None = None,
        *,
        provider: str = "mock",
        model: str = "mock",
    ) -> None:
        config = ProviderConfig(
            name=provider,
            api="openai",
            model=model,
            api_key="mock",
        )
        super().__init__(config)
        self.provider = provider
        self.model = model
        self._responses = list(responses or [])
        self.requests: list[LLMRequest] = []

    def queue(self, *items: MockItem) -> MockLLMClient:
        self._responses.extend(items)
        return self

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise LLMError("MockLLMClient has no response queued")
        item = self._responses.pop(0)
        if callable(item):
            item = item(request)
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, dict):
            return LLMResponse(
                text=json.dumps(item, ensure_ascii=False),
                structured=item,
                model=self.model,
                provider=self.provider,
            )
        text = str(item)
        return LLMResponse(
            text=text,
            structured=parse_json_object(text),
            model=self.model,
            provider=self.provider,
        )
