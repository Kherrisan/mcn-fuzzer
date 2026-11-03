"""Provider-neutral LLM client layer for RSC."""

from __future__ import annotations

from .anthropic_client import AnthropicClient
from .base import BaseLLMClient
from .cache import CachingLLMClient, LLMCache
from .errors import (
    LLMAuthError,
    LLMConfigError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    StructuredOutputUnsupported,
)
from .factory import create_client, create_client_for_stage
from .mock import MockLLMClient
from .openai_client import OpenAIClient
from .types import (
    LLMRequest,
    LLMResponse,
    Message,
    Usage,
    parse_json_object,
    validate_against_schema,
)
from .usage import UsageTracker

__all__ = [
    "AnthropicClient",
    "BaseLLMClient",
    "CachingLLMClient",
    "LLMAuthError",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMContextLengthError",
    "LLMError",
    "LLMCache",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "Message",
    "MockLLMClient",
    "OpenAIClient",
    "StructuredOutputUnsupported",
    "Usage",
    "UsageTracker",
    "create_client",
    "create_client_for_stage",
    "parse_json_object",
    "validate_against_schema",
]
