"""Named source bodies can omit duplicate derived references without changing proof."""

from copy import deepcopy

import pytest
from test_evidence import draft
from test_workflow import call

from rsc.workflow import WorkflowError
from rsc.workflow.source_format import decode_source_batch
from rsc.workflow.sources import token_count
from rsc.workflow.tools import validate_arguments


@pytest.mark.parametrize("format", ["records", "compact"])
def test_cited_sources_keep_all_original_fields_and_exact_legacy_receipts(tmp_path, format):
    w = draft(tmp_path)
    ids = [b["batch_id"] for b in w.data["batches"]]
    before = deepcopy(w.data["batches"])
    cited = call(w, "read_batches", batch_ids=ids, format=format, include_evidence=False)
    legacy = call(w, "read_batches", batch_ids=ids, format=format)
    for short, full, original in zip(cited["batches"], legacy["batches"], before, strict=True):
        assert short["receipt_id"] == full["receipt_id"]
        body = decode_source_batch(short["batch"])
        restored = deepcopy(body)
        for segment in restored["segments"]:
            assert "evidence" not in segment and segment["citation"]
            segment["evidence"] = {"batch_id": body["batch_id"], "block_id": segment["block_id"]}
        assert restored == decode_source_batch(full["batch"])
        for segment in body["segments"]:
            segment.pop("citation")
        assert body == original
    assert cited["tokens"] == token_count(cited) <= legacy["tokens"]
    if format == "records":
        assert cited["tokens"] < legacy["tokens"]
    assert w.data["batches"] == before
    assert not w.data["records"] and not w.data["processed"]
    first = decode_source_batch(cited["batches"][0]["batch"])
    citation = first["segments"][0]["citation"]
    result = call(
        w,
        "apply_changes",
        expected_revision=0,
        operation_id="cited",
        changes=[
            {
                "op": "add",
                "kind": "parties",
                "value": {"name": "client", "description": "sender"},
                "evidence": [citation],
            }
        ],
    )
    record = w.data["records"][result["record_ids"][0]]
    assert record["evidence"] == [
        {"batch_id": first["batch_id"], "block_id": first["segments"][0]["block_id"]}
    ]


def test_omitting_duplicate_evidence_does_not_issue_partial_or_undelivered_receipts(tmp_path):
    w = draft(tmp_path)
    ids = [b["batch_id"] for b in w.data["batches"]]
    with pytest.raises(WorkflowError, match="requires") as raised:
        call(w, "read_batches", batch_ids=ids, max_tokens=1, include_evidence=False)
    assert not w.data["receipts"]
    needed = int(str(raised.value).split("requires ", 1)[1].split(" ", 1)[0])
    page = call(w, "read_batches", batch_ids=ids, max_tokens=needed, include_evidence=False)
    assert page["tokens"] <= needed and page["delivered"] == ids[:1]
    assert page["remaining"] == ids[1:]
    assert set(w.data["receipts"]) == {w._key(ids[0])}
    assert not call(w, "check_gate")["ready"]


@pytest.mark.parametrize("invalid", ["false", 0, None])
def test_evidence_option_is_boolean_and_invalid_values_grant_no_receipt(tmp_path, invalid):
    w = draft(tmp_path)
    args = {"batch_ids": [w.data["batches"][0]["batch_id"]], "include_evidence": invalid}
    with pytest.raises(WorkflowError):
        validate_arguments("read_batches", args)
    with pytest.raises(WorkflowError, match="boolean"):
        call(w, "read_batches", **args)
    assert not w.data["receipts"]
