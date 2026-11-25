"""Exercise the incremental workflow with local tool-calling model responses."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from rsc.artifacts import read_jsonl
from rsc.extract.cli import ExtractConfig
from rsc.extract.registry import (
    LEGACY_SCAN_KINDS,
    ORDER,
    SCAN_KINDS,
    SCAN_WRITES,
    Assignment,
    EntityAddition,
    EntityUpdate,
    Evidence,
    Unresolved,
    Workspace,
    migrate_registry,
)
from rsc.extract.sources import Sources
from rsc.extract.tools import related
from rsc.protocol.validate import validate_protocol_file
from rsc.workflow.store import atomic_json, dumps, read_json


@pytest.fixture
def sources(tmp_path):
    result = []
    for number in (959, 3659):
        source = tmp_path / f"rfc{number}.txt"
        source.write_text(
            f"Network Working Group\nRequest for Comments: {number}\n\n"
            f"   Test protocol\n\n1. First Section\n\n   A client uses a server, see Section 2.\n"
            "2. Second Section\n\n   A server responds to a client.\n"
        )
        result.append(str(source))
    return Sources.create(tmp_path, result, 8000, 12000, [])


def citation(sources):
    doc_id, block_id = next(iter(sources.blocks))
    return {"document_id": doc_id, "block_id": block_id}


def party(sources, name):
    return {
        "kind": "parties",
        "value": {"name": name, "description": "Protocol endpoint"},
        "evidence": [citation(sources)],
    }


def test_batch_registry_summary_includes_all_evidence_matches(sources):
    batch_id, next_batch_id = sources.batches
    local = sources.batch_context(batch_id)
    remote = sources.batch_context(next_batch_id)
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "parties"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    remote_party = {
        **party(sources, "Client"),
        "aliases": ["server"],
        "evidence": [{"document_id": remote["doc_id"], "block_id": remote["segments"][0]["block"]}],
    }
    work.add_entities(add([remote_party]))
    assert related(work)["candidates"] == []

    entries = [
        {
            **party(sources, f"Endpoint {i}"),
            "aliases": [f"alias {i} " + "x" * 500],
            "evidence": [
                {"document_id": local["doc_id"], "block_id": local["segments"][0]["block"]}
            ],
        }
        for i in range(24)
    ]
    work.add_entities(add(entries[:20]))
    work.add_entities(add(entries[20:]))
    context = related(work)
    assert context["entity_counts"] == {kind: 25 if kind == "parties" else 0 for kind in ORDER}
    assert [row["name"] for row in context["candidates"]] == [f"Endpoint {i}" for i in range(24)]


def test_scan_prompt_keeps_every_open_item_and_only_their_citations():
    from rsc.extract.tools import projected_unresolved

    items = [
        {
            "id": "U1",
            "description": "Client and server disagree about who closes",
            "entity_ids": ["E1", "E2"],
            "missing_information": "which endpoint initiates the close",
            "check_refs": ["coverage:1"],
            "assignment": "batch-00001",
            "evidence": [
                {
                    "document_id": "rfc2326",
                    "block_id": "blk-000001",
                    "start": 10,
                    "end": 20,
                    "field": "/steps/0/name",
                    "note": "the state table",
                    "section_path": ["10.4"],
                },
                {
                    "document_id": "rfc2326",
                    "block_id": "blk-000001",
                    "start": 10,
                    "end": 20,
                    "field": "/steps/0/name",
                    "note": "same citation again",
                },
                {"document_id": "rfc2326", "block_id": "blk-000001", "field": ""},
                {
                    "document_id": "rfc7826",
                    "block_id": "blk-000002",
                    "field": "/steps/1/name",
                    "note": "second document",
                },
            ],
        },
        {
            "id": "U2",
            "description": "second issue",
            "evidence": [
                {
                    "document_id": "rfc2326",
                    "block_id": "blk-000001",
                    "field": "/steps/0/name",
                    "note": "cross-issue citation",
                }
            ],
        },
        {
            "id": "U3",
            "description": "legacy citation without a block",
            "evidence": [{"document_id": "rfc2326", "note": "no block id"}],
        },
    ]
    projected = projected_unresolved(items)

    # Every open item survives, in order, with its non-evidence keys untouched.
    assert [item["id"] for item in projected] == ["U1", "U2", "U3"]
    for original, copy in zip(items, projected, strict=True):
        for key, value in original.items():
            if key != "evidence":
                assert copy[key] == value

    first = projected[0]["evidence"]
    # Locations only: the same citation twice collapses, the empty field is kept as a
    # separate entry, and a different field on the same block stays distinct.
    assert first == [
        {"document_id": "rfc2326", "block_id": "blk-000001", "field": "/steps/0/name"},
        {"document_id": "rfc2326", "block_id": "blk-000001"},
        {"document_id": "rfc7826", "block_id": "blk-000002", "field": "/steps/1/name"},
    ]
    assert all(set(item) <= {"document_id", "block_id", "field"} for item in first)
    # A block cited by two items is kept for both.
    assert projected[1]["evidence"] == [
        {"document_id": "rfc2326", "block_id": "blk-000001", "field": "/steps/0/name"}
    ]
    # Evidence that carries no usable location is passed through unchanged.
    assert projected[2]["evidence"] == [{"document_id": "rfc2326", "note": "no block id"}]
    # The projection is a plain JSON list and never rewrites the registry items.
    assert json.loads(json.dumps(projected)) == projected
    assert items[0]["evidence"][0]["note"] == "the state table"
    assert items[0]["evidence"][0]["start"] == 10


def test_open_unresolved_reach_another_batch(sources):
    current, earlier = sources.batches
    remote = sources.batch_context(earlier)
    located = {"document_id": remote["doc_id"], "block_id": remote["segments"][0]["block"]}
    work = Workspace(sources, "test", empty_registry(), Assignment(earlier, "scan", "parties"))
    for i in range(12):
        work.record_unresolved(
            Unresolved(
                description=f"Missing definition {i} " + "x" * 1000,
                entity_ids=[],
                evidence=[located],
                missing_information=f"Need definition {i}",
            )
        )
    work.solve_unresolved("U1", "Definition found", [Evidence.model_validate(located)])
    work = Workspace(sources, "test", work.data, Assignment(current, "scan", "channels"))
    items = related(work)["unresolved"]
    # Closed items are gone and the open ones keep their registration order.
    assert [item["id"] for item in items] == [f"U{i}" for i in range(2, 13)]
    assert items[-1]["missing_information"] == "Need definition 11"
    # The remote batch's citation survives projection exactly, and never picks up a field.
    assert [item["evidence"] for item in items] == [[located] for _ in items]


def test_source_map_and_snapshot(sources, tmp_path):
    assert len(sources.batches) == 2
    for key, block in sources.blocks.items():
        if not block["text"].strip():
            continue
        locator = sources.evidence(*key)
        assert key in sources.sections[(key[0], tuple(locator["section_path"]))]
        assert sources.block_batches[key]
        assert sources.originals[key[0]][locator["start"] : locator["end"]].strip()
    doc_id, block_id = next(iter(sources.blocks))
    short = f"blk-{int(block_id.split('-')[1])}"
    assert sources.evidence(doc_id, short) == sources.evidence(doc_id, block_id)
    first = sources.manifest["documents"][0]
    (tmp_path / first["input"]).write_text("input edited")
    Sources(tmp_path, sources.manifest)
    (tmp_path / first["snapshot"]).write_text("frozen input edited")
    with pytest.raises(ValueError, match="source snapshot changed"):
        Sources(tmp_path, sources.manifest)


def empty_registry():
    return {
        "entities": {},
        "redirects": {},
        "unresolved": {},
        "withdrawn": {},
        "next_entity": 0,
        "next_issue": 0,
    }


def test_batch_writes_are_atomic_and_idempotent(sources):
    work = Workspace(sources, "test", empty_registry(), Assignment("b", "scan", "parties"))
    add = TypeAdapter(list[EntityAddition])
    first = party(sources, "Client")
    broken = {**party(sources, "Server"), "evidence": [{"document_id": "absent", "block_id": "?"}]}
    with pytest.raises(ValueError, match="unknown source"):
        work.add_entities(add.validate_python([first, broken]))
    assert not work.records
    entities = add.validate_python([first, party(sources, "Server")])
    assert [item["id"] for item in work.add_entities(entities)] == ["E1", "E2"]
    assert work.add_entities(entities) == work.add_entities(entities)
    cite = {**citation(sources), "field": "/description"}
    update = [
        EntityUpdate(id="E1", patch={"description": "sender"}, evidence=[citation(sources)]),
        EntityUpdate(id="E2", patch={"description": "receiver"}, evidence=[cite]),
    ]
    with pytest.raises(ValueError, match="duplicate update"):
        work.update_entities([update[0], update[0]])
    assert "revision" not in work.records["E1"]
    assert work.records["E1"]["value"]["description"] == "Protocol endpoint"
    assert [row["id"] for row in work.update_entities(update)] == ["E1", "E2"]
    assert work.update_entities(update)[0]["id"] == "E1"
    assert len(work.mutations) == 2
    work.update_entities(
        [EntityUpdate(id="E1", patch={"description": "initiator"}, evidence=[cite])]
    )
    work.update_entities(update)
    assert work.records["E1"]["value"]["description"] == "sender"
    assert work.records["E1"]["evidence"][-1]["field"] == "/description"


def test_merge_redirects_and_rewrites_channel_references(sources):
    work = Workspace(sources, "test", empty_registry(), Assignment("global", "global"))
    add = TypeAdapter(list[EntityAddition])
    work.add_entities(
        add.validate_python(
            [
                party(sources, "Client"),
                party(sources, "User"),
                {
                    "kind": "channels",
                    "evidence": [citation(sources)],
                    "value": {
                        "name": "c",
                        "description": "connection",
                        "transport": "TCP",
                        "connection": "connection_oriented",
                        "endpoint_party_names": ["User"],
                        "initiator_party_name": "User",
                        "acceptor_party_name": None,
                        "endpoint_sources": [
                            {"party_name": "User", "parameter": "port", "from": "fixed"}
                        ],
                    },
                },
            ]
        )
    )
    receipt = work.merge_entities(
        "E1",
        ["E2"],
        "RFC uses interchangeable names",
        [Evidence(**citation(sources))],
    )
    assert receipt["id"] == work.canonical("E2") == "E1"
    channel = work.records["E3"]
    assert "revision" not in channel
    assert channel["value"]["initiator_party_name"] == "Client"
    assert channel["value"]["endpoint_sources"][0]["party_name"] == "Client"
    assert not work.structural_issues()


def entity(sources, kind, **value):
    return {"kind": kind, "value": value, "evidence": [citation(sources)]}


def channel(sources, name, *, connected=True):
    return entity(
        sources,
        "channels",
        name=name,
        description="Channel",
        transport="TCP" if connected else "UDP",
        connection="connection_oriented" if connected else "connectionless",
        endpoint_party_names=["Client"],
        initiator_party_name="Client" if connected else None,
        acceptor_party_name="Client" if connected else None,
        endpoint_sources=[],
    )


def fixtures_by_kind(sources):
    return {
        "parties": party(sources, "Client"),
        "channels": channel(sources, "Datagrams", connected=False),
        "messages": entity(sources, "messages", name="Packet", associated_fields=[]),
        "steps": entity(
            sources,
            "steps",
            name="Send",
            action="send",
            channel_name="Datagrams",
            sender_name="Client",
            receiver_name="Client",
            message_name="Packet",
            condition="",
        ),
        "step_dependencies": entity(
            sources,
            "step_dependencies",
            prior_step="Send",
            dependent_step="Send",
            condition="repeat",
            field_relations=[],
        ),
    }


def interaction_fixtures(sources):
    """One interaction unit: the step, the state it writes and its dependency."""
    return {
        "steps": entity(
            sources,
            "steps",
            name="Send",
            action="send",
            channel_name="Datagrams",
            sender_name="Client",
            receiver_name="Client",
            message_name="Packet",
            condition="",
            processing={
                "before_send": [{"reads": [], "writes": ["state.Flag"], "rule": "Set flag"}]
            },
        ),
        "states": entity(
            sources, "states", name="Flag", party_name="Client", source="processing"
        ),
        "step_dependencies": entity(
            sources,
            "step_dependencies",
            prior_step="Send",
            dependent_step="Send",
            condition="repeat",
            field_relations=[],
        ),
    }


def fake_model(sources, turns, *, add_party=True):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    def run(messages, info):
        if isinstance(messages[-1].parts[-1], UserPromptPart):
            prompt = json.loads(messages[-1].parts[-1].content)
            if prompt.get("stage") not in ("review", "gap"):
                turns.append((prompt["assignment"], len(messages)))
        else:
            prompt = next(
                json.loads(part.content)
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
        if prompt.get("stage") == "review":
            return ModelResponse(parts=[ToolCallPart("complete_review", {})])
        if prompt.get("stage") == "gap":
            return ModelResponse(parts=[ToolCallPart("complete_gaps", {})])
        task = prompt["assignment"]
        already_added = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "add_entities"
            for message in messages
            for part in message.parts
        )
        if add_party and task["phase"] == "scan" and not already_added:
            fixtures = fixtures_by_kind(sources)
            if task["kind"] == "steps":
                entities = list(interaction_fixtures(sources).values())
            elif task["kind"] == "basics":
                entities = [fixtures[kind] for kind in SCAN_WRITES["basics"]]
            else:
                entities = [fixtures[task["kind"]]]
            return ModelResponse(
                parts=[
                    ToolCallPart("add_entities", {"entities": entities}),
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "complete_batch" if task["phase"] == "scan" else "complete_fix",
                    {"summary": "Read RFC"},
                ),
            ]
        )

    return FunctionModel(run)


def strip_suggestions(rows):
    return [{k: v for k, v in row.items() if k != "suggestion"} for row in rows]


def no_review():
    """Review/gap stub: finish each batch immediately with no gaps."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    def run(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            tool = "complete_review" if prompt.get("stage") == "review" else "complete_gaps"
            return ModelResponse(parts=[ToolCallPart(tool, {})])
        return ModelResponse(parts=[])

    return FunctionModel(run)


def test_complete_run_and_resume(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from rsc.extract.runner import Runner

    turns = []
    model = fake_model(sources, turns)
    first = Runner(tmp_path, sources, "test", ExtractConfig(), model, no_review())
    save = first.save

    def interrupt(**values):
        save(**values)
        if values.get("batch") == 1:
            raise OSError("interrupted after commit")

    monkeypatch.setattr(first, "save", interrupt)
    with pytest.raises(OSError, match="after commit"):
        asyncio.run(first.run())
    initial_log = tmp_path / "llm" / "scan" / "basics" / "batch-00001.jsonl"
    saved = read_jsonl(initial_log)
    assert [row["event"] for row in saved] == ["request", "response", "request", "response"]
    second = Runner(tmp_path, sources, "test", ExtractConfig(), model, no_review())
    asyncio.run(second.run())
    assert read_jsonl(initial_log) == saved
    assert (tmp_path / "llm" / "scan" / "steps" / "batch-00001.jsonl").is_file()
    assert [(task["kind"], task["id"]) for task, _ in turns] == [
        (kind, batch) for kind in SCAN_KINDS for batch in sources.batches
    ]
    assert turns[0][1] == 1
    assert turns[1][1] > 1
    checkpoint = read_json(tmp_path / "extract-checkpoint.json")
    assert len(checkpoint["registry"]["entities"]) == 6
    assert checkpoint["global_checks"] == 1
    assert not validate_protocol_file(tmp_path / "protocol.json")
    assert read_json(tmp_path / "unresolved.json") == []


def test_resume_rejects_a_six_pass_checkpoint(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    from rsc.extract.runner import Runner

    atomic_json(
        tmp_path / "extract-checkpoint.json",
        {
            "scan_kinds": list(ORDER),
            "category": 0,
            "batch": 0,
            "global_checks": 0,
            "global_errors": None,
            "registry": empty_registry(),
            "history": [],
            "compact_summary": "",
            "records": [],
            "published": False,
        },
    )
    with pytest.raises(ValueError, match="cannot change scan layout on resume"):
        Runner(tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review())


def test_model_log_keeps_failed_and_successful_attempts_in_one_file(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    path = tmp_path / "llm" / "category" / "steps" / "validate-steps-7.jsonl"
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        assert read_jsonl(path)[-1]["event"] == "request"
        if calls == 1:
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"event":"interrupted')
            raise ModelHTTPError(502, "test", {"error": "upstream unavailable"})
        if calls == 2:
            return ModelResponse(
                parts=[
                    ThinkingPart(content="test reasoning"),
                    ToolCallPart("validate_entities", {}),
                ]
            )
        return ModelResponse(parts=[ToolCallPart("complete_fix", {"summary": "checked"})])

    config = ExtractConfig(assignment_retries=1, history_tool_chars=1)
    runner = Runner(tmp_path, sources, "test", config, FunctionModel(respond), no_review())
    work = runner.workspace(Assignment("validate-steps-7", "category", "steps"))

    async def run():
        async with runner.toolset:
            return await runner.retry(work)

    history, record = asyncio.run(run())
    assert record["status"] == "completed"
    rows = read_jsonl(path)
    assert [(r["assignment_attempt"], r["request_number"], r["event"]) for r in rows] == [
        (1, 1, "request"),
        (1, 1, "error"),
        (2, 1, "request"),
        (2, 1, "response"),
        (2, 2, "request"),
        (2, 2, "response"),
    ]
    assert rows[0]["run_id"] == rows[1]["run_id"] != rows[2]["run_id"]
    assert len({r["run_id"] for r in rows[2:]}) == 1
    assert rows[1]["error"]["body"] == {"error": "upstream unavailable"}
    assert rows[2]["model_request_parameters"]["instruction_parts"]
    assert any(
        tool["name"] == "validate_entities"
        for tool in rows[2]["model_request_parameters"]["function_tools"]
    )
    assert rows[3]["response"]["parts"][0]["content"] == "test reasoning"
    assert history[0]["phase"] == "category" and history[0]["kind"] == "steps"
    assert history[0]["assignment_id"] == "validate-steps-7"
    # Completed rows are stored as produced; only budget pressure condenses them.
    parts = [part for message in rows[4]["messages"] for part in message["parts"]]
    result = next(p for p in parts if p["part_kind"] == "tool-return")
    assert result["content"]["error_count"] == 0
    assert result["content"]["current_group"] is None


def test_failed_assignment_records_spent_time_and_usage(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.exceptions import ModelAPIError
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("validate_entities", {})])
        raise ModelAPIError("test-model", "prompt rejected")

    config = ExtractConfig(assignment_retries=0)
    runner = Runner(tmp_path, sources, "test", config, FunctionModel(respond), no_review())
    batch_id = next(iter(sources.batches))
    work = runner.workspace(Assignment(batch_id, "scan", "parties"))

    async def run():
        async with runner.toolset:
            return await runner.retry(work)

    history, record = asyncio.run(run())
    assert record["status"] == "failed"
    assert "prompt rejected" in record["summary"]
    # The attempt worked for one request before the provider refused, and that time
    # and usage must survive into the checkpoint.
    assert record["elapsed_seconds"] > 0
    assert record["usage"]["requests"] >= 1
    assert record["tool_calls"] >= 1
    assert history == []


def test_review_stage_retries_a_model_error(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.exceptions import ModelAPIError
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    calls = 0

    def review(messages, info):
        nonlocal calls
        calls += 1
        latest = messages[-1].parts[-1]
        assert isinstance(latest, UserPromptPart)
        assert json.loads(latest.content)["stage"] == "review"
        if calls == 1:
            raise ModelAPIError("test-model", "prompt flagged by the provider")
        return ModelResponse(parts=[ToolCallPart("complete_review", {})])

    config = ExtractConfig(assignment_retries=1, review_requests_per_second=100)
    runner = Runner(
        tmp_path, sources, "test", config, fake_model(sources, []), FunctionModel(review)
    )
    asyncio.run(runner.run())

    batches = list(sources.batches)
    assert calls == len(batches) + 1
    assert {entry["stage"] for entry in runner.state["review"]["batches"].values()} == {
        "reviewed"
    }
    rows = read_jsonl(tmp_path / "llm" / "review" / f"{batches[0]}.jsonl")
    assert [(row["assignment_attempt"], row["event"]) for row in rows] == [
        (1, "request"),
        (1, "error"),
        (2, "request"),
        (2, "response"),
    ]
    review_records = [r for r in runner.state["records"] if r["phase"] == "review"]
    assert len(review_records) == len(batches)
    first = runner.state["review"]["batches"][batches[0]]
    # A provider error before any response bills no request, but the attempt's
    # wall clock is real and now survives into the checkpoint.
    assert first["review_retry_seconds"] > 0
    assert isinstance(first["review_retry_usage"], dict)


def test_make_agent_uses_configured_tool_retries(sources):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import FunctionToolset

    from rsc.extract.runner import make_agent

    agent = make_agent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[])),
        FunctionToolset(),
        ExtractConfig(max_tool_retries=3),
    )
    # pydantic_ai keeps this on a private attribute.
    assert agent._max_tool_retries == 3  # noqa: SLF001


