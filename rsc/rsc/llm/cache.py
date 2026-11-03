"""Content-addressed response cache and a caching client wrapper."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rsc.artifacts import read_json, write_json

from .base import BaseLLMClient
from .types import LLMRequest, LLMResponse


class LLMCache:
    """A tiny on-disk cache keyed by the complete request payload.

    The cache stores the normalised response (text, structured object, usage,
    ids), not the provider's raw wire object.  It is intentionally independent
    of the provider SDKs.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        path = self.path_for(key)
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return LLMResponse.from_cache_dict(data)

    def put(self, key: str, response: LLMResponse) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        write_json(temporary, response.to_cache_dict())
        temporary.replace(path)

    def clear(self) -> int:
        removed = 0
        for path in self.root.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


class CachingLLMClient(BaseLLMClient):
    """Wrap a client so identical requests reuse a stored response."""

    def __init__(self, inner: BaseLLMClient, cache: LLMCache) -> None:
        super().__init__(inner.config)
        self.inner = inner
        self.cache = cache

    @property
    def api(self) -> str:
        """Keep provider-specific request logic working through the cache."""
        return self.inner.api

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = {
            "provider": self.inner.provider,
            "api": self.inner.api,
            "base_url": self.inner.config.base_url,
            "model": self.inner._model(request),
            "system": request.system,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": self.inner._temperature(request),
            "max_tokens": self.inner._max_tokens(request),
            "stop": request.stop,
            "response_schema": request.response_schema,
            "schema_name": request.schema_name,
            "extra_body": request.extra_body,
        }
        key = self.cache.key(payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        response = self.inner.complete(request)
        self.cache.put(key, response)
        return response
