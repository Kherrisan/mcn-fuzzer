"""Source partitioning, keyword visibility, and immutable retrieval snapshots."""

from copy import deepcopy

import pytest
from protocol_samples import document

from rsc.protocol.context import Corpus
from rsc.protocol.keywords import KeywordIndex, normalize_keyword
from rsc.protocol.planning import plan_block_lists


def corpus(block_count=8):
    doc = document()
    template = doc["blocks"][0]
    doc["blocks"] = []
    for number in range(block_count):
        block = deepcopy(template)
        block.update(block_id=f"same-{number}", order=number, text=f"PASV port request {number}")
        doc["blocks"].append(block)
    return Corpus({"one": doc, "two": deepcopy(doc)})


def response(focus, names):
    return {"keywords": [{"text": name} for name in names]}


def test_partition_is_complete_stable_and_independent_of_input_dict_order():
    source = corpus()
    plan = plan_block_lists(source, 5)
    assert (
        plan.snapshot()
        == plan_block_lists(
            Corpus(
                dict(
                    reversed(
                        list(source.documents.items()),
                    )
                )
            ),
            5,
        ).snapshot()
    )
    blocks = [block for group in plan.lists for block in group.blocks]
    assert len({block.key for block in blocks}) == 16
    assert [block.sequence for block in blocks] == list(range(16))
    assert len(plan.lists) == 5
    assert all(block.list_id == group.list_id for group in plan.lists for block in group.blocks)
    assert len(plan_block_lists(source, 100).lists) == 16
    assert plan.snapshot()["partition_version"]


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_partition_size_is_rejected(count):
    with pytest.raises(ValueError):
        plan_block_lists(corpus(), count)


def test_keyword_normalization_ranking_and_stable_pagination():
    source = corpus()
    blocks = plan_block_lists(source, 1).lists[0].blocks
    index = KeywordIndex("FTP", source, list_id=blocks[0].list_id)
    index.commit(blocks[0], response(blocks[0], [" PASV ", "pasv", "Data-Port"]))
    index.commit(blocks[1], response(blocks[1], ["PASV"]))
    index.commit(blocks[2], response(blocks[2], ["data-port"]))
    assert normalize_keyword(" DATA-PORT \t mode ") == "data-port mode"
    assert len(index.record(*blocks[0].key)["keywords"]) == 2
    found = index.search(["pasv", "DATA-PORT"], before_sequence=2, top_k=1)
    assert found["items"][0]["block_id"] == blocks[0].block_id
    assert len(found["items"][0]["matched_keywords"]) == 2
    next_page = index.search(
        ["PASV", "data-port"], before_sequence=2, top_k=1, cursor=found["next_cursor"]
    )
    assert next_page["items"][0]["block_id"] == blocks[1].block_id
    assert next_page["next_cursor"] is None
    with pytest.raises(ValueError, match="cursor"):
        index.search(["different"], before_sequence=2, cursor=found["next_cursor"])
    assert index.search(["passive mode"])["status"] == "empty"


def test_private_indexes_frozen_barrier_and_historical_filters():
    source = corpus(2)
    groups = plan_block_lists(source, 2).lists
    first = KeywordIndex("FTP", source, list_id=groups[0].list_id)
    second = KeywordIndex("FTP", source, list_id=groups[1].list_id)
    left, right = groups[0].blocks[0], groups[1].blocks[0]
    first.commit(left, response(left, ["PASV"]))
    second.commit(right, response(right, ["PASV"]))
    assert len(first.search(["PASV"])["items"]) == 1
    frozen = KeywordIndex.merge([second, first])
    assert len(frozen.search(["PASV"])["items"]) == 2
    assert len(frozen.search(["PASV"], before_sequence=right.sequence)["items"]) == 1
    assert len(frozen.search(["PASV"], list_id=groups[1].list_id)["items"]) == 1
    assert (
        frozen.search(["PASV"], list_id=groups[1].list_id, before_sequence=right.sequence)["status"]
        == "empty"
    )
    with pytest.raises(ValueError, match="frozen"):
        frozen.commit(left, response(left, []))
    with pytest.raises(ValueError, match="another extraction list"):
        first.commit(right, response(right, ["PASV"]))
    saved = frozen.snapshot()
    first.commit(groups[0].blocks[1], response(groups[0].blocks[1], ["PASV"]))
    assert frozen.snapshot() == saved


def test_empty_and_failed_keywords_are_distinct_and_never_become_hits():
    source = corpus()
    blocks = plan_block_lists(source, 1).lists[0].blocks
    index = KeywordIndex("FTP", source)
    assert index.commit(blocks[0], response(blocks[0], []))["status"] == "empty"
    assert index.commit(blocks[1], error="model timeout")["status"] == "failed"
    assert not index.search(["PASV"])["items"]
    assert index.record(*blocks[2].key) is None
    with pytest.raises(ValueError, match="already committed"):
        index.commit(blocks[1], response(blocks[1], ["PASV"]))