def test_global_limit_is_durable(sources, tmp_path):
    pytest.importorskip("fastmcp")
    from rsc.extract.runner import Runner

    config = ExtractConfig()
    model = fake_model(sources, [], add_party=False)
    for _ in range(2):
        runner = Runner(tmp_path, sources, "test", config, model, no_review())
        with pytest.raises(ValueError, match="failed after 6 checks"):
            asyncio.run(runner.run())
        assert runner.state["global_checks"] == 6
        assert not (tmp_path / "protocol.json").exists()


def test_category_validation_is_visible_and_repairable(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    seen = []
    fallback = fake_model(sources, []).function

    def respond(messages, info):
        prompt = next(
            json.loads(part.content)
            for message in reversed(messages)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        task = prompt["assignment"]
        latest = messages[-1].parts[-1]
        if task["kind"] == "basics" and task["phase"] == "scan":
            if isinstance(latest, UserPromptPart):
                # Every batch repeats the same names, so the merged pass records
                # duplicates that the basics validation must surface and repair.
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "add_entities",
                            {
                                "entities": [
                                    party(sources, "Client"),
                                    channel(sources, "Datagrams", connected=False),
                                    entity(
                                        sources,
                                        "messages",
                                        name="Packet",
                                        associated_fields=[],
                                    ),
                                ]
                            },
                        )
                    ]
                )
        if task["phase"] == "category":
            if isinstance(latest, UserPromptPart):
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "validate_entities", {"view": "issues", "issue_type": "same_name"}
                        )
                    ]
                )
            if isinstance(latest, ToolReturnPart) and latest.tool_name == "validate_entities":
                seen.append(latest.content)
                duplicates = [
                    row for row in latest.content["issues"] if row["type"] == "same_name"
                ]
                if duplicates:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                "merge_entities",
                                {
                                    "keep_id": row["records"][0],
                                    "ids": row["records"][1:],
                                    "reason": "same concept",
                                    "evidence": [citation(sources)],
                                },
                            )
                            for row in duplicates
                        ]
                    )
        if task["phase"] == "scan" and task["kind"] != "basics":
            return fallback(messages, info)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "complete_batch" if task["phase"] == "scan" else "complete_fix",
                    {"summary": "Complete"},
                )
            ]
        )

    model = FunctionModel(respond)
    runner = Runner(tmp_path, sources, "test", ExtractConfig(), model, no_review())
    save = runner.save

    def interrupt(**values):
        save(**values)
        if values.get("category_repairs") == 1:
            raise OSError("interrupted after repair commit")

    monkeypatch.setattr(runner, "save", interrupt)
    with pytest.raises(OSError, match="after repair commit"):
        asyncio.run(runner.run())
    runner = Runner(tmp_path, sources, "test", ExtractConfig(), model, no_review())
    asyncio.run(runner.run())
    assert seen
    assert "same_name" in str(seen[0])
    targets = {row["to"] for row in runner.state["registry"]["redirects"].values()}
    assert targets == {"E1", "E2", "E3"}
    assert not validate_protocol_file(tmp_path / "protocol.json")


def test_party_alias_projection_preserves_only_declared_party_links(sources):
    work = Workspace(sources, "test", empty_registry(), Assignment("global", "global"))
    client = party(sources, "Client")
    client["value"]["alias"] = ["initiating client", "User", "Client"]
    work.add_entities(
        TypeAdapter(list[EntityAddition]).validate_python([client, party(sources, "User")])
    )
    assert work.protocol_model()["parties"][0]["alias"] == ["User"]
    assert work.records["E1"]["value"]["alias"] == ["initiating client", "User", "Client"]


def test_lifecycle_checks_mature_and_each_channel_requires_its_own_actions(sources):
    add = TypeAdapter(list[EntityAddition]).validate_python
    work = Workspace(
        sources, "test", empty_registry(), Assignment("check-party", "category", "parties")
    )
    work.add_entities(add([party(sources, "Client")]))
    assert work.deferred_checks() == ["party_channel"]
    assert not work.issues()
    work = Workspace(
        sources, "test", work.data, Assignment("check-channel", "category", "channels")
    )
    assert work.issues()[0]["type"] == "party_without_channel"
    work.add_entities(
        add(
            [
                channel(sources, "One"),
                channel(sources, "Two"),
                channel(sources, "Datagrams", connected=False),
            ]
        )
    )
    assert "channel_step" in work.deferred_checks()
    assert not work.issues()
    work = Workspace(sources, "test", work.data, Assignment("check-steps", "category", "steps"))
    issues = work.issues()
    assert all("blocking" not in issue for issue in issues)
    assert {f["check_ref"] for f in issues if f["type"].startswith("channel_requires_")} == {
        f"channel_requires_{action}:{key}"
        for key in ("E2", "E3")
        for action in ("connect", "listen", "close")
    }
    assert all(f["evidence"] for f in issues)
    with pytest.raises(ValueError, match="not an open coverage check"):
        work.record_unresolved(
            Unresolved(
                description="Missing action",
                entity_ids=["E2"],
                evidence=[citation(sources)],
                check_refs=["channel_requires_connect:E999"],
            )
        )
    assert not work.index.incoming["E2"]
    work.add_entities(
        add(
            [
                entity(
                    sources,
                    "steps",
                    name=action,
                    action=action,
                    party_name="Client",
                    channel_name="One",
                    condition="",
                )
                for action in ("connect", "listen", "close")
            ]
        )
    )
    assert len(work.index.incoming["E2"]) == 3
    assert {
        f["check_ref"] for f in work.issues() if f["type"].startswith("channel_requires_")
    } == {
        f"channel_requires_{action}:E3" for action in ("connect", "listen", "close")
    }


def test_cross_category_coverage_remains_open_until_repaired(sources):
    add = TypeAdapter(list[EntityAddition]).validate_python
    fixture = fixtures_by_kind(sources)
    work = Workspace(sources, "test", empty_registry(), Assignment("seed", "global"))
    work.add_entities(
        add(
            [
                *(fixture[k] for k in ORDER[:3]),
                entity(sources, "states", name="flag", party_name="Client", source="processing"),
                entity(sources, "messages", name="Component", associated_fields=[]),
            ]
        )
    )
    work.record_unresolved(
        Unresolved(
            description="The wire packet lacks its action",
            entity_ids=["E3"],
            evidence=[citation(sources)],
        )
    )
    work.data["unresolved"]["U1"]["deferred_kind"] = "steps"
    assert migrate_registry(work.data)
    work = Workspace(
        sources, "test", work.data, Assignment("check-messages", "category", "messages")
    )
    assert not work.issues()
    work = Workspace(sources, "test", work.data, Assignment("check-steps", "category", "steps"))
    checks = {f["check_ref"] for f in work.issues()}
    assert {"state_without_access:E4", "message_without_send:E3"} <= checks
    assert "deferred_entity:U1" not in checks
    assert "deferred_kind" not in work.open_unresolved()[0]
    work.add_entities(add([fixture["steps"]]))
    work.record_unresolved(
        Unresolved(
            description="The Component is embedded in the wire packet",
            entity_ids=["E5"],
            evidence=[citation(sources)],
            check_refs=["message_without_send:E5"],
        )
    )
    work.record_unresolved(
        Unresolved(
            description="State hook cannot be completed with supplied sources",
            entity_ids=["E4"],
            evidence=[citation(sources)],
            check_refs=["state_without_access:E4"],
        )
    )
    cite = Evidence.model_validate(citation(sources))
    work.solve_unresolved("U1", "Packet send has been added", [cite])
    assert {issue["check_ref"] for issue in work.issues()} == {
        "message_without_send:E5",
        "state_without_access:E4",
        "step_without_dependency:E6",
    }
    with pytest.raises(ValueError, match="linked coverage checks remain: state_without_access:E4"):
        work.solve_unresolved("U3", "Premature closure", [cite])
    work.update_entities(
        [EntityUpdate(id="E5", withdraw_reason="Component is an embedded field", evidence=[cite])]
    )
    assert work.solve_unresolved("U2", "Withdrew the standalone component", [cite])["resolved"]
    assert {issue["check_ref"] for issue in work.issues()} == {
        "state_without_access:E4",
        "step_without_dependency:E6",
    }
    assert work.protocol_model()["unresolved"][0]["object_names"] == ["flag"]
    assert not work.protocol_model()["meta"]["complete"]
    work = Workspace(
        sources, "test", work.data, Assignment("check-deps", "category", "step_dependencies")
    )
    assert {f["check_ref"] for f in work.issues()} == {
        "state_without_access:E4", "step_without_dependency:E6"
    }
    work.add_entities(add([fixture["step_dependencies"]]))
    work.update_entities(
        [
            EntityUpdate(
                id="E6",
                patch={
                    "processing": {
                        "before_send": [{"reads": [], "writes": ["state.flag"], "rule": "set"}]
                    }
                },
                evidence=[{**citation(sources), "field": "/processing"}],
            )
        ]
    )
    assert not work.issues()
    receipt = work.solve_unresolved("U3", "The send step now writes the flag", [cite])
    assert receipt == {"id": "U3", "resolved": True}
    assert work.solve_unresolved("U3", "The send step now writes the flag", [cite]) == receipt
    assert work.protocol_model()["meta"]["complete"]


def test_legacy_coverage_stays_valid_only_until_its_entity_changes(sources):
    work = Workspace(sources, "test", empty_registry(), Assignment("check", "category", "steps"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(
        add([entity(sources, "messages", name=name, associated_fields=[]) for name in ("A", "B")])
    )
    cite = citation(sources)
    for i in (1, 2):
        key = f"E{i}"
        ref = f"message_without_send:{key}"
        result = work.record_unresolved(
            Unresolved(
                description=f"{key} is embedded",
                entity_ids=[key],
                evidence=[cite],
                check_refs=[ref],
            )
        )
        record = work.data["unresolved"][result["id"]]
        record["disposition"] = "exempt"
        record.pop("check_entity_ids")
        record["check_revisions"] = {key: 1}
        work.records[key]["revision"] = i
    assert migrate_registry(work.data)
    assert all("revision" not in row for row in work.records.values())
    assert all("check_revisions" not in item for item in work.data["unresolved"].values())
    assert all("disposition" not in item for item in work.data["unresolved"].values())
    work = Workspace(sources, "test", work.data, Assignment("check", "category", "steps"))
    assert [item["id"] for item in work.open_unresolved()] == ["U1"]
    assert [item["check_ref"] for item in work.issues()] == [
        "message_without_send:E1", "message_without_send:E2"
    ]
    work.update_entities([EntityUpdate(id="E1", patch={"name": "Renamed A"}, evidence=[cite])])
    assert work.open_unresolved() == []
    assert {item["check_ref"] for item in work.issues()} == {
        "message_without_send:E1",
        "message_without_send:E2",
    }


def test_sixth_global_validation_can_publish(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    check = Workspace.structural_issues
    calls = []
    reviewed = []
    fallback = fake_model(sources, []).function

    def fail_first_five(work):
        if work.assignment.id == "global-validation":
            calls.append(work.assignment.id)
            if len(calls) <= 5:
                return [
                    {"type": "test", "records": [], "path": [], "description": f"try again {i}"}
                    for i in range(10)
                ]
        return check(work)

    def review(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt["assignment"]["phase"] == "global":
                reviewed.append(prompt["validation_errors"])
        return fallback(messages, info)

    monkeypatch.setattr(Workspace, "structural_issues", fail_first_five)
    runner = Runner(tmp_path, sources, "test", ExtractConfig(), FunctionModel(review), no_review())
    asyncio.run(runner.run())
    assert len(calls) == runner.state["global_checks"] == 6
    checks = [
        strip_suggestions(r["issues"])
        for r in runner.state["records"]
        if r["phase"] == "global-validation"
    ]
    assert [len(issues) for issues in reviewed] == [10] * 5
    assert [len(rows) for rows in checks] == [10] * 5 + [0]
    assert all(rows[0]["type"] == "test" for rows in checks[:-1])
    assert len(list((tmp_path / "llm" / "global").glob("global-fix-*.jsonl"))) == 5
    assert read_json(tmp_path / "protocol.json")["meta"]["complete"]


@pytest.mark.parametrize("repair_coverage", [False, True])
def test_global_fix_rechecks_tracked_coverage_and_unresolved_after_resume(
    sources, tmp_path, monkeypatch, repair_coverage
):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    reviewed = []
    cite = citation(sources)
    check_ref = "state_without_access:E6"
    flag_hook = {"reads": [], "writes": ["state.Flag"], "rule": "Set flag"}

    def check_details(rows):
        assert len(rows) == 1
        check = rows[0]
        assert check["check_ref"] == check_ref
        assert check["records"] == ["E6"]
        assert "Flag" in check["description"]
        assert check["evidence"][0]["block_id"] == cite["block_id"]

    def respond(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            assert prompt["assignment"]["phase"] == "global"
            assert prompt["validation_error_count"] == 1
            check_details(prompt["validation_errors"])
            assert prompt["repairable_categories"] == list(ORDER)
            reviewed.extend(row["id"] for row in prompt["unresolved"])
            assert "E6" in prompt["entities"]
            assert set(prompt["entities"]) <= {"E1", "E6"}
            assert prompt["unresolved"][-1]["check_refs"] == [check_ref]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "solve_unresolved",
                        {
                            "unresolved_id": "U1",
                            "reason": "The current model covers the referenced role",
                            "evidence": [cite],
                        },
                    )
                ]
            )
        if isinstance(latest, ToolReturnPart) and latest.tool_name == "solve_unresolved":
            assert latest.content == {"id": "U1", "resolved": True}
            return ModelResponse(parts=[ToolCallPart("validate_entities", {})])
        if isinstance(latest, ToolReturnPart) and latest.tool_name == "update_entities":
            assert latest.content[0]["id"] == "E4"
            return ModelResponse(parts=[ToolCallPart("complete_fix", {"summary": "Repaired flag"})])
        assert isinstance(latest, ToolReturnPart) and latest.tool_name == "validate_entities"
        assert latest.content["error_count"] == 1
        check_details(latest.content["rows"])
        assert [row["id"] for row in latest.content["unresolved"]] == reviewed[1:]
        if repair_coverage:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_entities",
                        {
                            "entities": [
                                {
                                    "id": "E4",
                                    "patch": {"processing": {"before_send": [flag_hook]}},
                                    "evidence": [{**cite, "field": "/processing"}],
                                }
                            ]
                        },
                    )
                ]
            )
        return ModelResponse(parts=[ToolCallPart("complete_fix", {"summary": "Reviewed gaps"})])

    config = ExtractConfig(max_global_validation_attempts=2)
    model = FunctionModel(respond)
    runner = Runner(tmp_path, sources, "test", config, model, no_review())
    work = runner.workspace(Assignment("seed", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add(list(fixtures_by_kind(sources).values())))
    for i in range(12):
        work.record_unresolved(
            Unresolved(description=f"Open item {i}", entity_ids=["E1"], evidence=[cite])
        )
    work.add_entities(
        add([entity(sources, "states", name="Flag", party_name="Client", source="processing")])
    )
    work.record_unresolved(
        Unresolved(
            description="The flag has no hook yet",
            entity_ids=["E6"],
            evidence=[cite],
            check_refs=[check_ref],
        )
    )
    runner.save(
        registry=work.data,
        category=len(SCAN_KINDS),
        records=[
            {"phase": "scan", "kind": kind, "id": batch}
            for kind in SCAN_KINDS
            for batch in sources.batches
        ],
    )
    save = runner.save

    def interrupt(**updates):
        save(**updates)
        if updates.get("global_checks") == 1:
            raise OSError("interrupted before global Fix")

    monkeypatch.setattr(runner, "save", interrupt)
    with pytest.raises(OSError, match="before global Fix"):
        asyncio.run(runner.run())
    assert not reviewed
    assert not runner.state["published"]
    check_details(runner.state["global_errors"])
    runner = Runner(tmp_path, sources, "test", config, model, no_review())
    if repair_coverage:
        asyncio.run(runner.run())
        assert runner.state["published"]
        assert runner.state["global_errors"] == []
        expected = reviewed[1:-1]
        assert [row["id"] for row in read_json(tmp_path / "unresolved.json")] == expected
        assert not read_json(tmp_path / "protocol.json")["meta"]["complete"]
    else:
        with pytest.raises(ValueError, match="global validation failed after 2 checks"):
            asyncio.run(runner.run())
        assert not runner.state["published"]
        check_details(runner.state["global_errors"])
        assert not (tmp_path / "protocol.json").exists()
        expected = reviewed[1:]
    assert reviewed == [f"U{i}" for i in range(1, 14)]
    assert runner.state["global_checks"] == 2
    receipt = next(row for row in runner.state["records"] if row["phase"] == "global")
    assert receipt["status"] == "completed_with_unresolved"
    assert receipt["unresolved_ids"] == sorted(expected)


@pytest.mark.parametrize(
    ("phase", "kind", "categories"),
    [
        ("scan", "messages", ("messages",)),
        ("scan", "basics", ("parties", "channels", "messages")),
        ("scan", "steps", ("steps", "states", "step_dependencies")),
        ("category", "basics", ORDER[:3]),
        ("category", "steps", ORDER),
        ("global", None, ORDER),
    ],
)
def test_scan_and_fix_prompt_describe_write_scope(sources, tmp_path, phase, kind, categories):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from rsc.extract.runner import Runner

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    assignment_id = next(iter(sources.batches)) if phase == "scan" else "fix"
    work = runner.workspace(Assignment(assignment_id, phase, kind))
    prompt = json.loads(runner.prompt(work, []))
    if phase == "scan" and len(categories) == 1:
        assert f"During this scan, write only {kind}." in prompt["rule"]
    elif phase == "scan":
        assert "During this scan, write only" not in prompt["rule"]
        assert "Order your own work" in prompt["rule"]
        if kind == "steps":
            assert "write steps, states and step_dependencies together" in prompt["rule"]
            assert "cryptographic handshakes" in prompt["rule"]
        else:
            assert "write parties, channels and messages together" in prompt["rule"]
            assert "belong to the interaction-unit pass" in prompt["rule"]
    else:
        assert prompt["repairable_categories"] == list(categories)
        assert prompt["repair"].startswith(
            f"You may write entities in {', '.join(categories)} during this Fix."
        )
        assert "write only" not in (prompt["rule"] or "")


def test_interaction_scan_writes_states_and_dependencies(sources):
    batch_id = next(iter(sources.batches))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "steps"))
    work.add_entities(add(list(interaction_fixtures(sources).values())))
    assert [row["kind"] for row in work.records.values()] == [
        "steps",
        "states",
        "step_dependencies",
    ]
    with pytest.raises(ValueError, match="this scan can only write steps, states"):
        work.add_entities(add([party(sources, "Client")]))


