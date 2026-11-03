"""Block-stage orchestration with deterministic local models and private list state."""

import asyncio
import json
from collections import defaultdict
from copy import deepcopy

import pytest
from protocol_samples import document
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
from rsc.protocol.agent_runtime import EventWriter, RequestCache
from rsc.protocol.context import Corpus
from rsc.protocol.extraction import ExtractionRuntime, extract_interactions, extract_objects
from rsc.protocol.ids import GlobalIdAllocator, IdConsistencyError
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.options import AgentOptions, ContextOptions
from rsc.protocol.planning import plan_block_lists
from rsc.protocol.proposals import CandidateState
from rsc.protocol.store import ExtractionStore


def source(count=6):
    doc = document()
    template = next(block for block in doc["blocks"] if "also called user" in block["text"])
    doc["blocks"] = [
        {**deepcopy(template), "block_id": f"block-{i}", "order": i, "text": f"Endpoint role {i}."}
        for i in range(count)
    ]
    return Corpus({doc["meta"]["source_id"]: doc})


def payload(messages):
    """Read the most recent structured turn input, skipping validation feedback."""

    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
                continue
            try:
                value = json.loads(part.content)
            except ValueError:
                continue
            if isinstance(value, dict) and "stage" in value:
                return value
    raise AssertionError("No current structured turn input")


class Models:
    def __init__(self, *, fail_objects=None):
        self.fail_objects = fail_objects
        self.trace = []
        self.calls = defaultdict(int)
        self.active = 0
        self.peak = 0
        self.snapshots = []
        self.histories = []

    def __call__(self, stage, phase):
        async def respond(messages, info):
            data = payload(messages)
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                ref = data["focus_block"]
                self.calls[stage] += 1
                self.trace.append((stage, ref["block_id"]))
                self.snapshots.append(deepcopy(data))
                self.histories.append(deepcopy(messages))
                await asyncio.sleep(0.002 if ref["block_id"].endswith("0") else 0)
                if stage == "interactions":
                    output = {
                        "keywords": [{"text": "interaction"}],
                        "steps": [],
                        "step_dependencies": [],
                        "unresolved": [],
                    }
                else:
                    if ref["block_id"] == self.fail_objects:
                        raise RuntimeError("object extraction failed")
                    returned = [
                        p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
                    ]
                    name = "peer-" + ref["block_id"]
                    if not any(
                        part.tool_call_id == "search-" + ref["block_id"] for part in returned
                    ):
                        return ModelResponse(
                            [
                                ToolCallPart(
                                    "search_objects",
                                    {"query": name, "kind": "parties"},
                                    "search-" + ref["block_id"],
                                )
                            ]
                        )
                    output = {
                        "keywords": [{"text": "endpoint"}],
                        "parties": [
                            {
                                "name": name,
                                "description": "The endpoint role " + ref["block_id"],
                            }
                        ],
                        "messages": [],
                        "channels": [],
                        "unresolved": [],
                    }
                return ModelResponse([TextPart(json.dumps(output))])
            finally:
                self.active -= 1

        return FunctionModel(respond, model_name=f"stage-{stage}-{phase}")


def runtime(models, *, lists=2, out=None, cache=None, allocator=None, corpus=None):
    corpus = corpus or source()
    if out is not None:
        out.mkdir()
    return ExtractionRuntime(
        "test",
        corpus,
        plan_block_lists(corpus, lists),
        models,
        allocator or GlobalIdAllocator("test"),
        EventWriter(out, capture=True),
        {name: RequestLimits(max_input_bytes=262144) for name in ("objects", "interactions")},
        out=out,
        cache=cache,
        agent_options=AgentOptions(max_model_requests=4),
        context_options=ContextOptions(max_tokens=100000),
    )


