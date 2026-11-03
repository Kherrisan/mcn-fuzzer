"""Version 7 final-model and model-response boundaries."""

from copy import deepcopy

import pytest
from protocol_samples import base
from pydantic import ValidationError

from rsc.protocol.contract import BASE, COLLECTIONS, protocol_schema
from rsc.protocol.models import (
    Issue,
    Keyword,
    LinkMergeResponse,
    ObjectExtractionResponse,
    ProtocolModel,
    protocol_json_schema,
)


def proposal():
    data = deepcopy(base())
    return {key: data[key] for key in BASE} | {"unresolved": []}


def test_schema_artifact_matches_the_typed_contract():
    assert protocol_schema() == protocol_json_schema()


def test_object_response_contains_names_but_no_definition_ids():
    response = ObjectExtractionResponse.model_validate(proposal())
    assert response.messages and response.channels
    for forbidden in ("id", "aliases", "evidence", "meaning", "identification"):
        bad = proposal()
        bad["parties"][0][forbidden] = "model-id"
        with pytest.raises(ValidationError):
            ObjectExtractionResponse.model_validate(bad)
    bad = proposal() | {"reused_objects": []}
    with pytest.raises(ValidationError):
        ObjectExtractionResponse.model_validate(bad)


@pytest.mark.parametrize("target", ["response", "final", "merge"])
def test_associated_fields_keep_only_names_and_reject_legacy_purposes(target):
    from rsc.protocol.models import AssociatedField, ProposedAssociatedField

    assert AssociatedField.model_validate({"name": "CSeq"}).model_dump() == {"name": "CSeq"}
    assert ProposedAssociatedField.model_validate({"name": "CSeq"}).model_dump() == {"name": "CSeq"}
    data = proposal()
    data["messages"][0]["associated_fields"][0]["purposes"] = ["correlation"]
    with pytest.raises(ValidationError):
        if target == "response":
            ObjectExtractionResponse.model_validate(data)
        elif target == "final":
            ProtocolModel.model_validate(_model(data))
        else:
            LinkMergeResponse.model_validate(
                {"status": "merged", "merged_object": data["messages"][0]}
            )


def _model(data):
    return {key: data.get(key, []) for key in COLLECTIONS}


def test_final_model_keeps_step_ids_and_rejects_removed_fields():
    data = base()
    assert ProtocolModel.model_validate(_model(data)).messages
    bad = deepcopy(data)
    bad["parties"][0]["evidence"] = []
    with pytest.raises(ValidationError):
        ProtocolModel.model_validate(_model(bad))


def test_issue_has_only_description_and_object_names():
    assert Keyword.model_validate({"text": "CSeq"})
    with pytest.raises(ValidationError):
        Keyword.model_validate({"text": "CSeq", "doc_id": "rfc"})
    assert Issue.model_validate(
        {"description": "Unclear role", "object_names": ["requester"]}
    ).object_names == ["requester"]
    assert Issue.model_validate({"description": "No specific object", "object_names": []})
    with pytest.raises(ValidationError):
        Issue.model_validate(
            {"id": "model-id", "kind": "ambiguity", "description": "Unclear", "object_ids": []}
        )


def test_response_models_omit_empty_fields_without_changing_the_final_schema():
    from rsc.protocol.contract import protocol_schema
    from rsc.protocol.models import (
        InteractionExtractionResponse,
        LinkPartitionResponse,
        Step,
        protocol_json_schema,
    )

    response = InteractionExtractionResponse.model_validate(
        {
            "steps": [
                {
                    "name": "send request",
                    "message_name": "request",
                    "channel_name": "control",
                    "sender_name": "requester",
                    "receiver_name": "responder",
                }
            ],
            "step_dependencies": [{"prior_step": "send request", "dependent_step": "send request"}],
        }
    )
    assert response.unresolved == []
    assert response.steps[0].condition == ""
    assert response.step_dependencies[0].field_relations == []
    assert response.step_dependencies[0].condition == ""
    dumped = response.model_dump()
    assert dumped["step_dependencies"][0]["field_relations"] == []
    kept = InteractionExtractionResponse.model_validate(
        {
            "steps": [
                {
                    "name": "send request",
                    "message_name": "request",
                    "channel_name": "control",
                    "sender_name": "requester",
                    "receiver_name": "responder",
                    "condition": "after setup",
                }
            ],
            "step_dependencies": [
                {
                    "prior_step": "send request",
                    "dependent_step": "send request",
                    "condition": "retry",
                    "field_relations": [{"prior_field": "CSeq", "dependent_field": "CSeq"}],
                }
            ],
            "unresolved": [{"description": "Check CSeq", "object_names": ["CSeq"]}],
        }
    )
    assert kept.steps[0].condition == "after setup"
    assert kept.unresolved[0].object_names == ["CSeq"]
    with pytest.raises(ValidationError):
        InteractionExtractionResponse.model_validate(
            {
                "steps": [
                    {
                        "name": "send request",
                        "message_name": "request",
                        "channel_name": "control",
                        "sender_name": "requester",
                        "receiver_name": "responder",
                        "condition": None,
                    }
                ],
                "step_dependencies": [],
            }
        )
    assert (
        LinkPartitionResponse.model_validate(
            {"groups": [{"member_names": ["requester"], "reason": "same role"}]}
        ).unresolved
        == []
    )
    with pytest.raises(ValidationError):
        Step.model_validate(
            {
                "id": "step-1",
                "name": "send request",
                "message_name": "request",
                "channel_name": "control",
                "sender_name": "requester",
                "receiver_name": "responder",
            }
        )
    assert protocol_schema() == protocol_json_schema()


def test_link_merge_object_uses_the_chosen_name_and_has_no_id_map():
    valid = {
        "status": "merged",
        "merged_object": {"name": "requester", "description": "request originator"},
        "unresolved": [],
    }
    assert LinkMergeResponse.model_validate(valid).merged_object.name == "requester"
    bad = deepcopy(valid)
    bad["merged_object"]["id"] = "requester"
    with pytest.raises(ValidationError):
        LinkMergeResponse.model_validate(bad)
    bad = deepcopy(valid)
    bad["nested_id_map"] = []
    with pytest.raises(ValidationError):
        LinkMergeResponse.model_validate(bad)
