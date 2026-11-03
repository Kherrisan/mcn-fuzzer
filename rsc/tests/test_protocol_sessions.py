"""Persistent agent sessions, real tool loops, and bounded deterministic compaction."""

import asyncio
import json

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from rsc.llm.budget import RequestBudgetError, RequestLimits
from rsc.protocol.agent_runtime import EventWriter, RequestCache, run_agent
from rsc.protocol.models import StrictModel
from rsc.protocol.options import AgentOptions, ContextOptions
from rsc.protocol.sessions import SessionState, resolve_context_capacity


class Result(StrictModel):
    name: str


def serialized(messages):
    return json.dumps(ModelMessagesTypeAdapter.dump_python(messages, mode="json"))


def options(capacity=100_000):
    return ContextOptions(max_tokens=capacity)


def test_capacity_precedence_and_unknown_model():
    model = FunctionModel(lambda messages, info: None, profile={"context_window": 40000})
    assert resolve_context_capacity(model, RequestLimits(), ContextOptions()) == 40000
    assert resolve_context_capacity(model, RequestLimits(), options(30000)) == 30000
    assert (
        resolve_context_capacity(model, RequestLimits(context_window_tokens=20000), options(30000))
        == 20000
    )
    with pytest.raises(ValueError, match="capacity is unknown"):
        resolve_context_capacity(
            FunctionModel(lambda messages, info: None), RequestLimits(), ContextOptions()
        )


def test_history_continues_across_blocks_and_schema_switch_and_lists_stay_private(tmp_path):
    class Interaction(StrictModel):
        step: str

    async def run():
        captured = []

        def respond(messages, info):
            captured.append(serialized(messages))
            data = {"step": "send"} if "interactions" in captured[-1] else {"name": "CLIENT"}
            return ModelResponse([TextPart(json.dumps(data))])

        events = EventWriter(tmp_path, capture=True)
        session = SessionState("list:1", context_options=options(), events=events)
        other = SessionState("list:2", context_options=options(), events=events)
        model = FunctionModel(respond)

        async def execute(selected, phase="objects", output=Result):
            return await run_agent(
                model,
                output,
                {"phase": phase, "focus_block": {"block_id": "focus"}},
                stage=phase,
                phase=phase,
                instructions="Extract.",
                events=events,
                limits=RequestLimits(),
                session=selected,
            )

        assert (await execute(session))["status"] == "ok"
        assert (await execute(session))["status"] == "ok"
        assert "CLIENT" in captured[1] and "CLIENT" not in captured[0]
        session.handoff(
            "interactions", {"parties": [{"name": "LINKED", "description": "role"}]}, [], 7
        )
        result = await execute(session, "interactions", Interaction)
        assert result["status"] == "ok", result
        assert "CLIENT" in captured[2] and "LINKED" in captured[2]
        assert "supersedes" in captured[2]
        assert (await execute(other))["status"] == "ok"
        assert "CLIENT" not in captured[3] and "LINKED" not in captured[3]
        assert len(session.turns) == 3 and len(other.turns) == 1
        log = events.events["session-messages.jsonl"]
        assert len([row for row in log if row["event"] == "turn_end"]) == 4
        assert all(row["messages"] for row in log if row["event"] == "turn_end")
        assert (tmp_path / "session-messages.jsonl").is_file()

    asyncio.run(run())


def seeded_session(capacity=6000, large_outputs=False):
    events = EventWriter()
    session = SessionState("list", context_options=options(capacity), events=events)
    session.begin_turn("old", "objects", "objects", {"focus_block": {"block_id": "old"}})
    output = "RESULT" + ("x" * 4500 if large_outputs else "")
    session.finish_turn(
        [
            ModelRequest([UserPromptPart("OLD_SOURCE" * 1000)], run_id="old"),
            ModelResponse([ToolCallPart("read_blocks", {}, "read")], run_id="old"),
            ModelRequest(
                [ToolReturnPart("read_blocks", "OLD_TOOL_BODY" * 1000, "read")], run_id="old"
            ),
            ModelResponse([TextPart(json.dumps({"name": output}))], run_id="old"),
        ],
        status="ok",
    )
    session.begin_turn(
        "current",
        "interactions",
        "interactions",
        {"issue": {"description": "ACTIVE_ISSUE", "object_names": ["TARGET"]}},
    )
    current = ModelRequest([UserPromptPart("CURRENT_FOCUS")], run_id="current")
    return session, events, [*session.messages, current]


def prepare(session, messages, *, max_bytes=1000000):
    return session.prepare_messages(
        messages,
        {},
        {"max_tokens": 128},
        RequestLimits(max_input_bytes=max_bytes, safety_tokens=64),
        FunctionModel(lambda messages, info: None),
    )


