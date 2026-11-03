"""Direct multi-round Issue traversal, immediate commits, and bounded retries."""

import asyncio
import json
from copy import deepcopy

from protocol_samples import TEXT, base, document, object_response
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from test_protocol_extraction import payload

from rsc.llm.budget import RequestLimits
from rsc.protocol.agent_runtime import EventWriter
from rsc.protocol.completion import complete_interactions, complete_objects
from rsc.protocol.context import Corpus
from rsc.protocol.extraction import ExtractionRuntime
from rsc.protocol.ids import GlobalIdAllocator
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.merge import issue
from rsc.protocol.options import AgentOptions, ContextOptions, RepairOptions
from rsc.protocol.planning import plan_block_lists
from rsc.protocol.proposals import CandidateState, empty_model
from rsc.protocol.store import ExtractionStore


def response(**values):
    data = {
        "parties": [],
        "messages": [],
        "channels": [],
        "unresolved": [],
        **values,
    }
    return object_response(data)


def setup(respond, *, out=None, unknown=0, semantic=False, max_requests=8):
    doc = document(
        TEXT + "\nThe responder uses port 21 and address 127.0.0.1. The timeout is 10.\n"
    )
    corpus = Corpus({doc["meta"]["source_id"]: doc})
    model = empty_model() | {key: value for key, value in base(doc).items() if key != "unresolved"}
    ev = []
    for name in ("port", "address")[:unknown]:
        model["channels"][0]["endpoint_sources"].append(
            {"party_name": "responder", "parameter": name, "from": "unspecified"}
        )
    state = CandidateState(model=model)
    for name in ("port", "address")[:unknown]:
        state.issues.append(issue(f"Complete {name}", ["control"]))
    if semantic:
        state.issues.append(
            issue("The role definition needs semantic clarification", ["requester"])
        )
    factory_calls = []

    def factory(stage, phase):
        factory_calls.append((stage, phase))
        lookup_id = f"lookup-{len(factory_calls)}"

        def with_retrieval(messages, info):
            if "loaded_channel" in respond.__code__.co_names and not any(
                isinstance(part, ToolReturnPart) and part.tool_call_id == lookup_id
                for message in messages
                for part in message.parts
            ):
                return ModelResponse(
                    [
                        ToolCallPart(
                            "lookup_object",
                            {"name": "control", "depth": 0},
                            lookup_id,
                        )
                    ]
                )
            return respond(messages, info)

        return FunctionModel(with_retrieval, model_name="completion-" + phase)

    if out is not None:
        out.mkdir()
    runtime = ExtractionRuntime(
        "test",
        corpus,
        plan_block_lists(corpus, 1),
        factory,
        GlobalIdAllocator("test"),
        EventWriter(out, capture=True),
        {"repair": RequestLimits(max_input_bytes=131072)},
        out=out,
        context_options=ContextOptions(max_tokens=100000),
        agent_options=AgentOptions(max_model_requests=max_requests),
    )
    keywords = KeywordIndex("test", corpus)
    return runtime, state, keywords, ExtractionStore(), factory_calls, ev


def loaded_channel(messages):
    """Read the actual full definition requested by the mock Agent."""

    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, ToolReturnPart) and part.tool_name == "lookup_object":
                return deepcopy(part.content["object"])
    raise AssertionError("The Agent must retrieve a definition before editing it")


def result(info, data, **kwargs):
    return ModelResponse([TextPart(json.dumps(data))], **kwargs)


def test_no_progress_defers_until_limit_without_growing_permanent_failures(tmp_path):
    async def run():
        inputs = []

        def respond(messages, info):
            inputs.append(payload(messages))
            return result(info, response())

        runtime, state, keywords, store, calls, _ = setup(
            respond,
            out=tmp_path / "model",
            semantic=True,
        )
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=3),
        )
        assert report["rounds"] == 3 and report["stop_reason"] == "round_limit"
        assert len(records) == 3 and len(calls) == 3
        assert len(updated.issues) == 1
        assert all(
            row["issue"]["description"] == inputs[0]["issue"]["description"] for row in inputs
        )
        assert set(inputs[0]["issue"]) == {"description", "object_names"}
        assert inputs[1]["previous_feedback"]
        paths = {path.name for path in runtime.out.rglob("*")}
        assert (
            "worklist.json" in paths
            and "tasks.jsonl" not in paths
            and "split-tasks.jsonl" not in paths
        )
        history = json.loads(
            (runtime.out / "objects-repair/round-003/gap-history.json").read_text()
        )
        assert (
            len(history) == 1 and history[0]["attempts"] == 3 and history[0]["status"] == "deferred"
        )

    asyncio.run(run())


