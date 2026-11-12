"""Source ownership and optimal locality on independently enumerated partitions."""

import json
from copy import deepcopy
from itertools import combinations, pairwise
from random import Random
from types import SimpleNamespace

import pytest

from rsc.workflow.partition import partition_batches, plan_parsers, source_capacity, source_weight
from rsc.workflow.sources import build_batches, token_count, validate_batches


def batch(index, size, chapter="1", doc="rfc"):
    return {
        "batch_id": f"batch-{index:05d}",
        "doc_id": doc,
        "section_path": [chapter],
        "segments": [{"text": "word " * size, "section_path": [chapter]}],
    }


def brute_force(batches, workers):
    """Enumerate all cuts, independent of the production dynamic program."""
    n = len(batches)
    workers = min(n, workers)
    weights = [source_weight(b) for b in batches]
    options = []
    for cuts in combinations(range(1, n), workers - 1):
        bounds = (0, *cuts, n)
        maximum = max(sum(weights[a:b]) for a, b in pairwise(bounds))
        docs = sum(batches[c - 1]["doc_id"] == batches[c]["doc_id"] for c in cuts)
        chapters = sum(
            batches[c - 1]["doc_id"] == batches[c]["doc_id"]
            and batches[c - 1]["section_path"] == batches[c]["section_path"]
            for c in cuts
        )
        options.append((maximum, chapters, docs, cuts))
    minimum = min(o[0] for o in options)
    chosen = min(o[1:] for o in options if o[0] * 10 <= minimum * 11)
    bounds = (0, *chosen[2], n)
    return [[b["batch_id"] for b in batches[a:z]] for a, z in pairwise(bounds)]


def test_partition_matches_exhaustive_optimum_and_preserves_inputs():
    rng = Random(42)
    for n in range(1, 9):
        for workers in range(1, 5):
            batches = [batch(i, rng.randint(1, 40), str(i // 2), str(i // 5)) for i in range(n)]
            before = deepcopy(batches)
            actual = partition_batches(batches, workers)
            assert actual == brute_force(batches, workers)
            assert [name for group in actual for name in group] == [b["batch_id"] for b in batches]
            assert all(actual)
            assert batches == before


def test_locality_can_trade_small_load_difference_for_a_whole_chapter():
    batches = [batch(1, 9, "1"), batch(2, 9, "2"), batch(3, 1, "2")]
    assert partition_batches(batches, 2) == [["batch-00001"], ["batch-00002", "batch-00003"]]


def test_large_batch_stays_whole_even_when_load_is_unbalanced():
    batches = [batch(1, 2), batch(2, 200), batch(3, 2)]
    assert partition_batches(batches, 3) == [[b["batch_id"]] for b in batches]


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_invalid_workers_rejected(workers):
    with pytest.raises(ValueError, match="positive integer"):
        partition_batches([batch(1, 2)], workers)


def test_empty_and_duplicate_batches_rejected():
    with pytest.raises(ValueError, match="no source batches"):
        partition_batches([], 3)
    with pytest.raises(ValueError, match="duplicate"):
        partition_batches([batch(1, 1), batch(1, 2)], 2)


def document(texts):
    return {
        "rfc": {
            "blocks": [
                {"block_id": f"b{i}", "order": i, "section_path": path, "text": text}
                for i, (path, text) in enumerate(texts)
            ]
        }
    }


def test_chapter_that_fits_maximum_is_not_cut_at_target():
    docs = document([(["1", "1"], "first " * 100), (["1", "2"], "second " * 100)])
    batches = build_batches(docs, 128, 600)
    assert len(batches) == 1
    assert token_count(batches[0]) > 128
    validate_batches(docs, batches, 600)


def test_large_chapter_splits_at_subsections_before_paragraphs():
    docs = document(
        [
            (["1", "1"], "first " * 130),
            (["1", "1"], "second " * 130),
            (["1", "2"], "third " * 130),
            (["1", "2"], "fourth " * 130),
        ]
    )
    batches = build_batches(docs, 128, 450)
    assert [b["section_path"] for b in batches] == [["1", "1"], ["1", "2"]]
    assert [len(b["segments"]) for b in batches] == [2, 2]
    validate_batches(docs, batches, 450)


def test_long_unicode_paragraph_has_exact_coverage_and_bounded_delivery():
    docs = document([(["1"], "你好🌍e\u0301\n" * 300)])
    batches = build_batches(docs, 128, 256)
    assert len(batches) > 1
    validate_batches(docs, batches, 256)
    assert (
        "".join(s["text"] for b in batches for s in b["segments"])
        == docs["rfc"]["blocks"][0]["text"]
    )


def test_empty_document_does_not_create_assignments():
    with pytest.raises(ValueError, match="no source text"):
        build_batches(document([(["1"], " \n ")]))


def test_automatic_partitions_respect_capacity_and_queue_separate_contexts():
    batches = [batch(i, 29, str(i // 2)) for i in range(10)]
    result = plan_parsers(batches, parse_context_tokens=160, parse_fixed_tokens=0,
                          parse_max_workers=2)
    assert result["capacity_tokens"] == 60
    assert len(result["partitions"]) == 5
    assert result["parallel_agents"] == 2
    assert all(p["source_tokens"] <= 60 for p in result["partitions"])
    assert [b for p in result["partitions"] for b in p["batch_ids"]] == [
        b["batch_id"] for b in batches]


def test_manual_count_is_honored_with_context_warning():
    result = plan_parsers([batch(1, 50), batch(2, 50)], parse_workers=1,
                          parse_context_tokens=160, parse_fixed_tokens=0)
    assert len(result["partitions"]) == result["parallel_agents"] == 1
    assert result["warnings"]


def test_capacity_cannot_be_relaxed_for_chapter_locality():
    batches = [batch(1, 9, "1"), batch(2, 9, "2"), batch(3, 1, "2")]
    with pytest.raises(ValueError, match="capacity"):
        partition_batches(batches, 1, capacity=15)
    assert all(sum(source_weight(b) for b in batches if b["batch_id"] in group) <= 15
               for group in partition_batches(batches, 2, capacity=15))


def test_oversized_source_batch_needs_preprocessing_split():
    with pytest.raises(ValueError, match="split"):
        plan_parsers([batch(1, 100)], parse_context_tokens=160, parse_fixed_tokens=0)


def test_context_budget_and_default_single_partition():
    assert source_capacity() == 74400
    assert plan_parsers([batch(1, 100)])["parallel_agents"] == 1
    with pytest.raises(ValueError, match="overhead"):
        source_capacity(100, 80)


def test_prepare_splits_to_capacity_and_restores_searchable_source(tmp_path, monkeypatch):
    from rsc.workflow import sources

    doc = document([(["chapter"], "source words " * 400)])["rfc"]
    original = tmp_path / "input.json"
    original.write_text(json.dumps(doc))
    item = SimpleNamespace(status="ok", document_path=original, doc_id="RFC959")
    monkeypatch.setattr(sources, "ingest_path", lambda *args: SimpleNamespace(documents=[item]))
    root = tmp_path / "task"
    for _ in range(2):
        documents, batches = sources.prepare(root, [str(original)], 8000, 12000,
                                            capacity_tokens=256)
        validate_batches(documents, batches, 256)
        assert all(source_weight(b) <= 256 for b in batches)
        text = root / "sources/text/source-001.txt"
        assert "[RFC959/b0] chapter" in text.read_text()
        assert doc["blocks"][0]["text"] in text.read_text()
        assert text.stat().st_mode & 0o222 == 0