def extraction_record(focus, stage="objects", *, status="ok"):
    from rsc.protocol.contract import COLLECTIONS

    return {
        "record_id": f"{stage}:{focus.doc_id}:{focus.block_id}",
        "run_id": "test-run",
        "stage": stage,
        **focus.ref(),
        "sequence": focus.sequence,
        "list_id": focus.list_id,
        "status": status,
        "items": {kind: [] for kind in COLLECTIONS},
        "reused_names": [],
        "unresolved": [],
        "error": "test failure" if status == "failed" else None,
    }


def test_artifact_history_is_immutable_and_renders_current_names():
    from rsc.protocol.store import ExtractionStore

    source = corpus()
    blocks = plan_block_lists(source, 1).lists[0].blocks
    original = extraction_record(blocks[0])
    original["items"]["parties"] = [{"name": "old name", "description": "role"}]
    original["reused_names"] = ["old name"]
    store = ExtractionStore()
    store.commit(original)
    original["items"]["parties"][0]["name"] = "mutated"
    rendered = store.visible(
        *blocks[0].key,
        blocks[1],
        "objects",
        lambda _: {"name": "new name", "description": "role"},
    )
    shown = rendered["objects"]["items"]["parties"][0]
    assert shown["name"] == "new name" and "id" not in shown
    assert rendered["objects"]["reused_names"] == ["new name"]
    assert store.snapshot()[0]["items"]["parties"][0]["name"] == "old name"


def test_retrieval_starts_empty_and_sources_do_not_become_an_automatic_window():
    from rsc.protocol.retrieval import RetrievalContext
    from rsc.protocol.store import ExtractionStore

    source = corpus(4)
    focus = plan_block_lists(source, 1).lists[0].blocks[1]
    context = RetrievalContext(source, focus=focus, phase="objects", store=ExtractionStore())
    assert context.snapshot()["reads"] == []
    result = context.add(*focus.key)
    assert result["artifact"]["block"]["text"] == source.blocks[focus.key]["text"]
    assert result["artifact"]["compile_result"] is None
    assert set(context.snapshot()) == {"phase", "focus_block", "revision", "reads"}


def test_artifact_visibility_is_private_until_stage_barrier():
    from rsc.protocol.retrieval import RetrievalContext
    from rsc.protocol.store import ExtractionStore

    source = corpus(4)
    left, right = plan_block_lists(source, 2).lists
    store = ExtractionStore()
    for focus in left.blocks + right.blocks:
        store.commit(extraction_record(focus))
        store.commit(extraction_record(focus, "interactions"))
    focus = right.blocks[1]
    objects = RetrievalContext(source, focus=focus, phase="objects", store=store)
    assert objects.add(*left.blocks[0].key)["artifact"]["compile_result"] is None
    assert objects.add(*focus.key)["artifact"]["compile_result"] is None
    assert set(objects.add(*right.blocks[0].key)["artifact"]["compile_result"]) == {"objects"}
    interactions = RetrievalContext(source, focus=focus, phase="interactions", store=store)
    assert set(interactions.add(*left.blocks[-1].key)["artifact"]["compile_result"]) == {"objects"}
    assert set(interactions.add(*right.blocks[-1].key)["artifact"]["compile_result"]) == {"objects"}
    assert set(interactions.add(*focus.key)["artifact"]["compile_result"]) == {"objects"}
    assert set(interactions.add(*right.blocks[0].key)["artifact"]["compile_result"]) == {
        "objects",
        "interactions",
    }


def test_retrieval_excerpt_continuation_and_snapshot_are_exact():
    from rsc.protocol.retrieval import RetrievalContext

    source = corpus(1)
    key = next(iter(source.blocks))
    source.blocks[key]["text"] = "x" * 2300 + "Ω" * 2300 + "tail"
    original = source.blocks[key]["text"]
    context = RetrievalContext(source)
    first = context.add(*key, full=True)["artifact"]["block"]
    assert first["text"] == original[:2400]
    assert first["text_end"] == first["next_start"] == 2400
    assert first["text_length"] == len(original)
    second = context.add(*key, text_start=first["next_start"])["artifact"]["block"]
    assert first["text"] + second["text"] == original
    assert second["next_start"] is None
    snapshot = context.snapshot()
    snapshot["reads"].clear()
    assert len(context.snapshot()["reads"]) == 2
    with pytest.raises(ValueError, match="Unicode"):
        context.add(*key, text_start=-1)
    with pytest.raises(ValueError, match="Unicode"):
        context.add(*key, text_end=len(original) + 1)
    assert source.blocks[key]["text"] == original


def test_store_fork_preserves_immutable_per_list_history():
    from rsc.protocol.store import ExtractionStore

    source = corpus(2)
    blocks = plan_block_lists(source, 1).lists[0].blocks
    store = ExtractionStore()
    store.commit(extraction_record(blocks[0]))
    child = store.fork()
    child.commit(extraction_record(blocks[1]))
    assert len(store.snapshot()) == 1
    assert len(child.snapshot()) == 2
