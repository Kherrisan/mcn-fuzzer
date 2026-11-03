"""Exact SDK body checks using local HTTP transports, without network or paid calls."""

import asyncio
import json

import httpx2
import pytest

from rsc.config import LLMConfig, ProviderConfig
from rsc.llm.budget import RequestBudgetError, RequestLimits
from rsc.protocol.agent_runtime import EventWriter, run_agent
from rsc.protocol.models import StrictModel
from rsc.protocol.options import ContextOptions
from rsc.protocol.providers import ModelFactory
from rsc.protocol.sessions import SessionState
from rsc.protocol.tools import TOOL_NAMES, make_tools
from rsc.protocol.wire import CURRENT_WIRE_REQUEST, WireRequest, check_wire_request


class Result(StrictModel):
    name: str


def factory(api, transport):
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
    provider = ProviderConfig(
        name=api,
        api=api,
        model="claude-test" if api == "anthropic" else "luna",
        api_key="must-not-appear-in-audit",
        base_url="https://unit.test/v1",
        extra_client_kwargs={"http_client": client},
        max_retries=3,
    )
    return ModelFactory(LLMConfig(api, {api: provider}))


def provider_response(request, api, data=None):
    data = {"name": "valid"} if data is None else data
    if api == "anthropic":
        return httpx2.Response(
            200,
            json={
                "id": "msg-local",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": json.dumps(data)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 0,
            "model": "luna",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(data),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_final_sdk_payload_is_measured_and_credentials_are_not_audited(api):
    async def run():
        requests = []

        async def transport(request):
            requests.append(request)
            return provider_response(request, api)

        models = factory(api, transport)
        events = EventWriter()
        delivered = []
        try:
            result = await run_agent(
                models("objects", "objects"),
                Result,
                {
                    "phase": "objects",
                    "focus_block": {
                        "doc_id": "rfc-test",
                        "block_id": "request-definition",
                        "block_type": "paragraph",
                        "text": "A request 请求\n" * 500,
                    },
                },
                stage="objects",
                phase="objects",
                instructions="Return a name.",
                tools=make_tools(),
                events=events,
                limits=RequestLimits(max_input_bytes=65536),
                before_request=lambda messages, record: delivered.append(record["request_index"]),
            )
            assert result["status"] == "ok", result
            assert len(requests) == 1 and delivered == [1]
            wire = events.events["provider-requests.jsonl"][0]
            assert wire["called"] and wire["error"] is None
            assert wire["request_bytes"] == len(requests[0].content)
            assert wire["payload"] == json.loads(requests[0].content)
            payload = wire["payload"]
            names = {
                tool["name"] if api == "anthropic" else tool["function"]["name"]
                for tool in payload["tools"]
            }
            assert names == TOOL_NAMES and len(names) == 7
            assert not names & {"validate_proposal", "final_result"}
            # The complete output schema appears once, in instructions, never as a tool.
            instructions = (
                "\n".join(part["text"] for part in payload["system"])
                if api == "anthropic"
                else "\n".join(
                    message["content"]
                    for message in payload["messages"]
                    if message["role"] in {"system", "developer"}
                )
            )
            assert instructions.count('"title": "Result"') == 1
            assert result["data"] == {"name": "valid"}
            assert result["tool_call_count"] == 0
            prompts = events.events["user-messages.jsonl"]
            assert len(prompts) == 1
            assert prompts[0]["run_id"] == wire["run_id"]
            assert prompts[0]["request_index"] == wire["request_index"]
            assert prompts[0]["model"] == wire["payload"]["model"]
            assert prompts[0]["user_messages"] == [
                {"message_index": index, "message": message}
                for index, message in enumerate(json.loads(requests[0].content)["messages"])
                if message["role"] == "user"
            ]
            assert "must-not-appear-in-audit" not in json.dumps(events.events)
        finally:
            await models.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_actual_sdk_preserves_tool_returns_and_session_history_across_phase_handoff(api):
    class InteractionResult(StrictModel):
        step: str

    async def run():
        requests = []
        tool_calls = []

        async def transport(request):
            requests.append(json.loads(request.content))
            if len(requests) > 1:
                data = {"name": "OBJECT_DEFINED"} if len(requests) == 2 else {"step": "SEND"}
                return provider_response(request, api, data)
            if api == "anthropic":
                return httpx2.Response(
                    200,
                    json={
                        "id": "msg-tool",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [
                            {"type": "tool_use", "id": "read-1", "name": "read_source", "input": {}}
                        ],
                        "stop_reason": "tool_use",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-tool",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "luna",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "read-1",
                                        "type": "function",
                                        "function": {"name": "read_source", "arguments": "{}"},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        def read_source() -> dict:
            """Read the requested source context for the focal block."""
            tool_calls.append(True)
            return {"text": "ACTUAL_RETRIEVED_SOURCE"}

        models = factory(api, transport)
        events = EventWriter()
        session = SessionState(
            "list-0", context_options=ContextOptions(max_tokens=100000), events=events
        )
        try:
            first = await run_agent(
                models("objects", "objects"),
                Result,
                {
                    "phase": "objects",
                    "focus_block": {
                        "doc_id": "rfc",
                        "block_id": "1",
                        "block_type": "paragraph",
                        "text": "OBJECT_SOURCE_FOCUS",
                    },
                },
                stage="objects",
                phase="objects",
                instructions="Read then extract an object.",
                tools=[read_source],
                events=events,
                limits=RequestLimits(),
                session=session,
            )
            assert first["status"] == "ok", first
            assert first["tool_call_count"] == 1 and len(requests) == 2
            session.handoff(
                "interactions",
                {"parties": [{"name": "LATEST_PARTY", "description": "sender"}]},
                [],
                4,
            )
            second = await run_agent(
                models("interactions", "interactions"),
                InteractionResult,
                {
                    "phase": "interactions",
                    "focus_block": {
                        "doc_id": "rfc",
                        "block_id": "2",
                        "block_type": "paragraph",
                        "text": "INTERACTION_SOURCE_FOCUS",
                    },
                },
                stage="interactions",
                phase="interactions",
                instructions="Extract an interaction.",
                tools=[read_source],
                events=events,
                limits=RequestLimits(),
                session=session,
            )
            assert second["status"] == "ok", second
            assert second["data"] == {"step": "SEND"}
            assert len(requests) == 3 and len(tool_calls) == 1
            latest = json.dumps(requests[-1])
            for retained in (
                "OBJECT_SOURCE_FOCUS",
                "ACTUAL_RETRIEVED_SOURCE",
                "OBJECT_DEFINED",
                "LATEST_PARTY",
                "INTERACTION_SOURCE_FOCUS",
                "supersedes",
            ):
                assert retained in latest
            if api == "anthropic":
                current_instructions = "\n".join(part["text"] for part in requests[-1]["system"])
            else:
                current_instructions = "\n".join(
                    message["content"]
                    for message in requests[-1]["messages"]
                    if message["role"] in {"system", "developer"}
                )
            assert '"title": "InteractionResult"' in current_instructions
            assert '"title": "Result"' not in current_instructions
            wire = events.events["provider-requests.jsonl"]
            assert len(wire) == 3 and all(row["called"] for row in wire)
            assert wire[-1]["payload"] == requests[-1]
        finally:
            await models.aclose()

    asyncio.run(run())


def test_provider_added_body_is_checked_before_transport_and_local_failure_is_not_retried():
    async def run():
        sent = []

        async def transport(request):
            sent.append(request)
            return provider_response(request, "openai")

        models = factory("openai", transport)
        model = models("objects", "objects")
        create = model.wrapped.client.chat.completions.create

        async def extended(**kwargs):
            # Simulate a provider adapter adding data after the framework payload is built.
            kwargs["extra_body"] = {"provider_extension": "x" * 50000}
            return await create(**kwargs)

        model.wrapped.client.chat.completions.create = extended
        events = EventWriter()
        delivered = []

        async def execute(label, limit):
            return await run_agent(
                model,
                Result,
                {},
                stage="objects",
                phase="objects",
                instructions="Return a name.",
                events=events,
                limits=RequestLimits(max_input_bytes=limit),
                run_id=label,
                before_request=lambda messages, record: delivered.append(label),
            )

        try:
            small, large = await asyncio.gather(
                execute("small", 16384),
                execute("large", 131072),
            )
            assert small["end_reason"] == "request_budget", small
            assert small["calls"] == 0 and small["skipped"] == 1 and small["failures"] == 0
            assert small["request_count"] == 1
            assert large["status"] == "ok" and large["calls"] == 1
            assert len(sent) == 1 and delivered == ["large"]
            wire = {row["run_id"]: row for row in events.events["provider-requests.jsonl"]}
            assert not wire["small"]["called"] and wire["large"]["called"]
            assert wire["small"]["request_bytes"] > 50000
            requests = {row["run_id"]: row for row in events.events["model-requests.jsonl"]}
            assert not requests["small"]["called"] and requests["large"]["called"]
            assert [row["run_id"] for row in events.events["user-messages.jsonl"]] == ["large"]
        finally:
            await models.aclose()

    asyncio.run(run())


def test_final_wire_token_guard_counts_provider_additions_independently_of_byte_cap():
    async def run():
        events = EventWriter()
        delivered = []
        scope = WireRequest(
            limits=RequestLimits(
                max_input_bytes=1_000_000, context_window_tokens=2000, safety_tokens=64
            ),
            events=events,
            run_id="token-wire",
            request_index=1,
            output_tokens=128,
            delivered=lambda: delivered.append(True),
            model_name="gpt-5.6-luna",
        )
        request = httpx2.Request(
            "POST",
            "https://unit.test/v1/chat/completions",
            json={
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "small original input"}],
                "max_tokens": 128,
                "provider_extension": "variable token values " * 4000,
            },
        )
        token = CURRENT_WIRE_REQUEST.set(scope)
        try:
            with pytest.raises(RequestBudgetError, match="context budget"):
                await check_wire_request(request)
        finally:
            CURRENT_WIRE_REQUEST.reset(token)
        assert not delivered and isinstance(scope.error, RequestBudgetError)
        row = events.events["provider-requests.jsonl"][0]
        assert not row["called"] and row["request_bytes"] < 1_000_000
        assert row["input_token_estimate"] > 2000
        assert row["counting_method"] == "tiktoken:o200k_base"

    asyncio.run(run())
