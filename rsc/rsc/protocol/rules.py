"""Shared semantic instructions for message schemas and every Agent phase."""

MESSAGE_SCOPE_RULES = (
    "A message is an actual protocol message or RFC-defined independently exchanged transfer unit. "
    "Grammar productions and table classification labels do not automatically constitute "
    "messages. Abstract categories, command groups, fields and syntax components are not "
    "independent message types. Ordinary message types need not carry associated fields. "
    "A concrete response code defines a separate message type "
    "only when its response carries associated fields needed in later interactions for "
    "correlation, connection setup, branching or interaction state. The response code itself "
    "or free-form reply wording alone does not justify a separate type; otherwise reuse "
    "an actually exchanged, source-supported shared response form, not a response-code "
    "family or classification label."
)

ASSOCIATED_FIELD_RULES = (
    "Attach a field only to the message that actually transmits it. Retain only fields "
    "needed for cross-message correlation, connection setup, interaction branching or "
    "interaction state. A message that triggers another message does not inherit that "
    "message's fields. Do not extract complete wire layouts or ordinary payload details."
)
