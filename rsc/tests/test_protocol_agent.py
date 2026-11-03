"""Actual PydanticAI tool loops with local models, cache, and request limits."""

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from rsc.llm.budget import RequestLimits
from rsc.protocol.agent_runtime import EventWriter, RequestCache, RunBudget, run_agent
from rsc.protocol.models import StrictModel
from rsc.protocol.options import AgentOptions


class Result(StrictModel):
    name: str


def test_real_tool_loop_request_cache_and_changed_tool_results(tmp_path):
    async def run():
        calls = []
        tool_reads = []
        state = {"name": "requester"}
        events = EventWriter(tmp_path / "logs", capture=True)
        cache = RequestCache(tmp_path / "cache")

        def respond(messages, info):
            calls.append(messages)
            returned = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "lookup"
            ]
            if not returned:
                return ModelResponse([ToolCallPart("lookup", {"name": "requester"}, "lookup-1")])
            return ModelResponse([TextPart(json.dumps({"name": returned[-1].content["name"]}))])

        model = FunctionModel(respond, model_name="tool-test")

        async def execute():
            budget = RunBudget(AgentOptions())

            def lookup(name: str) -> dict:
                """Return the complete visible party definition.

                Args:
                    name: Protocol name of the party.

                Returns:
                    The current party name.
                """
                tool_reads.append(name)
                return {"name": state["name"]}

            return await run_agent(
                model,
                Result,
                {"stage": "objects"},
                stage="objects",
                phase="objects",
                instructions="Read the party using lookup, then return its name.",
                tools=[lookup],
                events=events,
                cache=cache,
                budget=budget,
                limits=RequestLimits(max_input_bytes=65536),
            )

        first = await execute()
        assert first["status"] == "ok", first
        assert first["data"] == {"name": "requester"}
        assert first["request_count"] == 2 and first["tool_call_count"] == 1
        second = await execute()
        assert second["data"] == first["data"]
        assert len(calls) == 2
        assert tool_reads == ["requester", "requester"]
        state["name"] = "request originator"
        third = await execute()
        assert third["data"]["name"] == "request originator"
        assert len(calls) == 3
        rows = events.events["model-requests.jsonl"]
        assert len(rows) == 6 and all(row["called"] for row in rows)
        assert rows[0]["tool_definitions"][0]["description"]
        assert all(row["output_schema"]["output_tools"] == [] for row in rows)
        assert all(row["request_parameters"]["output_mode"] == "prompted" for row in rows)
        assert any(
            part["part_kind"] == "tool-return"
            for message in rows[1]["messages"]
            for part in message["parts"]
        )
        assert sum(row["cached"] for row in events.events["model-responses.jsonl"]) == 3
        assert len(list((tmp_path / "cache").glob("*.json"))) == 3
        assert not list((tmp_path / "cache").glob("*.tmp"))
        assert len((tmp_path / "logs/model-requests.jsonl").read_text().splitlines()) == 6

    asyncio.run(run())


def test_request_budget_is_checked_before_the_model():
    async def run():
        calls = []

        def respond(messages, info):
            calls.append(messages)
            return ModelResponse([TextPart(json.dumps({"name": "unexpected"}))])

        events = EventWriter()
        record = await run_agent(
            FunctionModel(respond),
            Result,
            {"text": "source"},
            stage="objects",
            phase="objects",
            instructions="Extract a name.",
            events=events,
            limits=RequestLimits(max_input_bytes=1),
        )
        assert not calls
        assert record["status"] == "failed" and record["end_reason"] == "request_budget"
        assert not events.events["model-requests.jsonl"][0]["called"]
        assert "model-responses.jsonl" not in events.events

    asyncio.run(run())


@pytest.mark.parametrize(
    "initial_text",
    ["{broken", json.dumps({"name": False}), json.dumps({"name": "invalid"})],
)
def test_invalid_output_receives_a_bounded_model_retry(initial_text):
    async def run():
        calls = []

        def respond(messages, info):
            calls.append(messages)
            content = initial_text if len(calls) == 1 else json.dumps({"name": "accepted"})
            return ModelResponse([TextPart(content)])

        events = EventWriter()
        record = await run_agent(
            FunctionModel(respond),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Extract a valid name.",
            events=events,
            limits=RequestLimits(max_input_bytes=65536),
            output_check=lambda result: (
                ["Name is unsupported."] if result.name == "invalid" else []
            ),
        )
        assert record["status"] == "ok", record
        assert record["data"] == {"name": "accepted"}
        assert record["output_retry_count"] == 1 and len(calls) == 2
        assert record["tool_call_count"] == 0
        if initial_text == json.dumps({"name": "invalid"}):
            assert "Name is unsupported." in json.dumps(events.events["model-requests.jsonl"][1])

    asyncio.run(run())


