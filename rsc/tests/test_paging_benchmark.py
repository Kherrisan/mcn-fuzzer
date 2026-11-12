"""Offline name-only JSON and whole-input merge budgets, with generated fixtures."""

import json
from copy import deepcopy

import pytest
import tiktoken
from protocol_samples import base, interactions

from rsc.workflow import Workflow
from rsc.workflow.paging import records_page
from rsc.workflow.sources import token_count
from rsc.workflow.store import dumps, read_json


def checkpoint(evidence, batches=(), *, repetitions=1):
    model = base()
    model.update(interactions(model))
    model["parties"][0]["description"] = 'quoted "λ"\n' + "protocol detail " * repetitions
    return {
        "items": [
            {"kind": kind, "value": value, "evidence": deepcopy(evidence)}
            for kind, values in model.items()
            if kind != "unresolved"
            for value in values
        ],
        "unresolved": [],
        "complete": True,
        "completed_batches": list(batches),
    }


def test_compact_json_preserves_complete_name_only_model_and_evidence():
    payload = checkpoint([{"batch_id": "batch-1", "block_id": "block-1"}])
    compact = dumps(payload)
    assert json.loads(compact) == payload
    assert all(set(row) == {"kind", "value", "evidence"} for row in payload["items"])
    dependencies = [row["value"] for row in payload["items"] if row["kind"] == "step_dependencies"]
    assert dependencies and all("name" not in value and "id" not in value for value in dependencies)
    steps = {row["value"]["name"] for row in payload["items"] if row["kind"] == "steps"}
    assert all(
        value[side] in steps
        for value in dependencies
        for side in ("prior_step", "dependent_step")
    )
    assert not {"id", "record_id", "revision", "schema_version"}.intersection(
        key for row in payload["items"] for key in [*row, *row["value"]]
    )
    encoding = tiktoken.get_encoding("cl100k_base")
    compact_tokens = len(encoding.encode(compact, disallowed_special=()))
    pretty_tokens = len(encoding.encode(json.dumps(payload, ensure_ascii=False, indent=2)))
    # Workflow budgeting deliberately counts spaced JSON, conservatively covering
    # the compact JSON agents read without the removed compact-table codec.
    assert compact_tokens < token_count(payload) < pretty_tokens


@pytest.mark.parametrize("repetitions", [1, 11000], ids=["minimum-output", "input-sized-output"])
@pytest.mark.parametrize("fits", [True, False], ids=["exact-fit", "one-token-short"])
def test_whole_model_merge_budget_counts_all_files_and_output_reserve(tmp_path, repetitions, fits):
    source = tmp_path / "rfc9999.txt"
    source.write_text(
        "RFC 9999\n\n1. Client\n\nThe client initiates connections.\n\n"
        "2. Server\n\nThe server accepts connections.\n"
    )
    workflow = Workflow.create(tmp_path / "run", [str(source)], "Budget", parse_workers=2)
    try:
        token = (workflow.root / "coordinator.token").read_text().strip()
        coordinator = workflow.authenticate(token)
        workflow.call(coordinator, "tick")
        assignments = workflow.store.assignments()
        assert len(assignments) == 2 and all(a["phase"] == "parse" for a in assignments)
        payloads, paths = [], []
        for assignment in assignments:
            batch = next(b for b in workflow.batches if b["batch_id"] == assignment["batches"][0])
            evidence = [
                {"batch_id": batch["batch_id"], "block_id": batch["segments"][0]["block_id"]}
            ]
            payload = checkpoint(evidence, assignment["batches"], repetitions=repetitions)
            path = workflow.root / "work" / assignment["worker"] / "result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dumps(payload))
            payloads.append(payload)
            paths.append(path)
        input_tokens = sum(token_count(read_json(path)) for path in paths)
        assert (input_tokens > 40000) == (repetitions > 1)
        fixed_tokens = workflow.data["merge_fixed_tokens"]
        required = fixed_tokens + input_tokens + max(input_tokens, 40000) + 20000
        context_tokens = (required * 4 + 2) // 3 - (not fits)
        workflow.store.settings_update(merge_context_tokens=context_tokens)

        for assignment, path in zip(assignments, paths, strict=True):
            identity = workflow.authenticate(
                (workflow.root / f"{assignment['worker']}.token").read_text().strip()
            )
            workflow.call(identity, "submit_parse_result", {"path": str(path)})

        report = read_json(workflow.root / "merge-budget.json")
        assert report == {
            "input_tokens": input_tokens,
            "estimated_context_tokens": required,
            "available_context_tokens": required if fits else required - 1,
            "fits": fits,
        }
        for assignment, payload in zip(assignments, payloads, strict=True):
            assert read_json(workflow.root / assignment["output_path"]) == payload
        assert not (workflow.root / "protocol.json").exists()
        merges = [a for a in workflow.store.assignments() if a["phase"] == "merge"]
        if fits:
            assert workflow.data["phase"] == "merge"
            assert len(merges) == 1
            assert merges[0]["inputs"] == [a["output_path"] for a in assignments]
        else:
            assert workflow.data["phase"] == "failed"
            assert not merges
            assert "whole-input merge exceeds context budget" in workflow.data["errors"][0]
        if repetitions > 1:
            first_page = records_page([row for p in payloads for row in p["items"]])
            assert first_page["next_cursor"] is not None
            assert first_page["tokens"] < report["input_tokens"]
    finally:
        workflow.store.close()
