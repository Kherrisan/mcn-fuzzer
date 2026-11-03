"""Thread-safe token/usage accounting."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .types import LLMResponse


@dataclass
class UsageTracker:
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record(self, response: LLMResponse) -> None:
        with self._lock:
            self.calls += 1
            if response.cached:
                self.cached_calls += 1
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
            self.cache_read_tokens += response.usage.cache_read_tokens
            self.cache_write_tokens += response.usage.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls": self.calls,
                "cached_calls": self.cached_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "total_tokens": self.total_tokens,
            }
