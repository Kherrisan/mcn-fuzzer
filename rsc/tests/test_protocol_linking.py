"""Offline Agent link transactions, complete group coverage and reference preservation."""

import asyncio
import json
from copy import deepcopy

import pytest
from protocol_samples import base, document, interactions
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_protocol_extraction import payload

from rsc.llm.budget import RequestLimits
from rsc.protocol.agent_runtime import EventWriter, RequestCache
from rsc.protocol.context import Corpus
from rsc.protocol.extraction import ExtractionRuntime
from rsc.protocol.history import GapHistory
from rsc.protocol.ids import GlobalIdAllocator
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.linking import Linker, link_interactions, link_objects
from rsc.protocol.merge import canonical, issue, semantic
from rsc.protocol.options import AgentOptions, ContextOptions, LinkOptions
from rsc.protocol.planning import plan_block_lists
from rsc.protocol.proposals import CandidateState, empty_model
from rsc.protocol.registry import NESTED
from rsc.protocol.store import ExtractionStore
from rsc.protocol.validate import object_errors


def merge_output(data):
    """Choose one member name and combine nested fields. Do not emit an ID map."""

    members = [deepcopy(row) for row in data["members"]]
    for row in members:
        row.pop("id", None)
    names = [row.get("name") for row in members if row.get("name")]
    chosen = min(names) if names else None
    chosen_row = next(row for row in members if row.get("name") == chosen) if chosen else members[0]
    merged = deepcopy(chosen_row)
    for field in NESTED:
        if not any(field in row for row in members):
            continue
        grouped = {}
        for member in members:
            for row in member.get(field, []):
                grouped.setdefault(canonical(semantic(row)), deepcopy(row))
        merged[field] = list(grouped.values())
    return {"status": "merged", "merged_object": merged, "unresolved": []}


class Models:
    def __init__(self, groups=None, *, alter=None):
        self.groups = groups or {}
        self.alter = alter
        self.trace = []
        self.active = self.peak = 0

    def __call__(self, stage, phase):
        async def respond(messages, info):
            data = payload(messages)
            self.trace.append((stage, deepcopy(data)))
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.001)
                if stage == "link_partition":
                    rows = data["summaries"]
                    grouped = {}
                    for row in rows:
                        name = row["name"]
                        label = self.groups.get(name, name)
                        grouped.setdefault(label, []).append(name)
                    output = {
                        "groups": [
                            {"member_names": names, "reason": "Hand-annotated concept identity"}
                            for names in grouped.values()
                        ],
                        "unresolved": [],
                    }
                else:
                    output = merge_output(data)
                if self.alter is not None:
                    output = self.alter(stage, data, output)
                return ModelResponse([TextPart(json.dumps(output))])
            finally:
                self.active -= 1

        return FunctionModel(respond, model_name="annotated-link-" + phase)


def setup(factory, *, model=None, doc=None, out=None, cache=None):
    doc = doc or document()
    corpus = Corpus({doc["meta"]["source_id"]: doc})
    if model is None:
        model = empty_model() | {
            key: value for key, value in base(doc).items() if key != "unresolved"
        }
    if out:
        out.mkdir()
    runtime = ExtractionRuntime(
        "test",
        corpus,
        plan_block_lists(corpus, 2),
        factory,
        GlobalIdAllocator("test"),
        EventWriter(out, capture=True),
        {
            stage: RequestLimits(max_input_bytes=262144)
            for stage in ("link_partition", "link_merge")
        },
        out=out,
        cache=cache,
        context_options=ContextOptions(max_tokens=1000000),
        agent_options=AgentOptions(max_output_retries=0),
    )
    return runtime, CandidateState(model=model), KeywordIndex("test", corpus), ExtractionStore()


async def run_link(setup_result, phase="objects", *, workers=2, options=None):
    runtime, state, keywords, store = setup_result
    function = link_objects if phase == "objects" else link_interactions
    return await function(
        runtime,
        state,
        keywords=keywords,
        store=store,
        options=options or LinkOptions(),
        workers=workers,
    )


