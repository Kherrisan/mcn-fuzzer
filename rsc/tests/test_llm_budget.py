"""Final payload limits remain enforced for the standalone LLM check adapters."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rsc.config import ProviderConfig
from rsc.llm.budget import RequestBudgetError, RequestLimits, estimate_tokens
from rsc.llm.errors import LLMContextLengthError, map_provider_error
from rsc.llm.openai_client import OpenAIClient
from rsc.llm.types import LLMRequest, Message


def test_exact_provider_payload_budget_and_context_error_mapping():
    create = Mock()
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAIClient(
        ProviderConfig(name="test", api="openai", model="test", api_key="x"), sdk_client=sdk
    )
    request = LLMRequest(
        messages=[Message("user", "hello")],
        response_schema={"type": "object", "description": "x" * 2000},
        metadata={"request_limits": json.dumps({"max_input_bytes": 500})},
    )
    with pytest.raises(RequestBudgetError):
        client.complete(request)
    create.assert_not_called()

    class BadRequest(Exception):
        pass

    error = map_provider_error(
        BadRequest("Your input exceeds the context window of this model"),
        SimpleNamespace(BadRequestError=BadRequest),
        "openai",
    )
    assert isinstance(error, LLMContextLengthError)


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_adapters_preserve_truncation_and_guard_final_payload(api):
    from rsc.llm.anthropic_client import AnthropicClient

    config = ProviderConfig(name="test", api=api, model="test", api_key="x")
    if api == "openai":
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content='{"partial":')
                )
            ]
        )
        create = Mock(return_value=response)
        client = OpenAIClient(
            config,
            sdk_client=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create))
            ),
        )
    else:
        response = SimpleNamespace(
            stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"partial":')]
        )
        create = Mock(return_value=response)
        client = AnthropicClient(
            config, sdk_client=SimpleNamespace(messages=SimpleNamespace(create=create))
        )
    request = LLMRequest(messages=[Message("user", "hello")], response_schema={"type": "object"})
    result = client.complete(request)
    assert result.finish_reason in {"length", "max_tokens"} and result.structured is None
    create.reset_mock()
    request.metadata["request_limits"] = json.dumps({"max_input_bytes": 32})
    with pytest.raises(RequestBudgetError):
        client.complete(request)
    create.assert_not_called()


def test_gpt_tokenizer_and_unknown_provider_fallback_keep_byte_limits_separate():
    payload = {"messages": [{"role": "user", "content": "known text " * 1000}]}
    tokens, method = estimate_tokens(payload, "gpt-5.6-luna")
    byte_estimate, fallback_method = estimate_tokens(payload, "claude-test")
    assert method == "tiktoken:o200k_base"
    assert fallback_method == "utf8-bytes-conservative" and byte_estimate > tokens
    limits = RequestLimits(context_window_tokens=tokens + 1000, safety_tokens=64)
    limits.check(byte_estimate, 128, input_tokens=tokens)
    with pytest.raises(RequestBudgetError, match="context budget"):
        limits.check(byte_estimate, 128, input_tokens=byte_estimate)
    with pytest.raises(RequestBudgetError, match="bytes exceeds"):
        RequestLimits(max_input_bytes=32).check(100, input_tokens=10)


def test_output_reserve_and_safety_are_included_in_token_budget():
    limits = RequestLimits(context_window_tokens=1000, output_token_reserve=256, safety_tokens=64)
    limits.check(10000, 1, input_tokens=680)
    with pytest.raises(RequestBudgetError, match="context budget"):
        limits.check(10000, 1, input_tokens=681)
