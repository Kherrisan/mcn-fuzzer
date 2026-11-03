"""Error taxonomy for the RSC LLM client layer."""

from __future__ import annotations

from importlib import import_module
from typing import Any


class LLMError(Exception):
    """Base class for every error raised by the LLM client layer."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class LLMConfigError(LLMError):
    """Invalid provider configuration or a missing SDK dependency."""


class LLMAuthError(LLMError):
    """Authentication or authorization failed."""


class LLMRateLimitError(LLMError):
    """The provider rate-limited the request."""


class LLMTimeoutError(LLMError):
    """The request timed out."""


class LLMConnectionError(LLMError):
    """The provider could not be reached."""


class LLMContextLengthError(LLMError):
    """The prompt exceeded the provider's context window."""


class LLMProviderError(LLMError):
    """Any other provider-side or API error."""


class LLMStructuredOutputError(LLMError):
    """The provider did not return a JSON object matching the requested schema."""


class StructuredOutputUnsupported(LLMError):
    """Internal: the provider rejected the requested structured-output mode.

    The OpenAI adapter uses this to fall back between ``json_schema``,
    ``json_object`` and tool-calling modes.  It is not meant to escape the
    client layer.
    """


def map_provider_error(
    error: Exception, sdk: Any, api: str, structured_markers: tuple[str, ...] = (),
) -> LLMError:
    """Map the SDKs' common exception taxonomy, preserving response metadata."""
    if isinstance(error, LLMError):
        return error
    if sdk is None:
        try:
            sdk = import_module(api)
        except ImportError:  # pragma: no cover - fake clients without an installed SDK
            return LLMProviderError(str(error))
    metadata = {
        "status_code": getattr(error, "status_code", None),
        "request_id": getattr(error, "request_id", None),
    }
    for name, mapped in (
        ("AuthenticationError", LLMAuthError),
        ("RateLimitError", LLMRateLimitError),
        ("APITimeoutError", LLMTimeoutError),
        ("APIConnectionError", LLMConnectionError),
    ):
        error_type = getattr(sdk, name, None)
        if error_type is not None and isinstance(error, error_type):
            return mapped(str(error), **metadata)
    bad_request = getattr(sdk, "BadRequestError", None)
    if bad_request is not None and isinstance(error, bad_request):
        message = str(error).lower()
        if any(marker in message for marker in (
            "context length", "maximum context", "too many tokens",
            "exceeds the context window", "context_window_exceeded",
            "prompt is too long", "request is too large",
        )):
            return LLMContextLengthError(str(error), **metadata)
        if any(marker in message for marker in structured_markers):
            return StructuredOutputUnsupported(str(error), **metadata)
    return LLMProviderError(str(error), **metadata)