def test_basics_scan_writes_three_categories(sources):
    batch_id = next(iter(sources.batches))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "basics"))
    work.add_entities(add(list(fixtures_by_kind(sources).values())[:3]))
    assert [row["kind"] for row in work.records.values()] == [
        "parties",
        "channels",
        "messages",
    ]
    with pytest.raises(ValueError, match="this scan can only write parties, channels"):
        work.add_entities(add([interaction_fixtures(sources)["steps"]]))


def test_basics_scan_ready_and_deferred(sources):
    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "basics"))
    assert work.repairable_categories == ORDER[:3]
    assert work.ready == ORDER[:2]
    assert work.write_scope == ("parties", "channels", "messages")
    assert work.deferred_checks() == ["channel_step"]


def test_basics_fix_repairable_categories(sources):
    work = Workspace(
        sources, "test", empty_registry(), Assignment("validate-basics", "category", "basics")
    )
    assert work.repairable_categories == ORDER[:3]
    assert work.ready == ORDER[:3]
    assert work.deferred_checks() == ["channel_step", "message_send"]
    for kind in ("parties", "channels", "messages"):
        assert not work.allowed(kind)
    with pytest.raises(ValueError, match="cannot write unscanned category steps"):
        work.allowed("steps")


def test_basics_prompt_orders_parties_then_channels_then_messages(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from rsc.extract.runner import Runner

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    batch_id = next(iter(sources.batches))
    prompt = json.loads(runner.prompt(runner.workspace(Assignment(batch_id, "scan", "basics")), []))
    rule = prompt["rule"]
    assert "write parties, channels and messages together" in rule
    assert "Order your own work in three steps" in rule
    assert "every distinct role the document treats as a participant" in rule
    assert "speaks it again to another" in rule
    assert "middlebox" not in rule
    assert "If a party appears only in messages" in rule
    assert "belong to the interaction-unit pass" in rule
    assert "write steps, states and step_dependencies together" not in rule


def test_legacy_four_pass_checkpoint_resumes(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    from rsc.extract.runner import Runner

    atomic_json(
        tmp_path / "extract-checkpoint.json",
        {
            "scan_kinds": list(LEGACY_SCAN_KINDS),
            "category": 1,
            "batch": 0,
            "global_checks": 0,
            "global_errors": None,
            "registry": empty_registry(),
            "history": [],
            "compact_summary": "",
            "records": [],
            "published": False,
        },
    )
    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    assert runner.scan_kinds == LEGACY_SCAN_KINDS


def test_interaction_scan_defers_only_dependencies(sources):
    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "steps"))
    assert work.repairable_categories == ORDER
    assert work.ready == ORDER[:5]
    assert work.write_scope == ("steps", "states", "step_dependencies")
    assert work.deferred_checks() == ["step_dependency"]


def test_interaction_scan_flags_missing_state_producer_immediately(sources):
    batch_id = next(iter(sources.batches))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "steps"))
    work.add_entities(
        add([entity(sources, "states", name="Lonely", party_name="Client", source="processing")])
    )
    description = {row["description"] for row in work.issues()}
    assert "State 'Lonely' has no hook reads or writes and no processing producer" in description


def test_interaction_unit_fix_covers_every_category(sources):
    work = Workspace(sources, "test", empty_registry(), Assignment("fix", "category", "steps"))
    assert work.repairable_categories == ORDER
    assert work.ready == ORDER
    assert work.deferred_checks() == []
    for kind in ORDER:
        assert not work.allowed(kind)


