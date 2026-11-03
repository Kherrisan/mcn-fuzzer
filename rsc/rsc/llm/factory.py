"""Factory helpers for constructing provider clients."""

from __future__ import annotations

from rsc.config import LLMConfig, ProviderConfig

from .anthropic_client import AnthropicClient
from .base import BaseLLMClient
from .cache import CachingLLMClient, LLMCache
from .errors import LLMConfigError
from .openai_client import OpenAIClient


def create_client(
    config: ProviderConfig,
    *,
    cache: LLMCache | None = None,
) -> BaseLLMClient:
    """Build the adapter selected by ``config.api``.

    Importing this module does not import either provider SDK; the SDK is
    imported lazily when a real client is constructed.  Tests can pass
    ``sdk_client`` directly to the adapter classes.
    """

    if config.api == "anthropic":
        client: BaseLLMClient = AnthropicClient(config)
    elif config.api == "openai":
        client = OpenAIClient(config)
    else:  # pragma: no cover - ProviderConfig validates this
        raise LLMConfigError(
            f"provider {config.name!r}: unsupported api {config.api!r}"
        )
    if cache is not None:
        return CachingLLMClient(client, cache)
    return client


def create_client_for_stage(
    config: LLMConfig,
    stage: str,
    *,
    cache: LLMCache | None = None,
) -> BaseLLMClient:
    """Build the client for a named pipeline stage, applying overrides."""

    return create_client(config.for_stage(stage), cache=cache)