def test_one_commit_resolves_other_worklist_items_without_duplicate_agent_runs(tmp_path):
    async def run():
        def respond(messages, info):
            data = payload(messages)
            channel = loaded_channel(messages)
            for endpoint in channel["endpoint_sources"]:
                if endpoint["parameter"] in data["issue"]["description"]:
                    endpoint["from"] = "21" if endpoint["parameter"] == "port" else "127.0.0.1"
            return result(info, response(channels=[channel]))

        runtime, state, keywords, store, calls, _ = setup(
            respond,
            out=tmp_path / "model",
            unknown=2,
        )
        updated, report, _ = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(),
        )
        assert report["rounds"] == 1 and report["stop_reason"] == "no_missing", report
        assert not updated.issues and len(calls) == 2
        round_root = runtime.out / "objects-repair/round-001"
        worklist = json.loads((round_root / "worklist.json").read_text())
        decisions = [
            json.loads(line) for line in (round_root / "decisions.jsonl").read_text().splitlines()
        ]
        assert len(worklist) == 2
        assert [row["status"] for row in decisions] == ["accepted", "accepted"]

    asyncio.run(run())


def test_new_issue_waits_until_next_round_and_reads_previous_commit():
    async def run():
        inputs = []

        def respond(messages, info):
            data = payload(messages)
            inputs.append(data)
            channel = loaded_channel(messages)
            if len(inputs) == 1:
                channel["endpoint_sources"][0]["from"] = "21"
                timer = deepcopy(channel["endpoint_sources"][0])
                timer.update(parameter="timeout", **{"from": "unspecified"})
                channel["endpoint_sources"].append(timer)
                extra = [{"description": "Complete timeout", "object_names": ["control"]}]
            else:
                assert channel["endpoint_sources"][0]["from"] == "21"
                channel["endpoint_sources"][1]["from"] = "10"
                extra = []
            return result(info, response(channels=[channel], unresolved=extra))

        runtime, state, keywords, store, _, _ = setup(respond, unknown=1)
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(),
        )
        assert len(inputs) == 2 and report["rounds"] == 2, report
        assert records[0]["round"] == 1 and records[1]["round"] == 2
        assert inputs[0]["issue"]["description"] == "Complete port"
        assert inputs[1]["issue"]["description"] == "Complete timeout"
        assert not updated.issues

    asyncio.run(run())


def test_no_complete_creates_snapshots_without_factory_or_rounds(tmp_path):
    async def run():
        def forbidden(messages, info):
            raise AssertionError("completion is disabled")

        runtime, state, keywords, store, calls, _ = setup(
            forbidden, unknown=1, out=tmp_path / "model"
        )
        before = deepcopy(state.model)
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(enabled=False),
        )
        assert report["enabled"] is False and report["stop_reason"] == "disabled"
        assert report["rounds"] == 0 and records == [] and calls == []
        assert updated.model == before and updated.issues
        assert (runtime.out / "objects-repair/model.json").is_file()
        assert (runtime.out / "objects-repair/inventory.json").is_file()
        assert not list((runtime.out / "objects-repair").glob("round-*"))
        _, interaction_report, _ = await complete_interactions(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(enabled=False),
        )
        assert interaction_report["stop_reason"] == "disabled" and calls == []

    asyncio.run(run())


def test_truncation_retries_with_shared_request_budget_and_continuing_history():
    async def run_with_budget(max_requests):
        inputs = []

        def respond(messages, info):
            inputs.append(payload(messages))
            if len(inputs) == 1:
                return result(info, response(), finish_reason="length")
            assert len(messages) > 1
            return result(info, response())

        runtime, state, keywords, store, _, _ = setup(
            respond,
            semantic=True,
            max_requests=max_requests,
        )
        _, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=1),
        )
        assert report["rounds"] == 1
        assert len(records) == min(max_requests, 2)
        assert report["calls"] == min(max_requests, 2)
        assert records[0]["end_reason"] == "output_truncated"
        if max_requests > 1:
            assert records[0]["run_id"] != records[1]["run_id"]
            assert [row["attempt"] for row in records] == [1, 2]
            assert inputs[1]["previous_feedback"]
        assert report["active_issues"] == 1

    asyncio.run(run_with_budget(1))
    asyncio.run(run_with_budget(2))


def test_completion_can_remove_wrong_endpoint_sources_after_full_read():
    async def run():
        def respond(messages, info):
            channel = loaded_channel(messages)
            channel["endpoint_sources"] = []
            return result(info, response(channels=[channel]))

        runtime, state, keywords, store, _, _ = setup(respond, unknown=1)
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=1),
        )
        assert updated.model["channels"][0]["endpoint_sources"] == []
        assert not updated.issues and report["stop_reason"] == "no_missing"
        assert report["run_failures"] == 0
        assert records[0]["status"] == "ok" and records[0]["repair_targets"]

    asyncio.run(run())


