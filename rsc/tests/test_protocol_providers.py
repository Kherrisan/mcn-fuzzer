"""Provider construction is lazy, tool-capable, and free of network requests."""

import asyncio
import json

import pytest
from pydantic_ai.models.function import FunctionModel

from rsc.config import ConfigError, LLMConfig, ProviderConfig
from rsc.protocol.providers import ModelFactory, adapt_client


def test_async_factory_preserves_stage_models_endpoints_and_token_parameters():
    async def run():
        providers = {
            "openai": ProviderConfig(
                name="openai",
                api="openai",
                model="luna",
                api_key="unit-test-key",
                base_url="http://localhost:8080/v1",
                max_tokens_param="max_tokens",
                max_retries=2,
            ),
            "anthropic": ProviderConfig(
                name="anthropic",
                api="anthropic",
                model="claude-test",
                api_key="unit-test-key",
                base_url="http://localhost:8081",
            ),
        }
        factory = ModelFactory(
            LLMConfig(
                "openai",
                providers,
                stages={"interactions": {"provider": "anthropic"}},
            )
        )
        assert not factory._clients
        objects = factory("objects", "objects")
        assert objects.model_name == "luna"
        assert objects.retry_limit == 2
        assert not objects.wrapped.profile["openai_chat_supports_max_completion_tokens"]
        assert objects.wrapped.client.max_retries == 0
        assert factory("objects", "objects") is objects
        interactions = factory("interactions", "interactions")
        assert interactions.model_name == "claude-test"
        assert "localhost:8081" in interactions.base_url
        assert interactions.wrapped.client.max_retries == 0
        assert "unit-test-key" not in json.dumps(objects.cache_identity)
        assert len(factory._clients) == 2
        await factory.aclose()
        assert not factory._clients

    asyncio.run(run())


def test_text_only_legacy_client_is_rejected_without_calling_it():
    class TextClient:
        def complete(self, request):
            pytest.fail("legacy text completion must not be called")

    with pytest.raises(ConfigError, match="tool-capable"):
        adapt_client(TextClient())
    model = FunctionModel(lambda messages, info: None)
    assert adapt_client(model) is model

    class Adapter:
        def as_pydantic_model(self):
            return model

    assert adapt_client(Adapter()) is model
