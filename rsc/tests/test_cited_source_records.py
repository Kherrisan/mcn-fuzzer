"""Single and bulk reads deliver identical full sources and scoped receipts."""

import re
from copy import deepcopy

import pytest
import test_parallel_workflow as h
from test_evidence import draft

from rsc.workflow import WorkflowError
from rsc.workflow.sources import token_count


def test_single_and_bulk_sources_preserve_exact_text_and_receipts(tmp_path):
    w = draft(tmp_path)
    ids = [b["batch_id"] for b in w.batches]
    before = deepcopy(w.batches)
    bulk = h.call(w, "parser-1", "read_batches", batch_ids=ids)
    assert bulk["tokens"] == token_count(bulk) <= bulk["max_tokens"]
    for delivered, original in zip(bulk["batches"], before, strict=True):
        single = h.call(w, "parser-1", "read_batches", batch_ids=[original["batch_id"]])
        assert single["batches"][0] == delivered
        body = deepcopy(delivered["batch"])
        for segment in body["segments"]:
            assert segment.pop("citation")
        assert body == original
    assert w.batches == before
    assert not w.store.draft("parse-0/parser-1")[1]
    assert not w.store.completed("parse-0/parser-1")
    w.db.close()


def test_undelivered_batches_never_receive_receipts(tmp_path):
    w = draft(tmp_path)
    ids = [b["batch_id"] for b in w.batches]
    with pytest.raises(WorkflowError, match="requires") as raised:
        h.call(w, "parser-1", "read_batches", batch_ids=ids, max_tokens=1)
    assert not w.db.execute("SELECT 1 FROM receipts").fetchone()
    needed = int(re.search(r"requires (?:at least )?(\d+)", str(raised.value))[1])
    page = h.call(w, "parser-1", "read_batches", batch_ids=ids, max_tokens=needed)
    assert page["tokens"] <= needed and page["delivered_ids"] == ids[:1]
    assert page["remaining"] == ids[1:]
    assert [row[0] for row in w.db.execute("SELECT batch FROM receipts")] == ids[:1]
    with pytest.raises(WorkflowError, match="incomplete"):
        h.call(w, "parser-1", "advance_phase")
    w.db.close()


@pytest.mark.parametrize("extra", [{"format": "compact"}, {"include_evidence": False}])
def test_removed_wire_options_grant_no_receipt(tmp_path, extra):
    w = draft(tmp_path)
    with pytest.raises(WorkflowError, match="Additional properties"):
        h.call(w, "parser-1", "read_batches", batch_ids=[w.batches[0]["batch_id"]], **extra)
    assert not w.db.execute("SELECT 1 FROM receipts").fetchone()
    w.db.close()