def test_returning_an_unchanged_definition_does_not_close_a_semantic_issue():
    async def run():
        def respond(messages, info):
            return result(info, response(channels=[loaded_channel(messages)]))

        runtime, state, keywords, store, _, _ = setup(respond)
        pending = issue("Review control endpoint roles", ["control"])
        state.issues.append(pending)
        before = deepcopy(state.model)
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=1),
        )
        assert updated.model == before and pending in updated.issues
        assert report["stop_reason"] == "round_limit"
        assert report["run_failures"] == 0 and records[0]["status"] == "ok"

    asyncio.run(run())


def test_invalid_unrelated_edit_does_not_discard_an_independent_valid_repair():
    async def run():
        def respond(messages, info):
            channel = loaded_channel(messages)
            channel["endpoint_sources"][0]["from"] = "21"
            bad = {
                "name": "unrelated",
                "description": "bad direction",
                "transport": "TCP",
                "connection": "connectionless",
                "endpoint_party_names": ["requester"],
                "initiator_party_name": "requester",
                "acceptor_party_name": None,
                "endpoint_sources": [],
            }
            return result(info, response(channels=[channel, bad]))

        runtime, state, keywords, store, _, ev = setup(respond, unknown=1)
        updated, report, records = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=1),
        )
        assert updated.model["channels"][0]["endpoint_sources"][0]["from"] == "21"
        assert report["run_failures"] == 1 and updated.issues
        assert records[0]["recovery"]["rejected"][0]["object_names"] == ["unrelated"]
        assert all(set(row) == {"description", "object_names"} for row in updated.issues)

    asyncio.run(run())


def test_unknown_parameters_at_both_endpoints_have_independent_worklist_entries():
    async def run():
        visited = []

        def respond(messages, info):
            data = payload(messages)
            visited.append(data["issue"]["description"])
            channel = loaded_channel(messages)
            for endpoint in channel["endpoint_sources"]:
                if endpoint["parameter"] in data["issue"]["description"]:
                    endpoint["from"] = "21"
            return result(info, response(channels=[channel]))

        runtime, state, keywords, store, _, _ = setup(respond, unknown=1)
        other = deepcopy(state.model["channels"][0]["endpoint_sources"][0])
        other["party_name"] = "requester"
        other["parameter"] = "address"
        state.model["channels"][0]["endpoint_sources"].append(other)
        state.issues.append(issue("Complete address", ["control"]))
        updated, report, _ = await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(),
        )
        assert sorted(visited) == ["Complete address", "Complete port"]
        assert report["rounds"] == 1 and not updated.issues
        round_report = report["round_reports"][0]
        assert round_report["starting_missing"] == round_report["resolved"] == 2
        assert round_report["called_issues"] == 2 and round_report["ending_missing"] == 0

    asyncio.run(run())


def test_phase_filter_does_not_mark_an_unrelated_base_issue_resolved():
    from protocol_samples import interactions

    async def run():
        def respond(messages, info):
            return result(info, response())

        runtime, state, keywords, store, _, ev = setup(respond)
        party = {"name": "optional-peer", "description": "Optional remote endpoint"}
        state.model["parties"].append(party)
        state.model.update(
            {key: value for key, value in interactions(state.model).items() if key != "unresolved"}
        )
        pending = issue("Clarify optional peer role", ["optional-peer"])
        state.issues.append(pending)
        state.history.refresh(state.issues, 0)
        state.history.defer([pending], "Needs additional source", attempted=True)
        tracking = state.history.key(pending)
        state.history.entries[tracking]["phase"] = "objects"
        updated, report, _ = await complete_interactions(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(),
        )
        assert report["stop_reason"] == "no_missing" and pending in updated.issues
        assert updated.history.entries[tracking]["status"] == "deferred"
        assert updated.history.entries[tracking]["attempts"] == 1

    asyncio.run(run())


def test_coordinator_history_continues_between_object_and_interaction_completion():
    async def run():
        histories = []

        def respond(messages, info):
            data = payload(messages)
            histories.append((data["phase"], deepcopy(messages)))
            assert "inventory" not in data and "window" not in data
            output = response()
            if data["phase"] == "interactions":
                output.update(steps=[], step_dependencies=[])
            return result(info, output)

        runtime, state, keywords, store, _, _ = setup(respond, semantic=True)
        await complete_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(objects_max_rounds=1),
        )
        coordinator = runtime.coordinator
        state.issues = [issue("Investigate interaction ordering", [])]
        coordinator.handoff("interactions_complete", state.model, state.issues, state.revision)
        await complete_interactions(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=RepairOptions(interactions_max_rounds=1),
        )
        assert runtime.coordinator is coordinator
        second = next(messages for phase, messages in histories if phase == "interactions")
        assert len(second) > len(histories[0][1])
        assert any("stage_handoff" in str(message) for message in second)
        assert any(isinstance(message, ModelResponse) for message in second)

    asyncio.run(run())