def test_message_merge_keeps_the_chosen_name_and_does_not_rewrite_other_strings(tmp_path):
    async def run():
        models = Models({"command": "request"})
        setup_result = setup(models, out=tmp_path / "model")
        runtime, state, _, _ = setup_result
        state.model.update(
            {k: v for k, v in interactions(state.model).items() if k != "unresolved"}
        )
        duplicate = deepcopy(state.model["messages"][0])
        duplicate["name"] = "command"
        state.model["messages"].append(duplicate)
        state.model["steps"][0]["message_name"] = "command"
        pending = issue("A definition needs clarification", ["command"])
        state.issues.append(pending)
        state.history.refresh(state.issues, 0)
        state.history.defer([pending], "earlier attempt", attempted=True)
        tracking = state.history.key(pending)
        before = deepcopy(state.model)
        updated, report = await run_link(setup_result)
        assert report["merged_groups"] == 1, report
        assert runtime.coordinator.pending_groups == []
        assert len(updated.model["messages"]) == 2
        assert updated.model["steps"][0]["message_name"] == "command"
        assert updated.model["steps"][0]["name"] == "send request"
        assert not object_errors(updated.model, runtime.corpus.documents)
        merged = updated.registry(runtime.corpus.documents).root("command")
        assert merged["name"] == "command"
        assert updated.issues[0]["object_names"] == ["command"]
        assert set(updated.issues[0]) == {"description", "object_names"}
        assert updated.history.key(updated.issues[0]) == tracking
        assert updated.history.entries[tracking]["attempts"] == 1
        saved = json.loads((runtime.out / "objects-link/input-model.json").read_text())
        assert saved == before
        assert report["before_counts"]["messages"] == 3
        assert report["after_counts"]["messages"] == 2
        assert runtime.events.events["retrieval-contexts.jsonl"]

    asyncio.run(run())


@pytest.mark.parametrize("candidate_name", ["message category", "321 reply"])
def test_message_scope_review_can_exclude_a_singleton(candidate_name, tmp_path):
    async def run():
        def alter(stage, data, output):
            if stage == "link_partition" and data["kind"] == "messages":
                row = data["summaries"][0]
                assert row["name"] == candidate_name
                return {
                    "groups": [],
                    "excluded": [
                        {
                            "name": candidate_name,
                            "index": row["index"],
                            "reason": "No in-scope message with usable associated fields",
                        }
                    ],
                }
            return output

        model = empty_model()
        model["messages"] = [{"name": candidate_name, "associated_fields": []}]
        models = Models(alter=alter)
        setup_result = setup(models, model=model, out=tmp_path / "model")
        state, report = await run_link(setup_result)
        assert state.model["messages"] == []
        assert report["excluded_counts"] == {"messages": 1}
        assert report["excluded_objects"] == 1
        assert report["partition_coverage"]["complete"]
        assert len(models.trace) == 1
        assert models.trace[0][0] == "link_partition"
        exclusions = (tmp_path / "model/objects-link/exclusions.jsonl").read_text().splitlines()
        assert json.loads(exclusions[0])["name"] == candidate_name

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    ["duplicate_partition", "missing_field_name", "wrong_channel_shape", "connectionless_roles"],
)
def test_invalid_link_proposal_keeps_original_complete_group(fault):
    async def run():
        def alter(stage, data, output):
            if (
                fault == "duplicate_partition"
                and stage == "link_partition"
                and data["kind"] == "messages"
            ):
                output["groups"].append(deepcopy(output["groups"][0]))
            elif stage == "link_merge" and fault == "missing_field_name":
                output["merged_object"]["associated_fields"][0].pop("name")
            elif stage == "link_merge" and fault == "wrong_channel_shape":
                output["merged_object"] = {"name": "control", "associated_fields": []}
            elif stage == "link_merge" and fault == "connectionless_roles":
                output["merged_object"]["connection"] = "connectionless"
            return output

        kind = (
            "channels" if fault in {"wrong_channel_shape", "connectionless_roles"} else "messages"
        )
        identity = "control" if kind == "channels" else "request"
        models = Models({"z-copy": identity}, alter=alter)
        setup_result = setup(models)
        _, state, _, _ = setup_result
        duplicate = deepcopy(state.model[kind][0])
        duplicate["name"] = "z-copy"
        state.model[kind].append(duplicate)
        before = deepcopy(state.model)
        updated, report = await run_link(setup_result)
        assert updated.model == before
        assert updated.issues and report["run_failures"] >= 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "correction",
    [
        {
            "endpoint_party_names": ["data-originator", "data-recipient"],
            "initiator_party_name": "data-recipient",
            "acceptor_party_name": "data-originator",
        },
        {"transport": "SCTP"},
        {
            "connection": "connectionless",
            "transport": "UDP",
            "initiator_party_name": None,
            "acceptor_party_name": None,
        },
    ],
)
def test_link_accepts_structurally_valid_channel_corrections(correction):
    """Exercise correction commits; the mock does not establish protocol accuracy."""

    async def run():
        def alter(stage, data, output):
            if stage == "link_merge" and data["kind"] == "channels":
                output["merged_object"].update(deepcopy(correction))
            return output

        models = Models({"z-copy": "control"}, alter=alter)
        setup_result = setup(models)
        runtime, state, _, _ = setup_result
        state.model["channels"].append(
            {
                **deepcopy(state.model["channels"][0]),
                "name": "z-copy",
                "transport": "UDP",
                "endpoint_party_names": ["other-endpoint", "responder"],
                "initiator_party_name": "responder",
                "acceptor_party_name": "other-endpoint",
            }
        )
        updated, report = await run_link(setup_result)
        assert report["merged_groups"] == 1, report
        assert report["run_failures"] == 0 and not updated.issues
        assert len(updated.model["channels"]) == 1
        for field, value in correction.items():
            assert updated.model["channels"][0][field] == value
        assert not object_errors(updated.model, runtime.corpus.documents)

    asyncio.run(run())


