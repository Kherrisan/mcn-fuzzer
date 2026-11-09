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

CHANNEL_LIFECYCLE_RULES = (
    "A connection-oriented channel represents one connection lifecycle, beginning with "
    "a source-supported connect or listen step and ending with transport close. "
    "A reconnect or session-resumption connection uses a different channel even when "
    "the parties, addresses, ports, or logical session are unchanged. Distinguish "
    "initial and resumed connections. Reuse message definitions, but create "
    "channel-specific steps for their exchanges. Preserve cross-channel prerequisites, "
    "state, and field relations supported by the source. Describe how earlier exchanges "
    "supply or derive values used on a later channel; distinguish direct reuse from "
    "derivation without assuming equality. Do not require the initial connection to close before "
    "resumption unless the source requires that order. A persistent listener need not "
    "listen again for every connection; listening readiness is distinct from an accepted "
    "connection. Do not invent missing network operations or expand arbitrary repetitions "
    "into infinitely many channels: model the distinct source-supported lifecycle roles "
    "and record any unspecified boundaries as unresolved. For connectionless channels, "
    "follow source-defined exchange boundaries without inventing connect or close. "
    "Reviewer: check lifecycle boundaries, separate resumption channels, and the "
    "cross-channel state needed to perform them."
)

NETWORK_ACTION_RULES = (
    "A step action is send, listen, connect, or close. send is one protocol message "
    "between a sender and a receiver. listen means that party starts listening and can "
    "accept connections. connect means that party opens the connection. close means that "
    "party closes the transport connection. Protocol termination messages stay send; "
    "add a separate close step only when the source "
    "describes the later transport close. Do not invent a message, sender, or receiver "
    "for a network action. Keep one network-action node per party, channel, and action. "
    "The same node may occur more than once in an ordered trace. Put occurrence-specific "
    "conditions on dependency edges or proposal constraints, not by copying the node. "
    "Do not add listen, connect, or close steps when the source does not describe them."
)

ASSOCIATED_FIELD_RULES = (
    "Attach a field only to the message that actually transmits it. Retain only fields "
    "needed for cross-message correlation, connection setup, interaction branching or "
    "interaction state. A message that triggers another message does not inherit that "
    "message's fields. Do not extract complete wire layouts or ordinary payload details."
)
