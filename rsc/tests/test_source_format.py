"""Source compaction must preserve every byte of text, metadata and source order."""

from copy import deepcopy

import pytest

from rsc.workflow.source_format import decode_source_batch, encode_source_batch
from rsc.workflow.sources import token_count


def test_source_column_round_trip_preserves_optional_fields_and_unicode():
    batch = {
        "batch_id": "b1",
        "doc_id": "rfc-test",
        "section_path": ["7"],
        "segments": [
            {
                "block_id": "a",
                "text": '  λ "quoted"\n\tline\n',
                "start": 0,
                "evidence": {"batch_id": "b1", "block_id": "a"},
            },
            {"block_id": "b", "text": "second", "extra": None},
            {
                "block_id": "c",
                "text": "third",
                "start": 0,
                "evidence": {"batch_id": "b1", "block_id": "c"},
            },
        ],
    }
    before = deepcopy(batch)
    packed = encode_source_batch(batch)
    assert decode_source_batch(packed) == batch == before
    assert len(packed["segments"]["groups"]) == 3
    assert decode_source_batch(batch) == batch
    bad = deepcopy(packed)
    bad["segments"]["groups"][0]["rows"][0].pop()
    with pytest.raises(ValueError):
        decode_source_batch(bad)
    bad = deepcopy(packed)
    bad["segments"]["groups"][0]["columns"][0] = bad["segments"]["groups"][0]["columns"][1]
    with pytest.raises(ValueError, match="duplicate"):
        decode_source_batch(bad)


def test_source_compaction_shrinks_repeated_metadata_without_dropping_content():
    batch = {
        "batch_id": "batch-00001",
        "section_path": ["7"],
        "segments": [
            {
                "block_id": f"blk-{i:06}",
                "start": 0,
                "end": 20,
                "text": "Complete source text.",
                "evidence": {"batch_id": "batch-00001", "block_id": f"blk-{i:06}"},
            }
            for i in range(100)
        ],
    }
    packed = encode_source_batch(batch)
    assert decode_source_batch(packed) == batch
    assert token_count(packed) < token_count(batch)
    batch["segments"][0]["evidence"]["batch_id"] = "wrong"
    with pytest.raises(ValueError, match="evidence"):
        encode_source_batch(batch)
