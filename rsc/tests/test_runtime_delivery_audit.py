"""A server receipt alone does not prove the outer code-mode result was delivered."""

import json
from copy import deepcopy
from datetime import UTC, datetime

from rsc.workflow.source_format import encode_source_batch
from rsc.workflow.sources import digest
from scripts.archive.benchmark_ftp_runtime import audit_source_deliveries


def fixture_data():
    batch = {"batch_id": "batch-1", "segments": [{"block_id": "block-1", "text": "Full RFC\n λ"}]}
    phases = [
        {"phase": "parse", "round": 0, "dispatch_id": "parse", "started_at": 1, "finished_at": 10},
        {
            "phase": "review",
            "round": 1,
            "dispatch_id": "review",
            "started_at": 11,
            "finished_at": 20,
        },
    ]
    return {
        "task_id": "test",
        "phase": "published",
        "phase_history": phases,
        "batches": [batch],
        "receipts": {"parse:batch-1": "p", "review:batch-1": "r"},
    }


def trace(path, role, stamp, body):
    events = [
        {
            "payload": {
                "type": "custom_tool_call",
                "input": f"tools.mcp__rsc_test_{role}__read_batches()",
            }
        },
        {
            "timestamp": datetime.fromtimestamp(stamp, UTC).isoformat(),
            "payload": {
                "type": "custom_tool_call_output",
                "output": [{"type": "input_text", "text": json.dumps(body, ensure_ascii=False)}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events))
    return path


def test_delivery_audit_requires_full_body_in_correct_role_and_phase(tmp_path):
    data = fixture_data()
    item = {"batch": data["batches"][0], "receipt_id": "p"}
    parser = trace(tmp_path / "parser.jsonl", "parser", 5, {"batches": [item]})
    metadata = {"batch": {"batch_id": "batch-1"}, "receipt_id": "r"}
    reviewer = trace(tmp_path / "reviewer.jsonl", "reviewer", 15, metadata)
    result = audit_source_deliveries(data, [parser, reviewer])
    assert not result["verified"]
    assert result["phases"][0]["verified_batches"] == 1
    assert result["phases"][1]["missing_batches"] == ["batch-1"]
    # A complete but stale-phase output cannot satisfy the review dispatch.
    reviewer = trace(reviewer, "reviewer", 5, {"batches": [{**item, "receipt_id": "r"}]})
    assert not audit_source_deliveries(data, [parser, reviewer])["verified"]
    packed = encode_source_batch(data["batches"][0])
    reviewer = trace(reviewer, "reviewer", 15, {"batches": [{"batch": packed, "receipt_id": "r"}]})
    assert audit_source_deliveries(data, [parser, reviewer])["verified"]
    # Changing a single source character invalidates delivery proof.
    packed["segments"]["groups"][0]["rows"][0][-1] = "Changed source"
    reviewer = trace(reviewer, "reviewer", 15, {"batches": [{"batch": packed, "receipt_id": "r"}]})
    assert not audit_source_deliveries(data, [parser, reviewer])["verified"]


def test_model_delivery_audit_rejects_metadata_stale_actor_and_changed_body(tmp_path):
    data = fixture_data()
    page = {
        "revision": 1,
        "format": "records",
        "delivered_ids": ["r1"],
        "items": [{"record_id": "r1", "value": {"name": "client"}}],
    }
    data["response_log"] = [
        {
            "dispatch_id": "review",
            "tool": "query_model",
            "sha256": digest(page),
            "revision": 1,
            "delivered_ids": ["r1"],
        }
    ]
    parser = trace(
        tmp_path / "parser.jsonl", "parser", 5, {"batch": data["batches"][0], "receipt_id": "p"}
    )
    item = {"batch": data["batches"][0], "receipt_id": "r"}
    reviewer = trace(
        tmp_path / "reviewer.jsonl",
        "reviewer",
        15,
        {"source": item, "model": {"revision": 1, "delivered_ids": ["r1"]}},
    )
    result = audit_source_deliveries(data, [parser, reviewer], include_model=True)
    assert not result["verified"]
    assert result["phases"][1]["verified_batches"] == 1
    assert result["model_pages"]["phases"][0]["missing_pages"][0]["tool"] == "query_model"
    for role, stamp in (("reviewer", 5), ("parser", 15)):
        trace(reviewer, role, stamp, {"source": item, "model": page})
        assert not audit_source_deliveries(data, [parser, reviewer], include_model=True)[
            "model_pages"
        ]["verified"]
    trace(reviewer, "reviewer", 15, {"source": item, "model": page})
    assert audit_source_deliveries(data, [parser, reviewer], include_model=True)["verified"]
    page["items"][0]["value"]["name"] = "changed"
    trace(reviewer, "reviewer", 15, {"source": item, "model": page})
    assert not audit_source_deliveries(data, [parser, reviewer], include_model=True)["verified"]


def test_delivery_audit_validates_citations_against_canonical_source(tmp_path):
    data = fixture_data()
    batch = deepcopy(data["batches"][0])
    batch["segments"][0]["citation"] = "e1"
    parser = trace(tmp_path / "parser.jsonl", "parser", 5, {"batch": batch, "receipt_id": "p"})
    reviewer = trace(
        tmp_path / "reviewer.jsonl",
        "reviewer",
        15,
        {"batch": encode_source_batch(batch), "receipt_id": "r"},
    )
    assert audit_source_deliveries(data, [parser, reviewer])["verified"]
    batch["segments"][0]["citation"] = "forged"
    trace(reviewer, "reviewer", 15, {"batch": batch, "receipt_id": "r"})
    assert not audit_source_deliveries(data, [parser, reviewer])["verified"]


def test_later_review_requires_visible_diff_and_audit_log(tmp_path):
    data = fixture_data()
    data["phase_history"] = [
        {
            "phase": "review",
            "round": 2,
            "dispatch_id": "review2",
            "started_at": 21,
            "finished_at": 30,
        }
    ]
    data["receipts"] = {"review2:batch-1": "r2"}
    model = {"revision": 2, "delivered_ids": ["r1"], "items": [{"name": "client"}]}
    diff = {"revision": 2, "delivered_ids": ["change-1"], "changes": [{"after": "client"}]}
    source = {"batch": data["batches"][0], "receipt_id": "r2"}
    path = trace(
        tmp_path / "reviewer.jsonl",
        "reviewer",
        25,
        {"source": source, "model": model, "diff": diff},
    )
    result = audit_source_deliveries(data, [path], include_model=True)
    assert result["model_pages"]["phases"][0]["missing_tools"] == ["get_changes", "query_model"]
    data["response_log"] = [
        {"dispatch_id": "review2", "tool": tool, "sha256": digest(page)}
        for tool, page in (("query_model", model), ("get_changes", diff))
    ]
    assert audit_source_deliveries(data, [path], include_model=True)["verified"]
    trace(path, "reviewer", 25, {"source": source, "model": model})
    result = audit_source_deliveries(data, [path], include_model=True)
    assert not result["verified"]
    assert result["model_pages"]["phases"][0]["missing_pages"][0]["tool"] == "get_changes"
    # Identical pages may be issued by both tools; retain both required tool entries.
    data["response_log"] = [
        {"dispatch_id": "review2", "tool": tool, "sha256": digest(model)}
        for tool in ("query_model", "get_changes")
    ]
    result = audit_source_deliveries(data, [path], include_model=True)
    assert result["verified"]
    assert result["model_pages"]["phases"][0]["required_pages"] == 2


def test_prefetched_sources_are_current_dispatch_proof_but_early_model_pages_are_not(tmp_path):
    data = fixture_data()
    review = data["phase_history"][1]
    review["source_started_at"] = 2
    data["phase_history"].insert(
        1,
        {
            "phase": "review_prepare",
            "round": 1,
            "dispatch_id": "review",
            "started_at": 2,
            "finished_at": 8,
        },
    )
    model = {"revision": 1, "delivered_ids": ["r1"], "items": [{"name": "client"}]}
    data["response_log"] = [
        {
            "dispatch_id": "review",
            "tool": "query_model",
            "sha256": digest(model),
            "revision": 1,
            "delivered_ids": ["r1"],
        }
    ]
    parser = trace(
        tmp_path / "parser.jsonl", "parser", 5, {"batch": data["batches"][0], "receipt_id": "p"}
    )
    preparation = trace(
        tmp_path / "prep.jsonl", "reviewer", 5, {"batch": data["batches"][0], "receipt_id": "r"}
    )
    early_model = trace(tmp_path / "model.jsonl", "reviewer", 6, model)
    result = audit_source_deliveries(data, [parser, preparation, early_model], include_model=True)
    assert all(not row["missing_batches"] for row in result["phases"])
    assert not result["model_pages"]["verified"]
    frozen_model = trace(early_model, "reviewer", 15, model)
    assert audit_source_deliveries(data, [parser, preparation, frozen_model], include_model=True)[
        "verified"
    ]
    # Another actor/receipt still cannot supply the independent reviewer's reading proof.
    trace(preparation, "parser", 5, {"batch": data["batches"][0], "receipt_id": "r"})
    assert not audit_source_deliveries(
        data, [parser, preparation, frozen_model], include_model=True
    )["verified"]


def test_live_preparation_receipts_are_not_omitted_from_delivery_audit(tmp_path):
    data = fixture_data()
    data["phase"] = "parse"
    data["phase_history"] = []
    data["assignment"] = {
        "phase": "parse",
        "dispatch_id": "parse",
        "round": 0,
        "created_at": 1,
    }
    data["review_preparation"] = {
        "phase": "review_prepare",
        "dispatch_id": "review",
        "round": 1,
        "created_at": 2,
        "prepared": False,
    }
    parser = trace(
        tmp_path / "parser.jsonl", "parser", 5, {"batch": data["batches"][0], "receipt_id": "p"}
    )
    preparation = trace(
        tmp_path / "prep.jsonl",
        "reviewer",
        5,
        {"batch": {"batch_id": "batch-1"}, "receipt_id": "r"},
    )
    report = audit_source_deliveries(data, [parser, preparation])
    assert not report["verified"]
    assert report["phases"][1]["missing_batches"] == ["batch-1"]
    trace(preparation, "reviewer", 5, {"batch": data["batches"][0], "receipt_id": "r"})
    assert audit_source_deliveries(data, [parser, preparation])["verified"]