def test_json_output_retries_share_the_budget_across_completion_attempts():
    async def run():
        calls = []

        def respond(messages, info):
            calls.append(messages)
            content = json.dumps({"name": "valid"}) if len(calls) == 2 else "{broken"
            return ModelResponse([TextPart(content)])

        options = AgentOptions(max_output_retries=1)
        budget = RunBudget(options)
        events = EventWriter()
        model = FunctionModel(respond)
        args = dict(
            stage="repair",
            phase="objects",
            instructions="Return a name.",
            events=events,
            budget=budget,
            limits=RequestLimits(max_input_bytes=65536),
        )
        first = await run_agent(model, Result, {}, **args)
        second = await run_agent(model, Result, {}, **args)
        assert first["status"] == "ok" and first["output_retry_count"] == 1
        assert second["status"] == "failed" and second["end_reason"] == "run_limit"
        assert second["data"] is None
        assert second["calls"] == second["skipped"] == 1
        assert len(calls) == 3 and budget.output_retries == 1
        assert not events.events["model-requests.jsonl"][-1]["called"]

    asyncio.run(run())


def test_shared_request_budget_limits_a_second_completion_attempt():
    async def run():
        calls = []

        def respond(messages, info):
            calls.append(messages)
            return ModelResponse([TextPart(json.dumps({"name": "one"}))])

        options = AgentOptions(max_model_requests=1)
        budget = RunBudget(options)
        events = EventWriter()
        model = FunctionModel(respond)
        args = dict(
            stage="repair",
            phase="objects",
            instructions="Return a name.",
            events=events,
            limits=RequestLimits(max_input_bytes=65536),
            options=options,
            budget=budget,
        )
        first = await run_agent(model, Result, {}, **args)
        second = await run_agent(model, Result, {}, **args)
        assert first["status"] == "ok"
        assert second["status"] == "failed" and second["end_reason"] == "run_limit"
        assert len(calls) == 1

    asyncio.run(run())


def test_same_cache_key_concurrent_requests_share_a_single_call(tmp_path):
    async def run():
        cache = RequestCache(tmp_path / "cache")
        calls = 0

        async def response():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return ModelResponse([ToolCallPart("result", {"name": "same"}, "id")])

        results = await asyncio.gather(*(cache.get_or_call("same-key", response) for _ in range(8)))
        assert calls == 1
        assert sum(cached for _, cached in results) == 7
        assert (
            json.loads((tmp_path / "cache/same-key.json").read_text())["parts"][0]["tool_call_id"]
            == "id"
        )

    asyncio.run(run())


def test_transport_retries_are_counted_and_audited():
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.models.wrapper import WrapperModel

    async def run():
        calls = 0

        def respond(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelHTTPError(status_code=503, model_name="test", body="retry")
            return ModelResponse([TextPart(json.dumps({"name": "recovered"}))])

        class RetryModel(WrapperModel):
            retry_limit = 1

        events = EventWriter()
        result = await run_agent(
            RetryModel(FunctionModel(respond)),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Return a name.",
            events=events,
            options=AgentOptions(max_model_requests=2),
            limits=RequestLimits(max_input_bytes=65536),
        )
        assert result["status"] == "ok", result
        assert calls == result["request_count"] == 2
        assert len(events.events["model-requests.jsonl"]) == 2
        assert events.events["model-responses.jsonl"][0]["error"]
        assert events.events["model-responses.jsonl"][1]["error"] is None

    asyncio.run(run())


def test_agent_timeout_cancels_a_pending_model_and_records_failure():
    async def run():
        async def respond(messages, info):
            await asyncio.sleep(1)
            return ModelResponse([TextPart(json.dumps({"name": "late"}))])

        events = EventWriter()
        result = await run_agent(
            FunctionModel(respond),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Return a name.",
            events=events,
            options=AgentOptions(max_run_seconds=0.02),
            limits=RequestLimits(max_input_bytes=65536),
        )
        assert result["status"] == "failed" and result["end_reason"] == "timeout"
        assert events.events["model-responses.jsonl"][-1]["error"]

    asyncio.run(run())


def test_truncated_response_cannot_become_a_successful_artifact():
    async def run():
        def respond(messages, info):
            return ModelResponse(
                [TextPart(json.dumps({"name": "partial"}))],
                finish_reason="length",
            )

        events = EventWriter()
        result = await run_agent(
            FunctionModel(respond),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Return a name.",
            events=events,
            limits=RequestLimits(max_input_bytes=65536),
        )
        assert result["status"] == "failed"
        assert result["end_reason"] == "output_truncated"
        response = events.events["model-responses.jsonl"][0]
        assert response["error"] and response["response"]["finish_reason"] == "length"

    asyncio.run(run())
