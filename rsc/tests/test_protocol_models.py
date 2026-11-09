"""Published protocol types remain schema-compatible after removing provider responses."""

from copy import deepcopy

import pytest
from protocol_samples import base
from pydantic import ValidationError

from rsc.protocol.contract import COLLECTIONS, protocol_schema
from rsc.protocol.models import (
    AssociatedField,
    Issue,
    ProtocolModel,
    protocol_json_schema,
    validate_step,
)


def test_published_schema_is_unchanged():
    assert protocol_schema() == protocol_json_schema()


def test_final_model_rejects_audit_fields():
    data = base()
    model = {key: data.get(key, []) for key in COLLECTIONS}
    assert ProtocolModel.model_validate(model).messages
    bad = deepcopy(model)
    bad["parties"][0]["evidence"] = []
    with pytest.raises(ValidationError):
        ProtocolModel.model_validate(bad)


def test_fields_and_issues_remain_minimal():
    assert AssociatedField.model_validate({"name": "Session"}).name == "Session"
    with pytest.raises(ValidationError):
        AssociatedField.model_validate({"name": "Session", "purposes": ["correlation"]})
    assert Issue.model_validate({"description": "Ambiguous", "object_names": []})
    with pytest.raises(ValidationError):
        Issue.model_validate({"id": "not-exported", "description": "Ambiguous", "object_names": []})


def test_published_steps_are_named_without_an_id():
    value = {
        "name": "send HELLO",
        "action": "send",
        "message_name": "HELLO",
        "channel_name": "control",
        "sender_name": "client",
        "receiver_name": "server",
        "condition": "",
    }
    assert validate_step(value).name == "send HELLO"
    with pytest.raises(ValidationError):
        validate_step({**value, "id": "step-123"})
