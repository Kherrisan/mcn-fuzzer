"""Names, whole-model references, and source evidence replace grouped patches."""

from copy import deepcopy

import pytest
import test_result_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.store import read_json

run = h.run


def test_same_name_inputs_are_allowed_but_one_checkpoint_is_unique(run):
    a = h.active(run)[0]
    payload = h.checkpoint(run, a)
    payload["items"].append(deepcopy(payload["items"][0]))
    with pytest.raises(WorkflowError, match="duplicate kind/name"):
        h.submit(run, a["worker"], payload)
    h.parsed(run)
    page = h.call(run, "merge-1", "query_model", view="index")
    assert len({r["input_file"] for r in page["items"]}) == 2
    assert len(page["items"]) == 2 * len({(r["kind"], r["name"]) for r in page["items"]})


def test_merger_preserves_source_evidence_and_dependency_conditions(run):
    h.parsed(run)
    h.merged(run)
    candidate = run.checkpoint("candidate.json")
    assert all(len(i["evidence"]) == 2 for i in candidate["items"])
    dependencies = [i for i in candidate["items"] if i["kind"] == "step_dependencies"]
    assert dependencies and all("name" not in i["value"] for i in dependencies)
    h.review(run)
    exported = read_json(run.root / "protocol.json")
    assert exported["step_dependencies"] == [i["value"] for i in dependencies]


def test_inconsistent_rename_gets_owner_fix_not_automatic_text_replacement(run):
    h.parsed(run)
    payload = run.checkpoint("parsers/parser-1.json")
    message = next(i for i in payload["items"] if i["kind"] == "messages")
    message["value"]["name"] += " distinct"
    with pytest.raises(WorkflowError, match="candidate validation failed"):
        h.submit(run, "merge-1", payload)
    assert run.data["phase"] == "merge" and run.data["round"] == 0
    assert h.active(run)[0]["worker"] == "merge-1"
    assert not (run.root / "merge.json").exists()


def test_explicit_qualified_field_reference_rename_validates(run):
    h.parsed(run)
    payload = run.checkpoint("parsers/parser-1.json")
    for item in payload["items"]:
        v = item["value"]
        if item["kind"] == "messages":
            v["name"] += " canonical"
        if item["kind"] == "steps" and "message_name" in v:
            v["message_name"] += " canonical"
    h.submit(run, "merge-1", payload)
    assert run.data["phase"] == "review", run.data


def test_local_message_fix_follows_reverse_references_without_full_review(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    payload = h.checkpoint(w)
    first = w.batches[0]
    # Every model definition is supported by the first source range; the second
    # range is unrelated and must not be reread for a local message correction.
    assert len(w.batches) > 1
    h.submit(w, "parser-1", payload)
    h.review(w, [h.finding(w)])
    fixed = w.checkpoint(h.active(w)[0]["candidate_path"])
    message = next(i for i in fixed["items"] if i["kind"] == "messages")
    message["value"]["associated_fields"].append({"name": "new source field"})
    h.submit(w, "parser-1", fixed)
    assert w.data["phase"] == "review", w.data
    assert h.active(w)[0]["batches"] == [first["batch_id"]]


def test_scoped_review_retains_unaffected_accepted_ambiguity(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    payload = h.checkpoint(w)
    payload["unresolved"] = [{"description": "external detail absent", "object_names": []}]
    h.submit(w, "parser-1", payload)
    ambiguity = {
        **h.finding(w, category="external_reference"),
        "description": "external detail absent",
        "batch_ids": [w.batches[-1]["batch_id"]],
    }
    h.review(w, [h.finding(w), ambiguity])
    h.submit(w, "parser-1", w.checkpoint(h.active(w)[0]["candidate_path"]))
    assert h.active(w)[0]["batches"] == [w.batches[0]["batch_id"]]
    h.review(w)
    assert w.data["phase"] == "published", w.data
    result = read_json(w.root / "protocol.json")
    assert not result["meta"]["complete"]
    assert "external detail absent" in result["unresolved"][0]["description"]


def test_partial_overlap_does_not_clear_multi_range_ambiguity(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    h.parsed(w)
    ambiguity = {
        **h.finding(w, category="source_ambiguity", global_=True),
        "description": "ambiguity spans both ranges",
        "batch_ids": [b["batch_id"] for b in w.batches],
    }
    h.review(w, [h.finding(w), ambiguity])
    h.submit(w, "parser-1", w.checkpoint(h.active(w)[0]["candidate_path"]))
    assert h.active(w)[0]["batches"] == [w.batches[0]["batch_id"]]
    h.review(w)
    assert not read_json(w.root / "protocol.json")["meta"]["complete"]
    assert "ambiguity spans both ranges" in w.data["unresolved"][0]["description"]


def test_changed_dependency_reviews_both_field_carrier_sources(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    payload = h.checkpoint(w)
    second = w.batches[-1]
    request = next(i for i in payload["items"] if i["value"].get("name") == "send request")
    request["evidence"] = [
        {"batch_id": second["batch_id"], "block_id": second["segments"][0]["block_id"]}
    ]
    h.submit(w, "parser-1", payload)
    h.review(w, [h.finding(w)])
    fixed = w.checkpoint(h.active(w)[0]["candidate_path"])
    edge = next(i for i in fixed["items"] if i["kind"] == "step_dependencies")
    edge["value"]["field_relations"][0]["prior_field"] = "Session"
    h.submit(w, "parser-1", fixed)
    assert w.data["phase"] == "review", w.data
    assert second["batch_id"] in h.active(w)[0]["batches"]


def test_new_unassociated_unresolved_issue_requires_full_review(tmp_path):
    w = h.create(tmp_path, parse_workers=1)
    h.parsed(w)
    h.review(w, [h.finding(w)])
    fixed = w.checkpoint(h.active(w)[0]["candidate_path"])
    fixed["unresolved"].append(
        {"description": "unknown global source constraint", "object_names": []}
    )
    h.submit(w, "parser-1", fixed)
    assert h.active(w)[0]["batches"] == [b["batch_id"] for b in w.batches]
