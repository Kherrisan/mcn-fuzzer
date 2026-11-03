"""Conservative, provider-neutral limits, also checked on the final wire payload."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from functools import lru_cache

from .errors import LLMContextLengthError


class RequestBudgetError(LLMContextLengthError):
    """Rejected locally, before a network call."""


def json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


@lru_cache(maxsize=32)
def _tokenizer(model_name: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def estimate_tokens(value, model_name: str = "") -> tuple[int, str]:
    """Count complete JSON content using a GPT tokenizer or a conservative fallback.

    JSON punctuation and message metadata deliberately contribute to the estimate.
    Unknown provider families use one UTF-8 byte per token rather than assuming
    they share OpenAI's tokenizer. No provider request is made to count tokens.
    """

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    name = model_name.casefold()
    if name.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        encoding = _tokenizer(model_name)
        return len(encoding.encode(text, disallowed_special=())) + 32, f"tiktoken:{encoding.name}"
    return len(text.encode("utf-8")) + 32, "utf8-bytes-conservative"


@dataclass(frozen=True)
class RequestLimits:
    max_input_bytes: int = 16 * 1024 * 1024
    context_window_tokens: int | None = None
    output_token_reserve: int | None = None
    safety_tokens: int = 2048

    def __post_init__(self):
        for key, value in asdict(self).items():
            if value is None and key in {"context_window_tokens", "output_token_reserve"}:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"request_limits.{key} must be a positive integer")
        if (
            self.context_window_tokens is not None
            and self.output_token_reserve is not None
            and self.output_token_reserve + self.safety_tokens >= self.context_window_tokens
        ):
            raise ValueError("context budget leaves no room for input")

    def check(
        self, size: int, output_tokens: int = 8192, *, input_tokens: int | None = None
    ) -> None:
        if size > self.max_input_bytes:
            raise RequestBudgetError(f"input {size} bytes exceeds limit {self.max_input_bytes}")
        # Callers with the full content use a model-specific count. Size remains
        # the conservative fallback for legacy callers with only byte totals.
        reserve = self.output_token_reserve or output_tokens
        if (
            self.context_window_tokens is not None
            and (size if input_tokens is None else input_tokens) + reserve + self.safety_tokens
            > self.context_window_tokens
        ):
            raise RequestBudgetError("input estimate plus output reserve exceeds context budget")

    def half(self):
        return replace(self, max_input_bytes=max(1, self.max_input_bytes // 2))


def check_provider_payload(request, params: dict) -> None:
    """Adapter additions (native schemas/tools/extra body) are included before sending."""
    raw = request.metadata.get("request_limits")
    if raw:
        limits = RequestLimits(**json.loads(raw))
        tokens, _ = estimate_tokens(params, params.get("model", request.model or ""))
        limits.check(
            json_bytes(params),
            params.get("max_tokens", params.get("max_completion_tokens", 8192)),
            input_tokens=tokens,
        )