def test_first_level_removes_sources_and_tools_but_keeps_model_output():
    session, events, messages = seeded_session()
    same_list = prepare(session, messages)
    assert same_list is messages
    text = serialized(messages)
    assert "OLD_SOURCE" not in text and "OLD_TOOL_BODY" not in text
    assert "RESULT" in text and "CURRENT_FOCUS" in text
    assert not any(
        isinstance(part, (ToolCallPart, ToolReturnPart))
        for message in messages
        for part in message.parts
    )
    assert events.events["context-compactions.jsonl"][-1]["levels"] == [1]
    assert events.events["context-compactions.jsonl"][-1]["after_tokens"] < 3000


def test_snapshot_supersedes_old_outputs_without_forging_assistant_messages():
    session, events, messages = seeded_session(6000, large_outputs=True)
    session.update_model(
        {"parties": [{"name": "CANONICAL", "description": "one role"}]},
        [{"description": "OPEN", "object_names": []}],
        12,
        pending_groups=[{"group": "pending"}],
    )
    prepare(session, messages)
    text = serialized(messages)
    assert "CANONICAL" in text and "RESULT" not in text
    assert "OPEN" in text and "pending" in text and "CURRENT_FOCUS" in text
    assert messages[0].kind == "request"
    assert events.events["context-compactions.jsonl"][-1]["levels"] == [1, 2]
    assert events.events["context-compactions.jsonl"][-1]["model_revision"] == 12


def test_first_level_treats_handoff_as_barrier_for_stale_outputs():
    session, _, messages = seeded_session()
    session.finish_turn(messages, status="ok")
    session.handoff("interactions", {"parties": [{"name": "NEW_DEFINITION"}]}, [], 20)
    session.begin_turn("next", "interactions", "interactions", {})
    messages = [
        *session.messages,
        ModelRequest([UserPromptPart("CURRENT_FOCUS")], run_id="next"),
    ]
    # The large source from the old stage forces first-level cleanup.
    prepare(session, messages)
    text = serialized(messages)
    assert "NEW_DEFINITION" in text
    assert "RESULT" not in text
    assert "OLD_SOURCE" not in text


def test_first_level_retains_only_committed_recovery_subset():
    session, events, messages = seeded_session()
    # Treat the response as failed, then accept only a validated recovered subset.
    session.active_turn.status = "failed"
    session.finish_turn(messages, status="failed")
    session.record_commit("old", {}, accepted=False)
    session.record_commit("current", {"parties": [{"name": "RECOVERED"}]})
    session.begin_turn("next", "objects", "objects", {})
    messages = [
        *session.messages,
        ModelRequest([UserPromptPart("CURRENT_FOCUS")], run_id="next"),
    ]
    prepare(session, messages)
    text = serialized(messages)
    assert "RESULT" not in text and "OLD_SOURCE" not in text
    assert "RECOVERED" in text and "commit_receipt" in text
    assert any(row["event"] == "commit" for row in events.events["session-messages.jsonl"])


def test_catalog_keeps_current_issue_and_definitions_remain_retrievable():
    session, events, messages = seeded_session(3000, large_outputs=True)
    model = {
        "parties": [
            {"name": f"PARTY_{i}", "description": "large description " * 200} for i in range(20)
        ]
    }
    session.update_model(model, [], 10)
    prepare(session, messages)
    text = serialized(messages)
    assert "name_catalog" in text and "lookup_object" in text
    assert "ACTIVE_ISSUE" in text and "CURRENT_FOCUS" in text
    assert session.model == model
    assert session.catalog_only
    record = events.events["context-compactions.jsonl"][-1]
    assert record["levels"] == [1, 2, 3]
    assert record["after_tokens"] < 3000 - 128 - 64


def test_catalog_keeps_bounded_recent_relevant_canonical_results():
    session, _, messages = seeded_session(6000, large_outputs=True)
    session.finish_turn(messages, status="ok")
    session.record_commit(
        "current",
        {"messages": [{"name": "TARGET", "associated_fields": [{"name": "STALE_FIELD"}]}]},
    )
    latest = {"name": "TARGET", "associated_fields": [{"name": "LATEST_FIELD"}]}
    model = {
        "messages": [latest]
        + [
            {"name": f"M{i}", "associated_fields": [{"name": "unused" * 20}] * 100}
            for i in range(20)
        ]
    }
    session.update_model(model, [], 15)
    session.begin_turn(
        "next",
        "interactions",
        "interactions",
        {"issue": {"description": "CURRENT_ISSUE", "object_names": ["TARGET"]}},
    )
    messages = [
        *session.messages,
        ModelRequest([UserPromptPart("CURRENT_FOCUS")], run_id="next"),
    ]
    prepare(session, messages)
    catalog = json.loads(messages[0].parts[0].content)
    assert catalog["event"] == "name_catalog"
    assert catalog["recent_results"]["messages"] == [latest]
    assert "CURRENT_ISSUE" in serialized(messages)
    assert "STALE_FIELD" not in serialized(messages)
    assert len(catalog["catalog"]["messages"]) == 21