def test_verify_coverage_requires_every_scan_kind_per_batch(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    from rsc.extract.runner import Runner

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    runner.save(
        records=[
            {"phase": "scan", "kind": kind, "id": batch}
            for kind in SCAN_KINDS[:-1]
            for batch in sources.batches
        ]
    )
    with pytest.raises(ValueError, match="incomplete or unordered"):
        runner.verify_coverage()


def test_advisory_step_without_processing_is_visible_but_not_blocking(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    fixture = fixtures_by_kind(sources)
    add = TypeAdapter(list[EntityAddition]).validate_python
    work = Workspace(sources, "test", empty_registry(), Assignment("check", "category", "steps"))
    work.add_entities(
        add(
            [
                fixture["parties"],
                fixture["channels"],
                fixture["messages"],
                fixture["steps"],
                fixture["step_dependencies"],
            ]
        )
    )
    assert work.issues() == []
    assert [row["check_ref"] for row in work.advisory_issues()] == [
        "step_without_processing:E4"
    ]
    async def check():
        async with Client(make_server(lambda: work)) as client:
            report = (await client.call_tool("validate_entities", {})).data
            assert report["error_count"] == 0
            assert report["advisory_count"] == 1
            assert report["rows"] == []
            assert report["current_group"] is None
            # Advisory rows never block, and their detail is a separate page.
            advisory = (
                await client.call_tool(
                    "validate_entities", {"view": "issues", "include_advisory": True}
                )
            ).data
            assert [row["check_ref"] for row in advisory["issues"]] == [
                "step_without_processing:E4"
            ]
            detail = (
                await client.call_tool(
                    "validate_entities",
                    {"view": "issue", "issue_id": advisory["issues"][0]["id"]},
                )
            ).data
            assert detail["issue"]["suggestion"]

    asyncio.run(check())


@pytest.mark.parametrize(
    ("phase", "kind", "categories"),
    [
        ("scan", "parties", ORDER[:1]),
        ("scan", "basics", ("parties", "channels", "messages")),
        ("scan", "steps", ("steps", "states", "step_dependencies")),
        ("category", "channels", ORDER[:2]),
        ("category", "basics", ORDER[:3]),
        ("global", None, ORDER),
    ],
)
def test_completion_tools_return_repairable_categories(sources, phase, kind, categories):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from rsc.extract.tools import make_server

    assignment = Assignment("test", phase, kind)
    work = Workspace(sources, "test", empty_registry(), assignment)
    expected = "complete_batch" if phase == "scan" else "complete_fix"
    other = "complete_fix" if phase == "scan" else "complete_batch"
    args = {"summary": "Finished"}
    add = TypeAdapter(list[EntityAddition]).validate_python
    if phase == "category":
        work.add_entities(add([party(sources, "Client")]))
        work.repair_start_count = len(work.issues())
    elif phase == "global":
        seed = Workspace(sources, "test", empty_registry(), Assignment("seed", "global"))
        seed.add_entities(add([party(sources, "Client")]))
        seed.record_unresolved(
            Unresolved(
                description="Client needs a channel",
                entity_ids=["E1"],
                evidence=[citation(sources)],
                check_refs=["party_without_channel:E1"],
            )
        )
        work = Workspace(sources, "test", seed.data, assignment)

    async def check():
        async with Client(make_server(lambda: work)) as client:
            with pytest.raises(ToolError, match=f"use {expected}"):
                await client.call_tool(other, args)
            assert work.completion is None
            if phase == "category":
                rejected = await client.call_tool(expected, args)
                assert rejected.data["accepted"] is False
                assert rejected.data["repairable_categories"] == list(categories)
                work.add_entities(add([channel(sources, "Datagrams", connected=False)]))
            if phase == "global":
                # A no-op global Fix is rejected once so the agent retries with a change.
                rejected = await client.call_tool(expected, args)
                assert rejected.data["accepted"] is False
                assert rejected.data["repair_start_count"] == work.repair_start_count
                assert rejected.data["error_count"] == len(work.issues())
                assert rejected.data["group_count"] == len(work.issues())
                assert len(rejected.data["errors"]) == len(work.issues())
                # Diagnostics are paged: a row carries the id, the site and locators,
                # and the suggestion is one call away.
                assert all(row["id"] and row["group_id"] for row in rejected.data["errors"])
                detail = (
                    await client.call_tool(
                        "validate_entities",
                        {"view": "issue", "issue_id": rejected.data["errors"][0]["id"]},
                    )
                ).data["issue"]
                assert detail["suggestion"]
            result = await client.call_tool(expected, args)
            assert result.data["accepted"] is True
            assert result.data["repairable_categories"] == list(categories)
            assert result.data["error_count"] == len(work.issues())
            assert result.data["repair_start_count"] == work.repair_start_count
            repeated = await client.call_tool(expected, args)
            assert repeated.data["accepted"] is True
            assert repeated.data["repairable_categories"] == list(categories)
            if phase != "global":
                assert result.data["error_count"] == 0
                assert "group_count" not in result.data
            if phase == "global":
                validation = (await client.call_tool("validate_entities", {})).data
                assert validation["error_count"] == 1
                assert [
                    {
                        "type": row["type"],
                        "check_ref": row["check_ref"],
                        "records": row["records"],
                        "path": row["path"],
                        "description": row["description"],
                        "evidence": row["evidence"],
                    }
                    for row in validation["rows"]
                ] == [
                    {
                        "type": "party_without_channel",
                        "check_ref": "party_without_channel:E1",
                        "records": ["E1"],
                        "path": [],
                        "description": "Party 'Client' is not a channel endpoint",
                        "evidence": [
                            {
                                "document_id": cite["document_id"],
                                "block_id": cite["block_id"],
                            }
                            for cite in seed.records["E1"]["evidence"][:3]
                        ],
                    }
                ]
                assert validation["unresolved"] == [
                    {
                        "id": seed.data["unresolved"]["U1"]["id"],
                        "description": seed.data["unresolved"]["U1"]["description"],
                        "check_refs": seed.data["unresolved"]["U1"]["check_refs"],
                    }
                ]
                assert validation["group_count"] == 1
                assert validation["current_group"]["group_id"] == "E1"

    asyncio.run(check())


def test_entity_tools_return_full_evidence_blocks_and_references(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    fixture = fixtures_by_kind(sources)
    location = citation(sources)
    doc, block_id = location["document_id"], location["block_id"]
    span = sources.evidence(doc, block_id)
    fixture["channels"]["evidence"] = [
        {
            **location,
            "start": span["start"],
            "end": span["start"] + 1,
            "note": f"Evidence {i}",
        }
        for i in range(10)
    ]
    fixture["channels"]["mentions"] = [location]
    step = fixture["steps"]
    variants = [
        {**step, "value": {**step["value"], "name": f"Send variant {i}"}} for i in range(10)
    ]
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add([*fixture.values(), *variants]))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            assert len(await client.list_tools()) == 15
            result = await client.call_tool("get_entity", {"id": "E2"})
            detail = result.data["entities"]["E2"]
            assert detail["references"] == [
                *[
                    {"from": f"E{i}", "field": ["channel_name"], "to": "E2"}
                    for i in [4, *range(6, 16)]
                ],
                {"from": "E2", "field": ["endpoint_party_names", 0], "to": "E1"},
            ]
            # Evidence arrives as locators; the text stays in read_source.
            assert detail["evidence"] == [
                {
                    "document_id": cite["document_id"],
                    "block_id": cite["block_id"],
                    "section_path": cite["section_path"],
                    "field": cite["field"],
                    "note": cite["note"],
                }
                for cite in work.records["E2"]["evidence"]
            ]
            assert len(detail["evidence"]) == 10
            assert detail["mentions"] == work.records["E2"]["mentions"]
            assert result.data["snapshot"] == work.snapshot
            found = await client.call_tool("search_entities", {"queries": ["Datagrams"]})
            assert [row["id"] for row in found.data["rows"]] == ["E2"]
            batch = await client.call_tool(
                "get_entity",
                {"ids": ["E1", "E2", "E2", "E999"]},
            )
            # The second detail does not fit what is left of this turn's reply budget,
            # so the first page names where to continue and the rest arrives after it.
            assert list(batch.data["entities"]) == ["E1"]
            assert batch.data["next_ids"] == ["E2"]
            rest = await client.call_tool(
                "get_entity",
                {
                    "ids": ["E1", "E2", "E2", "E999"],
                    "offset": batch.data["next_offset"],
                },
            )
            assert list(rest.data["entities"]) == ["E2"]
            assert rest.data["entities"]["E2"] == detail
            assert rest.data["next_offset"] is None
            assert "unknown active entity" in rest.data["errors"]["E999"]
            empty = await client.call_tool("get_entity", {"ids": []})
            assert empty.data["entities"] == {} and empty.data["errors"] == {}
            missing = await client.call_tool(
                "read_source",
                {"document_id": location["document_id"], "section_path": ["99"]},
            )
            assert missing.data["error"] == "unknown or empty section path"
            assert missing.data["nearby_section_paths"]
            assert all(
                (location["document_id"], tuple(path)) in sources.sections
                for path in missing.data["nearby_section_paths"]
            )

    asyncio.run(check())


def test_search_entities_returns_paged_union_of_name_and_alias_matches(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(
        add(
            [
                party(sources, "Client Proxy"),
                {**party(sources, "Server"), "aliases": ["Remote Peer"]},
                party(sources, "Client"),
                channel(sources, "Client"),
            ]
        )
    )

    async def check():
        async with Client(make_server(lambda: work)) as client:
            args = {"queries": ["client", "REMOTE PEER", "proxy", "client"], "kind": "parties"}
            first = await client.call_tool("search_entities", {**args, "limit": 2})
            second = await client.call_tool(
                "search_entities", {**args, "offset": first.data["next_offset"], "limit": 2}
            )
            assert [row["id"] for row in first.data["rows"]] == ["E2", "E3"]
            assert [row["id"] for row in second.data["rows"]] == ["E1"]
            assert second.data["next_offset"] is None
            single = await client.call_tool("search_entities", {"query": "client"})
            assert [row["id"] for row in single.data["rows"]] == ["E3", "E4", "E1"]
            listed = await client.call_tool("list_entities", {"kind": "parties", "limit": 1})
            assert [
                (row["id"], row["kind"], row["name"], row["aliases"])
                for row in listed.data["rows"]
            ] == [("E1", "parties", "Client Proxy", [])]
            assert listed.data["rows"][0]["evidence"][0]["block_id"]
            empty = await client.call_tool("search_entities", {"queries": []})
            assert empty.data["rows"] == []
            assert empty.data["next_offset"] is None
            assert empty.data["count"] == 0

    asyncio.run(check())


@pytest.mark.parametrize("repair_rounds", [1, 2])
def test_category_review_and_tool_return_all_issues(sources, tmp_path, repair_rounds):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    reviewed = []
    cite = citation(sources)

    def respond(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            reviewed.append(prompt["validation_errors"])
            assert prompt["repairable_categories"] == list(ORDER[:2])
            assert [item["id"] for item in prompt["unresolved"]] == ["U1"]
            ids = {i for issue in reviewed[-1] for i in issue["records"]}
            # Only the current repair site travels whole; the rest of the catalog is
            # one validate_entities call away.
            assert set(prompt["entities"]) <= ids
            assert prompt["current_group"]["group_id"] in prompt["entities"]
            assert all(
                row["id"] == i and row["name"] and row["evidence"] and "revision" not in row
                for i, row in prompt["entities"].items()
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "validate_entities",
                        {"view": "issues", "max_tokens": 20000},
                    )
                ]
            )
        if isinstance(latest, ToolReturnPart) and latest.tool_name == "validate_entities":
            # One bounded page still lists every blocking row here.
            assert latest.content["error_count"] == len(reviewed[-1])
            assert latest.content["issues"] == reviewed[-1]
            assert "blocking_count" not in latest.content
            errors = latest.content["issues"][:13]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_entities",
                        {
                            "entities": [
                                {
                                    "id": row["records"][0],
                                    "withdraw_reason": "This object is not a channel participant",
                                    "evidence": [cite],
                                }
                                for row in errors
                            ],
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "complete_fix",
                    {"summary": "Checked source"},
                )
            ]
        )

    config = ExtractConfig(max_repair_rounds=repair_rounds)
    runner = Runner(tmp_path, sources, "test", config, FunctionModel(respond), no_review())
    work = runner.workspace(Assignment("seed", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add([party(sources, f"Role {i}") for i in range(20)]))
    work.add_entities(add([party(sources, f"Role {i}") for i in range(20, 25)]))
    work.record_unresolved(
        Unresolved(description="Earlier unresolved issue", entity_ids=[], evidence=[cite])
    )
    runner.save(registry=work.data, category=1, batch=len(sources.batches))
    atomic_json(
        tmp_path / "extract-failure.json",
        {
            "assignment": {"id": "validate-channels", "phase": "category", "kind": "channels"},
            "error": "request_limit exceeded",
        },
    )

    async def check():
        async with runner.toolset:
            await runner.validate_category(1, "channels")

    asyncio.run(check())
    assert [len(issues) for issues in reviewed] == [25, 12][:repair_rounds]
    assert runner.state["category"] == 2
    assert runner.state["batch"] == runner.state["category_repairs"] == 0
    remaining = runner.workspace(Assignment("validate-channels", "category", "channels")).issues()
    assert len(remaining) == (12 if repair_rounds == 1 else 0)
    assert [
        row["error_count"]
        for row in runner.state["records"]
        if row["phase"] == "category-validation"
    ] == [25, 12, 0][:repair_rounds + 1]


def test_condensed_rows_keep_the_handle_that_rereads_them():
    from rsc.extract.runner import condense_messages

    big = "blk-000129 " + "RFC text " * 600
    small = {"text": "ok"}
    messages = [
        {
            "parts": [
                {"part_kind": "thinking", "content": "reasoning " * 100},
                {
                    "part_kind": "tool-call",
                    "tool_name": "read_source",
                    "tool_call_id": "c1",
                    "args": {"block_id": "blk-000129", "padding": "x" * 900},
                },
                {
                    "part_kind": "tool-return",
                    "tool_name": "read_source",
                    "tool_call_id": "c1",
                    "content": {"text": big},
                },
                {
                    "part_kind": "tool-return",
                    "tool_name": "get_entity",
                    "tool_call_id": "c2",
                    "content": small,
                },
            ]
        }
    ]
    condensed, stubs, thinking = condense_messages(messages, 1200)
    assert (stubs, thinking) == (1, 1)
    parts = condensed[0]["parts"]
    assert [part["part_kind"] for part in parts] == ["tool-call", "tool-return", "tool-return"]
    # The call keeps its id, name and argument shape...
    assert parts[0]["tool_call_id"] == "c1"
    assert parts[0]["args"]["block_id"] == "blk-000129"
    assert parts[0]["args"]["padding"].endswith("…")
    # ...and the result keeps the locator that re-reads it.
    assert parts[1]["tool_call_id"] == "c1"
    assert parts[1]["tool_name"] == "read_source"
    assert parts[1]["content"]["condensed"] is True
    assert parts[1]["content"]["blocks"] == ["blk-000129"]
    assert parts[2]["content"] == small
    # The stored row is untouched: only the planned copy is condensed.
    assert messages[0]["parts"][0]["part_kind"] == "thinking"


def test_history_compaction_drops_then_condenses_a_quarter_each():
    from rsc.extract.runner import plan_history

    def row(index):
        return {
            "messages": [
                {
                    "parts": [
                        {
                            "part_kind": "tool-return",
                            "tool_name": "read_source",
                            "tool_call_id": f"c{index}",
                            "content": {"text": "x" * 4000},
                        }
                    ]
                }
            ],
            "tokens": 2000,
            "decision": f"decision {index}",
            "assignment_id": f"batch-{index}",
        }

    rows = [row(index) for index in range(8)]
    kept, dropped, stats = plan_history(rows, aim=6000, floor=5000, max_chars=200)

    # Phase one removes whole oldest rows and covers at least a quarter of the window.
    assert stats["dropped_tokens"] >= 5000
    assert [item["assignment_id"] for item in dropped] == ["batch-0", "batch-1", "batch-2"]
    # Phase two condenses the rows right after them and covers another quarter.
    assert stats["condensed_before_tokens"] >= 5000
    assert stats["condensed_after_tokens"] < stats["condensed_before_tokens"]
    condensed_ids = [item["assignment_id"] for item in kept if item.get("condensed")]
    assert condensed_ids == ["batch-3", "batch-4", "batch-5"]
    # The two sets are disjoint and the rest of the history is untouched.
    assert not set(condensed_ids) & {item["assignment_id"] for item in dropped}
    assert [item["assignment_id"] for item in kept if not item.get("condensed")] == [
        "batch-6",
        "batch-7",
    ]
    assert stats["retained_tokens"] <= 6000


def test_history_compaction_keeps_the_checkpoint_when_a_floor_is_unreachable():
    from rsc.extract.runner import plan_history

    rows = [
        {"messages": [], "tokens": 100, "decision": f"decision {index}"} for index in range(3)
    ]
    # 300 tokens of history cannot cover two floors of 5000 tokens.
    kept, dropped, stats = plan_history(rows, aim=10_000, floor=5000, max_chars=1200)
    assert (kept, dropped) == (None, None)
    assert stats["insufficient"] is True


def test_history_compaction_reports_an_unfittable_request_instead_of_dropping_more():
    from rsc.extract.runner import plan_history

    def row(index):
        return {
            "messages": [
                {
                    "parts": [
                        {
                            "part_kind": "text",
                            "content": "y" * 400,
                        }
                    ]
                }
            ],
            "tokens": 3000,
            "decision": f"decision {index}",
        }

    rows = [row(index) for index in range(8)]
    kept, dropped, stats = plan_history(rows, aim=1000, floor=3000, max_chars=40)
    # Both floors are met, but the untouched remainder alone still exceeds the aim;
    # the caller keeps its checkpoint instead of shrinking history further.
    assert (kept, dropped) == (None, None)
    assert stats["over_budget"] is True
    assert stats["dropped_rows"] == 1 and stats["condensed_rows"] == 1


def test_measure_projection_drops_only_the_thinking_signature():
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from rsc.extract.runner import (
        ModelMeasure,
        encrypted_reasoning,
        tokens,
        without_signatures,
    )

    signature = "gAAAAAB" + "b" * 4000
    messages = [
        ModelRequest(parts=[TextPart("inspect RFC 2326")]),
        ModelResponse(
            parts=[
                ThinkingPart(content="check the state table", signature=signature),
                ToolCallPart("read_source", {"block_id": "blk-000001"}, tool_call_id="c1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("read_source", {"text": "x" * 2000}, "c1")]),
    ]
    luna = ModelMeasure("openai-codex:gpt-6-luna")
    other = ModelMeasure("openai-codex:gpt-5")

    raw = luna.raw(messages)
    projection = luna(messages)
    # The projection is the same serialized messages minus the encrypted signature.
    assert projection == tokens(without_signatures(luna.text(messages)))
    assert signature in luna.text(messages)
    assert projection < raw
    assert raw - projection > 500
    # Visible reasoning, prompts and tool traffic stay in the count.
    assert projection > tokens("check the state table") + tokens("inspect RFC 2326")
    # Models that do not replay encrypted reasoning keep the plain count.
    assert other(messages) == other.raw(messages) == raw
    assert encrypted_reasoning("openai-codex:gpt-6-luna")
    assert not encrypted_reasoning("openai-codex:gpt-5")
    assert not encrypted_reasoning("")


def test_resume_remeasures_stored_history_for_the_current_model(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ThinkingPart,
        ToolReturnPart,
    )

    from rsc.extract.runner import Runner, tokens, without_signatures

    messages = [
        ModelResponse(
            parts=[ThinkingPart(content="", signature="gAAAAAB" + "c" * 4000)],
        ),
        ModelRequest(parts=[ToolReturnPart("read_source", {"text": "y" * 800}, "c1")]),
    ]
    from pydantic_ai import ModelMessagesTypeAdapter

    stored = json.loads(ModelMessagesTypeAdapter.dump_json(messages))
    inflated = tokens(json.dumps(stored, ensure_ascii=False, separators=(",", ":")))
    checkpoint = {
        "scan_kinds": list(SCAN_KINDS),
        "category": 0,
        "batch": 0,
        "global_checks": 0,
        "global_errors": None,
        "registry": {
            "entities": {},
            "redirects": {},
            "withdrawn": {},
            "unresolved": {},
            "next_entity": 0,
            "next_issue": 0,
        },
        "history": [
            {
                "phase": "scan",
                "kind": "basics",
                "assignment_id": "batch-00001",
                "messages": stored,
                "tokens": inflated,
                "decision": "basics batch-00001",
            }
        ],
        "compact_summary": "",
        "records": [],
        "published": False,
    }
    atomic_json(tmp_path / "extract-checkpoint.json", checkpoint)

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    row = runner.state["history"][0]
    # A checkpoint written by the old accounting is re-measured for this model.
    assert row["tokens"] == runner.measure(row["messages"])
    assert row["tokens"] < inflated
    assert row["tokens"] == tokens(
        without_signatures(runner.measure.text(row["messages"]))
    )


def test_rejection_reports_raw_projection_limit_and_floor(capsys):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    from rsc.extract.runner import ContextBudget, HistoryBudget, ModelMeasure

    live = [ModelRequest(parts=[ToolReturnPart("read_source", {"text": "z" * 6000}, "c1")])]
    budget = HistoryBudget(
        kept=[],
        dropped=[],
        offset=0,
        limit=50,
        floor=64_600,
        max_chars=1200,
        summary="",
        summary_tokens=128,
        measure=ModelMeasure("openai-codex:gpt-6-luna"),
    )
    context = SimpleNamespace(messages=live)
    with pytest.raises(ValueError, match="exceeds model context budget"):
        asyncio.run(
            ContextBudget().before_model_request(SimpleNamespace(deps=budget), context)
        )
    line = capsys.readouterr().out.strip()
    assert line.startswith("context budget rejection:")
    assert "raw " in line and "projection " in line
    assert "limit 50" in line
    assert "history 0 rows / 0 tokens" in line
    assert "floor 64600" in line


def test_condensed_rows_are_measured_with_the_projection():
    from rsc.extract.runner import ModelMeasure, plan_history

    measure = ModelMeasure("openai-codex:gpt-6-luna")
    signature = "gAAAAAB" + "d" * 3000

    def row(index):
        return {
            "messages": [
                {
                    "kind": "response",
                    "parts": [
                        {
                            "part_kind": "thinking",
                            "content": "",
                            "signature": signature,
                        }
                    ],
                },
                {
                    "kind": "request",
                    "parts": [
                        {
                            "part_kind": "tool-return",
                            "tool_name": "read_source",
                            "tool_call_id": f"c{index}",
                            "content": {"text": "w" * 4000},
                        }
                    ],
                },
            ],
            "decision": f"decision {index}",
            "assignment_id": f"batch-{index}",
        }

    rows = []
    for index in range(6):
        item = row(index)
        item["tokens"] = measure(item["messages"])
        rows.append(item)
    kept, dropped, stats = plan_history(
        rows, aim=6000, floor=4000, max_chars=200, measure=measure
    )
    assert dropped and kept
    for item in kept:
        assert item["tokens"] == measure(item["messages"])
    condensed = [item for item in kept if item.get("condensed")]
    assert condensed
    for item in condensed:
        assert item["tokens"] < item["original_tokens"]
    assert stats["condensed_after_tokens"] == sum(
        item["tokens"] for item in condensed
    )


def test_context_budget_leaves_history_alone_without_pressure():
    from pydantic_ai import ModelMessagesTypeAdapter
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    from rsc.extract.runner import ContextBudget, HistoryBudget

    old = [
        ModelRequest(
            parts=[ToolReturnPart("read_source", {"text": "blk-000001 " + "x" * 8000}, "c1")]
        )
    ]
    current = [ModelRequest(parts=[ToolReturnPart("get_entity", {"id": "E1"}, "c2")])]
    budget = HistoryBudget(
        kept=[
            {
                "messages": json.loads(ModelMessagesTypeAdapter.dump_json(old)),
                "tokens": 100,
                "decision": "earlier decision",
            }
        ],
        dropped=[],
        offset=1,
        limit=200_000,
        floor=1000,
        max_chars=1200,
        summary="",
        summary_tokens=128,
    )
    context = SimpleNamespace(messages=old + current)
    result = asyncio.run(
        ContextBudget().before_model_request(SimpleNamespace(deps=budget), context)
    )
    # Nothing is stubbed or dropped: the request already fits.
    assert budget.stats == []
    assert result.messages[-1].parts[0].content == {"id": "E1"}
    assert result.messages[0].parts[0].content["text"].startswith("blk-000001")


def test_context_budget_compacts_completed_rows_only_on_pressure():
    from pydantic_ai import ModelMessagesTypeAdapter
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    from rsc.extract.runner import ContextBudget, HistoryBudget, tokens

    def exchange(block):
        return [
            ModelRequest(
                parts=[ToolReturnPart("read_source", {"text": block * 4000}, f"c{block}")]
            )
        ]

    def stored(messages):
        return json.loads(ModelMessagesTypeAdapter.dump_json(messages))

    old, middle = exchange("a"), exchange("b")
    current = [ModelRequest(parts=[ToolReturnPart("get_entity", {"id": "E1"}, "c9")])]
    kept = [
        {
            "messages": stored(old),
            "tokens": tokens(dumps(stored(old))),
            "decision": "first decision",
        },
        {
            "messages": stored(middle),
            "tokens": tokens(dumps(stored(middle))),
            "decision": "second decision",
        },
    ]
    floor = kept[0]["tokens"]
    budget = HistoryBudget(
        kept=[dict(row) for row in kept],
        dropped=[],
        offset=2,
        # Tight enough to force one compaction, large enough that the compacted
        # request fits afterwards.
        limit=800,
        floor=floor,
        max_chars=120,
        summary="",
        summary_tokens=512,
    )
    context = SimpleNamespace(messages=old + middle + current)
    result = asyncio.run(
        ContextBudget().before_model_request(SimpleNamespace(deps=budget), context)
    )
    # The active tool exchange is intact and the completed rows were compacted.
    assert result.messages[-1].parts[0].content == {"id": "E1"}
    assert budget.stats and budget.stats[-1]["dropped_rows"] == 1
    assert budget.stats[-1]["condensed_rows"] >= 1
    assert "decision" in result.messages[0].parts[0].content


def test_repairs_start_without_scan_history(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")

    from rsc.extract.cli import ExtractConfig
    from rsc.extract.runner import Runner

    runner = Runner(tmp_path, sources, "test", ExtractConfig())
    runner.save(
        history=[{"messages": [], "tokens": 10, "decision": "scan decision"}],
        compact_summary="scan summary",
        category=1,
    )

    async def check():
        async with runner.toolset:
            # No parties exist, so the category passes with no issues and no agent turn.
            await runner.validate_category(1, "channels")

    asyncio.run(check())
    assert runner.state["history"] == []
    assert runner.state["compact_summary"] == ""


def test_category_fix_rounds_keep_their_own_history(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from rsc.extract.registry import Workspace
    from rsc.extract.runner import Runner

    runner = Runner(tmp_path, sources, "test", ExtractConfig())
    rounds = {}
    started = set()
    real = Workspace.structural_issues

    def fail_twice(work):
        # Two repair rounds per category, enough to see whether rounds replay.
        if work.assignment.phase != "category":
            return real(work)
        if work.assignment.id not in started:
            started.add(work.assignment.id)
            rounds[work.assignment.kind] = rounds.get(work.assignment.kind, 0) + 1
        if rounds[work.assignment.kind] <= 2:
            return [
                {"type": "structure", "records": [], "path": [], "description": "round"}
            ]
        return real(work)

    seen = []
    row = {
        "phase": "category",
        "kind": "basics",
        "assignment_id": "validate-basics",
        "messages": [],
        "tokens": 5,
        "decision": "round",
    }

    async def fake_retry(work, issues=(), defer=None):
        seen.append((work.assignment.kind, [r.get("phase") for r in runner.state["history"]]))
        return (
            [{**row, "kind": work.assignment.kind, "assignment_id": work.assignment.id}],
            {
                "phase": "category",
                "kind": work.assignment.kind,
                "id": work.assignment.id,
                "status": "completed",
                "summary": "done",
                "unresolved_ids": [],
                "changed_entities": [],
                "elapsed_seconds": 0.0,
                "usage": {},
                "tool_calls": 0,
                "repeated_tool_calls": 0,
                "compactions": [],
                "mutations": [],
            },
        )

    monkeypatch.setattr(Workspace, "structural_issues", fail_twice)
    monkeypatch.setattr(runner, "retry", fake_retry)

    async def check():
        async with runner.toolset:
            # A scan conversation is still in the checkpoint when repairs begin.
            runner.save(
                category=0,
                batch=2,
                history=[{**row, "phase": "scan", "decision": "scan"}],
                compact_summary="scan summary",
            )
            await runner.validate_category(0, "basics")
            await runner.validate_category(1, "steps")

    asyncio.run(check())
    # Repairs never replay scan rows, each Fix round replays the previous round of its
    # own kind, and switching kind starts that history over.
    assert seen == [
        ("basics", []),
        ("basics", ["category"]),
        ("steps", []),
        ("steps", ["category"]),
    ]


def message_with_fields(sources, names):
    return entity(
        sources,
        "messages",
        name="Request",
        associated_fields=[{"name": name} for name in names],
    )


def add_message(work, sources, names):
    work.add_entities(
        TypeAdapter(list[EntityAddition]).validate_python(
            [message_with_fields(sources, names)]
        )
    )
    return "E1"


def update_with(work, sources, **fields):
    return work.update_entities(
        [EntityUpdate.model_validate({"id": "E1", "evidence": [citation(sources)], **fields})]
    )


def field_names(work):
    return [field["name"] for field in work.records["E1"]["value"]["associated_fields"]]


def test_associated_fields_delta_adds_renames_and_removes_in_place(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A", "B", "C", "D"])
    work.update_entities(
        [
            EntityUpdate.model_validate(
                {
                    "id": "E1",
                    "evidence": [citation(sources)],
                    "associated_fields_delta": {
                        "remove": ["B"],
                        "replace": [{"name": "C", "new_name": "C2"}],
                        "add": [{"name": "X", "before": "D"}, {"name": "Y"}],
                    },
                }
            )
        ]
    )
    # Removals vanish, renames stay in place, anchored additions land before their anchor,
    # unanchored additions append.
    assert field_names(work) == ["A", "C2", "X", "D", "Y"]
    # The saved request holds the expanded list, in the old shape, without the delta key.
    record = work.mutations[-1]["request"][0]
    assert "associated_fields_delta" not in record
    assert record["patch"]["associated_fields"] == [{"name": name} for name in field_names(work)]


def test_associated_fields_delta_keeps_submission_order_for_one_anchor(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A", "B"])
    work.update_entities(
        [
            EntityUpdate.model_validate(
                {
                    "id": "E1",
                    "evidence": [citation(sources)],
                    "associated_fields_delta": {
                        "add": [
                            {"name": "X1", "before": "B"},
                            {"name": "X2", "before": "B"},
                            {"name": "X3", "before": "A"},
                        ]
                    },
                }
            )
        ]
    )
    assert field_names(work) == ["X3", "A", "X1", "X2", "B"]


def test_associated_fields_delta_matches_the_full_list_path(sources):
    delta_work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    list_work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    for work in (delta_work, list_work):
        add_message(work, sources, ["A", "B", "C"])
    update_with(
        delta_work,
        sources,
        associated_fields_delta={"remove": ["A"], "add": [{"name": "D", "before": "C"}]},
    )
    update_with(list_work, sources, patch={"associated_fields": [
        {"name": "B"}, {"name": "D"}, {"name": "C"}
    ]})
    assert delta_work.records == list_work.records
    assert delta_work.issues() == list_work.issues()
    assert delta_work.snapshot == list_work.snapshot


@pytest.mark.parametrize(
    ("delta", "message"),
    [
        ({"remove": ["missing"]}, "not in associated_fields"),
        ({"replace": [{"name": "missing", "new_name": "X"}]}, "not in associated_fields"),
        ({"replace": [{"name": "A", "new_name": "B"}]}, "names already exist"),
        ({"add": [{"name": "B"}]}, "names already exist"),
        ({"add": [{"name": "X", "before": "missing"}]}, "is not in the resulting fields"),
        ({"remove": ["A"], "replace": [{"name": "A", "new_name": "X"}]}, "both removed"),
        ({}, "at least one add, replace or remove"),
    ],
)
def test_associated_fields_delta_rejects_bad_edits(sources, delta, message):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A", "B"])
    before = deepcopy(work.records)
    with pytest.raises(ValueError, match=message):
        update_with(work, sources, associated_fields_delta=delta)
    assert work.records == before
    assert work.snapshot == 1


def test_associated_fields_delta_rejects_stale_snapshot_and_other_kinds(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A"])
    update_with(work, sources, associated_fields_delta={"add": [{"name": "B"}], "snapshot": 1})
    with pytest.raises(ValueError, match="stale associated_fields snapshot"):
        update_with(
            work, sources, associated_fields_delta={"add": [{"name": "C"}], "snapshot": 999}
        )
    assert field_names(work) == ["A", "B"]
    party_work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "parties"))
    party_work.add_entities(
        TypeAdapter(list[EntityAddition]).validate_python([party(sources, "C")])
    )
    with pytest.raises(ValueError, match="only applies to messages"):
        party_work.update_entities(
            [
                EntityUpdate.model_validate(
                    {
                        "id": "E1",
                        "evidence": [citation(sources)],
                        "associated_fields_delta": {"add": [{"name": "X"}]},
                    }
                )
            ]
        )


def test_associated_fields_delta_cannot_mix_with_the_full_list(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A"])
    with pytest.raises(ValueError, match="either patch.associated_fields or"):
        update_with(
            work,
            sources,
            patch={"associated_fields": [{"name": "B"}]},
            associated_fields_delta={"add": [{"name": "C"}]},
        )


def test_associated_fields_delta_still_needs_field_evidence(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A"])
    with pytest.raises(ValueError, match="missing field evidence"):
        work.update_entities(
            [
                EntityUpdate.model_validate(
                    {
                        "id": "E1",
                        "evidence": [
                            {**citation(sources), "field": "/name"}
                        ],
                        "associated_fields_delta": {"add": [{"name": "B"}]},
                    }
                )
            ]
        )


def test_associated_fields_delta_rejects_moving_cited_indices(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    fixture = message_with_fields(sources, ["A", "B", "C"])
    fixture["evidence"] = [{**citation(sources), "field": "/associated_fields/1"}]
    work.add_entities(TypeAdapter(list[EntityAddition]).validate_python([fixture]))
    with pytest.raises(ValueError, match="/associated_fields/1"):
        update_with(work, sources, associated_fields_delta={"remove": ["A"]})
    # Dropping the trailing field leaves the cited index alone.
    update_with(work, sources, associated_fields_delta={"remove": ["C"]})
    assert field_names(work) == ["A", "B"]


def test_associated_fields_delta_shares_one_snapshot_across_entities(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    work.add_entities(
        TypeAdapter(list[EntityAddition]).validate_python(
            [
                message_with_fields(sources, ["A"]),
                {**message_with_fields(sources, ["B"]), "value": {"name": "Response",
                 "associated_fields": [{"name": "B"}]}},
            ]
        )
    )
    snapshot = work.snapshot
    work.update_entities(
        [
            EntityUpdate.model_validate(
                {
                    "id": "E1",
                    "evidence": [citation(sources)],
                    "associated_fields_delta": {"add": [{"name": "A2"}], "snapshot": snapshot},
                }
            ),
            EntityUpdate.model_validate(
                {
                    "id": "E2",
                    "evidence": [citation(sources)],
                    "associated_fields_delta": {"add": [{"name": "B2"}], "snapshot": snapshot},
                }
            ),
        ]
    )
    assert [field["name"] for field in work.records["E1"]["value"]["associated_fields"]] == [
        "A",
        "A2",
    ]
    assert [field["name"] for field in work.records["E2"]["value"]["associated_fields"]] == [
        "B",
        "B2",
    ]
    # One mutation carried both edits, so a shared read snapshot stays valid for the call.
    assert len(work.mutations) == 2


def test_associated_fields_delta_replays_once_and_stays_optional(sources):
    work = Workspace(sources, "t", empty_registry(), Assignment("test", "scan", "messages"))
    add_message(work, sources, ["A"])
    update_with(work, sources, associated_fields_delta={"add": [{"name": "B", "before": "A"}]})
    mutations = len(work.mutations)
    # An identical submission is a retry: the receipt answers and no second write lands.
    update_with(work, sources, associated_fields_delta={"add": [{"name": "B", "before": "A"}]})
    assert len(work.mutations) == mutations
    assert field_names(work) == ["B", "A"]
    # Without the new key the update path is unchanged.
    update_with(work, sources, patch={"name": "Request2"})
    assert work.records["E1"]["value"]["name"] == "Request2"
    assert field_names(work) == ["B", "A"]
    assert "associated_fields_delta" not in work.mutations[-1]["request"][0]


def test_oversized_entity_pages_its_fields(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    fixture = entity(
        sources,
        "messages",
        name="Packet",
        associated_fields=[
            {"name": f"field-{index} " + "long source field name " * 6} for index in range(120)
        ],
    )
    work.add_entities(TypeAdapter(list[EntityAddition]).validate_python([fixture]))
    expected = [str(index) for index in range(len(fixture["value"]["associated_fields"]))]

    async def check():
        async with Client(make_server(lambda: work)) as client:
            seen = []
            offset = 0
            pages = 0
            while True:
                page = (
                    await client.call_tool(
                        "get_entity",
                        {"id": "E1", "max_tokens": 900, "field_offset": offset},
                    )
                ).data
                assert page["page_tokens"] <= page["page_limit"]
                body = page["entities"]["E1"]
                assert body["evidence"]
                seen.extend(body["value"].get("associated_fields") or {})
                pages += 1
                if page["next_field_offset"] is None:
                    assert page["fields_truncated"] is False
                    break
                offset = page["next_field_offset"]
                assert page["remaining_fields"]
                assert pages < 40
            # No page ever exceeded its budget, and together they cover every field.
            assert sorted(seen, key=int) == expected
            assert pages > 1

    asyncio.run(check())


def test_read_source_rejects_malformed_block_lists(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    block_id = sources.batches[batch_id]["segments"][0]["block_id"]

    async def check():
        async with Client(make_server(lambda: work)) as client:
            for blocks in (
                [{"block_id": block_id}],
                [{"document_id": sources.batches[batch_id]["doc_id"]}],
            ):
                with pytest.raises(Exception, match="document_id and block_id"):
                    await client.call_tool("read_source", {"blocks": blocks})
            # A non-dict item is rejected by argument validation instead.
            with pytest.raises(Exception, match="valid dictionary"):
                await client.call_tool("read_source", {"blocks": [block_id]})
            ok = (
                await client.call_tool(
                    "read_source",
                    {
                        "blocks": [
                            {
                                "document_id": sources.batches[batch_id]["doc_id"],
                                "block_id": block_id,
                            }
                        ]
                    },
                )
            ).data
            assert ok["count"] == 1 and ok["blocks"][0]["block_id"] == block_id

    asyncio.run(check())


def test_retried_assignment_records_each_compaction_once(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from dataclasses import asdict

    from rsc.extract.registry import Assignment
    from rsc.extract.runner import Runner

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    stats = {
        "dropped_rows": 2,
        "dropped_tokens": 70000,
        "condensed_rows": 2,
        "condensed_before_tokens": 70000,
        "condensed_after_tokens": 20000,
        "floor_tokens": 64600,
    }
    attempts = []

    async def flaky(work, issues=(), *, assignment_attempt=1):
        attempts.append(assignment_attempt)
        if assignment_attempt == 1:
            # The failed attempt compacted history before giving up.
            runner.compactions.append(dict(stats))
            raise ValueError("simulated model error")
        record = {
            **asdict(work.assignment),
            "status": "completed",
            "summary": "done",
            "unresolved_ids": [],
            "changed_entities": [],
            "elapsed_seconds": 0.0,
            "usage": {},
            "tool_calls": 0,
            "repeated_tool_calls": 0,
            "compactions": [],
            "mutations": [],
        }
        return [], record

    monkeypatch.setattr(runner, "execute", flaky)
    work = runner.workspace(Assignment("batch-00001", "scan", "basics"))
    asyncio.run(runner.retry(work))
    assert attempts == [1, 2]


def test_read_source_pages_by_real_tokens_and_dedups_blocks(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    batch = sources.batches[batch_id]
    keys = [
        {"document_id": batch["doc_id"], "block_id": part["block_id"]}
        for part in batch["segments"]
    ] * 3

    async def check():
        async with Client(make_server(lambda: work)) as client:
            first = (
                await client.call_tool(
                    "read_source", {"blocks": keys, "max_tokens": 256}
                )
            ).data
            # Repeated locators are read once.
            assert first["count"] == len(batch["segments"])
            assert len(first["blocks"]) <= first["count"]
            assert first["page_tokens"] <= first["page_limit"] + 200
            if first["next_offset"] is not None:
                rest = (
                    await client.call_tool(
                        "read_source",
                        {
                            "blocks": keys,
                            "max_tokens": 4000,
                            "offset": first["next_offset"],
                        },
                    )
                ).data
                assert rest["next_offset"] is None
            story = (
                await client.call_tool(
                    "read_source",
                    {
                        "document_id": batch["doc_id"],
                        "section_path": list(
                            sources.blocks[
                                (batch["doc_id"], keys[0]["block_id"])
                            ]["section_path"]
                        ),
                        "max_tokens": 200,
                    },
                )
            ).data
            assert story["page_tokens"] <= story["page_limit"]
            assert story["text"]
            if story["next_offset"] is not None:
                assert len(story["text"]) < 10000

    asyncio.run(check())


def test_parallel_reads_share_one_turn_budget_and_writes_stay_whole(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.paging import MINIMUM_PAGE_TOKENS, TurnBudget
    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    for start in range(0, 60, 20):
        work.add_entities(
            add([party(sources, f"Role {index}") for index in range(start, start + 20)])
        )
    budget = TurnBudget(3000)

    async def check():
        async with Client(
            make_server(lambda: work, turn_budget=budget)
        ) as client:
            pages = await asyncio.gather(
                *(
                    client.call_tool(
                        "list_entities", {"kind": "parties", "max_tokens": 2000}
                    )
                    for _ in range(6)
                )
            )
            spent = sum(page.data["page_tokens"] for page in pages)
            limit = 3000 + MINIMUM_PAGE_TOKENS * len(pages)
            assert spent <= limit, (spent, limit)
            # Every reply that could not hold the whole index says where to continue.
            partial = [page.data for page in pages if page.data["next_offset"] is not None]
            assert partial
            for page in partial:
                assert page["next_offset"] > 0
            # A write and its receipt are never paged away, even with the budget spent.
            receipt = (
                await client.call_tool(
                    "add_entities",
                    {"entities": [party(sources, "Late party")]},
                )
            ).data
            assert receipt[0]["id"]
            assert "E" in receipt[0]["id"]

    asyncio.run(check())


def test_snapshot_cursors_report_staleness_and_changes(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python

    async def check():
        async with Client(make_server(lambda: work)) as client:
            first = (await client.call_tool("validate_entities", {})).data
            assert first["snapshot"] == 0
            before = first["error_count"]
            work.add_entities(add([party(sources, "Client")]))
            # A party without a channel is a new blocking issue at the next snapshot.
            stale = (await client.call_tool("list_entities", {"snapshot": 0})).data
            assert stale["stale"] is True
            assert stale["snapshot"] == work.snapshot == 1
            fresh = (await client.call_tool("list_entities", {"snapshot": 1})).data
            assert fresh["rows"] and "stale" not in fresh
            grown = (await client.call_tool("validate_entities", {})).data
            assert grown["snapshot"] == 1
            assert grown["error_count"] >= before
            assert "changed_since" not in grown
            new_ids = [
                row["id"] for row in grown["rows"] if row["type"] == "party_without_channel"
            ]
            assert new_ids
            assert grown["group_count"] >= 1
            assert grown["current_group"]["group_id"] == grown["rows"][0]["group_id"]
            # Fix the row, then ask again with the snapshot the caller already saw.
            work.add_entities(add([channel(sources, "Datagrams", connected=False)]))
            after = (
                await client.call_tool("validate_entities", {"snapshot": 1})
            ).data
            assert after["stale"] is True
            assert after["snapshot"] == 2
            assert set(after["changed_since"]["gone"]) == set(new_ids)
            assert after["changed_since"]["snapshot"] == 1

    asyncio.run(check())


def test_validate_entities_pages_groups_and_issues(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add([party(sources, f"Role {index}") for index in range(15)]))
    assert len(work.issues()) == 15

    async def check():
        async with Client(make_server(lambda: work)) as client:
            catalog = (
                await client.call_tool(
                    "validate_entities", {"view": "groups", "max_tokens": 200}
                )
            ).data
            assert catalog["group_count"] == 15
            assert catalog["next_offset"] is not None
            assert len(catalog["groups"]) < 15
            group_id = catalog["groups"][0]["group_id"]
            one = (
                await client.call_tool(
                    "validate_entities", {"view": "group", "group_id": group_id}
                )
            ).data
            assert [row["group_id"] for row in one["issues"]] == [group_id]
            typed = (
                await client.call_tool(
                    "validate_entities",
                    {"view": "issues", "issue_type": "party_without_channel"},
                )
            ).data
            assert len(typed["issues"]) == 15
            clean = (
                await client.call_tool(
                    "validate_entities",
                    {"view": "group", "group_id": "no-such-group"},
                )
            ).data
            assert clean["issues"] == [] and clean["note"]

    asyncio.run(check())


def test_probe_runs_list_entities_on_an_empty_workspace(monkeypatch):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import (
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract import runner as runner_module

    def respond(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("list_entities", {})])
        assert isinstance(latest, ToolReturnPart) and latest.tool_name == "list_entities"
        assert latest.content["rows"] == []
        assert latest.content["snapshot"] == 0
        return ModelResponse(parts=[TextPart("OK")])

    model = FunctionModel(respond)
    real = runner_module.make_agent
    monkeypatch.setattr(
        runner_module,
        "make_agent",
        lambda name, toolset, config: real(model, toolset, config),
    )
    asyncio.run(runner_module.probe(ExtractConfig()))


def review_model(handler):
    """Reviewer/gap-fix model driven by tool calls; records each initial prompt."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    calls = []

    def run(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            calls.append(prompt.get("stage"))
            parts = handler(prompt, len(calls)) or []
            return ModelResponse(parts=parts)
        return ModelResponse(parts=[ToolCallPart("complete_review", {})])

    return FunctionModel(run), calls


def test_review_instructions_cover_all_categories_and_nesting():
    from rsc.extract.review import INSTRUCTIONS

    for term in (
        "Party",
        "Channel",
        "Message",
        "State",
        "Step",
        "Step dependency",
        "endpoint_sources",
        "before_send",
        "after_send",
        "on_receive",
        "message_bytes",
        "field_relations",
        "evidence",
    ):
        assert term in INSTRUCTIONS
    assert "FTP" not in INSTRUCTIONS and "TLS" not in INSTRUCTIONS
    assert "ClientHello" not in INSTRUCTIONS
    assert "do not require a state to gain attributes" in INSTRUCTIONS


def test_review_records_gaps_and_deduplicates(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart

    from rsc.extract.runner import Runner

    batch_id = next(iter(sources.batches))
    block = sources.batches[batch_id]["segments"][1]
    payload = {
        "kind": "states",
        "name": "session flag",
        "block_id": block["block_id"],
        "missing_attributes": ["reset condition"],
    }

    def handler(prompt, count):
        if not prompt.get("recorded_gaps"):
            return [
                ToolCallPart("record_gap", payload),
                ToolCallPart("record_gap", payload),
            ]
        return [ToolCallPart("complete_review", {})]

    model, calls = review_model(handler)
    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        no_review(),
        model,
    )
    asyncio.run(runner.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    entry = review["batches"][batch_id]
    assert entry["stage"] == "reviewed"
    assert len(entry["gaps"]) == 1
    gap = entry["gaps"][0]
    assert gap["document_id"] == sources.batches[batch_id]["doc_id"]
    assert gap["block_id"] == block["block_id"]
    assert gap["section_path"] == block["section_path"]
    assert gap["missing_attributes"] == ["reset condition"]
    assert gap["gap_id"].startswith(f"{gap['document_id']}-g")
    # The document group that followed fixed this gap.
    assert gap["status"] == "done"


def test_review_toolset_is_read_only(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "review"))
    reviewer = {
        "list_entities",
        "search_entities",
        "get_entity",
        "read_source",
        "validate_entities",
        "record_gap",
        "complete_review",
    }

    async def check():
        async with Client(make_server(lambda: work, readonly=True)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == reviewer
            # A read-only server does not even offer the tool, so the model cannot
            # spend a request on a rejected write.
            with pytest.raises(Exception, match="add_entities"):
                await client.call_tool("add_entities", {"entities": [party(sources, "Client")]})
            report = (await client.call_tool("validate_entities", {})).data
            assert report["error_count"] >= 0
        async with Client(make_server(lambda: work)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert len(names) == 15
            assert names - reviewer == {
                "add_entities",
                "update_entities",
                "merge_entities",
                "record_unresolved",
                "solve_unresolved",
                "complete_batch",
                "complete_fix",
                "complete_gaps",
            }

    asyncio.run(check())


def test_review_batches_run_concurrently(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    batches = list(sources.batches)
    arrival = {"count": 0}
    together = asyncio.Event()
    serial = []

    async def review(messages, info):
        latest = messages[-1].parts[-1]
        if not isinstance(latest, UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("complete_review", {})])
        prompt = json.loads(latest.content)
        arrival["count"] += 1
        if arrival["count"] >= len(batches):
            together.set()
        try:
            await asyncio.wait_for(together.wait(), 0.5)
        except TimeoutError:
            # A serial schedule reaches the second reviewer only after the first
            # assignment finished, so the first one waits out the timeout.
            serial.append(prompt["batch"]["batch_id"])
        block = prompt["batch"]["text"].split("]")[0].removeprefix("[")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "record_gap",
                    {
                        "kind": "parties",
                        "name": f"party {prompt['batch']['batch_id']}",
                        "block_id": block,
                    },
                )
            ]
        )

    config = ExtractConfig(review_requests_per_second=100, review_concurrency=2)
    runner = Runner(tmp_path, sources, "test", config, no_review(), FunctionModel(review))
    asyncio.run(runner.review_gaps())
    assert not serial, f"reviewers started serially: {serial}"
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    for batch_id in batches:
        entry = review["batches"][batch_id]
        assert entry["stage"] == "reviewed"
        # Each task writes into its own workspace: no batch sees the other's gap.
        assert [gap["batch_id"] for gap in entry["gaps"]] == [batch_id]
        assert entry["gaps"][0]["name"] == f"party {batch_id}"


def test_batches_review_in_parallel_then_fix_grouped(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    order = []
    batches = list(sources.batches)

    def handler(prompt, count):
        batch_id = prompt["batch"]["batch_id"]
        order.append(("review", batch_id))
        if batch_id == batches[0] and not prompt.get("recorded_gaps"):
            block = prompt["batch"]["text"].split("]")[0].removeprefix("[")
            return [
                ToolCallPart(
                    "record_gap", {"kind": "parties", "name": "client", "block_id": block}
                )
            ]
        return [ToolCallPart("complete_review", {})]

    model, calls = review_model(handler)
    fallback = fake_model(sources, [])

    def main(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt.get("stage") == "gap":
                order.append(("gap", prompt["assignment"]))
        return fallback.function(messages, info)

    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        FunctionModel(main),
        model,
    )
    asyncio.run(runner.review_gaps())
    reviews = [index for index, (phase, _) in enumerate(order) if phase == "review"]
    gaps = [index for index, (phase, _) in enumerate(order) if phase == "gap"]
    assert len(reviews) == len(batches) and gaps
    # Every batch is reviewed before the first document group runs.
    assert max(reviews) < min(gaps)


def test_zero_gap_batch_skips_gap_fix(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart

    from rsc.extract.runner import Runner

    model, calls = review_model(lambda prompt, count: [ToolCallPart("complete_review", {})])
    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        no_review(),
        model,
    )
    asyncio.run(runner.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert {entry["stage"] for entry in review["batches"].values()} == {"reviewed"}
    assert review["groups"] == {}
    assert not list((tmp_path / "llm").glob("gap/*"))


def test_review_role_is_read_only_and_completion_tools_are_separated(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    review_work = Workspace(
        sources, "test", empty_registry(), Assignment(batch_id, "review")
    )
    gap_work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    scan_work = Workspace(
        sources, "test", empty_registry(), Assignment(batch_id, "scan", "parties")
    )

    async def check():
        async with Client(make_server(lambda: review_work)) as client:
            with pytest.raises(Exception, match="reviewer cannot modify"):
                await client.call_tool("add_entities", {"entities": [party(sources, "Client")]})
            with pytest.raises(Exception, match="use complete_fix for review assignments"):
                await client.call_tool("complete_fix", {"summary": "x"})
            with pytest.raises(Exception, match="requires a gap"):
                await client.call_tool("complete_gaps", {})
        async with Client(make_server(lambda: gap_work)) as client:
            with pytest.raises(Exception, match="requires a review"):
                await client.call_tool("complete_review", {})
            accepted = await client.call_tool("complete_gaps", {})
            assert accepted.data["accepted"] is True
        async with Client(make_server(lambda: scan_work)) as client:
            with pytest.raises(Exception, match="requires a review"):
                await client.call_tool("complete_review", {})

    asyncio.run(check())


def failing_review_model(failing):
    """Reviewer fake that fails the named batch once, then completes."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    attempts = {}

    def run(messages, info):
        latest = messages[-1].parts[-1]
        if not isinstance(latest, UserPromptPart):
            return ModelResponse(parts=[])
        prompt = json.loads(latest.content)
        batch_id = prompt["batch"]["batch_id"]
        if batch_id == failing and not attempts.get(batch_id):
            attempts[batch_id] = 1
            return ModelResponse(parts=[])
        return ModelResponse(parts=[ToolCallPart("complete_review", {})])

    return FunctionModel(run), attempts


def test_review_failure_is_isolated(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")

    from rsc.extract.runner import Runner

    batches = list(sources.batches)
    failing, sibling = batches[0], batches[1]
    model, attempts = failing_review_model(failing)
    config = ExtractConfig(
        review_requests_per_second=100, assignment_retries=0, max_tool_retries=0
    )
    runner = Runner(tmp_path, sources, "test", config, no_review(), model)
    with pytest.raises(ValueError):
        asyncio.run(runner.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["batches"][failing]["stage"] == "review_failed"
    # The sibling finished even though one batch failed, and its work is on disk.
    assert review["batches"][sibling]["stage"] == "reviewed"
    assert review["batches"][sibling]["review_calls"] >= 1

    resumed = Runner(tmp_path, sources, "test", config, no_review(), model)
    asyncio.run(resumed.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["batches"][failing]["stage"] == "reviewed"
    # Only the failed batch was reviewed again.
    assert attempts == {failing: 1}


def test_review_failure_keeps_batch_unfinished_and_resume_retries_it(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")

    from rsc.extract.runner import Runner

    batch_id = next(iter(sources.batches))
    model, attempts = failing_review_model(batch_id)
    config = ExtractConfig(
        review_requests_per_second=100, assignment_retries=0, max_tool_retries=0
    )
    runner = Runner(tmp_path, sources, "test", config, no_review(), model)
    with pytest.raises(ValueError):
        asyncio.run(runner.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["batches"][batch_id]["stage"] == "review_failed"
    resumed = Runner(tmp_path, sources, "test", config, no_review(), model)
    asyncio.run(resumed.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["batches"][batch_id]["stage"] == "reviewed"


def test_gap_failure_resumes_at_gap_stage(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    batch_id = next(iter(sources.batches))
    document_id = sources.batches[batch_id]["doc_id"]
    group_id = f"gap-{document_id}-1"
    fix_attempts = {"n": 0}

    def handler(prompt, count):
        if not prompt.get("recorded_gaps"):
            block = prompt["batch"]["text"].split("]")[0].removeprefix("[")
            return [
                ToolCallPart(
                    "record_gap", {"kind": "parties", "name": "client", "block_id": block}
                )
            ]
        return [ToolCallPart("complete_review", {})]

    reviewer, _ = review_model(handler)

    def main(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt.get("stage") == "gap":
                fix_attempts["n"] += 1
                if fix_attempts["n"] == 1:
                    return ModelResponse(parts=[])
                return ModelResponse(parts=[ToolCallPart("complete_gaps", {})])
        return ModelResponse(parts=[])

    config = ExtractConfig(
        review_requests_per_second=100, assignment_retries=0, max_tool_retries=0
    )
    runner = Runner(tmp_path, sources, "test", config, FunctionModel(main), reviewer)
    with pytest.raises(ValueError):
        asyncio.run(runner.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["groups"][group_id]["status"] == "failed"
    assert review["batches"][batch_id]["stage"] == "reviewed"

    resumed = Runner(tmp_path, sources, "test", config, FunctionModel(main), reviewer)
    asyncio.run(resumed.review_gaps())
    review = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert review["groups"][group_id]["status"] == "done"
    assert review["batches"][batch_id]["gaps"][0]["status"] == "done"


def test_gaps_grouped_by_document(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    batches = list(sources.batches)
    expected = {
        batches[0]: [("states", "flag"), ("parties", "client"), ("messages", "hello")],
        batches[1]: [("states", "other flag"), ("parties", "other client")],
    }

    def handler(prompt, count):
        batch_id = prompt["batch"]["batch_id"]
        if prompt.get("recorded_gaps"):
            return [ToolCallPart("complete_review", {})]
        block = prompt["batch"]["text"].split("]")[0].removeprefix("[")
        return [
            ToolCallPart(
                "record_gap", {"kind": kind, "name": name, "block_id": block}
            )
            for kind, name in expected[batch_id]
        ]

    reviewer, _ = review_model(handler)
    seen = []
    fallback = fake_model(sources, [])

    def main(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt.get("stage") == "gap":
                seen.append(
                    (
                        prompt["assignment"],
                        prompt["document_id"],
                        [gap["gap_id"] for gap in prompt["gaps"]],
                    )
                )
        return fallback.function(messages, info)

    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        FunctionModel(main),
        reviewer,
    )
    asyncio.run(runner.review_gaps())
    state = read_json(tmp_path / "extract-checkpoint.json")["review"]
    first, second = (sources.batches[batch_id]["doc_id"] for batch_id in batches)
    assert set(state["groups"]) == {f"gap-{first}-1", f"gap-{second}-1"}
    group = state["groups"][f"gap-{first}-1"]
    assert group["status"] == "done"
    assert group["gap_ids"] == [f"{first}-g{n}" for n in (1, 2, 3)]
    assert state["groups"][f"gap-{second}-1"]["gap_ids"] == [
        f"{second}-g{n}" for n in (1, 2)
    ]
    # Documents run in manifest order, and the groups never interleave.
    assert [(item[0], item[1]) for item in seen] == [
        (f"gap-{first}-1", first),
        (f"gap-{second}-1", second),
    ]


def test_gap_group_respects_max_and_ids_are_stable(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    batch_id = next(iter(sources.batches))
    document_id = sources.batches[batch_id]["doc_id"]
    names = [f"missing {number}" for number in range(1, 8)]

    def handler(prompt, count):
        # Only the first document records gaps, so its seven gaps span three groups.
        if prompt["batch"]["batch_id"] != batch_id or prompt.get("recorded_gaps"):
            return [ToolCallPart("complete_review", {})]
        block = prompt["batch"]["text"].split("]")[0].removeprefix("[")
        return [
            ToolCallPart("record_gap", {"kind": "states", "name": name, "block_id": block})
            for name in names
        ]

    reviewer, _ = review_model(handler)
    calls = []

    def main(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            calls.append(json.loads(latest.content)["assignment"])
        return ModelResponse(parts=[ToolCallPart("complete_gaps", {})])

    config = ExtractConfig(review_requests_per_second=100, gap_group_max=3)
    runner = Runner(tmp_path, sources, "test", config, FunctionModel(main), reviewer)
    asyncio.run(runner.review_gaps())
    state = read_json(tmp_path / "extract-checkpoint.json")["review"]
    groups = {
        f"gap-{document_id}-{number}": state["groups"][f"gap-{document_id}-{number}"]
        for number in (1, 2, 3)
    }
    assert [len(group["gap_ids"]) for group in groups.values()] == [3, 3, 1]
    ids = [gap_id for group in groups.values() for gap_id in group["gap_ids"]]
    assert ids == [f"{document_id}-g{number}" for number in range(1, 8)]

    before = {
        key: (group["gap_ids"], group["status"]) for key, group in groups.items()
    }
    # A resume recomputes the same ids and skips every finished group.
    resumed = Runner(tmp_path, sources, "test", config, FunctionModel(main), reviewer)
    calls.clear()
    asyncio.run(resumed.gap_groups())
    state = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert calls == []
    assert {
        key: (group["gap_ids"], group["status"]) for key, group in groups.items()
    } == before
    assert {
        key: group["gap_ids"] for key, group in state["groups"].items()
    } == {key: ids for key, (ids, _) in before.items()}

    # A crash after the first group must not shift the boundaries of the rest.
    crashed = read_json(tmp_path / "extract-checkpoint.json")
    finished = crashed["review"]["groups"][f"gap-{document_id}-1"]["gap_ids"]
    for number in (2, 3):
        crashed["review"]["groups"][f"gap-{document_id}-{number}"]["status"] = "failed"
    for gap in crashed["review"]["batches"][batch_id]["gaps"]:
        if gap["gap_id"] not in finished:
            gap["status"] = "pending"
    atomic_json(tmp_path / "extract-checkpoint.json", crashed)
    resumed = Runner(tmp_path, sources, "test", config, FunctionModel(main), reviewer)
    calls.clear()
    asyncio.run(resumed.gap_groups())
    state = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert calls == [f"gap-{document_id}-2", f"gap-{document_id}-3"]
    for key, (gap_ids, _) in before.items():
        assert state["groups"][key]["gap_ids"] == gap_ids
        assert state["groups"][key]["status"] == "done"


def test_record_gap_uses_supplied_block_id(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    batch = sources.batches[batch_id]
    block = batch["segments"][2]
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "review"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            result = await client.call_tool(
                "record_gap",
                {"kind": "states", "name": "flag", "block_id": block["block_id"]},
            )
            gap = result.data["gap"]
            assert result.data["recorded"] is True
            assert gap["block_id"] == block["block_id"]
            assert gap["document_id"] == batch["doc_id"]
            assert gap["section_path"] == block["section_path"]
            assert gap["batch_id"] == batch_id
            assert gap["evidence_text"] == ""

    asyncio.run(check())


def test_record_gap_keeps_unmatched_text_verbatim(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    block_id = sources.batches[batch_id]["segments"][0]["block_id"]
    quote = "  a paraphrase that appears nowhere in this RFC,   word for word  "
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "review"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            result = await client.call_tool(
                "record_gap",
                {
                    "kind": "states",
                    "name": "flag",
                    "block_id": block_id,
                    "evidence_text": quote,
                },
            )
            assert result.data["recorded"] is True
            # Trimmed at the edges and otherwise stored exactly as given.
            assert result.data["gap"]["evidence_text"] == quote.strip()

    asyncio.run(check())


def test_record_gap_rejects_unknown_block_id(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    batch = sources.batches[batch_id]
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "review"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            with pytest.raises(Exception) as caught:
                await client.call_tool(
                    "record_gap",
                    {"kind": "states", "name": "flag", "block_id": "blk-999999"},
                )
            message = str(caught.value)
            assert "is not a block of" in message
            assert batch["segments"][0]["block_id"] in message

    asyncio.run(check())


def test_record_gap_dedupes_by_block(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    block_id = sources.batches[batch_id]["segments"][0]["block_id"]
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "review"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            first = await client.call_tool(
                "record_gap",
                {
                    "kind": "states",
                    "name": "flag",
                    "block_id": block_id,
                    "evidence_text": "first wording",
                },
            )
            second = await client.call_tool(
                "record_gap",
                {
                    "kind": "states",
                    "name": "flag",
                    "block_id": block_id,
                    "evidence_text": "a different wording",
                },
            )
            assert first.data["recorded"] is True
            assert second.data["recorded"] is False
            assert second.data["gap"]["evidence_text"] == "first wording"
            assert len(work.gaps) == 1

    asyncio.run(check())


def test_complete_gaps_records_unresolved_ids(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    document_id = sources.batches[batch_id]["doc_id"]
    gaps = [
        {
            "gap_id": f"{document_id}-g{number}",
            "kind": "states",
            "name": f"gap {number}",
            "missing_attributes": [],
            "document_id": document_id,
            "block_id": sources.batches[batch_id]["segments"][0]["block_id"],
            "section_path": [],
            "batch_id": batch_id,
            "evidence_text": "",
            "note": f"cited from {batch_id}",
            "status": "pending",
        }
        for number in (1, 2)
    ]
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    work.gaps = gaps

    async def check():
        async with Client(make_server(lambda: work)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            # Both arguments stay optional so the single-batch path is unchanged.
            assert tools["complete_gaps"].inputSchema.get("required") in (None, [])
            with pytest.raises(Exception, match="unknown gap ids"):
                await client.call_tool(
                    "complete_gaps", {"unresolved_gap_ids": [f"{document_id}-g9"]}
                )
            accepted = await client.call_tool(
                "complete_gaps",
                {"summary": "one gap is not supported", "unresolved_gap_ids": [gaps[1]["gap_id"]]},
            )
            assert accepted.data["accepted"] is True
        assert work.completion["unresolved_gap_ids"] == [gaps[1]["gap_id"]]
        assert work.completion["summary"] == "one gap is not supported"
        assert work.completion["status"] == "gaps_completed"

    asyncio.run(check())

    plain = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    plain.gaps = []

    async def empty_call():
        async with Client(make_server(lambda: plain)) as client:
            accepted = await client.call_tool("complete_gaps", {})
            assert accepted.data["accepted"] is True
        assert plain.completion["unresolved_gap_ids"] == []

    asyncio.run(empty_call())


def test_old_review_checkpoint_is_migrated(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    batches = list(sources.batches)
    done_batch, gap_batch = batches[0], batches[1]
    gap_doc = sources.batches[gap_batch]["doc_id"]

    def old_gap(batch_id, name):
        return {
            "kind": "states",
            "name": name,
            "evidence_text": "old quote",
            "missing_attributes": [],
            "document_id": sources.batches[batch_id]["doc_id"],
            "block_id": sources.batches[batch_id]["segments"][0]["block_id"],
            "section_path": [],
            "batch_id": batch_id,
            "note": f"cited from {batch_id}",
        }

    atomic_json(
        tmp_path / "extract-checkpoint.json",
        {
            "scan_kinds": list(SCAN_KINDS),
            "category": len(SCAN_KINDS),
            "batch": 0,
            "global_checks": 0,
            "global_errors": None,
            "registry": empty_registry(),
            "history": [],
            "compact_summary": "",
            "records": [],
            "published": False,
            "review": {
                "cursor": 1,
                "batches": {
                    done_batch: {"stage": "done", "gaps": [old_gap(done_batch, "legacy gap")]},
                    gap_batch: {"stage": "gap", "gaps": [old_gap(gap_batch, "open gap")]},
                },
            },
        },
    )
    seen = []
    fallback = fake_model(sources, [])

    def main(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt.get("stage") == "gap":
                seen.append(prompt)
        return fallback.function(messages, info)

    reviewer, _ = review_model(lambda prompt, count: [ToolCallPart("complete_review", {})])
    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        FunctionModel(main),
        reviewer,
    )
    review = runner.state["review"]
    assert "cursor" not in review
    assert review["batches"][done_batch]["stage"] == "done"
    assert review["batches"][done_batch]["gaps"][0]["status"] == "legacy_done"
    assert review["batches"][gap_batch]["stage"] == "reviewed"
    assert review["batches"][gap_batch]["gaps"][0]["status"] == "pending"

    asyncio.run(runner.review_gaps())
    state = read_json(tmp_path / "extract-checkpoint.json")["review"]
    assert state["batches"][done_batch]["gaps"][0]["status"] == "legacy_done"
    assert seen and seen[0]["assignment"] == f"gap-{gap_doc}-1"
    assert seen[0]["document_id"] == gap_doc
    assert [gap["name"] for gap in seen[0]["gaps"]] == ["open gap"]
    # The finished batch is not reviewed again and its legacy gap is never re-fixed.
    assert state["batches"][done_batch]["stage"] == "done"
    assert set(state["groups"]) == {f"gap-{gap_doc}-1"}
    assert state["batches"][gap_batch]["gaps"][0]["status"] == "done"
    assert seen[0]["source_batches"] == [gap_batch]


def test_list_entities_batch_filter_returns_full_definitions(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add([party(sources, "Client")]))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            result = await client.call_tool(
                "list_entities", {"batch_id": batch_id, "full": True}
            )
            row = result.data["rows"][0]
            assert row["value"]["description"] == "Protocol endpoint"
            assert row["evidence"][0]["block_id"]
            assert "text" not in row["evidence"][0]
            assert result.data["snapshot"] == work.snapshot
            assert result.data["related_step_count"] == 0

    asyncio.run(check())


def test_complete_gaps_takes_no_arguments(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "gap"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            schema = tools["complete_gaps"].inputSchema
            assert set(schema["properties"]) == {"summary", "unresolved_gap_ids"}
            assert schema.get("required") in (None, [])
            accepted = await client.call_tool("complete_gaps", {})
            assert accepted.data["accepted"] is True

    asyncio.run(check())


def test_every_issue_type_has_a_suggestion():
    from rsc.extract.guidance import GENERIC_HINT, issue_suggestion, with_suggestions

    hints = [
        {
            "type": "missing_reference",
            "description": "states reference ('Client','Flag') has 0 matches",
        },
        {"type": "same_name", "description": "ambiguous parties name 'Client'"},
        {"type": "structure", "description": "state 'Client Flag' has no processing producer"},
        {"type": "structure", "description": "field 'digest' is not carried by 'Alert'"},
        {"type": "structure", "description": "channel 'C' role is not an endpoint"},
        {"type": "structure", "description": "connectionless channel has connection roles"},
        {"type": "state_without_access", "description": "State 'Flag' has no hook access"},
        {
            "type": "channel_requires_close",
            "description": "Channel 'C' requires an endpoint close step",
        },
        {"type": "deferred_entity", "description": "missing definition"},
    ]
    for row in hints:
        text = issue_suggestion(row)
        assert text and text != GENERIC_HINT
    rows = with_suggestions(hints)
    assert all(row["suggestion"] == issue_suggestion(row) for row in rows)
    assert hints[0] not in rows or "suggestion" not in hints[0]
    producer = with_suggestions([hints[2]])[0]["suggestion"]
    assert "writer" in producer or "writes state.Client Flag" in producer
    assert "external" in producer


def test_validate_entities_returns_suggestions(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work = Workspace(sources, "test", empty_registry(), Assignment("test", "category", "channels"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(add([party(sources, "Client")]))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            report = (await client.call_tool("validate_entities", {})).data
            assert report["error_count"] >= 1
            rows = report["rows"] or (
                await client.call_tool("validate_entities", {"view": "issues"})
            ).data["issues"]
            assert all(row["id"] and row["group_id"] for row in rows)
            details = [
                (
                    await client.call_tool(
                        "validate_entities", {"view": "issue", "issue_id": row["id"]}
                    )
                ).data["issue"]
                for row in rows[:3]
            ]
            assert all(detail["suggestion"] for detail in details)

    asyncio.run(check())


def test_global_fix_prompt_carries_suggestions_and_start_count(sources, tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    seen = []
    original = Workspace.structural_issues
    fired = []

    def one_error(work):
        if work.assignment.id == "global-validation" and not fired:
            fired.append(1)
            return [{"type": "test", "records": [], "path": [], "description": "try once"}]
        return original(work)

    fallback = fake_model(sources, []).function

    def watch(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt["assignment"]["phase"] == "global":
                seen.append(prompt)
        return fallback(messages, info)

    monkeypatch.setattr(Workspace, "structural_issues", one_error)
    runner = Runner(tmp_path, sources, "test", ExtractConfig(), FunctionModel(watch), no_review())
    asyncio.run(runner.run())
    assert seen
    assert seen[0]["repair_start_count"] == 1
    assert seen[0]["validation_error_count"] == 1
    assert [row["description"] for row in seen[0]["validation_errors"]] == ["try once"]
    assert seen[0]["validation_errors_next_offset"] is None
    assert seen[0]["fix_groups"]
    assert seen[0]["current_group"]["issues_in_page"] == 1


def test_list_entities_returns_related_step_scenarios(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    first, second = tuple(sources.batches)
    seed = Workspace(sources, "test", empty_registry(), Assignment(first, "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    fixture = fixtures_by_kind(sources)
    seed.add_entities(
        add(
            [
                fixture["messages"],
                fixture["steps"],
                {
                    **fixture["steps"],
                    "evidence": [
                        {
                            "document_id": sources.batches[second]["doc_id"],
                            "block_id": sources.batches[second]["segments"][0]["block_id"],
                        }
                    ],
                    "value": {**fixture["steps"]["value"], "name": "Send elsewhere"},
                },
            ]
        )
    )

    work = Workspace(sources, "test", seed.data, Assignment(first, "review"))

    async def check():
        async with Client(make_server(lambda: work)) as client:
            result = (await client.call_tool("list_entities", {"batch_id": first})).data
            steps = result.get("related_steps") or []
            assert steps, "same-message steps from other batches are summarized"
            row = steps[0]
            assert set(row) == {"id", "name", "action", "channel", "condition", "hooks"}
            assert "rule" not in json.dumps(row)

    asyncio.run(check())


def test_hook_stats_recorded_after_review(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ToolCallPart

    from rsc.extract.runner import Runner

    model, calls = review_model(lambda prompt, count: [ToolCallPart("complete_review", {})])
    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(review_requests_per_second=100),
        no_review(),
        model,
    )
    asyncio.run(runner.review_gaps())
    records = [r for r in runner.state["records"] if r["phase"] == "review"]
    assert records and all("hook_stats" in r for r in records)
    assert set(records[0]["hook_stats"]) == {"hooks", "actions"}


def check_row(sources, target, kind="channel_requires_connect"):
    """One synthetic coverage row, shaped like the checks Fix rounds work on."""
    return {
        "type": kind,
        "check_ref": f"{kind}:{target}",
        "records": [target],
        "path": [],
        "description": f"{target} needs a {kind.rsplit('_', 1)[-1]}",
        "evidence": [citation(sources)],
    }


CHANNEL_CHECKS = (
    "channel_requires_connect",
    "channel_requires_listen",
    "channel_requires_close",
)


def unresolved_item(sources, item_id, targets, **updates):
    """An open item that explains the given targets' checks, with a frozen location."""
    kinds = updates.pop("kinds", CHANNEL_CHECKS)
    item = {
        "id": item_id,
        "description": "The source leaves the transport lifecycle to the layers above.",
        "missing_information": "Transport operations for this channel.",
        "entity_ids": list(targets),
        "evidence": [citation(sources)],
        "check_refs": [f"{kind}:{target}" for target in targets for kind in kinds],
    }
    item.update(updates)
    return item


def fix_group(rows, unresolved=()):
    """The single group the given rows and open items produce."""
    from rsc.extract.groups import fix_groups

    return fix_groups(rows, list(unresolved), {})[0]


def test_blocked_signature_ignores_order_but_tracks_content(sources):
    from rsc.extract.groups import blocked_signature

    first, second = [{"document_id": doc, "block_id": block} for doc, block in
                     list(sources.blocks)[:2]]
    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
    ]
    item = unresolved_item(sources, "U1", ["E1"])
    item["evidence"] = [first, second]
    group = fix_group(rows, [item])
    signature = blocked_signature(group, [item], sources)
    assert signature
    # Reordering the checks or the cited evidence changes nothing.
    reordered = deepcopy(item)
    reordered["evidence"] = [second, first]
    shuffled = [deepcopy(rows[1]), deepcopy(rows[0])]
    assert blocked_signature(fix_group(shuffled, [reordered]), [reordered], sources) == signature
    # A note, an extra check or a location that no longer resolves does change it.
    noted = deepcopy(item)
    noted["evidence"] = [{**first, "note": "different reading"}, second]
    assert blocked_signature(group, [noted], sources) != signature
    extra = [*rows, check_row(sources, "E1", "channel_requires_close")]
    assert blocked_signature(fix_group(extra, [item]), [item], sources) != signature
    gone = deepcopy(item)
    gone["evidence"] = [{"document_id": "rfc5246", "block_id": "blk-999999"}]
    assert blocked_signature(group, [gone], sources) == ""


@pytest.mark.parametrize(
    "case",
    [
        "item-covers-only-part-of-the-group",
        "structural-row-without-a-check",
        "repeated-check-ref",
        "item-does-not-cover-every-check",
    ],
)
def test_blocked_signature_needs_one_located_item_for_every_check(sources, case):
    from rsc.extract.groups import blocked_signature

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
    ]
    kinds = CHANNEL_CHECKS
    if case == "item-covers-only-part-of-the-group":
        kinds = ("channel_requires_connect",)
    elif case == "structural-row-without-a-check":
        rows = [*rows, {"type": "structure", "records": ["E1"], "path": ["x"], "description": "s"}]
    elif case == "repeated-check-ref":
        rows = [rows[0], {**rows[1], "check_ref": rows[0]["check_ref"]}]
    elif case == "item-does-not-cover-every-check":
        rows = [*rows, check_row(sources, "E1", "channel_requires_close")]
        kinds = ("channel_requires_connect", "channel_requires_listen")
    item = unresolved_item(sources, "U1", ["E1"], kinds=kinds)
    group = fix_group(rows, [item])
    assert blocked_signature(group, [item], sources) == ""


def test_blocked_signature_rejects_two_candidates_and_missing_evidence(sources):
    from rsc.extract.groups import blocked_signature

    rows = [check_row(sources, "E1", "channel_requires_connect")]
    first = unresolved_item(sources, "U1", ["E1"])
    second = unresolved_item(sources, "U2", ["E1"])
    group = fix_group(rows, [first, second])
    # Both items explain the check, so neither one is the answer yet.
    assert blocked_signature(group, [first, second], sources) == ""
    alone = fix_group(rows, [first])
    assert blocked_signature(alone, [first], sources)
    blind = deepcopy(first)
    blind["evidence"] = []
    assert blocked_signature(alone, [blind], sources) == ""


def test_deferred_groups_wait_behind_their_peers(sources):
    from rsc.extract.groups import blocked_signature, ordered_issue_rows

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_connect"),
        check_row(sources, "E3", "channel_requires_connect"),
        check_row(sources, "E3", "channel_requires_listen"),
        check_row(sources, "E3", "channel_requires_close"),
    ]
    items = [unresolved_item(sources, "U1", ["E1"]), unresolved_item(sources, "U3", ["E3"])]
    ordered, _ = ordered_issue_rows(rows, items, {}, sources, {})
    assert [group["group_id"] for group in ordered] == ["E3", "E1", "E2"]
    signature = blocked_signature(ordered[0], items, sources)
    assert signature
    deferred = {"E3": signature}
    ordered, rows_after = ordered_issue_rows(rows, items, {}, sources, deferred)
    # The stalled group keeps its own rank but loses priority, and the rows follow.
    assert [group["group_id"] for group in ordered] == ["E1", "E2", "E3"]
    assert [row["group_id"] for row in rows_after] == [
        "E1", "E1", "E2", "E3", "E3", "E3"
    ]
    assert deferred == {"E3": signature}

@pytest.mark.parametrize(
    "case",
    ["evidence-changed", "check-added", "check-removed", "item-closed", "item-replaced"],
)
def test_deferral_clears_when_the_signature_changes(sources, case):
    from rsc.extract.groups import blocked_signature, ordered_issue_rows

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_connect"),
    ]
    first, second = [{"document_id": doc, "block_id": block} for doc, block in
                     list(sources.blocks)[:2]]
    item = unresolved_item(sources, "U1", ["E1"])
    item["evidence"] = [first]
    items = [item]
    deferred = {"E1": blocked_signature(fix_group(rows, items), items, sources)}
    assert deferred["E1"]

    if case == "evidence-changed":
        items = [{**item, "evidence": [second]}]
    elif case == "check-added":
        rows = [*rows, check_row(sources, "E1", "channel_requires_close")]
    elif case == "check-removed":
        rows = rows[:1] + rows[2:]
    elif case == "item-closed":
        items = []
    elif case == "item-replaced":
        items = [{**item, "id": "U9", "description": "A different explanation."}]

    ordered, _ = ordered_issue_rows(rows, items, {}, sources, deferred)
    eligible = [group["group_id"] for group in ordered if group["count"]]
    # The group is selectable again, so it is no longer part of the deferred set.
    assert "E1" not in deferred
    if rows[0]["records"] == ["E1"]:
        assert eligible[0] == "E1" or "E1" in eligible[:2]
    assert deferred == {}


def test_head_group_skips_deferred_groups(sources):
    from rsc.extract.groups import head_group, ordered_issue_rows

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E3", "channel_requires_connect"),
        check_row(sources, "E3", "channel_requires_listen"),
        check_row(sources, "E3", "channel_requires_close"),
    ]
    groups, _ = ordered_issue_rows(rows, [], {}, sources, {})
    assert head_group(groups, {})["group_id"] == "E3"
    # A group is skipped only while its own entry holds it back.
    assert head_group(groups, {"E1": "signature"})["group_id"] == "E3"
    assert head_group(groups, {"E3": "signature"})["group_id"] == "E1"
    # Everything deferred: the round opens the catalog with no preferred group.
    assert head_group(groups, {"E1": "s", "E3": "s"}) is None
    assert head_group([], {}) is None


def deferrable_runner(sources, tmp_path, monkeypatch, respond, **config):
    """A Runner whose basics Fix sees two located groups, one per entity."""
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from rsc.extract.runner import Runner

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_connect"),
        check_row(sources, "E2", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_close"),
    ]
    items = [
        unresolved_item(sources, "U1", ["E1"]),
        unresolved_item(sources, "U2", ["E2"]),
    ]
    seed = Workspace(sources, "test", empty_registry(), Assignment("seed", "global"))
    add = TypeAdapter(list[EntityAddition]).validate_python
    seed.add_entities(
        add([party(sources, "Initiator"), party(sources, "Acceptor")])
    )
    registry = seed.data
    registry["unresolved"] = {item["id"]: item for item in items}

    prompts = []
    seen_calls = []

    def model(messages, info):
        latest = messages[-1].parts[-1]
        if isinstance(latest, UserPromptPart):
            prompt = json.loads(latest.content)
            if prompt["assignment"]["phase"] == "category":
                prompts.append(prompt)
            reply = respond(prompt, latest, seen_calls)
            if reply is not None:
                return reply
            head = prompt.get("current_group")
            if head is None:
                return ModelResponse(parts=[TextPart("Every group waits this round.")])
            seen_calls.append(head["group_id"])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "validate_entities", {"view": "group", "group_id": head["group_id"]}
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("This repair has no source-supported change.")])

    monkeypatch.setattr(Workspace, "structural_issues", lambda work: [])
    monkeypatch.setattr(Workspace, "coverage_issues", lambda work: [dict(row) for row in rows])
    runner = Runner(
        tmp_path,
        sources,
        "test",
        ExtractConfig(**config),
        FunctionModel(model),
        no_review(),
    )
    runner.save(
        registry=registry,
        category=0,
        batch=0,
        history=[],
        compact_summary="",
        category_repairs=0,
    )
    return runner, prompts


def test_category_fix_defers_a_group_without_progress(sources, tmp_path, monkeypatch, capsys):
    runner, prompts = deferrable_runner(
        sources, tmp_path, monkeypatch, lambda prompt, latest, calls: None, max_repair_rounds=2
    )

    async def check():
        async with runner.toolset:
            await runner.validate_category(0, "basics")

    asyncio.run(check())
    out = capsys.readouterr().out
    assert "deferring E2" in out and "deferring E1" in out
    # The largest group leads; once it stalls it keeps its rank but moves behind.
    assert prompts[0]["current_group"]["group_id"] == "E2"
    assert [row["group_id"] for row in prompts[0]["fix_groups"]] == ["E2", "E1"]
    assert prompts[1]["current_group"]["group_id"] == "E1"
    assert [row["group_id"] for row in prompts[1]["fix_groups"]] == ["E1", "E2"]
    # Both groups stalled: the round still gets the catalog, without a preferred group.
    assert "current_group" not in prompts[2]
    # With every group waiting they keep their original rank.
    assert [row["group_id"] for row in prompts[2]["fix_groups"]] == ["E2", "E1"]
    assert prompts[2]["validation_error_count"] == 5
    # Every attempt read its group and was sealed as unfinished, as before.
    rounds = [record for record in runner.state["records"] if record.get("phase") == "category"]
    assert [record["status"] for record in rounds] == ["failed", "failed"]
    # The scheduling state is memory only: nothing about it reaches the checkpoint.
    saved = read_json(tmp_path / "extract-checkpoint.json")
    assert "deferred_groups" not in json.dumps(saved)


def test_category_fix_keeps_the_group_after_other_failures(sources, tmp_path, monkeypatch, capsys):
    from rsc.extract.runner import AssignmentFailed

    runner, prompts = deferrable_runner(
        sources, tmp_path, monkeypatch, lambda prompt, latest, calls: None, max_repair_rounds=1
    )

    async def failing(work, issues=(), defer=None, **kwargs):
        raise AssignmentFailed(f"{work.assignment.id}: provider error", incomplete=False)

    async def check():
        async with runner.toolset:
            monkeypatch.setattr(runner, "execute", failing)
            await runner.validate_category(0, "basics")

    asyncio.run(check())
    out = capsys.readouterr().out
    # A provider error, a timeout or a tool rejection is not a recorded stall.
    assert "deferring" not in out
    assert out.count("failed; retry") == 2
    rounds = [record for record in runner.state["records"] if record.get("phase") == "category"]
    assert [record["status"] for record in rounds] == ["failed"]


def test_category_fix_keeps_the_group_when_it_was_never_read(
    sources, tmp_path, monkeypatch, capsys
):
    def never_read(prompt, latest, calls):
        from pydantic_ai.messages import ModelResponse, TextPart

        return ModelResponse(parts=[TextPart("Nothing to change here.")])

    runner, prompts = deferrable_runner(
        sources, tmp_path, monkeypatch, never_read, max_repair_rounds=1
    )

    async def check():
        async with runner.toolset:
            await runner.validate_category(0, "basics")

    asyncio.run(check())
    out = capsys.readouterr().out
    # An attempt that never opened the group cannot claim the group had no progress.
    assert "deferring" not in out
    assert [prompt["current_group"]["group_id"] for prompt in prompts] == ["E2", "E2", "E2"]


def test_category_fix_keeps_the_group_when_its_signature_moved(
    sources, tmp_path, monkeypatch, capsys
):
    runner, prompts = deferrable_runner(
        sources, tmp_path, monkeypatch, lambda prompt, latest, calls: None, max_repair_rounds=1
    )
    cites = [{"document_id": doc, "block_id": block} for doc, block in list(sources.blocks)[:2]]
    calls = []

    def alternating(work):
        # Every read sees a different citation for the same checks, so the blocked
        # signature an attempt started with never survives to the end of it.
        cites_turn = cites[len(calls) % 2]
        calls.append(1)
        return [
            {**row, "evidence": [cites_turn]}
            for row in [
                check_row(sources, "E1", "channel_requires_connect"),
                check_row(sources, "E1", "channel_requires_listen"),
                check_row(sources, "E2", "channel_requires_connect"),
                check_row(sources, "E2", "channel_requires_listen"),
                check_row(sources, "E2", "channel_requires_close"),
            ]
        ]

    async def check():
        async with runner.toolset:
            monkeypatch.setattr(Workspace, "coverage_issues", alternating)
            await runner.validate_category(0, "basics")

    asyncio.run(check())
    out = capsys.readouterr().out
    assert "deferring" not in out
    # The group stays selectable: its signature moved, so it is ranked by size again.
    assert [prompt["current_group"]["group_id"] for prompt in prompts[:2]] == ["E2", "E2"]


def test_validate_entities_follows_the_deferred_order(sources, monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.groups import blocked_signature
    from rsc.extract.tools import make_server

    rows = [
        check_row(sources, "E1", "channel_requires_connect"),
        check_row(sources, "E1", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_connect"),
        check_row(sources, "E2", "channel_requires_listen"),
        check_row(sources, "E2", "channel_requires_close"),
    ]
    items = [unresolved_item(sources, "U1", ["E1"]), unresolved_item(sources, "U2", ["E2"])]
    work = Workspace(sources, "test", empty_registry(), Assignment("test", "category", "channels"))
    work.data["unresolved"] = {item["id"]: item for item in items}
    monkeypatch.setattr(Workspace, "structural_issues", lambda self: [])
    monkeypatch.setattr(Workspace, "coverage_issues", lambda self: [dict(row) for row in rows])

    async def check():
        async with Client(make_server(lambda: work)) as client:
            groups_view = {"view": "groups"}
            catalog = (await client.call_tool("validate_entities", groups_view)).data["groups"]
            assert [row["group_id"] for row in catalog] == ["E2", "E1"]
            summary = (await client.call_tool("validate_entities", {})).data
            assert summary["current_group"]["group_id"] == "E2"
            # A read of the group is what lets a stalled attempt be rescheduled.
            await client.call_tool("validate_entities", {"view": "group", "group_id": "E2"})
            assert ("group", "E2") in work.read_groups
            group = {
                "group_id": "E2",
                "issues": [
                    describe
                    for describe in work.describe_issues(rows)
                    if (describe.get("check_ref") or "").endswith(":E2")
                ],
            }
            signature = blocked_signature(group, items, sources)
            assert signature
            work.deferred_groups["E2"] = signature
            catalog = (await client.call_tool("validate_entities", groups_view)).data["groups"]
            # The same order reaches every view, and the deferred group is last.
            assert [row["group_id"] for row in catalog] == ["E1", "E2"]
            summary = (await client.call_tool("validate_entities", {})).data
            assert summary["current_group"]["group_id"] == "E1"

    asyncio.run(check())


def test_category_fix_never_defers_an_exhausted_budget(sources, tmp_path, monkeypatch, capsys):
    from rsc.extract.runner import BudgetExhausted

    runner, prompts = deferrable_runner(
        sources, tmp_path, monkeypatch, lambda prompt, latest, calls: None, max_repair_rounds=1
    )

    async def exhausted(work, issues=(), defer=None, **kwargs):
        raise BudgetExhausted(f"{work.assignment.id}: request limit", usage={})

    async def check():
        async with runner.toolset:
            monkeypatch.setattr(runner, "execute", exhausted)
            await runner.validate_category(0, "basics")

    asyncio.run(check())
    out = capsys.readouterr().out
    # A budget limit seals the attempt; it is not evidence that the group is stuck.
    assert "deferring" not in out
    rounds = [
        record for record in runner.state["records"] if record.get("phase") == "category"
    ]
    assert [record["status"] for record in rounds] == ["budget_exhausted"]


def basics_batch(sources):
    """A scan/basics workspace on the first batch, plus the additions it needs."""
    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, "scan", "basics"))
    return work, batch_id


def add_channeled_party(work, sources, name="Client"):
    add = TypeAdapter(list[EntityAddition]).validate_python
    work.add_entities(
        add(
            [
                party(sources, name),
                channel(sources, f"{name} channel"),
            ]
        )
    )


def test_complete_batch_checks_and_seals_a_clean_basics_batch(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    add_channeled_party(work, sources)
    # The old layout's two calls: validate, then submit.
    plain, _ = basics_batch(sources)
    add_channeled_party(plain, sources)

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            report = (await client.call_tool("validate_entities", {})).data
            assert report["error_count"] == 0
            assert report["advisory_count"] == 0
            sealed = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert sealed["accepted"] is True
            assert work.completion == {
                "status": "completed",
                "summary": "Wrote basics",
                "unresolved_ids": [],
            }
            # A sealed batch answers the same way to a repeat of the same summary.
            repeated = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert repeated == sealed
        async with Client(make_server(lambda: plain)) as client:
            legacy = (await client.call_tool("complete_batch", {"summary": "Wrote basics"})).data
        # One call instead of two, and the same receipt as the old two-call path.
        assert sealed == legacy


def test_complete_batch_refuses_a_blocked_basics_batch(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    work.add_entities(TypeAdapter(list[EntityAddition]).validate_python([party(sources, "Client")]))
    before = json.loads(dumps(work.data))

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            summary = (await client.call_tool("validate_entities", {})).data
            reply = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote a party", "check_first": True}
                )
            ).data
            assert reply["accepted"] is False
            assert reply["checked"] is True
            # The reply is the summary view of the state that refused the seal.
            assert reply["snapshot"] == summary["snapshot"]
            assert reply["error_count"] == summary["error_count"] == 1
            assert reply["advisory_count"] == summary["advisory_count"] == 0
            assert reply["unresolved_count"] == summary["unresolved_count"] == 0
            assert [row["id"] for row in reply["rows"]] == [row["id"] for row in summary["rows"]]
            assert reply["next_offset"] == summary["next_offset"]
            assert reply["current_group"]["group_id"] == summary["current_group"]["group_id"]
            assert reply["current_group"]["issues"] == summary["current_group"]["issues"]
            assert "did not seal" in reply["message"]
            assert "do not repeat check_first" in reply["message"]
            # Nothing was sealed and no mutation was undone.
            assert work.completion is None
            assert json.loads(dumps(work.data)) == before

    asyncio.run(check())


def test_complete_batch_refuses_an_advisory_only_basics_batch(sources, monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    advisory = [
        {
            **check_row(sources, "E9", "step_without_processing"),
            "advisory": True,
        }
    ]
    work, _ = basics_batch(sources)
    add_channeled_party(work, sources)
    monkeypatch.setattr(Workspace, "coverage_issues", lambda self: [dict(row) for row in advisory])

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            reply = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert reply["accepted"] is False
            assert reply["error_count"] == 0
            assert reply["advisory_count"] == 1
            # Nothing blocks, so the advisory rows stop the seal and must be readable.
            assert [row["type"] for row in reply["advisory"]] == ["step_without_processing"]
            assert "include_advisory" in reply["advisory_hint"]
            assert work.completion is None

    asyncio.run(check())


def test_complete_batch_seals_a_clean_basics_batch_with_open_items(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    add_channeled_party(work, sources)
    work.record_unresolved(
        Unresolved(
            description="The batch does not say which side closes",
            entity_ids=["E1"],
            evidence=[citation(sources)],
            missing_information="which side closes",
        )
    )

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            reply = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert reply["accepted"] is True
            assert work.completion["status"] == "completed_with_unresolved"
            assert work.completion["unresolved_ids"] == ["U1"]

    asyncio.run(check())


def test_complete_batch_does_not_let_an_open_item_explain_a_check_away(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    work.add_entities(TypeAdapter(list[EntityAddition]).validate_python([party(sources, "Client")]))
    check_ref = work.issues()[0]["check_ref"]
    work.record_unresolved(
        Unresolved(
            description="The channel is still to be written",
            entity_ids=["E1"],
            evidence=[citation(sources)],
            check_refs=[check_ref],
        )
    )
    assert [row["check_ref"] for row in work.issues()] == [check_ref]

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            summary = (await client.call_tool("validate_entities", {})).data
            reply = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote a party", "check_first": True}
                )
            ).data
            # An explanation is not a repair: the host still counts the whole registry.
            assert reply["accepted"] is False
            assert reply["error_count"] == summary["error_count"] == 1
            assert reply["unresolved_count"] == summary["unresolved_count"] == 1
            assert work.completion is None

    asyncio.run(check())


def test_complete_batch_validates_after_an_in_flight_write(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            # The write and the checked submission leave in the same parallel turn.
            _, reply = await asyncio.gather(
                client.call_tool("add_entities", {"entities": [party(sources, "Client")]}),
                client.call_tool(
                    "complete_batch", {"summary": "Wrote a party", "check_first": True}
                ),
            )
            assert reply.data["accepted"] is False
            # The row the write introduced is exactly what stopped the seal.
            assert reply.data["error_count"] == 1
            assert {row["check_ref"] for row in reply.data["rows"]} == {
                "party_without_channel:E1"
            }
            assert work.completion is None

    asyncio.run(check())


@pytest.mark.parametrize(
    ("phase", "kind", "layout"),
    [
        ("scan", "steps", True),
        ("scan", "basics", False),
        ("category", "basics", True),
        ("global", None, True),
    ],
)
def test_check_first_is_only_for_basics_of_the_two_pass_layout(sources, phase, kind, layout):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from rsc.extract.tools import make_server

    batch_id = next(iter(sources.batches))
    work = Workspace(sources, "test", empty_registry(), Assignment(batch_id, phase, kind))

    async def check():
        async with Client(make_server(lambda: work, checked_basics=layout)) as client:
            with pytest.raises(ToolError, match="two-pass scan layout"):
                await client.call_tool(
                    "complete_batch", {"summary": "Done", "check_first": True}
                )
            assert work.completion is None

    asyncio.run(check())


def test_complete_batch_rejects_a_bad_summary_before_checking(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    add_channeled_party(work, sources)

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            with pytest.raises(ToolError, match="nonempty summary"):
                await client.call_tool(
                    "complete_batch", {"summary": "  ", "check_first": True}
                )
            with pytest.raises(ToolError, match="at most 2000"):
                await client.call_tool(
                    "complete_batch", {"summary": "x" * 2001, "check_first": True}
                )
            assert work.completion is None

    asyncio.run(check())


def test_complete_batch_keeps_a_sealed_batch_sealed(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    add_channeled_party(work, sources)

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            await client.call_tool(
                "complete_batch", {"summary": "Wrote basics", "check_first": True}
            )
            with pytest.raises(ToolError, match="another completion"):
                await client.call_tool(
                    "complete_batch", {"summary": "Something else", "check_first": True}
                )
            # A later write cannot unseal it either.
            with pytest.raises(ToolError, match="already sealed"):
                await client.call_tool("add_entities", {"entities": [party(sources, "Later")]})
            assert work.completion["summary"] == "Wrote basics"

    asyncio.run(check())


def test_complete_batch_fixes_after_the_checked_reply_and_submits(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)
    work.add_entities(TypeAdapter(list[EntityAddition]).validate_python([party(sources, "Client")]))
    add = TypeAdapter(list[EntityAddition]).validate_python

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            refusal = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote a party", "check_first": True}
                )
            ).data
            assert refusal["accepted"] is False
            row = refusal["rows"][0]
            detail = (
                await client.call_tool(
                    "validate_entities", {"view": "issue", "issue_id": row["id"]}
                )
            ).data
            assert detail["issue"]["suggestion"]
            work.add_entities(add([channel(sources, "Client channel")]))
            sealed = (
                await client.call_tool("complete_batch", {"summary": "Wrote a party"})
            ).data
            assert sealed["accepted"] is True
            assert work.completion["status"] == "completed"
            # The next attempt may also ask for the check again; it seals cleanly.
            other, _ = basics_batch(sources)
            add_channeled_party(other, sources)
        async with Client(make_server(lambda: other, checked_basics=True)) as client:
            again = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert again["accepted"] is True

    asyncio.run(check())


def test_checked_completion_resumes_from_an_old_checkpoint(sources, tmp_path):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.runner import Runner
    from rsc.extract.tools import make_server

    work, batch_id = basics_batch(sources)
    add_channeled_party(work, sources)
    # A checkpoint written before this feature existed: no new keys anywhere.
    saved = json.loads(dumps(work.data))
    assert "check_first" not in json.dumps(saved)
    assert "checked_basics" not in json.dumps(saved)
    resumed = Workspace(sources, "test", saved, Assignment(batch_id, "scan", "basics"))

    async def check():
        async with Client(make_server(lambda: resumed, checked_basics=True)) as client:
            reply = (
                await client.call_tool(
                    "complete_batch", {"summary": "Wrote basics", "check_first": True}
                )
            ).data
            assert reply["accepted"] is True
            assert resumed.completion["status"] == "completed"
            # Nothing new is persisted by the checked path.
            assert json.loads(dumps(resumed.data))["unresolved"] == saved["unresolved"]

    asyncio.run(check())
    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    assert runner.scan_kinds == ("basics", "steps")


def test_only_complete_batch_offers_check_first(sources):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from rsc.extract.tools import make_server

    work, _ = basics_batch(sources)

    async def check():
        async with Client(make_server(lambda: work, checked_basics=True)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            schema = tools["complete_batch"].inputSchema
            assert "check_first" in schema["properties"]
            # Optional, so an old call shape stays valid.
            assert "check_first" not in (schema.get("required") or [])
            for name in ("validate_entities", "add_entities", "complete_fix", "record_gap"):
                assert "check_first" not in tools[name].inputSchema["properties"]

    asyncio.run(check())


def test_only_a_basics_prompt_asks_for_the_checked_submission(sources, tmp_path):
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("fastmcp")
    from rsc.extract.runner import Runner

    runner = Runner(
        tmp_path, sources, "test", ExtractConfig(), fake_model(sources, []), no_review()
    )
    batch_id = next(iter(sources.batches))
    asked = {}
    for kind in ("basics", "steps"):
        work = runner.workspace(Assignment(batch_id, "scan", kind))
        asked[kind] = "check_first=true" in json.loads(runner.prompt(work, []))["rule"]
    assert asked == {"basics": True, "steps": False}
    # The legacy single-kind layouts never see the checked wording.
    legacy = runner.workspace(Assignment(batch_id, "scan", "parties"))
    rule = json.loads(runner.prompt(legacy, []))["rule"]
    assert "check_first" not in rule
