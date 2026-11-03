"""Timing diagnostics do not change request caching or cancellation behavior."""

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rsc.llm.budget import RequestLimits
from rsc.protocol.agent_runtime import EventWriter, RequestCache, run_agent
from rsc.protocol.models import StrictModel


class Result(StrictModel):
    name: str


def test_span_immutable_audit_and_write_costs(tmp_path):
    events = EventWriter(tmp_path, capture=True)
    original = {"nested": {"names": ["FTP"]}}
    events.append("sample.jsonl", original)
    assert original == {"nested": {"names": ["FTP"]}}
    assert events.events["sample.jsonl"][0]["logged_at"].endswith("Z")

    with events.span("prepare", stage="objects") as metrics:
        metrics.update(block_count=3, operation="incorrect", duration_seconds=-1, span_id="bad")
    start, end = events.events["timings.jsonl"]
    assert start["event"] == "start" and end["event"] == "end"
    assert start["span_id"] == end["span_id"] != "bad"
    assert end["operation"] == "prepare" and end["status"] == "ok"
    assert end["duration_seconds"] >= 0 and end["block_count"] == 3
    assert end["elapsed_seconds"] >= start["elapsed_seconds"] >= 0

    summary = events.timing_summary()
    assert summary["operations"]["prepare"] == {
        "count": 1,
        "failures": 0,
        "total_seconds": end["duration_seconds"],
        "max_seconds": end["duration_seconds"],
    }
    for name, metric in summary["audit_writes"].items():
        assert metric["bytes"] == (tmp_path / name).stat().st_size
        assert metric["total_seconds"] >= metric["max_seconds"] > 0
    summary["operations"]["prepare"]["count"] = -1
    assert events.timing_summary()["operations"]["prepare"]["count"] == 1


def test_span_records_failures_without_source_text():
    events = EventWriter()
    with pytest.raises(ValueError, match="private source"):
        with events.span("compile", phase="objects"):
            raise ValueError("private source")
    end = events.events["timings.jsonl"][-1]
    assert end["status"] == "error" and end["error"] == "ValueError"
    assert "private source" not in json.dumps(events.events["timings.jsonl"])
    assert events.timing_summary()["operations"]["compile"]["failures"] == 1


def test_request_cache_timings_and_validation(tmp_path):
    async def run():
        calls = 0

        async def respond(messages, info):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return ModelResponse([TextPart(json.dumps({"name": "FTP"}))])

        events = EventWriter(tmp_path / "logs", capture=True)
        model = FunctionModel(respond, model_name="timing-test")

        async def execute():
            return await run_agent(
                model,
                Result,
                {"source": "FTP"},
                stage="objects",
                phase="objects",
                instructions="Return the protocol name.",
                events=events,
                limits=RequestLimits(max_input_bytes=65536),
                cache=RequestCache(tmp_path / "cache"),
                output_check=lambda output: [],
            )

        first = await execute()
        second = await execute()
        assert first["status"] == second["status"] == "ok"
        assert calls == 1 and second["cache_hits"] == 1
        responses = events.events["model-responses.jsonl"]
        assert responses[0]["provider_latency_ms"] > 0
        assert responses[1]["provider_latency_ms"] == 0
        for record in [first, second, *responses]:
            assert record["preparation_ms"] > 0
            assert record["cache_overhead_ms"] > 0
        for record in [first, second]:
            assert record["validation_ms"] > 0
        assert all(row["logged_at"].endswith("Z") for row in responses)
        operations = events.timing_summary()["operations"]
        assert operations["agent_run"]["count"] == 2
        assert operations["model_request"]["count"] == 2
        assert operations["output_validation"]["count"] == 2
        for row in events.events["timings.jsonl"]:
            assert row["stage"] == row["phase"] == "objects"
            assert row["run_id"]
            if row["operation"] == "model_request":
                assert row["request_index"] == 1

    asyncio.run(run())


def test_cancellation_keeps_run_and_request_timing_ends():
    async def run():
        ready = asyncio.Event()

        async def respond(messages, info):
            ready.set()
            await asyncio.Event().wait()

        events = EventWriter()
        task = asyncio.create_task(
            run_agent(
                FunctionModel(respond),
                Result,
                {},
                stage="interactions",
                phase="interactions",
                instructions="Extract interactions.",
                events=events,
                limits=RequestLimits(max_input_bytes=65536),
            )
        )
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ends = [row for row in events.events["timings.jsonl"] if row["event"] == "end"]
        assert {row["operation"] for row in ends} == {"agent_run", "model_request"}
        assert all(row["status"] == "cancelled" for row in ends)
        assert all(row["error"] == "CancelledError" for row in ends)
        response = events.events["model-responses.jsonl"][0]
        assert response["provider_latency_ms"] > 0
        assert response["cache_overhead_ms"] == 0

    asyncio.run(run())
