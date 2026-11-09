"""MCP tool contracts shared by the socket service and stdio bridge."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from rsc.protocol.contract import DEFINITIONS, protocol_schema

from .state import WorkflowError

STRING = {"type": "string", "minLength": 1}
INTEGER = {"type": "integer", "minimum": 0}
TOKEN = {"type": "integer", "minimum": 1, "maximum": 30000}
FORMAT = {"enum": ["compact", "records"]}
OBJECT = {"type": "object"}
OBJECT_ARRAY = {"type": "array", "items": OBJECT}
STRING_ARRAY = {"type": "array", "items": STRING}
# Required parameters precede optional parameters in each contract.
CONTRACTS = {
    "workflow_status": ("Read phase, assignment, allowed tools and gate blockers.", {}, []),
    "claim_assignment": (
        "Claim the exact dispatch ID provided by the supervisor.",
        {"dispatch_id": STRING},
        ["dispatch_id"],
    ),
    "check_gate": ("Check the current phase without advancing it.", {}, []),
    "advance_phase": (
        "Submit this phase. The server selects the next phase after validation.",
        {},
        [],
    ),
    "prepare_sources": (
        "Parse the task's immutable RFC inputs and create bounded chapter batches.",
        {},
        [],
    ),
    "list_batches": (
        "List source batches and pending work; follow next_cursor and resend revision.",
        {"filter": OBJECT, "cursor": INTEGER, "max_tokens": TOKEN, "revision": INTEGER},
        [],
    ),
    "read_batch": (
        "Read the COMPLETE source batch and receive its delivery receipt.",
        {"batch_id": STRING},
        ["batch_id"],
    ),
    "read_batches": (
        "Read complete batches in assignment order until max_tokens. "
        "Undelivered batches get no receipt.",
        {"batch_ids": STRING_ARRAY, "max_tokens": TOKEN},
        ["batch_ids"],
    ),
    "search_source": (
        "Find supplementary source text; this does not satisfy batch reading gates.",
        {
            "query": STRING,
            "filters": OBJECT,
            "cursor": INTEGER,
            "max_tokens": TOKEN,
            "revision": INTEGER,
        },
        ["query"],
    ),
    "query_model": (
        "Read records, internal IDs and evidence. format=compact is the default table; "
        "format=records returns the original objects. Follow next_cursor with revision.",
        {
            "kind": STRING,
            "filters": OBJECT,
            "record_ids": STRING_ARRAY,
            "detail": {"type": "boolean"},
            "cursor": INTEGER,
            "format": FORMAT,
            "max_tokens": TOKEN,
            "revision": INTEGER,
        },
        [],
    ),
    "apply_changes": (
        "Atomically add/update/delete records. Add requires kind,value,evidence; update requires "
        "record_id,value,evidence; delete requires record_id,reason. Evidence is a list of "
        "{batch_id,block_id}. Step IDs are allocated by the server. Use the current revision "
        "and a unique operation_id. Reusing the same operation with the same content is safe.",
        {"expected_revision": INTEGER, "operation_id": STRING, "changes": OBJECT_ARRAY},
        ["expected_revision", "operation_id", "changes"],
    ),
    "complete_batch": (
        "Complete the next parsing batch using its receipt and a report with summary, "
        "record_ids and optional finding_ids. Explicitly justify no relevant information.",
        {"batch_id": STRING, "receipt_id": STRING, "report": OBJECT},
        ["batch_id", "receipt_id", "report"],
    ),
    "list_findings": (
        "Read findings, resolutions and reviewer decisions, following pagination.",
        {"filters": OBJECT, "cursor": INTEGER, "max_tokens": TOKEN, "revision": INTEGER},
        [],
    ),
    "get_changes": (
        "Read every page of the model diff for this review's base and current revision. "
        "Each change keeps its own before and after.",
        {
            "from_revision": INTEGER,
            "to_revision": INTEGER,
            "cursor": INTEGER,
            "max_tokens": TOKEN,
            "revision": INTEGER,
        },
        ["from_revision", "to_revision"],
    ),
    "submit_findings": (
        "Submit findings with description, evidence [{batch_id,block_id}], "
        "optional record_ids and suggestion. Cannot edit the model.",
        {"reviewed_revision": INTEGER, "findings": OBJECT_ARRAY},
        ["reviewed_revision", "findings"],
    ),
    "complete_review_batch": (
        "Record source/model comparison with receipt and report.summary; "
        "include finding_ids or explicitly report no findings.",
        {"batch_id": STRING, "receipt_id": STRING, "report": OBJECT},
        ["batch_id", "receipt_id", "report"],
    ),
    "submit_resolutions": (
        "Submit resolutions with finding_id,status (applied/not_applicable/"
        "unresolved),reason,evidence. Only the reviewer can accept them.",
        {"expected_revision": INTEGER, "resolutions": OBJECT_ARRAY},
        ["expected_revision", "resolutions"],
    ),
    "review_resolutions": (
        "Decide each submitted resolution using finding_id,accepted:boolean,reason.",
        {"reviewed_revision": INTEGER, "decisions": OBJECT_ARRAY},
        ["reviewed_revision", "decisions"],
    ),
}


def schema(name):
    description, properties, required = CONTRACTS[name]
    properties = deepcopy(properties)
    if name == "apply_changes":
        definitions = deepcopy(protocol_schema()["$defs"])
        values = []
        for definition in [*DEFINITIONS.values(), "issue"]:
            values.append(deepcopy(definitions[definition]))
        properties["changes"]["items"] = {
            "type": "object",
            "properties": {
                "op": {"enum": ["add", "update", "delete"]},
                "kind": {"enum": [*DEFINITIONS, "unresolved"]},
                "record_id": STRING,
                "value": {"anyOf": values},
                "evidence": OBJECT_ARRAY,
                "reason": STRING,
            },
            "required": ["op"],
            "additionalProperties": False,
        }
    result = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }
    if name == "apply_changes":
        result["inputSchema"]["$defs"] = definitions
    return result


def validate_arguments(name, arguments):
    if name not in CONTRACTS:
        raise WorkflowError("unknown tool")
    errors = list(Draft202012Validator(schema(name)["inputSchema"]).iter_errors(arguments))
    if errors:
        raise WorkflowError("invalid arguments: " + "; ".join(e.message for e in errors))