def test_link_does_not_reapply_old_party_mapping_to_corrected_channel():
    async def run():
        def alter(stage, data, output):
            if stage == "link_merge" and data["kind"] == "channels":
                output["merged_object"].update(
                    endpoint_party_names=["data-endpoint", "responder"],
                    initiator_party_name="data-endpoint",
                )
            return output

        models = Models({"z-copy": "control"}, alter=alter)
        runtime, state, keywords, store = setup(models)
        state.model["channels"].append({**deepcopy(state.model["channels"][0]), "name": "z-copy"})
        linker = Linker(runtime, state, "objects", keywords, store, LinkOptions(), 1)
        linker.party_names = {"data-endpoint": "control-interpreter"}
        await linker.link_kind("channels")
        updated, report = linker.finish()
        assert report["merged_groups"] == 1, report
        channel = updated.model["channels"][0]
        assert channel["endpoint_party_names"] == ["data-endpoint", "responder"]
        assert channel["initiator_party_name"] == "data-endpoint"

    asyncio.run(run())


def test_step_names_persist_and_distinct_dependency_conditions_remain():
    async def run():
        models = Models({"send request copy": "send request"})
        setup_result = setup(models)
        runtime, state, _, _ = setup_result
        relation = interactions(state.model)
        state.model["steps"] = relation["steps"]
        duplicate = deepcopy(state.model["steps"][0])
        duplicate["id"] = "step-copy"
        duplicate["name"] = "send request copy"
        state.model["steps"].append(duplicate)
        original = deepcopy(relation["step_dependencies"][0])
        original["field_relations"] = []
        other = deepcopy(original)
        other["condition"] = "different condition"
        other["prior_step"] = "send response"
        copy = deepcopy(original)
        copy["prior_step"] = "send request copy"
        state.model["step_dependencies"] = [original, other, copy]
        updated, report = await run_link(setup_result, "interactions")
        assert report["merged_groups"] >= 1, (report, updated.issues)
        names = {row["name"] for row in updated.model["steps"]}
        assert "send response" in names
        assert len(names) == 2
        assert all(row["id"] and row["name"] for row in updated.model["steps"])
        conditions = {row["condition"] for row in updated.model["step_dependencies"]}
        assert "different condition" in conditions
        assert any(not row["field_relations"] for row in updated.model["step_dependencies"])
        assert not object_errors(updated.model, runtime.corpus.documents)
        calls = [data["kind"] for stage, data in models.trace if stage == "link_partition"]
        assert calls == ["steps", "step_dependencies"]

    asyncio.run(run())


