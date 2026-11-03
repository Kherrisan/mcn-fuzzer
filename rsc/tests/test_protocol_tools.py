"""Retrieval tools exercised through real PydanticAI FunctionModel conversations."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from protocol_samples import base, document
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from rsc.llm.budget import RequestLimits
from rsc.protocol.agent_runtime import EventWriter, RequestCache, run_agent
from rsc.protocol.context import Corpus
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.models import StrictModel
from rsc.protocol.options import AgentOptions
from rsc.protocol.planning import plan_block_lists
from rsc.protocol.registry import Registry
from rsc.protocol.retrieval import RetrievalContext
from rsc.protocol.store import ExtractionStore
from rsc.protocol.tools import AgentDeps, make_tools


class Result(StrictModel):
    done: bool


def environment(*, max_context=12, material=False, max_search_results=8):
    doc = document()
    doc_id = doc["meta"]["source_id"]
    corpus = Corpus({doc_id: doc})
    blocks = plan_block_lists(corpus, 1).lists[0].blocks
    focus = blocks[-1]
    target = next(
        block for block in blocks if "also called user" in corpus.blocks[block.key]["text"]
    )
    keywords = KeywordIndex("test", corpus, list_id=focus.list_id)
    for block in (target, focus):
        keywords.commit(
            block,
            {"keywords": [{"text": "requester"}]},
        )
    options = AgentOptions(
        max_model_requests=12,
        max_tool_blocks=max_context,
        max_search_results=max_search_results,
    )
    context = RetrievalContext(
        corpus,
        store=ExtractionStore(),
        focus=None if material else focus,
        phase="objects",
        agent_options=options,
    )
    deps = AgentDeps(
        "test",
        "repair" if material else "objects",
        "objects",
        corpus,
        Registry(base(doc)),
        keywords,
        context,
        EventWriter(),
        options=options,
    )
    return deps, target, focus


async def execute(deps, respond, *, cache=None, limits=None, tools=None):
    return await run_agent(
        FunctionModel(respond, model_name="rsc-tools-test"),
        Result,
        deps.payload(),
        stage=deps.stage,
        phase=deps.phase,
        instructions="Use retrieval tools, then return done.",
        events=deps.events,
        limits=limits or RequestLimits(max_input_bytes=262144),
        options=deps.options,
        cache=cache,
        tools=make_tools() if tools is None else tools,
        deps=deps,
        prepare_messages=deps.prepare_messages,
        before_request=deps.before_request,
        after_run=deps.audit_tool_failures,
        run_id="deterministic-test-run",
    )


def prompt(messages):
    return next(
        json.loads(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
        and isinstance(part.content, str)
        and '"context_version"' in part.content
    )


def test_all_seven_tools_run_with_source_audit_full_definition_reads_and_cache():
    async def run():
        provider_calls = []
        cache = RequestCache()
        deps, target, focus = environment()
        calls = [
            ("search_keywords", {"keywords": ["requester"], "top_k": 1}),
            ("search_text", {"query": "Session", "top_k": 1}),
            ("read_blocks", {"block_refs": [target.ref()]}),
            ("get_parent_section", {"block_ref": target.ref()}),
            ("get_neighbor_blocks", {"block_ref": target.ref(), "before": 0, "after": 1}),
            ("search_objects", {"query": "requester", "kind": "parties", "top_k": 1}),
            ("lookup_object", {"name": "requester", "depth": 0, "limit": 1}),
        ]

        def respond(messages, info):
            provider_calls.append(deepcopy(messages))
            returns = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if len(returns) == 6:
                assert not deps.delivered_object_ids
            if len(returns) == 7:
                assert deps.delivered_object_ids == {"requester"}
            if len(returns) < len(calls):
                name, args = calls[len(returns)]
                return ModelResponse([ToolCallPart(name, args, name + "-call")])
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond, cache=cache)
        assert result["status"] == "ok", result
        assert result["request_count"] == 8 and result["tool_call_count"] == 7
        assert len(provider_calls) == 8
        assert deps.queried_object_ids == {"requester"}
        assert len(deps.queries) == 1
        logs = deps.events.events["tool-calls.jsonl"]
        assert {row["tool_name"] for row in logs} == {name for name, _ in calls}
        assert all(row["context_revision_after"] >= row["context_revision_before"] for row in logs)
        assert all(row["elapsed_ms"] >= 0 for row in logs)
        assert [row["request_index"] for row in logs] == list(range(1, 8))
        assert all(row["status"] == "ok" for row in logs), logs
        request = deps.events.events["model-requests.jsonl"][0]
        assert len(request["tool_definitions"]) == 7
        assert all(tool["description"] for tool in request["tool_definitions"])
        assert len(deps.events.events["retrieval-contexts.jsonl"]) == 8
        for message in provider_calls[-1]:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "read_blocks":
                    assert "compile_result" in json.dumps(part.content)
                    assert (
                        part.content["items"][0]["block"]["text"]
                        == deps.corpus.blocks[target.key]["text"]
                    )
        assert "window" not in prompt(provider_calls[-1])
        assert "blocks" not in prompt(provider_calls[-1])
        # Cached model decisions must still execute all current retrieval implementations.
        replay, _, _ = environment()
        replay_result = await execute(replay, respond, cache=cache)
        assert replay_result["status"] == "ok", replay_result
        assert len(provider_calls) == 8 and replay_result["cache_hits"] == 8
        assert len(replay.events.events["tool-calls.jsonl"]) == 7
        assert replay.delivered_object_ids == {"requester"}

    asyncio.run(run())


def test_bad_arguments_unknown_sources_and_invalid_cursors_remain_distinct():
    async def run():
        deps, target, _ = environment()
        calls = [
            ("search_text", {"query": "[.*]"}),
            ("search_text", {"query": "not_a_source_word"}),
            ("search_text", {"query": "Session", "top_k": 99}),
            ("search_keywords", {"keywords": ["requester"], "cursor": "not-a-cursor"}),
            (
                "read_blocks",
                {"block_refs": [{"doc_id": "not-selected", "block_id": target.block_id}]},
            ),
            ("read_blocks", {"block_refs": [target.ref()], "range": {"start": 0, "end": 999999}}),
            ("get_neighbor_blocks", {"block_ref": target.ref(), "before": -1}),
        ]

        def respond(messages, info):
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if len(returns) < len(calls):
                name, args = calls[len(returns)]
                return ModelResponse([ToolCallPart(name, args, str(len(returns)))])
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond)
        assert result["status"] == "ok", result
        logs = deps.events.events["tool-calls.jsonl"]
        assert [row["status"] for row in logs] == [
            "error",
            "empty",
            "error",
            "error",
            "missing",
            "error",
            "invalid",
        ]
        assert result["tool_call_count"] == 7

    asyncio.run(run())


def test_source_is_not_delivered_when_the_actual_request_cannot_fit():
    async def run():
        deps, _, focus = environment()

        def never_call(messages, info):
            raise AssertionError("An oversized request must not call the model")

        result = await execute(deps, never_call, limits=RequestLimits(max_input_bytes=1))
        assert result["status"] == "failed" and result["end_reason"] == "request_budget"
        assert deps.events.events.get("retrieval-contexts.jsonl", []) == []
        assert not deps.events.events["model-requests.jsonl"][0]["called"]
        assert deps.context.snapshot()["reads"] == []

    asyncio.run(run())


def test_completion_material_has_no_focus_and_logs_sent_source_context():
    async def run():
        deps, target, _ = environment(material=True)

        def respond(messages, info):
            value = prompt(messages)
            assert "window" not in value and "id" not in value
            assert "blocks" not in value and "context_revision" not in value
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returns:
                return ModelResponse(
                    [
                        ToolCallPart(
                            "read_blocks",
                            {"block_refs": [target.ref()], "range": {"start": 0, "end": 8}},
                            "read",
                        )
                    ]
                )
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond)
        assert result["status"] == "ok", result
        assert len(deps.events.events["retrieval-contexts.jsonl"]) == 2

    asyncio.run(run())


def test_model_generated_sources_do_not_count_as_delivered_material():
    deps, target, _ = environment()
    block = {"doc_id": target.doc_id, **deps.corpus.blocks[target.key]}
    invented = [
        ModelResponse([ToolCallPart("read_blocks", {"block": block}, "attempt")]),
        ModelRequest(
            [ToolReturnPart("read_blocks", {"echo": block}, "attempt", outcome="retried")]
        ),
        ModelResponse([TextPart(json.dumps({"echo": block}))]),
    ]
    deps.before_request(invented, {"run_id": "test", "request_index": 1})
    assert not deps.delivered_object_ids


def test_source_reads_are_limited_per_call_without_a_lifetime_quota():
    async def run():
        deps, target, _ = environment(max_context=1)
        from types import SimpleNamespace

        from rsc.protocol.models import BlockRef
        from rsc.protocol.tools import read_blocks

        ctx = SimpleNamespace(deps=deps, run_id="quota", tool_call_id="read", model=None)
        blocks = plan_block_lists(deps.corpus, 1).lists[0].blocks
        for focus in blocks:
            result = await read_blocks(ctx, [BlockRef(**focus.ref())])
            assert result["status"] == "ok"
            assert result["items"][0]["block"]["text"] == deps.corpus.blocks[focus.key]["text"]
        assert len(deps.context.snapshot()["reads"]) == len(blocks)
        result = await read_blocks(ctx, [BlockRef(**target.ref()), BlockRef(**blocks[-1].ref())])
        assert result["status"] == "error"
        assert "per-call" in result["error"]

    asyncio.run(run())


def test_terminal_invalid_tool_arguments_are_audited_without_another_request():
    async def run():
        deps, target, _ = environment(material=True)
        deps.options = replace(deps.options, max_model_requests=1)

        def respond(messages, info):
            return ModelResponse(
                [ToolCallPart("read_blocks", {"block_refs": "invalid"}, "invalid-read")]
            )

        result = await execute(deps, respond)
        assert result["status"] == "failed"
        assert result["request_count"] == 1
        assert result["tool_call_count"] == result["tool_failures"] == 1
        failures = deps.events.events["tool-calls.jsonl"]
        assert len(failures) == 1
        assert failures[0]["tool_call_id"] == "invalid-read"
        assert failures[0]["status"] == "invalid"
        assert failures[0]["request_index"] == 1
        assert failures[0]["arguments"] == {"block_refs": "invalid"}
        assert deps.events.totals["tool_failures"] == 1
        assert not deps.events.tool_origins
        assert not deps.delivered_object_ids

    asyncio.run(run())


def test_terminal_successful_query_is_not_marked_as_delivered():
    async def run():
        deps, _, _ = environment(material=True)
        deps.options = replace(deps.options, max_model_requests=1)

        def respond(messages, info):
            return ModelResponse(
                [
                    ToolCallPart(
                        "search_objects",
                        {"query": "requester", "kind": "parties", "top_k": 1},
                        "last-query",
                    )
                ]
            )

        result = await execute(deps, respond)
        assert result["status"] == "failed"
        assert result["request_count"] == result["tool_call_count"] == 1
        assert result["tool_failures"] == 0
        assert len(deps.queries) == 1
        assert deps.queries[0]["delivered"] is False
        assert not deps.delivered_object_ids

    asyncio.run(run())


def _schema_values(value, key):
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for item in value.values():
            yield from _schema_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_values(item, key)


def test_tool_schema_uses_the_run_limit_and_isolated_copies():
    async def run_with(maximum, shared_tools):
        deps, _, _ = environment()
        deps.options = replace(
            deps.options,
            max_search_results=maximum,
            max_keywords_per_search=2,
        )
        calls = [
            ("search_text", {"query": "Session"}),
            ("search_text", {"query": "Session", "top_k": None}),
            ("search_keywords", {"keywords": ["requester"], "top_k": None}),
            ("search_objects", {"query": "requester", "top_k": None}),
            ("lookup_object", {"name": "requester", "limit": None, "depth": 0}),
        ]
        received = {}

        def respond(messages, info):
            received.update({tool.name: tool for tool in info.function_tools})
            definitions = {tool.name: tool.parameters_json_schema for tool in info.function_tools}
            for name in ("search_text", "search_keywords", "search_objects"):
                page = definitions[name]["properties"]["top_k"]
                assert maximum in _schema_values(page, "maximum")
            lookup = definitions["lookup_object"]["properties"]
            for name in ("limit", "depth"):
                assert maximum in _schema_values(lookup[name], "maximum")
            assert 2 in _schema_values(
                definitions["search_keywords"]["properties"]["keywords"], "maxItems"
            )
            returns = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            for result in returns:
                assert result.outcome == "success", result
                assert result.content.get("status") in ("ok", "empty", "partial"), result
            if len(returns) < len(calls):
                name, args = calls[len(returns)]
                return ModelResponse([ToolCallPart(name, args, str(len(returns)))])
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond, tools=shared_tools)
        assert result["status"] == "ok", result
        logged = {
            row["name"]: row
            for row in deps.events.events["model-requests.jsonl"][0]["tool_definitions"]
        }
        assert set(received) == set(logged)
        for name, tool_def in received.items():
            assert tool_def.parameters_json_schema == logged[name]["parameters_json_schema"]
            assert tool_def.description == logged[name]["description"]
        default_page = min(8, maximum)
        for row in deps.events.events["tool-calls.jsonl"]:
            args = row["arguments"]
            if row["tool_name"].startswith("search_"):
                assert args["top_k"] in (None, default_page), args
            elif row["tool_name"] == "lookup_object":
                assert args["limit"] in (None, default_page), args
        page = received["search_objects"].parameters_json_schema["properties"]["top_k"]
        assert page["anyOf"][0]["maximum"] == maximum
        assert "null" in {item["type"] for item in page["anyOf"]}
        assert str(default_page) in page["description"]
        assert "kind alone is not a query" in received["search_objects"].description
        assert "connectionless" in received["search_objects"].description
        return received

    async def run():
        tools = make_tools()
        original = {tool.name: deepcopy(tool.function_schema.json_schema) for tool in tools}
        low, high, again = await asyncio.gather(
            run_with(2, tools),
            run_with(8, tools),
            run_with(2, tools),
        )
        assert (
            low["search_text"].parameters_json_schema["properties"]["top_k"]["anyOf"][0]["maximum"]
            == 2
        )
        assert (
            high["lookup_object"].parameters_json_schema["properties"]["limit"]["anyOf"][0][
                "maximum"
            ]
            == 8
        )
        assert again["lookup_object"].parameters_json_schema["properties"]["depth"]["maximum"] == 2
        assert {tool.name: tool.function_schema.json_schema for tool in tools} == original

    asyncio.run(run())


def test_missing_deps_prepare_uses_default_agent_options():
    tool = next(item for item in make_tools() if item.name == "search_objects")
    prepared = asyncio.run(tool.prepare_tool_def(type("Ctx", (), {"deps": None})()))
    page = prepared.parameters_json_schema["properties"]["top_k"]
    assert page["anyOf"][0]["maximum"] == AgentOptions().max_search_results == 8
    assert "8" in page["description"]


def test_page_size_rejects_bool_string_and_float():
    async def run():
        deps, _, _ = environment()
        deps.options = replace(deps.options, max_output_retries=8)
        calls = [
            ("search_text", {"query": "requester", "top_k": True}),
            ("search_text", {"query": "requester", "top_k": "2"}),
            ("search_text", {"query": "requester", "top_k": 1.5}),
            ("lookup_object", {"name": "requester", "limit": False}),
        ]

        def respond(messages, info):
            returns = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if len(returns) < len(calls):
                name, args = calls[len(returns)]
                return ModelResponse([ToolCallPart(name, args, str(len(returns)))])
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond)
        assert result["status"] == "ok", result
        logs = deps.events.events["tool-calls.jsonl"]
        assert [row["status"] for row in logs] == ["invalid", "invalid", "invalid", "invalid"]
        assert all(row["result"][0]["type"] == "int_type" for row in logs)

    asyncio.run(run())


def test_omitted_page_size_uses_min_of_eight_and_the_configured_limit():
    narrow, _, _ = environment(max_search_results=2)
    wide, _, _ = environment(max_search_results=20)
    assert narrow.resolve_page_size(None) == 2
    assert wide.resolve_page_size(None) == 8
    assert narrow.resolve_page_size(2) == 2
    with pytest.raises(ValueError, match="1 to 2"):
        narrow.resolve_page_size(3)


def test_search_rejects_kind_only_blank_query_and_uses_the_small_default_page():
    async def run():
        deps, _, _ = environment(max_search_results=2)
        calls = [
            ("search_objects", {"kind": "parties"}),
            ("search_objects", {"query": "  ", "kind": "parties"}),
            ("search_keywords", {"keywords": [" "]}),
            ("search_text", {"query": "requester", "top_k": None}),
            ("search_objects", {"query": "requester", "kind": "parties", "cursor": "other"}),
        ]

        def respond(messages, info):
            returns = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if len(returns) < len(calls):
                name, args = calls[len(returns)]
                return ModelResponse([ToolCallPart(name, args, str(len(returns)))])
            return ModelResponse([TextPart(json.dumps({"done": True}))])

        result = await execute(deps, respond)
        assert result["status"] == "ok", result
        logs = deps.events.events["tool-calls.jsonl"]
        assert [row["status"] for row in logs[:3]] == ["error", "error", "error"]
        assert logs[3]["status"] == "ok"
        assert len(logs[3]["result"]["items"]) <= 2
        assert logs[4]["status"] == "error"

    asyncio.run(run())


def test_source_search_schema_and_execution_honor_per_call_source_limit():
    async def run():
        deps, _, _ = environment(max_context=1)
        tools = make_tools()
        source = next(item for item in tools if item.name == "search_text")
        from types import SimpleNamespace

        prepared = await source.prepare_tool_def(SimpleNamespace(deps=deps))
        page = prepared.parameters_json_schema["properties"]["top_k"]
        assert page["anyOf"][0]["maximum"] == 1
        assert deps.resolve_source_page_size(None) == 1
        with pytest.raises(ValueError, match="1 to 1"):
            deps.resolve_source_page_size(2)
        assert deps.resolve_page_size(None) == 8  # Object summaries have no source bodies.

    asyncio.run(run())


def test_inherited_tool_errors_are_not_reaudited_when_ids_repeat():
    deps, _, _ = environment()
    deps.events.tool_origins["current"] = {"reused-id": {"arguments": {}, "request_index": 1}}
    old = ModelRequest(
        [ToolReturnPart("read_blocks", {"error": "old"}, "reused-id", outcome="retried")]
    )
    deps.audit_tool_failures(
        [old], {"run_id": "current", "request_index": 1, "turn_message_start": 1}
    )
    assert not deps.events.events.get("tool-calls.jsonl")
    deps.audit_tool_failures(
        [old, old], {"run_id": "current", "request_index": 2, "turn_message_start": 1}
    )
    assert len(deps.events.events["tool-calls.jsonl"]) == 1
