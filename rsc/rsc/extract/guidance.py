"""Shared guidance text for prompts and validation suggestions.

The hook event lists and issue hints live here so the extraction prompts and the
validator responses use the same wording.
"""

HOOK_EVENTS = {
    "before_send": (
        "before_send — construction and send prerequisites: select and encode message "
        "fields and name where each computed value comes from; check the send "
        "preconditions (required state, earlier negotiation, conditions); set the "
        "version, type, marker or parameter the message carries; keep the encoded form "
        "for later accumulation"
    ),
    "on_receive": (
        "on_receive — receiver parsing, checks and saving: parse structure and fields "
        "including length, encoding and nesting constraints; verify identity, algorithm, "
        "version, sequence, MAC or signature; handle rejection, alert, abort or fallback; "
        "save the state later steps need (request parameters, negotiated results, key "
        "material, markers)"
    ),
    "after_send": (
        "after_send — changes to the sender's own state after transmission: append the "
        "sent encoding to the accumulated context; advance a sender counter, sequence "
        "number or key generation; switch traffic or read/write key direction; store "
        "values the message itself created (tickets, ages, nonces, markers); clear "
        "connection state after termination or failure"
    ),
}

HOOK_RULES = (
    "Write a hook operation only when that moment really carries a calculation, a "
    "verification or a state change. When an operation has no writes, say in its rule "
    "that it only verifies. An empty hook is not a gap and must not be filled with an "
    "operation that does nothing."
)

TYPE_HINTS = {
    "missing_reference": (
        "Match the name to an existing entity of the required category, or add the "
        "missing entity with source evidence."
    ),
    "ambiguous_reference": (
        "Use the fully qualified or uniquely matching name, or merge the duplicates "
        "that the source treats as the same concept."
    ),
    "same_name": (
        "Give distinct concepts distinct names, or merge them when the source defines "
        "one concept with several spellings."
    ),
    "channel_without_step": (
        "Add the step that uses this channel, or record why the source defines a "
        "channel without any modeled action."
    ),
    "channel_requires_connect": (
        "Add a connect step by the channel's declared initiator role."
    ),
    "channel_requires_listen": (
        "Add a listen step by the channel's declared acceptor role."
    ),
    "channel_requires_close": (
        "Add a close step by an endpoint of this channel; a protocol termination "
        "message and a transport close are different things."
    ),
    "message_without_send": (
        "Add the send step that transmits this message, or record why it is never "
        "exchanged."
    ),
    "party_without_channel": (
        "Add the channel that names this party as an endpoint, or record that it only "
        "appears inside another participant's description."
    ),
    "state_without_access": (
        "Read or write this state inside a step processing operation, or remove it when "
        "no source requirement needs it."
    ),
    "step_without_dependency": (
        "Add the prerequisite or field relation the source requires, or record why "
        "none exists; do not infer an edge only because two actions both exist."
    ),
    "step_without_processing": (
        "Express the construction, verification or state update the source requires in "
        "the matching hook as reads, writes or rule; a condition that only narrates it "
        "is not an operation. If the source prescribes a bare transmission, record the "
        "gap instead of inventing work."
    ),
    "duplicate_dependency": (
        "Merge the duplicate dependency into one edge with the union of its conditions "
        "and field relations."
    ),
    "duplicate_field": (
        "Keep one entry per field name and merge the duplicate definitions."
    ),
    "deferred_entity": (
        "Supply the missing entity or processing rule, or close the deferred issue with "
        "source evidence."
    ),
    "protocol": (
        "Repair the model so this source-supported requirement is expressed by an "
        "existing entity or operation."
    ),
}

STRUCTURE_HINTS = (
    (
        "has no processing producer",
        "Either add the operation that writes state.<name> in the step that computes it, "
        "or change this state's source to external with evidence; a processing state "
        "read by a step but never written fails validation.",
    ),
    (
        "is not carried by",
        "Attach the field to the message that transmits it, or correct the message or "
        "field name the processing refers to.",
    ),
    (
        "role is not an endpoint",
        "Use an endpoint party name of the channel, or add the party to "
        "endpoint_party_names when the source makes it an endpoint.",
    ),
    (
        "connectionless channel has connection roles",
        "Remove the initiator and acceptor roles from a connectionless channel.",
    ),
)

GENERIC_HINT = (
    "Repair the model so this source-supported requirement is expressed, keeping the "
    "existing valid fields, processing operations, conditions and branches."
)


ISSUE_HINT_CONTRACT = (
    "Each validation row carries a suggestion aligned with the extraction rules; follow it "
    "and repair the underlying model, not only the reported name."
)


def issue_suggestion(issue):
    """Return the prompt-aligned suggestion text for one validation issue."""
    kind = issue.get("type") or ""
    description = (issue.get("description") or "").lower()
    if kind == "structure":
        for marker, hint in STRUCTURE_HINTS:
            if marker in description:
                return hint.replace("<name>", _name(issue))
    return TYPE_HINTS.get(kind, GENERIC_HINT)


def _name(issue):
    """Best-effort state or object name from a structural description."""
    description = issue.get("description") or ""
    quote = description.split("'")
    if len(quote) >= 3:
        return quote[1]
    return "this state"


def with_suggestions(issues):
    """Attach a suggestion to each issue without mutating the source rows."""
    return [{**issue, "suggestion": issue_suggestion(issue)} for issue in issues]