def test_list_parallelism_changes_speed_not_ids_models_or_keyword_history(tmp_path):
    async def run():
        first_models = Models()
        one = runtime(first_models, out=tmp_path / "one")
        first = await extract_objects(one, 1)
        second_models = Models()
        many = runtime(second_models, out=tmp_path / "many", corpus=one.corpus)
        second = await extract_objects(many, 4)
        assert len(first.state.model["parties"]) == 6
        assert first.state.model == second.state.model
        assert first.keywords.snapshot() == second.keywords.snapshot()
        assert first.store.snapshot() == second.store.snapshot()
        assert first_models.peak == 1 and second_models.peak == 2
        assert first.report["agent_runs"] == 6 and first.report["run_failures"] == 0
        assert first.report["new_roots"]["parties"] == 6
        assert first.report["reused_roots"] == first.report["supplemented_roots"] == 0
        assert len(first.keywords.snapshot()["records"]) == 6
        assert len(first.keywords.snapshot()["postings"]) == 1
        assert not one.events.events.get("keyword-retrieval.jsonl")
        assert first_models.calls == {"objects": 12}
        assert all("window" not in row and "inventory" not in row for row in first_models.snapshots)
        assert all(
            set(row["focus_block"]) == {"doc_id", "block_id", "block_type", "text"}
            for row in first_models.snapshots
        )
        for group in many.plan.lists:
            ids = [focus.block_id for focus in group.blocks]
            trace = [
                (stage, identity) for stage, identity in second_models.trace if identity in ids
            ]
            assert trace == [
                pair for identity in ids for pair in [("objects", identity), ("objects", identity)]
            ]
            saved = json.loads(
                (many.out / "lists" / group.list_id / "objects-model.json").read_text()
            )
            assert len(saved["parties"]) == len(group.blocks)
        logs = [json.loads(line) for line in (many.out / "objects.jsonl").read_text().splitlines()]
        assert [row["sequence"] for row in logs] == list(range(6))
        # Each retained conversation sees its own previous blocks, never another list's turns.
        for history in second_models.histories:
            inputs = [
                json.loads(part.content)
                for message in history
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart)
                and isinstance(part.content, str)
                and part.content.startswith('{"')
            ]
            refs = [row["focus_block"]["block_id"] for row in inputs if "focus_block" in row]
            owners = {
                focus.list_id
                for group in many.plan.lists
                for focus in group.blocks
                if focus.block_id in refs
            }
            assert len(owners) == 1

    asyncio.run(run())


def test_cached_requests_reexecute_tools_and_remain_stable_across_worker_counts():
    async def run():
        cache = RequestCache()
        models = Models()
        first_runtime = runtime(models, cache=cache)
        first = await extract_objects(first_runtime, 1)
        count = dict(models.calls)
        second_runtime = runtime(models, cache=cache)
        second = await extract_objects(second_runtime, 2)
        assert dict(models.calls) == count
        assert first.state.model == second.state.model
        assert second.report["cache_hits"] == 12
        assert len(second_runtime.events.events["tool-calls.jsonl"]) == 6

    asyncio.run(run())


def test_list_history_supports_reuse_without_automatic_inventory():
    async def run():
        histories = []

        def factory(stage, phase):
            def respond(messages, info):
                data = payload(messages)
                assert "inventory" not in data and "window" not in data
                histories.append(deepcopy(messages))
                return ModelResponse(
                    [
                        TextPart(
                            json.dumps(
                                {
                                    "parties": [
                                        {"name": "peer", "description": "Protocol command sender"}
                                    ],
                                    "messages": [],
                                    "channels": [],
                                }
                            )
                        )
                    ]
                )

            return FunctionModel(respond, model_name="reuse-history")

        service = runtime(factory, lists=1, corpus=source(2))
        result = await extract_objects(service, 1)
        assert result.report["run_failures"] == 0
        assert len(result.state.model["parties"]) == 1
        assert len(histories) == 2 and len(histories[1]) > len(histories[0])
        assert not service.events.events.get("tool-calls.jsonl")

    asyncio.run(run())


def test_interactions_continue_sessions_and_extend_private_keyword_indexes():
    async def run():
        models = Models()
        service = runtime(models)
        objects = await extract_objects(service, 2)
        sessions = dict(service.sessions)
        assert objects.state.revision > max(session.model_revision for session in sessions.values())
        initial = objects.keywords.snapshot()
        for session in service.sessions.values():
            session.handoff(
                "interactions", objects.state.model, objects.state.issues, objects.state.revision
            )
        interactions = await extract_interactions(
            service,
            3,
            base=objects.state,
            keywords=objects.keywords,
            store=objects.store,
        )
        assert service.sessions == sessions
        assert interactions.state.revision > max(
            session.model_revision for session in sessions.values()
        )
        assert all(
            session.model_revision >= objects.state.revision for session in sessions.values()
        )
        assert all(item.session is sessions[item.list_id] for item in interactions.lists)
        assert models.calls.get("keywords", 0) == 0
        assert interactions.report["agent_runs"] == 6
        assert interactions.state.model == objects.state.model
        assert objects.keywords.snapshot() == initial
        assert len(interactions.keywords.snapshot()["records"]) == 12
        hits = interactions.keywords.search(["endpoint", "interaction"])["items"]
        assert len(hits) == 6
        assert all(len(hit["matched_keywords"]) == 2 for hit in hits)
        assert len(interactions.store.snapshot()) == 12

    asyncio.run(run())


