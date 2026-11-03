"""Tests for the provider-neutral LLM client layer.

Run from the ``rsc/`` directory::

    ../.venv/bin/python -m unittest discover -s tests -v

The tests use fake SDK clients, so no network access and no real API key are
required.  ``jsonschema``-dependent assertions are skipped when the optional
validator is not installed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsc.config import ConfigError, LLMConfig, ProviderConfig, load_llm_config
from rsc.llm import (
    AnthropicClient,
    BaseLLMClient,
    CachingLLMClient,
    LLMAuthError,
    LLMCache,
    LLMContextLengthError,
    LLMRequest,
    LLMResponse,
    LLMStructuredOutputError,
    Message,
    MockLLMClient,
    OpenAIClient,
    StructuredOutputUnsupported,
    Usage,
    UsageTracker,
    create_client,
    create_client_for_stage,
    parse_json_object,
    validate_against_schema,
)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def provider(api: str = "anthropic", **overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "name": "p",
        "api": api,
        "model": "m",
        "api_key": "k",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


class FakeMessages:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAnthropic:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(response, error)


def anthropic_response(blocks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=3,
        ),
        stop_reason="tool_use",
        id="req_a",
        model="claude-test",
    )


class FakeCompletions:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeOpenAI:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(response, error))


def openai_response(
    *,
    content: object = None,
    tool_calls: list[object] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=2),
    )
    return SimpleNamespace(choices=[choice], usage=usage, id="req_o", model="gpt-test")


class ConfigTests(unittest.TestCase):
    def test_resolve_api_key_from_env(self) -> None:
        config = provider(api_key=None, api_key_env="MY_KEY")
        self.assertEqual(config.resolve_api_key({"MY_KEY": "secret"}), "secret")

    def test_resolve_api_key_with_expansion(self) -> None:
        config = provider(api_key="${MY_KEY}", api_key_env=None)
        self.assertEqual(config.resolve_api_key({"MY_KEY": "secret"}), "secret")

    def test_missing_api_key_raises(self) -> None:
        config = provider(api_key=None, api_key_env="MISSING")
        with self.assertRaises(ConfigError):
            config.resolve_api_key({})

    def test_invalid_api_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            provider(api="unknown")

    def test_load_and_stage_override(self) -> None:
        raw = {
            "llm": {
                "default": "a",
                "cache_dir": "cache",
                "providers": {
                    "a": {"api": "anthropic", "model": "m1", "api_key": "k1"},
                    "b": {
                        "api": "openai",
                        "model": "m2",
                        "api_key": "k2",
                        "structured_mode": "tool",
                    },
                },
                "stages": {
                    "extract": {"provider": "b", "model": "m3", "temperature": 0.5}
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rsc.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_llm_config(path)
            self.assertEqual(config.default_provider, "a")
            self.assertEqual(config.for_stage("objects").model, "m1")
            extract = config.for_stage("extract")
            self.assertEqual(extract.model, "m3")
            self.assertEqual(extract.temperature, 0.5)
            self.assertEqual(extract.api, "openai")
            self.assertEqual(config.cache_dir, (Path(tmp) / "cache").resolve())

    def test_unknown_stage_provider(self) -> None:
        raw = {
            "default": "a",
            "providers": {"a": {"api": "anthropic", "model": "m", "api_key": "k"}},
            "stages": {"x": {"provider": "b"}},
        }
        with self.assertRaises(ConfigError):
            LLMConfig.from_dict(raw)

    def test_unknown_stage_override(self) -> None:
        raw = {
            "default": "a",
            "providers": {"a": {"api": "anthropic", "model": "m", "api_key": "k"}},
            "stages": {"x": {"bogus": 1}},
        }
        with self.assertRaises(ConfigError):
            LLMConfig.from_dict(raw)


class RequestTests(unittest.TestCase):
    def test_empty_request_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LLMRequest()

    def test_temperature_range(self) -> None:
        with self.assertRaises(ValueError):
            LLMRequest(messages=[Message("user", "hi")], temperature=3)

    def test_system_hoisting(self) -> None:
        class Dummy(BaseLLMClient):
            def complete(self, request: LLMRequest) -> LLMResponse:
                raise NotImplementedError

        client = Dummy(provider())
        system, messages = client._system_and_messages(
            LLMRequest(
                system="s",
                messages=[Message("system", "s2"), Message("user", "u")],
            )
        )
        self.assertEqual(system, "s\n\ns2")
        self.assertEqual([message.role for message in messages], ["user"])


class AnthropicTests(unittest.TestCase):
    def test_structured_tool_call(self) -> None:
        blocks = [SimpleNamespace(type="tool_use", input={"ok": True}, name="result")]
        fake = FakeAnthropic(anthropic_response(blocks))
        client = AnthropicClient(provider(), sdk_client=fake)
        result = client.complete_structured(
            LLMRequest(messages=[Message("user", "hi")]), SCHEMA
        )
        self.assertEqual(result.structured, {"ok": True})
        params = fake.messages.calls[0]
        self.assertEqual(params["tools"][0]["input_schema"], SCHEMA)  # type: ignore[index]
        self.assertEqual(params["tool_choice"], {"type": "tool", "name": "result"})
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.cache_read_tokens, 2)
        self.assertEqual(result.request_id, "req_a")

    def test_text_json_fallback(self) -> None:
        fake = FakeAnthropic(
            anthropic_response([SimpleNamespace(type="text", text='{"ok": true}')])
        )
        client = AnthropicClient(provider(), sdk_client=fake)
        result = client.complete_structured(
            LLMRequest(messages=[Message("user", "hi")]), SCHEMA
        )
        self.assertEqual(result.structured, {"ok": True})

    def test_missing_structured_raises(self) -> None:
        fake = FakeAnthropic(
            anthropic_response([SimpleNamespace(type="text", text="no json")])
        )
        client = AnthropicClient(provider(), sdk_client=fake)
        with self.assertRaises(LLMStructuredOutputError):
            client.complete_structured(
                LLMRequest(messages=[Message("user", "hi")]), SCHEMA
            )

    def test_system_and_stop(self) -> None:
        fake = FakeAnthropic(anthropic_response([SimpleNamespace(type="text", text="ok")]))
        client = AnthropicClient(provider(), sdk_client=fake)
        client.complete(
            LLMRequest(
                system="sys",
                messages=[Message("user", "hi")],
                stop=["END"],
            )
        )
        params = fake.messages.calls[0]
        self.assertEqual(params["system"], "sys")
        self.assertEqual(params["stop_sequences"], ["END"])

    def test_error_mapping(self) -> None:
        class AuthenticationError(Exception):
            pass

        client = AnthropicClient(provider(), sdk_client=SimpleNamespace())
        client._sdk = SimpleNamespace(AuthenticationError=AuthenticationError)
        mapped = client._map_error(AuthenticationError("bad key"))
        self.assertIsInstance(mapped, LLMAuthError)


class OpenAITests(unittest.TestCase):
    def test_json_schema_mode(self) -> None:
        fake = FakeOpenAI(openai_response(content='{"ok": true}'))
        client = OpenAIClient(
            provider("openai", structured_mode="json_schema"), sdk_client=fake
        )
        result = client.complete_structured(
            LLMRequest(messages=[Message("user", "hi")]), SCHEMA
        )
        params = fake.chat.completions.calls[0]
        self.assertEqual(params["response_format"]["type"], "json_schema")  # type: ignore[index]
        self.assertEqual(
            params["response_format"]["json_schema"]["schema"],  # type: ignore[index]
            SCHEMA,
        )
        self.assertTrue(
            params["response_format"]["json_schema"]["strict"]  # type: ignore[index]
        )
        self.assertEqual(result.structured, {"ok": True})

    def test_json_object_mode_adds_instruction(self) -> None:
        fake = FakeOpenAI(openai_response(content='{"ok": true}'))
        client = OpenAIClient(
            provider("openai", structured_mode="json_object"), sdk_client=fake
        )
        client.complete_structured(LLMRequest(messages=[Message("user", "hi")]), SCHEMA)
        params = fake.chat.completions.calls[0]
        self.assertEqual(params["response_format"], {"type": "json_object"})
        self.assertEqual(params["messages"][0]["role"], "system")  # type: ignore[index]
        self.assertIn("JSON", params["messages"][0]["content"])  # type: ignore[index]

    def test_tool_mode(self) -> None:
        tool_call = SimpleNamespace(function=SimpleNamespace(arguments='{"ok": true}'))
        fake = FakeOpenAI(openai_response(content=None, tool_calls=[tool_call]))
        client = OpenAIClient(
            provider("openai", structured_mode="tool"), sdk_client=fake
        )
        result = client.complete_structured(
            LLMRequest(messages=[Message("user", "hi")]), SCHEMA
        )
        params = fake.chat.completions.calls[0]
        self.assertEqual(params["tools"][0]["function"]["parameters"], SCHEMA)  # type: ignore[index]
        self.assertEqual(
            params["tool_choice"]["function"]["name"], "result"  # type: ignore[index]
        )
        self.assertEqual(result.structured, {"ok": True})

    def test_auto_fallback(self) -> None:
        class FallbackClient(OpenAIClient):
            def __init__(self, config: ProviderConfig) -> None:
                super().__init__(config, sdk_client=SimpleNamespace())
                self.modes: list[str] = []

            def _complete_once(
                self, request: LLMRequest, mode: str | None, started: float
            ) -> LLMResponse:
                self.modes.append(str(mode))
                if mode != "tool":
                    raise StructuredOutputUnsupported(str(mode))
                return LLMResponse(
                    text="",
                    structured={"ok": True},
                    provider="p",
                    model="m",
                )

        client = FallbackClient(provider("openai", structured_mode="auto"))
        result = client.complete_structured(
            LLMRequest(messages=[Message("user", "hi")]), SCHEMA
        )
        self.assertEqual(client.modes, ["json_schema", "json_object", "tool"])
        self.assertEqual(result.structured, {"ok": True})

    def test_max_completion_tokens(self) -> None:
        fake = FakeOpenAI(openai_response(content='{"ok": true}'))
        client = OpenAIClient(
            provider(
                "openai",
                structured_mode="json_schema",
                max_tokens_param="max_completion_tokens",
            ),
            sdk_client=fake,
        )
        client.complete_structured(LLMRequest(messages=[Message("user", "hi")]), SCHEMA)
        params = fake.chat.completions.calls[0]
        self.assertIn("max_completion_tokens", params)
        self.assertNotIn("max_tokens", params)

    def test_error_mapping(self) -> None:
        class BadRequestError(Exception):
            pass

        client = OpenAIClient(provider("openai"), sdk_client=SimpleNamespace())
        client._sdk = SimpleNamespace(BadRequestError=BadRequestError)
        unsupported = client._map_error(
            BadRequestError("response_format is not supported")
        )
        self.assertIsInstance(unsupported, StructuredOutputUnsupported)
        context = client._map_error(BadRequestError("maximum context length exceeded"))
        self.assertIsInstance(context, LLMContextLengthError)


class MockAndCacheTests(unittest.TestCase):
    def test_mock_queue(self) -> None:
        client = MockLLMClient(
            [{"ok": True}, lambda request: LLMResponse(text="second")]
        )
        first = client.complete(LLMRequest(messages=[Message("user", "a")]))
        second = client.complete(LLMRequest(messages=[Message("user", "b")]))
        self.assertEqual(first.structured, {"ok": True})
        self.assertEqual(second.text, "second")
        self.assertEqual(len(client.requests), 2)

    def test_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = LLMCache(tmp)
            response = LLMResponse(
                text="hi",
                structured={"ok": True},
                usage=Usage(1, 2, 3, 4),
                model="m",
                provider="p",
                finish_reason="stop",
                request_id="r",
            )
            key = cache.key({"a": 1})
            cache.put(key, response)
            loaded = cache.get(key)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.text, "hi")
            self.assertEqual(loaded.usage.cache_write_tokens, 4)
            self.assertTrue(loaded.cached)

    def test_caching_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inner = MockLLMClient(
                [
                    LLMResponse(
                        text="x",
                        structured={"ok": True},
                        provider="mock",
                        model="mock",
                    )
                ]
            )
            client = CachingLLMClient(inner, LLMCache(tmp))
            request = LLMRequest(messages=[Message("user", "hi")])
            first = client.complete(request)
            second = client.complete(request)
            self.assertEqual(len(inner.requests), 1)
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)


class UsageTests(unittest.TestCase):
    def test_usage_tracker(self) -> None:
        tracker = UsageTracker()
        tracker.record(LLMResponse(usage=Usage(10, 5, 1, 2)))
        tracker.record(LLMResponse(usage=Usage(1, 1), cached=True))
        self.assertEqual(tracker.calls, 2)
        self.assertEqual(tracker.cached_calls, 1)
        self.assertEqual(tracker.total_tokens, 17)
        self.assertEqual(tracker.to_dict()["cache_read_tokens"], 1)


class ParsingTests(unittest.TestCase):
    def test_parse_json_object(self) -> None:
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_object('prefix {"a": 1} suffix'), {"a": 1})
        self.assertIsNone(parse_json_object("no json here"))

    def test_validate_schema(self) -> None:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")
        with self.assertRaises(LLMStructuredOutputError):
            validate_against_schema({"ok": "yes"}, SCHEMA)


class FactoryTests(unittest.TestCase):
    def test_dispatch(self) -> None:
        class DummyAnthropic(BaseLLMClient):
            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse()

        class DummyOpenAI(BaseLLMClient):
            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse()

        with patch("rsc.llm.factory.AnthropicClient", DummyAnthropic):
            self.assertIsInstance(create_client(provider("anthropic")), DummyAnthropic)
        with patch("rsc.llm.factory.OpenAIClient", DummyOpenAI):
            self.assertIsInstance(create_client(provider("openai")), DummyOpenAI)

    def test_stage_override(self) -> None:
        class DummyOpenAI(BaseLLMClient):
            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse()

        config = LLMConfig(
            default_provider="a",
            providers={"a": provider("anthropic"), "o": provider("openai")},
            stages={"extract": {"provider": "o"}},
        )
        with patch("rsc.llm.factory.OpenAIClient", DummyOpenAI):
            client = create_client_for_stage(config, "extract")
            self.assertIsInstance(client, DummyOpenAI)


if __name__ == "__main__":
    unittest.main()
