"""End-to-end compilation with real PydanticAI loops and no external services."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from protocol_samples import TEXT, base, interaction_response, interactions, object_response
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_protocol_extraction import payload

from rsc import (
    CompileError,
    ContextOptions,
    RepairOptions,
    compile_protocol,
    compile_protocol_async,
)
from rsc.artifacts import read_json, read_jsonl, write_json
from rsc.cli import main
from rsc.config import load_compile_options
from rsc.ingest import IngestOptions, ingest_path
from rsc.llm import MockLLMClient
from rsc.protocol.agent_runtime import RequestCache
from rsc.protocol.compiler import resolve_options
from rsc.protocol.contract import BASE, COLLECTIONS, INTERACTIONS
from rsc.protocol.merge import nested_objects
from rsc.protocol.proposals import apply_id_map
from rsc.protocol.validate import validate_protocol_file


@pytest.fixture
def batch(tmp_path):
    source = tmp_path / "rfc9999.txt"
    body = TEXT.split("1. Interaction", 1)[1]
    source.write_text(
        TEXT.split("1. Interaction", 1)[0] + "1. Interaction\n\n" + " ".join(body.split()) + "\n"
    )
    result = ingest_path(source, IngestOptions(out_dir=tmp_path / "input"))
    assert result.exit_code() == 0
    return tmp_path / "input"


def source_document(batch):
    return read_json(batch / read_json(batch / "batch.json")["documents"][0]["document"])


def test_compile_timing_covers_stages_and_full_blocks(batch, tmp_path):
    out = tmp_path / "timed-model"
    models = Models(source_document(batch))
    report = compile_protocol(batch, "test", out, model_factory=models)
    summary = read_json(out / "timing-summary.json")
    assert summary["preflight_seconds"] >= 0
    for operation in (
        "stage.objects", "stage.objects_link", "stage.interactions", "stage.finalize"
    ):
        assert summary["operations"][operation]["count"] == 1
    assert summary["operations"]["focus"]["count"] > 0
    assert report["timings"]["operations"] == summary["operations"]
    rows = [json.loads(line) for line in (out / "timings.jsonl").read_text().splitlines()]
    blocks = [row for row in rows if row["operation"] == "block" and row["event"] == "end"]
    assert blocks
    for row in blocks:
        assert row["duration_seconds"] >= row["agent_seconds"]
        assert all(row[key] >= 0 for key in (
            "prepare_seconds", "context_seconds", "commit_seconds", "persist_seconds"
        ))
    assert validate_protocol_file(out / "protocol.json") == []


def test_cancelled_compile_keeps_partial_timing_summary(batch, tmp_path):
    async def run():
        ready = asyncio.Event()

        async def respond(messages, info):
            ready.set()
            await asyncio.Event().wait()

        out = tmp_path / "cancelled-model"
        task = asyncio.create_task(compile_protocol_async(
            batch, "test", out,
            model_factory=lambda stage, phase: FunctionModel(
                respond, profile={"context_window": 262144}
            ),
        ))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        summary = read_json(out / "timing-summary.json")
        assert summary["operations"]["stage.objects"]["failures"] == 1
        rows = [json.loads(line) for line in (out / "timings.jsonl").read_text().splitlines()]
        ends = [row for row in rows if row["event"] == "end"]
        assert any(row["operation"] == "stage.objects" and row["status"] == "cancelled"
                   for row in ends)
        assert not (out / "protocol.json").exists()

    asyncio.run(run())


def local_ids(data):
    mapping = {
        row["id"]: "local:" + row["id"]
        for kind in COLLECTIONS
        for root in data.get(kind, [])
        for row in nested_objects(root)
    }
    return apply_id_map(data, mapping)


class Models:
    def __init__(self, doc, *, fail=None, empty=False, missing_role=False):
        self.doc, self.fail, self.empty, self.missing_role = doc, fail, empty, missing_role
        self.factories, self.calls = [], []

    def __call__(self, stage, phase):
        self.factories.append((stage, phase))

        def respond(messages, info):
            data = payload(messages)
            self.calls.append((stage, phase))
            if stage == self.fail or self.fail == "all":
                raise RuntimeError("injected provider failure")
            if stage in ("objects", "interactions"):
                kinds = BASE if stage == "objects" else INTERACTIONS
                output = {kind: [] for kind in kinds} | {"unresolved": []}
                focus = data["focus_block"]
                source = next(b for b in self.doc["blocks"] if b["block_id"] == focus["block_id"])
                if "also called user" in source["text"] and not self.empty:
                    if stage == "objects":
                        output.update(object_response(base(self.doc)))
                        if self.missing_role:
                            output["parties"] = [
                                row for row in output["parties"] if row["name"] != "requester"
                            ]
                            output["unresolved"] = [
                                {
                                    "description": "Requester role is missing",
                                    "object_names": ["requester"],
                                }
                            ]
                    else:
                        output.update(interaction_response(interactions(base(self.doc), self.doc)))
                output["keywords"] = [
                    {"text": word} for word in ("request", "response")
                    if word in source["text"].casefold()
                ]
            elif stage == "link_partition":
                output = {
                    "groups": [
                        {"member_names": [row["name"]], "reason": "Distinct annotation"}
                        for row in data["summaries"]
                    ],
                    "unresolved": [],
                }
            elif stage == "repair":
                output = {
                    kind: [] for kind in (BASE if phase == "objects" else COLLECTIONS)
                }
                output["unresolved"] = []
            else:
                raise AssertionError(f"unexpected merge {data}")
            return ModelResponse([TextPart(json.dumps(output))])

        return FunctionModel(
            respond, model_name="compiler-" + stage + "-" + phase,
            profile={"context_window": 262144},
        )


def test_all_stages_cache_and_protocol_validation(batch, tmp_path, capsys):
    models = Models(source_document(batch))
    cache = RequestCache(tmp_path / "cache")
    first = compile_protocol(batch, "TEST/1", tmp_path / "a", model_factory=models, cache=cache)
    assert first["complete"] and first["model_written"], first.get("unresolved")
    assert set(first["stages"]) == {
        "objects",
        "objects_link",
        "objects_repair",
        "interactions",
        "interactions_link",
        "interactions_repair",
    }
    assert first["counts"]["steps"] == 2 and first["counts"]["step_dependencies"] == 4
    assert validate_protocol_file(tmp_path / "a/protocol.json") == []
    calls = len(models.calls)
    second = compile_protocol(
        batch,
        "TEST/1",
        tmp_path / "b",
        model_factory=models,
        cache=cache,
        workers=1,
        objects_workers=4,
        interactions_workers=2,
    )
    assert second["complete"] and second["cache_hits"] == second["calls"]
    assert len(models.calls) == calls
    assert read_json(tmp_path / "a/protocol.json") == read_json(tmp_path / "b/protocol.json")
    assert main(["validate", "protocol", str(tmp_path / "a/protocol.json")]) == 0
    assert main(["stats", str(tmp_path / "a"), "--validate", "--json"]) == 0
    capsys.readouterr()
    assert not list((tmp_path / "a").rglob("tasks.jsonl"))


def test_compile_records_shared_coordinator_and_list_stage_handoffs(batch, tmp_path):
    out = tmp_path / "sessions"
    report = compile_protocol(batch, "TEST", out, model_factory=Models(source_document(batch)))
    assert report["complete"]
    audit = read_jsonl(out / "session-messages.jsonl")
    coordinator = [row for row in audit if row["session_id"] == "coordinator"]
    assert [row["phase"] for row in coordinator if row["event"] == "handoff"] == [
        "objects_link", "objects_complete", "interactions_link", "interactions_complete"
    ]
    starts = [row for row in audit if row["event"] == "turn_start"]
    assert all(row["session_id"] == "coordinator" for row in starts
               if row["stage"] not in {"objects", "interactions"})
    list_ids = {row["session_id"] for row in starts if row["stage"] == "objects"}
    assert list_ids == {row["session_id"] for row in starts if row["stage"] == "interactions"}
    for session_id in list_ids:
        rows = [row for row in audit if row["session_id"] == session_id]
        handoffs = [row for row in rows if row["event"] == "handoff"]
        assert len(handoffs) == 1 and handoffs[0]["phase"] == "interactions"
        first = next(row for row in rows if row["event"] == "turn_end")
        assert handoffs[0]["model_revision"] > first["model_revision"]
    assert not any((out / name).exists() for name in (
        "windows.jsonl", "link-contexts.jsonl", "repair-contexts.jsonl"
    ))


@pytest.mark.parametrize("defect", ["missing_batch", "failed_entry", "checksum", "overlap"])
def test_input_preflight_precedes_model_factory(batch, tmp_path, defect):
    factory = Mock(side_effect=AssertionError("factory must not run"))
    out = tmp_path / "out"
    if defect == "missing_batch":
        (batch / "batch.json").unlink()
    elif defect == "overlap":
        out = batch / "model"
    else:
        data = read_json(batch / "batch.json")
        data["documents"][0]["status" if defect == "failed_entry" else "input_sha256"] = (
            "failed" if defect == "failed_entry" else "0" * 64
        )
        write_json(batch / "batch.json", data)
    with pytest.raises(CompileError):
        compile_protocol(batch, "TEST", out, model_factory=factory)
    factory.assert_not_called()


def test_unknown_model_capacity_fails_before_any_request(batch, tmp_path):
    calls = []

    def respond(messages, info):
        calls.append(messages)
        raise AssertionError("capacity preflight must precede model calls")

    with pytest.raises(CompileError, match="context"):
        compile_protocol(
            batch, "TEST", tmp_path / "unknown-capacity",
            model_factory=lambda stage, phase: FunctionModel(
                respond, profile={"context_window": None}
            ),
        )
    assert calls == []


def test_removed_window_config_has_a_migration_error(tmp_path):
    config = tmp_path / "old.json"
    write_json(config, {"compile": {"window": {"context_blocks_before": 3}}})
    with pytest.raises(ValueError, match=r"compile\.window.*compile\.context"):
        load_compile_options(config)


@pytest.mark.parametrize(
    "context", [{"max_tokens": 0}, {"compact_at": 0.5, "compact_to": 0.5},
                {"compact_at": float("nan")}, {"compact_to": True}]
)
def test_invalid_context_options_precede_factory(batch, tmp_path, context):
    factory = Mock()
    with pytest.raises(CompileError, match="context"):
        compile_protocol(
            batch, "TEST", tmp_path / "bad-context", model_factory=factory,
            context_options=context,
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "option",
    [
        {"workers": 0},
        {"objects_workers": True},
        {"interactions_workers": -1},
        {"block_lists": 0},
        {"context_blocks_before": -1},
        {"repair_options": {"objects_max_rounds": 0}},
        {"repair_options": {"max_objects_per_task": 4}},
        {"agent_options": {"max_model_requests": 0}},
    ],
)
def test_invalid_options_precede_factory(batch, tmp_path, option):
    factory = Mock()
    with pytest.raises(CompileError):
        compile_protocol(batch, "TEST", tmp_path / "model", model_factory=factory, **option)
    factory.assert_not_called()


def test_disabled_completion_and_force_remove_managed_artifacts(batch, tmp_path):
    out = tmp_path / "model"
    models = Models(source_document(batch))
    complete = compile_protocol(
        batch, "TEST", out, model_factory=models, repair_options=RepairOptions(enabled=False)
    )
    assert complete["complete"], complete
    assert not any(stage == "repair" for stage, _ in models.factories)
    for stage in ("objects_repair", "interactions_repair"):
        assert complete["stages"][stage]["stop_reason"] == "disabled"
        assert complete["stages"][stage]["agent_runs"] == 0
    assert not list(out.rglob("round-*"))
    assert (out / "repair.jsonl").read_text() == ""
    assert (out / "objects-repair/inventory.json").is_file()
    (out / "notes.txt").write_text("user notes")
    for name in ("windows.jsonl", "link-contexts.jsonl", "repair-contexts.jsonl"):
        (out / name).write_text("old window audit")
    obsolete = out / "objects-repair/round-003"
    obsolete.mkdir()
    (obsolete / "tasks.jsonl").write_text("old task records")
    with pytest.raises(CompileError, match="force"):
        compile_protocol(batch, "TEST", out, model_factory=Mock())
    failures = Models(source_document(batch), fail="all")
    failed = compile_protocol(
        batch, "TEST", out, model_factory=failures, force=True, repair_options={"enabled": False}
    )
    assert failed["exit_code"] == 1 and not failed["model_written"]
    assert not (out / "protocol.json").exists() and not obsolete.exists()
    assert (out / "notes.txt").read_text() == "user notes"
    assert not any((out / name).exists() for name in (
        "windows.jsonl", "link-contexts.jsonl", "repair-contexts.jsonl"
    ))
    assert not any(stage == "repair" for stage, _ in failures.factories)


@pytest.mark.parametrize("variant", ["provider_failure", "missing_role", "empty"])
def test_partial_or_empty_outputs_never_claim_complete(batch, tmp_path, variant):
    models = Models(
        source_document(batch),
        fail="interactions" if variant == "provider_failure" else None,
        missing_role=variant == "missing_role",
        empty=variant == "empty",
    )
    out = tmp_path / "model"
    report = compile_protocol(
        batch, "TEST", out, model_factory=models, repair_options={"enabled": False}
    )
    assert report["exit_code"] == 1 and report["unresolved"]
    assert report["model_written"] is (variant != "empty")
    if report["model_written"]:
        assert validate_protocol_file(out / "protocol.json") == []
        model = read_json(out / "protocol.json")
        assert all(set(row) == {"description", "object_names"} for row in model["unresolved"])


def test_text_only_adapter_rejected_before_requests_and_async_entrypoint(batch, tmp_path):
    client = MockLLMClient([])
    with pytest.raises(CompileError, match="tool-capable"):
        compile_protocol(batch, "TEST", tmp_path / "bad", client_factory=lambda _: client)
    assert not (tmp_path / "bad").exists()

    async def run():
        models = Models(source_document(batch))
        with pytest.raises(CompileError, match="compile_protocol_async"):
            compile_protocol(batch, "TEST", tmp_path / "nested", model_factory=models)
        return await compile_protocol_async(batch, "TEST", tmp_path / "async", model_factory=models)

    assert asyncio.run(run())["complete"]


def test_config_and_cli_precedence_and_disabled_flag(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.json"
    write_json(
        config,
        {
            "compile": {
                "workers": 2,
                "objects_workers": 3,
                "block_lists": 5,
                "repair": {"objects_max_rounds": 2},
                "context": {"max_tokens": 131072, "compact_at": 0.75},
            }
        },
    )
    options = load_compile_options(config)
    assert options.workers == 2 and options.repair.objects_max_rounds == 2
    resolved = resolve_options(config, workers=4, interactions_workers=7)
    assert (resolved.workers, resolved.objects_workers, resolved.interactions_workers) == (4, 3, 7)
    observed = {}

    def compile_stub(*args, **kwargs):
        observed.update(kwargs)
        return {
            "complete": True,
            "exit_code": 0,
            "path": "model",
            "calls": 0,
            "failures": 0,
            "unresolved": 0,
            "model_written": True,
        }

    monkeypatch.setattr("rsc.cli.pipeline.compile_protocol", compile_stub)
    assert (
        main(
            [
                "compile",
                "input",
                "--protocol",
                "TEST",
                "--out",
                "model",
                "--config",
                str(config),
                "--workers",
                "1",
                "--objects-workers",
                "6",
                "--context-limit-tokens",
                "262144",
                "--context-compact-to",
                "0.4",
                "--no-complete",
                "--objects-repair-max-rounds",
                "3",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["complete"]
    assert (observed["workers"], observed["objects_workers"], observed["interactions_workers"]) == (
        1,
        6,
        1,
    )
    assert observed["block_lists"] == 5
    assert (
        not observed["repair_options"].enabled
        and observed["repair_options"].objects_max_rounds == 3
    )
    assert observed["context_options"] == ContextOptions(
        max_tokens=262144, compact_at=0.75, compact_to=0.4
    )
    for command in ("recall", "complete", "normalize"):
        with pytest.raises(SystemExit):
            main([command, "input"])


def test_modified_source_snapshot_is_rejected(batch, tmp_path):
    compile_protocol(
        batch, "TEST", tmp_path / "model", model_factory=Models(source_document(batch))
    )
    model_path = tmp_path / "model/protocol.json"
    model = read_json(model_path)
    source_path = model_path.parent / model["meta"]["documents"][0]["path"]
    document = read_json(source_path)
    document["blocks"][0]["text"] += "changed"
    write_json(source_path, document)
    assert "checksum" in str(validate_protocol_file(model_path))


def test_request_budget_skips_all_models_and_offline_issue_preview(batch, tmp_path):
    from scripts.replay_repair import replay

    models = Models(source_document(batch))
    limited = compile_protocol(
        batch,
        "TEST",
        tmp_path / "limited",
        model_factory=models,
        repair_options={"enabled": False},
        request_limits={"default": {"max_input_bytes": 1}},
    )
    assert not models.calls and limited["calls"] == 0 and limited["skipped"] > 0
    assert not limited["model_written"]
    pending = Models(source_document(batch), missing_role=True)
    source = tmp_path / "pending"
    compile_protocol(
        batch, "TEST", source, model_factory=pending, repair_options={"enabled": False}
    )
    before = (source / "objects-link/model.json").read_bytes()
    report = replay(source, tmp_path / "preview")
    assert report["network_calls"] == 0 and report["phases"]["objects"]["issues"] > 0
    assert (tmp_path / "preview/objects/worklist.json").is_file()
    assert not list((tmp_path / "preview").rglob("tasks.jsonl"))
    assert (source / "objects-link/model.json").read_bytes() == before
    with pytest.raises(ValueError, match="empty"):
        replay(source, tmp_path / "preview")


def test_consistency_collision_blocks_publication_and_preserves_actual_call_counts(
    batch, tmp_path, monkeypatch
):
    from rsc.protocol.ids import GlobalIdAllocator

    monkeypatch.setattr(
        "rsc.protocol.compiler.GlobalIdAllocator",
        lambda protocol: GlobalIdAllocator(protocol, id_factory=lambda key: "collision"),
    )
    models = Models(source_document(batch))
    out = tmp_path / "collision"
    report = compile_protocol(batch, "TEST", out, model_factory=models)
    assert report["exit_code"] == 1 and not report["model_written"]
    assert "IdConsistencyError" in report["fatal_error"]
    assert not (out / "protocol.json").exists()
    assert report["calls"] == len(models.calls) > 0
    assert report["agent_runs"] > 0


def test_report_request_counts_match_audit_records(batch, tmp_path):
    from rsc.artifacts import read_jsonl

    out = tmp_path / "report"
    result = compile_protocol(batch, "TEST", out, model_factory=Models(source_document(batch)))
    requests = read_jsonl(out / "model-requests.jsonl")
    responses = read_jsonl(out / "model-responses.jsonl")
    assert result["versions"]["packages"]["pydantic-ai-slim"]
    assert result["versions"]["prompt_contract"]
    assert {row["stage"] for row in result["models"]} == {
        "objects",
        "interactions",
        "link_partition",
        "link_merge",
        "repair",
    }
    assert result["calls"] == sum(row["called"] for row in requests)
    assert result["skipped"] == sum(not row["called"] for row in requests)
    assert result["failures"] == sum(row["error"] is not None for row in responses)
    assert result["stages"]["objects"]["max_request_bytes"] > 0
    assert result["stages"]["objects_link"]["partition_coverage"]["complete"]
    assert result["stages"]["objects_repair"]["stop_reason"] == "no_missing"
    assert result["stages"]["interactions_repair"]["remaining_missing"] == 0
    assert result["context_snapshots"] == len(read_jsonl(out / "retrieval-contexts.jsonl"))


def test_final_issue_can_reference_a_surviving_nested_field(batch, tmp_path):
    from rsc.protocol.merge import issue

    out = tmp_path / "nested"
    compile_protocol(batch, "TEST", out, model_factory=Models(source_document(batch)))
    path = out / "protocol.json"
    model = read_json(path)
    field = model["messages"][0]["associated_fields"][0]
    model["unresolved"] = [issue("Check field meaning", [field["name"]])]
    model["meta"]["complete"] = False
    write_json(path, model)
    assert not validate_protocol_file(path)
    model["unresolved"][0]["object_names"] = ["not-defined"]
    write_json(path, model)
    assert not validate_protocol_file(path)
    model["unresolved"][0]["object_names"] = "not-a-list"
    write_json(path, model)
    assert validate_protocol_file(path)