def test_failed_block_records_failed_keyword_annotation_and_retains_history():
    async def run():
        models = Models(fail_objects="block-0")
        service = runtime(models, lists=1)
        result = await extract_objects(service, 1)
        assert result.report["run_failures"] == 1
        assert len(result.state.model["parties"]) == 5
        first = service.plan.lists[0].blocks[0]
        assert result.keywords.record(*first.key)["status"] == "failed"
        second_history = next(
            messages
            for messages in models.histories
            if payload(messages)["focus_block"]["block_id"] == "block-1"
        )
        assert any("block-0" in str(message) for message in second_history)
        assert service.sessions[first.list_id].turns[0].status == "failed"

    asyncio.run(run())


def test_tool_limit_retries_block_three_times_with_fresh_budgets():
    from dataclasses import replace

    async def run():
        object_attempts = 0

        def factory(stage, phase):
            def respond(messages, info):
                nonlocal object_attempts
                object_attempts += 1
                if object_attempts <= 3:
                    return ModelResponse(
                        [
                            ToolCallPart(
                                "search_objects",
                                {"query": "peer", "top_k": 1},
                                f"search-{object_attempts}-1",
                            ),
                            ToolCallPart(
                                "search_objects",
                                {"query": "peer", "top_k": 1},
                                f"search-{object_attempts}-2",
                            ),
                        ]
                    )
                return ModelResponse(
                    [
                        TextPart(
                            json.dumps(
                                {
                                    "parties": [{"name": "peer", "description": "Endpoint role"}],
                                    "messages": [],
                                    "channels": [],
                                    "unresolved": [],
                                }
                            )
                        )
                    ]
                )

            return FunctionModel(respond, model_name=f"retry-{stage}-{phase}")

        service = runtime(factory, corpus=source(1), lists=1)
        service.agent_options = replace(
            service.agent_options, max_tool_calls=1, max_block_retries=3
        )
        result = await extract_objects(service, 1)
        record = result.lists[0].records[0]

        assert object_attempts == 4
        assert record["status"] == "ok"
        assert record["attempt_count"] == 4
        assert record["retry_count"] == 3
        assert record["failed_attempt_count"] == 3
        assert [row["attempt"] for row in record["attempts"]] == [1, 2, 3, 4]
        assert [row["tool_call_count"] for row in record["attempts"]] == [1, 1, 1, 0]
        assert len({row["run_id"] for row in record["attempts"]}) == 4
        assert result.report["agent_runs"] == 4
        assert result.report["run_failures"] == 3
        assert result.report["block_retries"] == 3
        assert result.report["tool_call_count"] == 3
        assert [row["name"] for row in result.state.model["parties"]] == ["peer"]
        assert not result.state.issues

    asyncio.run(run())


def test_allocator_collision_aborts_the_extraction_barrier():
    async def run():
        def factory(stage, phase):
            async def respond(messages, info):
                output = {
                    "steps": [
                        {
                            "name": f"send {name}",
                            "message_name": "request",
                            "channel_name": "control",
                            "sender_name": "requester",
                            "receiver_name": "responder",
                            "condition": "",
                        }
                        for name in ("request", "response")
                    ],
                    "step_dependencies": [],
                    "unresolved": [],
                }
                return ModelResponse([TextPart(json.dumps(output))])

            return FunctionModel(respond, model_name="collision")

        service = runtime(
            factory,
            allocator=GlobalIdAllocator("test", id_factory=lambda key: "collision"),
            lists=1,
            corpus=source(1),
        )
        with pytest.raises(ExceptionGroup) as caught:
            await extract_interactions(
                service,
                1,
                base=CandidateState(),
                keywords=KeywordIndex("test", service.corpus),
                store=ExtractionStore(),
            )
        assert any(isinstance(error, IdConsistencyError) for error in caught.value.exceptions)

    asyncio.run(run())


