"""Names, whole-model references, and source evidence replace grouped patches."""

from copy import deepcopy

import pytest
import test_result_workflow as h

from rsc.protocol.validate import validate_protocol_file
from rsc.workflow import WorkflowError
from rsc.workflow.store import read_json

run = h.run


def test_processing_cross_partition_rename_and_publication(run):
    assignments = h.active(run)
    first, second = (h.checkpoint(run, a) for a in assignments)
    receive, send = [item for item in first["items"] if item["kind"] == "steps"]
    receive["value"]["processing"] = {
        "on_receive": [
            {
                "reads": ["message.Session", "message_bytes"],
                "writes": ["state.session"],
                "rule": "Store the request's session for use in the response.",
            }
        ],
        "after_send": [],
    }
    send["value"]["processing"] = {
        "before_send": [
            {
                "reads": ["state.session"],
                "writes": ["message.Session"],
                "rule": "Echo the stored session.",
            }
        ],
    }
    first["items"] = [receive, send]
    for party, source in (("responder", "processing"), ("requester", "external")):
        second["items"].append(
            {
                "kind": "states",
                "value": {"name": "session", "party_name": party, "source": source},
                "evidence": second["items"][0]["evidence"],
            }
        )
    h.submit(run, assignments[0]["worker"], first)
    h.submit(run, assignments[1]["worker"], second)
    h.dispatch(run)
    index = h.call(run, "merge-1", "query_model", view="index", kind="states")
    assert {item["party_name"] for item in index["items"]} == {"requester", "responder"}

    second["items"] = [i for i in second["items"] if i["kind"] != "steps"]
    payload = {"items": [*first["items"], *second["items"]], "unresolved": []}
    second["items"][-2]["value"]["name"] = "reply_session"
    with pytest.raises(WorkflowError, match="states reference"):
        h.submit(run, "merge-1", payload)

    write = receive["value"]["processing"]["on_receive"][0]
    read = send["value"]["processing"]["before_send"][0]
    write["writes"] = ["state.reply_session"]
    read["reads"] = ["state.reply_session"]
    for bad, expected in (
        ("message.Session", "only before_send"),
        ("message_bytes", "message_bytes is read-only"),
        ("state.missing", "states reference"),
    ):
        write["writes"] = [bad]
        with pytest.raises(WorkflowError, match=expected):
            h.submit(run, "merge-1", payload)
    write["writes"] = []
    with pytest.raises(WorkflowError, match="no processing producer"):
        h.submit(run, "merge-1", payload)
    write["writes"] = ["state.reply_session"]
    h.submit(run, "merge-1", payload)
    h.dispatch(run)
    h.review(run)
    output = run.root / "protocol.json"
    assert validate_protocol_file(output) == []
    published = read_json(output)
    assert len(published["states"]) == 2
    assert published["steps"][0]["processing"] == {"on_receive": [write]}
    assert published["steps"][1]["processing"] == {"before_send": [read]}


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
        "resolved_index": 0,
        "category": "external_reference",
        "description": "Reviewer confirms that this needs another document",
        "batch_ids": [w.batches[-1]["batch_id"]],
    }
    h.review(w, [h.finding(w)], classifications=[ambiguity])
    h.submit(w, "parser-1", w.checkpoint(h.active(w)[0]["candidate_path"]))
    assert h.active(w)[0]["batches"] == [w.batches[0]["batch_id"]]
    assert h.active(w)[0]["unresolved_indexes"] == []
    assert h.active(w)[0]["retained_classifications"] == [ambiguity]
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
