"""Protocol shape checks. Name references are not resolved."""

import copy

from protocol_samples import base, document, interactions

from rsc.protocol.contract import COLLECTIONS

def empty_model():
    return {kind: [] for kind in COLLECTIONS}
from rsc.protocol.validate import object_errors, prune_invalid


def merged_sample():
    model = empty_model() | {key: value for key, value in base().items() if key != "unresolved"}
    model.update({key: value for key, value in interactions(model).items() if key != "unresolved"})
    return model


def test_missing_names_and_unrelated_fields_are_not_reference_errors():
    model = merged_sample()
    model["steps"][0]["receiver_name"] = "ghost"
    model["step_dependencies"][0]["field_relations"][0]["prior_field"] = "missing"
    assert object_errors(model, {"rfc9999": document()}) == {}


def test_dependency_field_relation_accepts_derivation():
    model = merged_sample()
    model["step_dependencies"][0]["field_relations"][0]["derive"] = "increment by one"
    assert object_errors(model, {"rfc9999": document()}) == {}


def test_connectionless_roles_are_a_structural_error():
    model = merged_sample()
    model["channels"][0]["connection"] = "connectionless"
    errors = str(object_errors(model, {"rfc9999": document()}))
    assert "connectionless" in errors


def test_minimal_dependencies_allow_self_loops_empty_fields_and_distinct_conditions():
    model = merged_sample()
    edges = model["step_dependencies"]
    assert edges[1]["prior_step"] == edges[1]["dependent_step"]
    edges[0]["field_relations"] = []
    assert object_errors(model, {"rfc9999": document()}) == {}
    extra = copy.deepcopy(edges[0])
    extra["condition"] = "different condition"
    extra["alternative_group"] = "either"
    model["step_dependencies"].append(extra)
    errors = str(object_errors(model, {"rfc9999": document()}))
    assert "alternative_group" in errors or "Additional" in errors


def test_schema_invalid_channel_is_pruned_without_deleting_name_references():
    model = merged_sample()
    model["channels"][0]["connection"] = "connectionless"
    valid, rejected = prune_invalid(model, {"rfc9999": document()})
    assert not valid["channels"]
    assert len(valid["steps"]) == len(model["steps"])
    assert rejected
    assert set(rejected[0]) == {"description", "object_names"}
    assert "control" in rejected[0]["object_names"]


def test_channel_connection_roles_stay_distinct():
    model = merged_sample()
    reverse = copy.deepcopy(model["channels"][0])
    reverse.update(
        name="reverse",
        initiator_party_name="responder",
        acceptor_party_name="requester",
    )
    model["channels"].append(reverse)
    model["steps"][0]["channel_name"] = "reverse"
    assert not object_errors(model, {"rfc9999": document()})
    assert model["steps"][0]["name"]
    assert {channel["name"] for channel in model["channels"]} == {"control", "reverse"}