def test_cross_page_link_reviews_whole_unions_and_reports_call_limit():
    async def run(limit):
        labels = {"p-3": "p-0", "p-4": "p-0"}
        models = Models(labels)
        setup_result = setup(models)
        runtime, state, keywords, store = setup_result
        template = state.model["parties"][0]
        state.model = empty_model()
        state.model["parties"] = [
            {**deepcopy(template), "name": f"p-{i}", "description": "Role " + "x" * 15000}
            for i in range(6)
        ]
        linker = Linker(
            runtime,
            state,
            "objects",
            keywords,
            store,
            LinkOptions(partition_max_input_bytes=150000, max_partition_calls=limit),
            2,
        )
        entries = [
            {"slot": index, "name": row["name"], "summary": linker.summary("parties", row)}
            for index, row in enumerate(state.model["parties"])
        ]
        result = await linker.partition("parties", entries)
        names = [entries[slot]["name"] for group in result for slot in group]
        assert sorted(names) == [f"p-{i}" for i in range(6)]
        return linker, result, entries, models

    linker, groups, entries, models = asyncio.run(run(64))
    grouped = [[entries[slot]["name"] for slot in group] for group in groups]
    assert ["p-0", "p-3", "p-4"] in [sorted(group) for group in grouped]
    reviews = [data for stage, data in models.trace if stage == "link_partition"]
    assert any(
        {"p-0", "p-3", "p-4"} <= {row["name"] for row in data["summaries"]} for data in reviews
    )
    capped, _, _, _ = asyncio.run(run(1))
    assert not capped.coverage_complete and capped.state.issues
    assert capped.partition_requests == 1


def test_serial_coordinator_merge_order_and_request_cache_are_stable():
    async def run():
        doc = document()
        template = base(doc)["parties"][0]
        model = empty_model()
        model["parties"] = [
            {**deepcopy(template), "name": identity, "description": identity}
            for identity in ("a", "a-copy", "b", "b-copy")
        ]
        cache = RequestCache()
        models = Models({"a-copy": "a", "b-copy": "b"})
        first = setup(models, model=deepcopy(model), doc=doc, cache=cache)
        a, report_a = await run_link(first, workers=1)
        second = setup(models, model=deepcopy(model), doc=doc, cache=cache)
        b, report_b = await run_link(second, workers=4)
        assert a.model == b.model and a.redirects == b.redirects
        assert report_a["merged_groups"] == 2 and report_b["cache_hits"] == 3
        assert report_b["calls"] == report_b["cache_hits"] == 3
        assert len(models.trace) == 3
        uncached = Models({"a-copy": "a", "b-copy": "b"})
        await run_link(setup(uncached, model=deepcopy(model), doc=doc), workers=4)
        assert uncached.peak == 1

    asyncio.run(run())


def test_issue_tracking_follows_description_and_names_not_hidden_ids():
    history = GapHistory()
    first = issue("missing field reference", ["message-old"])
    distinct = issue("missing field reference", ["message-other"])
    history.refresh([first, distinct], 0)
    history.defer([first], "pending source", attempted=True)
    tracking = history.key(first)
    history.remap({"step-old": "step-new"})
    regenerated = issue("missing field reference", ["message-old"])
    renamed = issue("missing field reference", ["message-known"])
    assert history.key(regenerated) == tracking
    assert history.key(renamed) != tracking
    assert history.key(distinct) != tracking
    history.refresh([regenerated, distinct], 1)
    assert len(history.entries) == 2 and history.entries[tracking]["attempts"] == 1
    assert set(regenerated) == {"description", "object_names"}


def test_multiple_documents_keep_duplicate_block_ids_and_connection_direction_variants():
    async def run():
        first, second = document(doc_id="doc-a"), document(doc_id="doc-b")
        assert {b["block_id"] for b in first["blocks"]} & {b["block_id"] for b in second["blocks"]}
        model = empty_model()
        a, b = base(first), base(second)
        model["parties"] = a["parties"] + [{**b["parties"][0], "name": "user"}]
        model["channels"] = a["channels"] + [
            {
                **b["channels"][0],
                "name": "z-control",
                "endpoint_party_names": ["user", "responder"],
                "initiator_party_name": "responder",
                "acceptor_party_name": "user",
            }
        ]

        def preserve_distinct_directions(stage, data, output):
            if stage == "link_merge" and data["kind"] == "channels":
                return {
                    "status": "deferred",
                    "merged_object": None,
                    "unresolved": [
                        issue(
                            "The annotated connection variants have different directions",
                            ["control", "z-control"],
                        )
                    ],
                }
            return output

        models = Models(
            {"user": "requester", "z-control": "control"},
            alter=preserve_distinct_directions,
        )
        runtime, state, _, store = setup(models, model=model, doc=first)
        runtime.corpus = Corpus({"doc-a": first, "doc-b": second})
        runtime.plan = plan_block_lists(runtime.corpus, 2)
        keywords = KeywordIndex("test", runtime.corpus)
        updated, report = await link_objects(
            runtime,
            state,
            keywords=keywords,
            store=store,
            options=LinkOptions(),
            workers=2,
        )
        assert len(updated.model["parties"]) == 2 and len(updated.model["channels"]) == 2
        requester = updated.registry(runtime.corpus.documents).root("requester")
        assert requester["name"] == "requester"
        assert {
            (row["initiator_party_name"], row["acceptor_party_name"])
            for row in updated.model["channels"]
        } == {
            ("requester", "responder"),
            ("responder", "requester"),
        }
        assert report["merged_groups"] == 1 and report["deferred_groups"] == 1
        assert not object_errors(updated.model, runtime.corpus.documents) and updated.issues

    asyncio.run(run())


