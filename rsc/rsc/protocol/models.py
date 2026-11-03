"""Strict protocol and Agent response types for the version 7 contract."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .rules import ASSOCIATED_FIELD_RULES, MESSAGE_SCOPE_RULES

PROTOCOL_VERSION = "7.0.0"
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
        description="Field name, constant, or a short source phrase. Example: PASV response.port.",
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
        description="Reusable channel name; specialize only for a supported connection distinction."
    )
    description: NonEmpty = Field(description="One short phrase for this connection.")
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


class Step(StrictModel):
    id: NonEmpty = Field(description="Python-assigned step identity. Not a name reference.")
    name: NonEmpty = Field(description="Persistent step name used by dependencies.")
    message_name: NonEmpty
    channel_name: NonEmpty
    sender_name: NonEmpty
    receiver_name: NonEmpty
    condition: str


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


class ProposedAssociatedField(StrictModel):
    name: NonEmpty = Field(description="Protocol field name. Reuse the same field's name.")


class ProposedParty(StrictModel):
    """A direct communication role; retain a container separately only for a distinct role."""

    name: NonEmpty = Field(
        description=(
            "Reuse the existing name for the same communication role; otherwise use an RFC term."
        )
    )
    description: NonEmpty = Field(
        description="One short phrase stating the direct communication role."
    )


class ProposedProtocolMessage(StrictModel):
    name: NonEmpty = Field(
        description="Short protocol term. Reuse the same message's name. " + MESSAGE_SCOPE_RULES
    )
    associated_fields: list[ProposedAssociatedField] = Field(description=ASSOCIATED_FIELD_RULES)


class ProposedEndpointSource(StrictModel):
    party_name: NonEmpty
    parameter: NonEmpty
    from_: NonEmpty = Field(
        alias="from",
        description="Field name, constant, or a short source phrase.",
    )

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ProposedChannel(StrictModel):
    name: NonEmpty = Field(
        description="Reusable channel name; specialize only for a supported connection distinction."
    )
    description: NonEmpty = Field(description="One short phrase for this connection.")
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
    endpoint_sources: list[ProposedEndpointSource] = Field(
        description=(
            "Sources of endpoint parameters; "
            "additional detail alone does not define another channel."
        )
    )


class ProposedStep(StrictModel):
    """A reusable interaction. name is persisted and referenced by dependencies."""

    name: NonEmpty = Field(
        description="Persistent step name. Distinguish direction and reuse an existing name."
    )
    message_name: NonEmpty
    channel_name: NonEmpty
    sender_name: NonEmpty
    receiver_name: NonEmpty
    condition: str = Field(
        default="",
        description="Omit when there is no extra condition. Null is not allowed.",
    )


class ProposedFieldRelation(StrictModel):
    prior_field: NonEmpty
    dependent_field: NonEmpty


class ProposedStepDependency(StrictModel):
    prior_step: NonEmpty = Field(description="Name of the earlier step, not a message name.")
    dependent_step: NonEmpty = Field(description="Name of the later step, not a message name.")
    condition: str = Field(
        default="",
        description="Omit when there is no extra condition. Null is not allowed.",
    )
    field_relations: list[ProposedFieldRelation] = Field(
        default_factory=list,
        description="Omit when no field names are related. Null is not allowed.",
    )


class Keyword(StrictModel):
    text: NonEmpty


class ObjectExtractionResponse(StrictModel):
    keywords: list[Keyword] = Field(
        default_factory=list,
        description="Useful protocol concept terms from this focus block. Omit when empty.",
    )
    parties: list[ProposedParty]
    messages: list[ProposedProtocolMessage]
    channels: list[ProposedChannel]
    unresolved: list[Issue] = Field(
        default_factory=list,
        description="Omit when there are no issues. Null is not allowed.",
    )


class InteractionExtractionResponse(StrictModel):
    keywords: list[Keyword] = Field(
        default_factory=list,
        description="Useful protocol concept terms from this focus block. Omit when empty.",
    )
    steps: list[ProposedStep]
    step_dependencies: list[ProposedStepDependency]
    unresolved: list[Issue] = Field(
        default_factory=list,
        description="Omit when there are no issues. Null is not allowed.",
    )


class ObjectRepairResponse(StrictModel):
    parties: list[ProposedParty]
    messages: list[ProposedProtocolMessage]
    channels: list[ProposedChannel]
    unresolved: list[Issue] = Field(
        default_factory=list,
        description="Omit when there are no issues. Null is not allowed.",
    )


class InteractionRepairResponse(ObjectRepairResponse):
    steps: list[ProposedStep]
    step_dependencies: list[ProposedStepDependency]


class LinkGroup(StrictModel):
    member_names: Annotated[list[NonEmpty], Field(min_length=1)]
    member_indexes: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list,
        description=(
            "Supplied summary indexes in member_names order. Required when distinct "
            "definitions sharing a name are split across groups or exclusions."
        ),
    )
    reason: NonEmpty = Field(
        description=(
            "Identity basis or a concrete incompatibility; wording differences are insufficient."
        )
    )


class LinkExclusion(StrictModel):
    name: NonEmpty = Field(
        description="Name of one supplied out-of-scope party, message or channel."
    )
    index: Annotated[int, Field(ge=0)] | None = Field(
        default=None,
        description=(
            "Supplied summary index; required to disambiguate distinct same-name occurrences."
        ),
    )
    reason: NonEmpty = Field(
        description="Why the candidate is outside the target protocol interaction model."
    )


class LinkPartitionResponse(StrictModel):
    groups: list[LinkGroup]
    excluded: list[LinkExclusion] = Field(
        default_factory=list,
        description=(
            "Out-of-scope party, message or channel occurrences. "
            "Each supplied occurrence belongs to exactly one group or exclusion."
        ),
    )
    unresolved: list[Issue] = Field(
        default_factory=list,
        description="Omit when there are no issues. Null is not allowed.",
    )


class MergedStep(StrictModel):
    """A step produced while merging existing definitions. Python assigns the step ID later."""

    name: NonEmpty
    message_name: NonEmpty
    channel_name: NonEmpty
    sender_name: NonEmpty
    receiver_name: NonEmpty
    condition: str = Field(
        default="",
        description="Omit when there is no extra condition. Null is not allowed.",
    )


MergedRootObject = (
    ProposedParty | ProposedProtocolMessage | ProposedChannel | MergedStep | ProposedStepDependency
)


class LinkMergeResponse(StrictModel):
    status: Literal["merged", "deferred"]
    merged_object: MergedRootObject | None
    unresolved: list[Issue] = Field(
        default_factory=list,
        description="Omit when there are no issues. Null is not allowed.",
    )

    @model_validator(mode="after")
    def consistent_status(self) -> LinkMergeResponse:
        if (self.status == "merged") != (self.merged_object is not None):
            raise ValueError("merged status requires an object; deferred status requires null")
        return self


class Source(StrictModel):
    doc_id: NonEmpty
    rfc: NonEmpty
    input_sha256: NonEmpty
    document_sha256: NonEmpty
    path: NonEmpty


class ProtocolMeta(StrictModel):
    schema_version: Literal["7.0.0"]
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
    """Build the published schema from the same types used by Agents."""

    names = {
        "AssociatedField": "field",
        "Party": "party",
        "ProtocolMessage": "message",
        "Channel": "channel",
        "EndpointSource": "endpoint_source",
        "Step": "step",
        "FieldRelation": "relation",
        "StepDependency": "dependency",
        "Issue": "issue",
        "Source": "source",
        "ProtocolMeta": "meta",
    }
    schema = Protocol.model_json_schema(by_alias=True)

    def rename(value: Any) -> Any:
        if isinstance(value, list):
            return [rename(item) for item in value]
        if isinstance(value, dict):
            return {
                key: (
                    "#/$defs/" + names.get(item[8:], item[8:])
                    if key == "$ref" and item.startswith("#/$defs/")
                    else rename(item)
                )
                for key, item in value.items()
            }
        return value

    schema = rename(schema)
    schema["$defs"] = {names.get(key, key): value for key, value in schema["$defs"].items()}
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://rsc.local/schemas/protocol.schema.json"
    schema["title"] = "Protocol interaction model"
    return schema