def test_timeout_recovers_valid_components_of_a_complete_json_proposal():
    from dataclasses import replace

    async def run():
        def factory(stage, phase):
            calls = 0

            async def respond(messages, info):
                nonlocal calls
                calls += 1
                if calls > 1:
                    await asyncio.sleep(0.3)
                proposal = {
                    "parties": [{"name": "peer", "description": "Endpoint role"}],
                    "messages": [],
                    "channels": [
                        {
                            "name": "bad",
                            "description": "bad",
                            "transport": "TCP",
                            "connection": "connectionless",
                            "endpoint_party_names": ["peer"],
                            "initiator_party_name": "peer",
                            "acceptor_party_name": None,
                            "endpoint_sources": [],
                        }
                    ],
                    "unresolved": [
                        {"description": "Unsupported unrelated issue target", "object_names": []}
                    ],
                }
                # The valid party component can be recovered independently from the
                # invalid unrelated Issue when the validation retry later times out.
                return ModelResponse([TextPart(json.dumps(proposal))])

            return FunctionModel(respond, model_name="timeout-recovery")

        active = runtime(factory, corpus=source(1), lists=1)
        active.agent_options = replace(active.agent_options, max_run_seconds=0.15)
        result = await extract_objects(active, 1)
        assert [row["name"] for row in result.state.model["parties"]] == ["peer"]
        assert result.report["run_failures"] == 1 and result.state.issues
        record = result.store.snapshot()[0]
        assert record["status"] == "partial" and record["items"]["parties"]
        assert result.lists[0].records[0]["end_reason"] == "timeout"
        assert result.lists[0].records[0]["output_retry_count"] == 1
        session = result.lists[0].session
        receipts = [
            json.loads(part.content)
            for message in session.messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
            and isinstance(part.content, str)
            and '"event":"commit_receipt"' in part.content
        ]
        assert receipts[-1]["accepted"] is True
        assert receipts[-1]["items"]["parties"] == record["items"]["parties"]
        assert not receipts[-1]["items"]["channels"]

    asyncio.run(run())


def test_truncated_structured_output_is_not_recovered_as_a_valid_proposal():
    async def run():
        def factory(stage, phase):
            def respond(messages, info):
                output = {
                    "parties": [{"name": "peer", "description": "Endpoint role"}],
                    "messages": [],
                    "channels": [],
                    "unresolved": [],
                }
                return ModelResponse(
                    [TextPart(json.dumps(output))],
                    finish_reason="length" if stage == "objects" else "stop",
                )

            return FunctionModel(respond, model_name="truncated-recovery")

        result = await extract_objects(runtime(factory, corpus=source(1), lists=1), 1)
        assert not result.state.model["parties"] and result.state.issues
        assert result.store.snapshot()[0]["status"] == "failed"
        assert "recovery" not in result.lists[0].records[0]

    asyncio.run(run())


def test_keyword_phase_barrier_keeps_public_future_definitions_and_private_writes():
    corpus = source(4)
    plan = plan_block_lists(corpus, 2)
    indexes = []
    for group in plan.lists:
        index = KeywordIndex("test", corpus, list_id=group.list_id)
        for focus in group.blocks:
            index.commit(focus, {"keywords": [{"text": "shared"}]})
        indexes.append(index)
    public = KeywordIndex.merge(indexes)
    first = plan.lists[0].blocks[0]
    private = public.fork(list_id=first.list_id)
    other = public.fork(list_id=plan.lists[1].list_id)
    private.commit(first, {"keywords": [{"text": "shared"}, {"text": "private"}]})
    hits = private.search(["shared"], before_sequence=first.sequence, list_id=first.list_id)
    assert len(hits["items"]) == 4
    assert not other.search(["private"])["items"]
    assert not public.search(["private"])["items"]
    merged = KeywordIndex.merge([private, other])
    assert len(merged.search(["shared"])["items"]) == 4
    assert merged.record(*first.key, phase="objects")["phase"] == "objects"
    assert merged.record(*first.key, phase="interactions")["phase"] == "interactions"