def test_link_truncation_restart_keeps_whole_group_and_shares_request_limit():
    from dataclasses import replace

    async def run(max_requests):
        inputs = []
        models = Models({"z-copy": "requester"})

        def factory(stage, phase):
            if stage != "link_merge":
                return models(stage, phase)

            def respond(messages, info):
                data = payload(messages)
                inputs.append(data)
                return ModelResponse(
                    [TextPart(json.dumps(merge_output(data)))],
                    finish_reason="length",
                )

            return FunctionModel(respond, model_name="truncated-link")

        setup_result = setup(factory)
        runtime, state, _, _ = setup_result
        runtime.agent_options = replace(runtime.agent_options, max_model_requests=max_requests)
        duplicate = deepcopy(state.model["parties"][0])
        duplicate["name"] = "z-copy"
        state.model["parties"].append(duplicate)
        _, report = await run_link(setup_result, options=LinkOptions(merge_max_input_bytes=131072))
        assert len(inputs) == min(max_requests, 2)
        assert all(
            {row["name"] for row in data["members"]} == {"requester", "z-copy"} for data in inputs
        )
        assert report["deferred_groups"] == 1 and len(state.model["parties"]) == 3

    asyncio.run(run(1))
    asyncio.run(run(2))


def test_diagnostic_id_normalization_preserves_words_and_longer_identifiers():
    from rsc.protocol.merge import rewrite_diagnostic_ids

    diagnostic = "a: missing a; data-a and data and a-long and a:child remain distinct"
    assert rewrite_diagnostic_ids(diagnostic, {"a": "canonical"}) == (
        "canonical: missing canonical; data-a and data and a-long and a:child remain distinct"
    )
    history = GapHistory()
    before = issue("a: missing value in data-a", ["a"])
    history.refresh([before], 0)
    tracking = history.key(before)
    history.remap({"a": "canonical"})
    after = issue("a: missing value in data-a", ["a"])
    assert history.key(after) == tracking


def test_group_byte_limit_applies_only_to_new_input_not_inherited_conversation():
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from rsc.protocol.models import LinkPartitionResponse

    async def run():
        models = Models()
        runtime, state, keywords, store = setup(models)
        linker = Linker(runtime, state, "objects", keywords, store, LinkOptions(), 8)
        runtime.coordinator.messages.append(ModelRequest([UserPromptPart("history " * 2000)]))
        deps = linker.deps(state, "link_partition")
        value = deps.payload(kind="parties", summaries=[{"name": "peer"}])
        result = await runtime.run(
            "link_partition",
            "objects",
            LinkPartitionResponse,
            value,
            run_id="small-group",
            instructions="Partition the supplied names.",
            deps=deps,
            session=runtime.coordinator,
            new_input_max_bytes=1024,
        )
        assert result["status"] == "ok"
        assert result["max_request_bytes"] > 1024
        result = await runtime.run(
            "link_partition",
            "objects",
            LinkPartitionResponse,
            deps.payload(kind="parties", summaries=[{"name": "x" * 2000}]),
            run_id="large-group",
            instructions="Partition the supplied names.",
            deps=deps,
            session=runtime.coordinator,
            new_input_max_bytes=1024,
        )
        assert result["status"] == "failed" and result["end_reason"] == "request_budget"
        assert result["calls"] == 0

    asyncio.run(run())