def test_catalog_drops_optional_recent_results_before_name_entries():
    session, _, messages = seeded_session(6000, large_outputs=True)
    session.finish_turn(messages, status="ok")
    latest = {"name": "TARGET", "associated_fields": [{"name": "BIG" * 2000}]}
    session.record_commit("current", {"messages": [latest]})
    session.update_model({"messages": [latest]}, [], 15)
    session.begin_turn("next", "interactions", "interactions", {"issue": {"description": "ACTIVE"}})
    messages = [
        *session.messages,
        ModelRequest([UserPromptPart("CURRENT_FOCUS")], run_id="next"),
    ]
    prepare(session, messages)
    catalog = json.loads(messages[0].parts[0].content)
    assert catalog["recent_results"]["messages"] == []
    assert catalog["catalog"]["messages"] == [{"name": "TARGET"}]
    assert not catalog["truncated"]
    assert catalog["current_work"]["issue"]["description"] == "ACTIVE"


def test_compression_does_not_cut_current_tool_exchange_or_current_focus():
    session, _, messages = seeded_session()
    messages.extend(
        [
            ModelResponse([ToolCallPart("read_blocks", {}, "current_read")], run_id="current"),
            ModelRequest(
                [ToolReturnPart("read_blocks", "CURRENT_TOOL_BODY", "current_read")],
                run_id="current",
            ),
        ]
    )
    prepare(session, messages)
    text = serialized(messages)
    assert "CURRENT_FOCUS" in text and "CURRENT_TOOL_BODY" in text
    calls = [
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    returns = [
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert calls == returns == ["current_read"]


def test_byte_cap_triggers_compaction_and_unfittable_current_input_fails():
    session, events, messages = seeded_session(100000)
    prepare(session, messages, max_bytes=4000)
    assert events.events["context-compactions.jsonl"][-1]["reason"] == "byte_limit"
    messages[-1].parts = [UserPromptPart("UNFIT_CURRENT" * 1000)]
    with pytest.raises(RequestBudgetError, match="bytes exceeds"):
        prepare(session, messages, max_bytes=4000)
    assert "UNFIT_CURRENT" * 1000 in serialized(messages)


def test_pending_calls_are_closed_and_trial_budgets_ignore_inherited_retries():
    async def run():
        events = EventWriter()
        session = SessionState("list", context_options=options(), events=events)
        calls = 0

        def respond(messages, info):
            nonlocal calls
            calls += 1
            # Both trials independently spend exactly one output retry.
            return ModelResponse([TextPart("{broken" if calls % 2 else '{"name":"accepted"}')])

        for _ in range(2):
            result = await run_agent(
                FunctionModel(respond),
                Result,
                {},
                stage="objects",
                phase="objects",
                instructions="Extract.",
                events=events,
                limits=RequestLimits(),
                session=session,
                options=AgentOptions(max_output_retries=1),
            )
            assert result["status"] == "ok", result
            assert result["output_retry_count"] == 1

        def too_many_tools(messages, info):
            return ModelResponse([ToolCallPart("lookup", {}, "a"), ToolCallPart("lookup", {}, "b")])

        def lookup() -> str:
            """Return a small lookup result."""
            return "value"

        failed = await run_agent(
            FunctionModel(too_many_tools),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Lookup.",
            tools=[lookup],
            events=events,
            limits=RequestLimits(),
            session=session,
            options=AgentOptions(max_tool_calls=1),
        )
        assert failed["end_reason"] == "run_limit", failed
        returns = [
            part
            for message in session.messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert {part.tool_call_id for part in returns} == {"a", "b"}
        assert all(part.outcome == "interrupted" for part in returns)

    asyncio.run(run())


def test_history_and_compaction_are_part_of_request_cache_key(tmp_path):
    async def run():
        events = EventWriter()
        cache = RequestCache(tmp_path / "cache")
        calls = 0

        def respond(messages, info):
            nonlocal calls
            calls += 1
            return ModelResponse([TextPart('{"name":"accepted"}')])

        async def execute(session):
            return await run_agent(
                FunctionModel(respond),
                Result,
                {"focus": "same"},
                stage="objects",
                phase="objects",
                instructions="Extract.",
                events=events,
                limits=RequestLimits(),
                session=session,
                cache=cache,
            )

        first = SessionState("first", context_options=options(), events=events)
        assert (await execute(first))["status"] == "ok"
        assert (await execute(first))["cache_hits"] == 0  # New history changes the key.
        second = SessionState("second", context_options=options(), events=events)
        assert (await execute(second))["cache_hits"] == 1
        assert calls == 2

    asyncio.run(run())


def test_real_agent_compaction_updates_stored_history_and_tool_results_survive():
    async def run():
        captured = []
        events = EventWriter()
        session = SessionState("list", context_options=options(9000), events=events)

        def respond(messages, info):
            captured.append(serialized(messages))
            current_run = messages[-1].run_id
            returned = [
                part
                for message in messages
                if message.run_id == current_run
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                return ModelResponse([ToolCallPart("read_blocks", {}, "read-current")])
            return ModelResponse([TextPart('{"name":"accepted"}')])

        def read_blocks() -> dict:
            """Read relevant source text for the current block."""
            return {"text": "ACTUAL_TOOL_TEXT"}

        for index in range(3):
            result = await run_agent(
                FunctionModel(respond),
                Result,
                {"focus_block": {"block_id": str(index), "text": f"SOURCE_{index}" * 300}},
                stage="objects",
                phase="objects",
                instructions="Read the block then extract.",
                tools=[read_blocks],
                events=events,
                limits=RequestLimits(safety_tokens=64),
                model_settings={"max_tokens": 128},
                session=session,
            )
            assert result["status"] == "ok", result
            assert result["tool_call_count"] == 1
        assert session.compaction_count >= 1
        assert "SOURCE_0" not in serialized(session.messages)
        assert "SOURCE_2" in serialized(session.messages)
        assert "ACTUAL_TOOL_TEXT" in captured[-1]
        assert len(session.turns) == 3
        assert all(turn.status == "ok" for turn in session.turns)

    asyncio.run(run())


def test_session_budget_failure_is_audited_before_model_and_unknown_capacity_is_explicit():
    async def run():
        calls = []

        def respond(messages, info):
            calls.append(messages)
            return ModelResponse([TextPart('{"name":"unexpected"}')])

        events = EventWriter()
        session = SessionState("small", context_options=options(1000), events=events)
        result = await run_agent(
            FunctionModel(respond),
            Result,
            {"focus": "large" * 2000},
            stage="objects",
            phase="objects",
            instructions="Extract.",
            events=events,
            session=session,
            limits=RequestLimits(safety_tokens=64),
            model_settings={"max_tokens": 128},
        )
        assert result["end_reason"] == "request_budget", result
        assert result["calls"] == 0 and result["skipped"] == 1
        assert not events.events["model-requests.jsonl"][-1]["called"]
        assert not calls
        unknown = SessionState("unknown", events=events)
        result = await run_agent(
            FunctionModel(respond),
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Extract.",
            events=events,
            session=unknown,
            limits=RequestLimits(),
        )
        assert result["status"] == "failed" and "capacity is unknown" in result["error"]
        assert not calls and unknown.active_turn is None

    asyncio.run(run())


def test_session_single_writer_rejection_does_not_clear_the_running_trial():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        events = EventWriter()
        session = SessionState("shared", context_options=options(), events=events)

        async def respond(messages, info):
            started.set()
            await release.wait()
            return ModelResponse([TextPart('{"name":"finished"}')])

        async def execute(run_id):
            return await run_agent(
                FunctionModel(respond),
                Result,
                {},
                stage="objects",
                phase="objects",
                instructions="Extract.",
                events=events,
                session=session,
                limits=RequestLimits(),
                run_id=run_id,
            )

        first = asyncio.create_task(execute("owner"))
        await started.wait()
        second = await execute("contender")
        assert second["status"] == "failed" and "active trial" in second["error"]
        assert session.active_turn.run_id == "owner"
        release.set()
        assert (await first)["status"] == "ok"
        assert session.active_turn is None
        assert len(session.turns) == 1

    asyncio.run(run())


def test_session_reserves_provider_default_output_tokens_when_run_settings_are_omitted():
    async def run():
        events = EventWriter()
        session = SessionState("defaults", context_options=options(), events=events)
        model = FunctionModel(
            lambda messages, info: ModelResponse([TextPart('{"name":"accepted"}')]),
            settings={"max_tokens": 16000},
        )
        result = await run_agent(
            model,
            Result,
            {},
            stage="objects",
            phase="objects",
            instructions="Extract.",
            events=events,
            session=session,
            limits=RequestLimits(),
        )
        assert result["status"] == "ok", result
        budget = [
            row
            for row in events.events["session-messages.jsonl"]
            if row["event"] == "request_budget"
        ]
        assert budget[-1]["output_reserve"] == 16000
        assert events.events["model-requests.jsonl"][-1]["model_settings"]["max_tokens"] == 16000

    asyncio.run(run())
