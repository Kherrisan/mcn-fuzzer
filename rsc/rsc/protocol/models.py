"""Strict published protocol types for the version 8 contract."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .rules import (
    ASSOCIATED_FIELD_RULES,
    CHANNEL_LIFECYCLE_RULES,
    MESSAGE_SCOPE_RULES,
    NETWORK_ACTION_RULES,
)

PROTOCOL_VERSION = "8.0.0"
NonEmpty = Annotated[str, Field(min_length=1)]
ObjectKind = Literal["parties", "messages", "channels", "steps", "step_dependencies"]
Phase = Literal["objects", "interactions"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BlockRef(StrictModel):
    doc_id: NonEmpty = Field(description="Document identity from the selected input batch.")
    block_id: NonEmpty = Field(description="Block identity scoped to the specified document.")


class AssociatedField(StrictModel):
    name: NonEmpty = Field(description="Protocol field name. Do not invent a hash or sequence.")


class Party(StrictModel):
    """A direct communication role; retain a container separately only for a distinct role."""

    name: NonEmpty = Field(
        description=(
            "Reuse the existing name for the same communication role; otherwise use an RFC term."
        )
    )
    description: NonEmpty = Field(
        description="One short phrase stating the direct communication role."
    )


class ProtocolMessage(StrictModel):
    name: NonEmpty = Field(
        description="Short protocol term. Reuse the same message's name. " + MESSAGE_SCOPE_RULES
    )
    associated_fields: list[AssociatedField] = Field(description=ASSOCIATED_FIELD_RULES)


class EndpointSource(StrictModel):
    party_name: NonEmpty
    parameter: NonEmpty
    from_: NonEmpty = Field(
        alias="from",
        description="Field name, constant, or a short source phrase identifying the endpoint source.",
    )

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class Channel(StrictModel):
    name: NonEmpty = Field(
        description="Name for a source-supported connection lifecycle. " + CHANNEL_LIFECYCLE_RULES
    )
    description: NonEmpty = Field(description="One short phrase for this connection lifecycle.")
    transport: NonEmpty = Field(
        description="Underlying transport stated by the source, such as TCP or UDP."
    )
    connection: Literal["connection_oriented", "connectionless"]
    endpoint_party_names: Annotated[list[NonEmpty], Field(min_length=1)] = Field(
        description="Names of the actual communication endpoints."
    )
    initiator_party_name: NonEmpty | None = Field(
        description="Party establishing the connection; null when unknown or inapplicable."
    )
    acceptor_party_name: NonEmpty | None = Field(
        description="Party accepting the connection; null when unknown or inapplicable."
    )
    endpoint_sources: list[EndpointSource] = Field(
        description=(
            "Sources of endpoint parameters; "
            "additional detail alone does not define another channel."
        )
    )


class SendStep(StrictModel):
    """One directional protocol message, including protocol termination messages."""

    name: NonEmpty = Field(description="Unique step name. Dependencies and proposals use this name.")
    action: Literal["send"] = Field(description=NETWORK_ACTION_RULES)
    channel_name: NonEmpty
    condition: str = Field(
        description=(
            "Condition shared by every occurrence. Put context-specific conditions on "
            "dependency edges or proposal constraints."
        )
    )
    message_name: NonEmpty
    sender_name: NonEmpty
    receiver_name: NonEmpty


class ListenStep(StrictModel):
    """The party starts listening and can accept connections. This is not an accept event."""

    name: NonEmpty = Field(description="Unique step name. Dependencies and proposals use this name.")
    action: Literal["listen"] = Field(description=NETWORK_ACTION_RULES)
    channel_name: NonEmpty
    condition: str = Field(
        description=(
            "Condition shared by every occurrence. Put context-specific conditions on "
            "dependency edges or proposal constraints."
        )
    )
    party_name: NonEmpty = Field(description="Endpoint party that performs this network action.")


class ConnectStep(StrictModel):
    """The party actively opens the transport connection."""

    name: NonEmpty = Field(description="Unique step name. Dependencies and proposals use this name.")
    action: Literal["connect"] = Field(description=NETWORK_ACTION_RULES)
    channel_name: NonEmpty
    condition: str = Field(
        description=(
            "Condition shared by every occurrence. Put context-specific conditions on "
            "dependency edges or proposal constraints."
        )
    )
    party_name: NonEmpty = Field(description="Endpoint party that performs this network action.")


class CloseStep(StrictModel):
    """The party closes the transport connection. This is not a protocol termination message."""

    name: NonEmpty = Field(description="Unique step name. Dependencies and proposals use this name.")
    action: Literal["close"] = Field(description=NETWORK_ACTION_RULES)
    channel_name: NonEmpty
    condition: str = Field(
        description=(
            "Condition shared by every occurrence. Put context-specific conditions on "
            "dependency edges or proposal constraints."
        )
    )
    party_name: NonEmpty = Field(description="Endpoint party that performs this network action.")


Step = Annotated[
    SendStep | ListenStep | ConnectStep | CloseStep,
    Field(discriminator="action"),
]
STEP_ADAPTER = TypeAdapter(Step)


def validate_step(value: Any) -> SendStep | ListenStep | ConnectStep | CloseStep:
    return STEP_ADAPTER.validate_python(value)


class FieldRelation(StrictModel):
    prior_field: NonEmpty = Field(description="Field name, optionally qualified as Message.field.")
    dependent_field: NonEmpty = Field(
        description="Field name, optionally qualified as Message.field."
    )


class StepDependency(StrictModel):
    """A dependency on an earlier occurrence, without requiring adjacency."""

    prior_step: NonEmpty = Field(description="Name of the earlier step.")
    dependent_step: NonEmpty = Field(description="Name of the later step.")
    condition: str = Field(description="Short dependency condition, or an empty string.")
    field_relations: list[FieldRelation]


class Issue(StrictModel):
    description: NonEmpty
    object_names: list[str] = Field(
        description="Names of affected objects or fields. Empty when none apply."
    )


class ProtocolModel(StrictModel):
    parties: list[Party] = Field(default_factory=list)
    messages: list[ProtocolMessage] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    step_dependencies: list[StepDependency] = Field(default_factory=list)


class Source(StrictModel):
    doc_id: NonEmpty
    rfc: NonEmpty
    input_sha256: NonEmpty
    document_sha256: NonEmpty
    path: NonEmpty


class ProtocolMeta(StrictModel):
    schema_version: Literal["8.0.0"]
    protocol: NonEmpty
    complete: bool
    documents: Annotated[list[Source], Field(min_length=1)]


class Protocol(StrictModel):
    meta: ProtocolMeta
    parties: list[Party]
    messages: list[ProtocolMessage]
    channels: list[Channel]
    steps: list[Step]
    step_dependencies: list[StepDependency]
    unresolved: list[Issue]


def protocol_json_schema() -> dict[str, Any]:
    """Build the published schema from the final model types."""

    names = {
        "AssociatedField": "field",
        "Party": "party",
        "ProtocolMessage": "message",
        "Channel": "channel",
        "EndpointSource": "endpoint_source",
        "SendStep": "send_step",
        "ListenStep": "listen_step",
        "ConnectStep": "connect_step",
        "CloseStep": "close_step",
        "FieldRelation": "relation",
        "StepDependency": "dependency",
        "Issue": "issue",
        "Source": "source",
        "ProtocolMeta": "meta",
    }
    schema = Protocol.model_json_schema(by_alias=True)

    def rename_ref(item: str) -> str:
        return "#/$defs/" + names.get(item[8:], item[8:])

    def rename(value: Any) -> Any:
        if isinstance(value, list):
            return [rename(item) for item in value]
        if isinstance(value, dict):
            renamed = {}
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
                    renamed[key] = rename_ref(item)
                elif key == "mapping" and isinstance(item, dict):
                    renamed[key] = {
                        name: rename_ref(target)
                        if isinstance(target, str) and target.startswith("#/$defs/")
                        else target
                        for name, target in item.items()
                    }
                else:
                    renamed[key] = rename(item)
            return renamed
        return value

    schema = rename(schema)
    schema["$defs"] = {names.get(key, key): value for key, value in schema["$defs"].items()}
    items = schema["properties"]["steps"]["items"]
    schema["$defs"]["step"] = items
    schema["properties"]["steps"]["items"] = {"$ref": "#/$defs/step"}
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://rsc.local/schemas/protocol.schema.json"
    schema["title"] = "Protocol interaction model"
    return schema
