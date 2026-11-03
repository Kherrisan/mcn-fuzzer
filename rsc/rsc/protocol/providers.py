"""Async PydanticAI models using the existing RSC provider and stage configuration."""

from __future__ import annotations

from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from rsc.config import ConfigError, LLMConfig, ProviderConfig

from .wire import install_wire_check


class ConfiguredModel(WrapperModel):
    """Expose non-secret transport identity and an explicit outer retry limit."""

    def __init__(self, wrapped: Model, config: ProviderConfig) -> None:
        super().__init__(wrapped)
        self.retry_limit = config.max_retries
        self.wire_guard_enabled = True
        self.cache_identity = {
            "provider": config.name,
            "api": config.api,
            "endpoint": self.base_url,
            "model": config.model,
            "max_tokens_param": config.max_tokens_param,
        }


class ModelFactory:
    """Lazily create models; disabled stages never construct a provider client."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._models: dict[tuple[str, str], ConfiguredModel] = {}
        self._clients: list[Any] = []

    def __call__(self, stage: str, phase: str) -> Model:
        key = stage, phase
        if key in self._models:
            return self._models[key]
        qualified = f"{phase}_{stage}"
        selected = qualified if qualified in self.config.stages else stage
        config = self.config.for_stage(selected)
        kwargs = dict(config.extra_client_kwargs)
        protected = {"api_key", "base_url", "timeout", "max_retries", "default_headers"}
        if protected & kwargs.keys():
            raise ConfigError(
                "extra_client_kwargs cannot override credentials, endpoint, timeout, "
                "retries, or headers"
            )
        kwargs.update(
            api_key=config.resolve_api_key(),
            timeout=config.timeout_s,
            max_retries=0,
            default_headers=config.default_headers,
        )
        if config.base_url is not None:
            kwargs["base_url"] = config.base_url
        if "http_client" not in kwargs:
            kwargs["http_client"] = httpx2.AsyncClient(timeout=config.timeout_s)
        if not isinstance(kwargs["http_client"], httpx2.AsyncClient):
            raise ConfigError("Agent providers require an httpx2.AsyncClient for final-body checks")
        install_wire_check(kwargs["http_client"])
        settings = {"max_tokens": config.max_tokens}
        if config.temperature is not None:
            settings["temperature"] = config.temperature
        if config.api == "openai":
            client = AsyncOpenAI(**kwargs)
            provider = OpenAIProvider(openai_client=client)
            model = OpenAIChatModel(
                config.model,
                provider=provider,
                settings=settings,
                profile={
                    "supports_tools": True,
                    "openai_chat_supports_max_completion_tokens": config.max_tokens_param
                    == "max_completion_tokens",
                },
            )
        elif config.api == "anthropic":
            client = AsyncAnthropic(**kwargs)
            provider = AnthropicProvider(anthropic_client=client)
            model = AnthropicModel(config.model, provider=provider, settings=settings)
        else:
            raise ConfigError(f"unsupported Agent API: {config.api}")
        self._clients.append(client)
        wrapped = ConfiguredModel(model, config)
        self._models[key] = wrapped
        return wrapped

    async def aclose(self) -> None:
        """Close only clients created by this factory."""

        clients, self._clients = self._clients, []
        for client in clients:
            await client.close()
        self._models.clear()


def adapt_client(client) -> Model:
    """Accept full Model adapters, rejecting text-only legacy completion clients."""

    if isinstance(client, Model):
        return client
    factory = getattr(client, "as_pydantic_model", None)
    if factory is not None:
        model = factory()
        if isinstance(model, Model):
            return model
    raise ConfigError(
        "client_factory requires a tool-capable PydanticAI Model or as_pydantic_model() adapter; "
        "text-only completion clients cannot run Agent tools"
    )
