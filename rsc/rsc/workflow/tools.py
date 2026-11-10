"""MCP tool contracts shared by the socket service and stdio bridge."""

import json
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
EVIDENCE = {
    "type": "array",
    "minItems": 1,
    "items": {
        "anyOf": [
            {"type": "string", "minLength": 1},
            {
                "type": "object",
                "properties": {"batch_id": STRING, "block_id": STRING},
                "required": ["batch_id", "block_id"],
                "additionalProperties": False,
            },
        ],
    },
}
REPORT_VALUE = {
    "type": "object",
    "properties": {
        "summary": STRING,
    },
    "required": ["summary"],
    "additionalProperties": True,
}
REPORT = {
    "type": "object",
    "properties": {"batch_id": STRING, "receipt_id": STRING, "report": REPORT_VALUE},
    "required": ["batch_id", "receipt_id", "report"],
    "additionalProperties": False,
}
STRING_ARRAY = {"type": "array", "items": STRING}
JSON_VALUE = {
    "anyOf": [{"type": kind} for kind in ("string", "number", "boolean", "null", "object", "array")]
}
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
        "List source batches and pending work; follow next_cursor and resend revision. "
        "detail=true includes this dispatch's already-issued receipt_id; listing issues no "
        "receipt and completes no report. Complete source bodies remain required. "
        "With filter={pending:true}, completing reports changes the filtered collection; "
        "restart cursor=0 for the next work window after reporting the delivered one.",
        {
            "filter": OBJECT,
            "cursor": INTEGER,
            "max_tokens": TOKEN,
            "revision": INTEGER,
            "detail": {"type": "boolean"},
        },
        [],
    ),
    "read_batch": (
        "Read the COMPLETE source batch and receive its delivery receipt.",
        {"batch_id": STRING},
        ["batch_id"],
    ),
    "read_batches": (
        "Read complete batches in source order until max_tokens (default 24000). "
        "Nonadjacent assigned batches are allowed; prefer segment.citation for evidence. "
        "format=compact uses lossless segments={encoding:columns-v1,groups:[{columns,rows,"
        "derive_evidence}]}; each row maps columns to values, and derive_evidence adds "
        "{batch_id:parent batch_id,block_id:row block_id}. No source text is omitted. "
        "Every segment has a short citation such as e42. Copy these directly as "
        'evidence:["e42"] in changes, findings and resolutions; the server expands them '
        "to exact batch/block pairs and still requires current-dispatch receipts. "
        "include_evidence=false omits only the redundant per-segment evidence object; "
        "all original named source fields and citations remain. The canonical pair is "
        "{batch_id:parent batch_id,block_id:segment block_id}. Default true retains legacy output. "
        "Undelivered batches get no receipt.",
        {
            "batch_ids": STRING_ARRAY,
            "max_tokens": TOKEN,
            "format": FORMAT,
            "include_evidence": {"type": "boolean"},
        },
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
            "batch_ids": STRING_ARRAY,
            "detail": {"type": "boolean"},
            "cursor": INTEGER,
            "format": FORMAT,
            "max_tokens": TOKEN,
            "revision": INTEGER,
        },
        [],
    ),
    "apply_changes": (
        "Atomically add/update/patch/delete records. Add requires kind,value,evidence; "
        "Edits select a record by kind plus target fields (prefer target:{name:...}), or legacy "
        "record_id. target must match EXACTLY one record in the transaction's starting model. "
        "Dependencies can use target:{prior_step:...,dependent_step:...}; "
        "add fields if ambiguous. "
        "Never guess opaque IDs or combine target with record_id. Delete also needs reason. "
        "Update replaces the COMPLETE value. Prefer patch for a few changed fields: "
        "patch needs a selector,nonempty partial value,evidence, shallow-merges fields "
        "into the existing value. Retain prior evidence plus the supplied current-source evidence. "
        "Arrays replace the whole array. Merged records pass the same complete validation. "
        'Use condition:"" for an unconditional ordering edge or a step with no intrinsic guard; '
        "the edge/action already supplies the order/action. Preserve real branch guards and "
        "comparisons instead of repeating ordering or message descriptions. "
        'Evidence is a list of source citation strings (e.g. ["e42"]) or legacy '
        "{batch_id,block_id} objects. Copy citations from source reads; do not guess pairs. "
        "Internal record IDs are allocated by the server; step names are "
        "immutable on update. Use the current revision "
        "and a unique operation_id. Reusing the same operation with the same content is safe. "
        "For kind=unresolved, value has description and object_names, and no name field. "
        "Result includes draft_reference_errors; field_relations require two send steps and exact "
        "associated_fields names on their messages. Network ordering uses empty field_relations "
        "and endpoint binding uses channel.endpoint_sources.",
        {"expected_revision": INTEGER, "operation_id": STRING, "changes": OBJECT_ARRAY},
        ["expected_revision", "operation_id", "changes"],
    ),
    "complete_batch": (
        "Complete parsing batches in one atomic call using batches=[{batch_id,receipt_id,report}]. "
        "Single-batch arguments remain supported. Submission order is unrestricted. "
        "Each batch needs its own receipt and a report with summary. "
        "record_ids and finding_ids are optional; use cached ledgers when supplying them. "
        "Explicitly justify no relevant information.",
        {
            "batch_id": STRING,
            "receipt_id": STRING,
            "report": REPORT_VALUE,
            "batches": {"type": "array", "items": REPORT, "minItems": 1},
        },
        [],
    ),
    "list_findings": (
        "Read findings, resolutions and reviewer decisions, following pagination.",
        {"filters": OBJECT, "cursor": INTEGER, "max_tokens": TOKEN, "revision": INTEGER},
        [],
    ),
    "get_changes": (
        "Read every model-diff page in later reviews (round > 1) from review_base to the "
        "current revision. Initial review uses the full current snapshot, without historical "
        "diffs. Each change keeps its own before and after.",
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
        "Submit findings with description, evidence (prefer source citation strings; "
        "legacy {batch_id,block_id} objects remain accepted), "
        "optional record_ids and suggestion. Cannot edit the model.",
        {"reviewed_revision": INTEGER, "findings": OBJECT_ARRAY},
        ["reviewed_revision", "findings"],
    ),
    "complete_review_batch": (
        "Atomically record source/model comparisons using batches=[{batch_id,receipt_id,report}], "
        "or single-batch arguments. Submission order is unrestricted. "
        "Require receipt and report.summary; "
        "include finding_ids or explicitly report no findings.",
        {
            "batch_id": STRING,
            "receipt_id": STRING,
            "report": REPORT_VALUE,
            "batches": {"type": "array", "items": REPORT, "minItems": 1},
        },
        [],
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


def _value_shape(value, definitions):
    """Keep nested value types visible when code-mode discovery drops $defs."""
    if "$ref" in value:
        return _value_shape(definitions[value["$ref"].rsplit("/", 1)[-1]], definitions)
    for union in ("anyOf", "oneOf"):
        if union in value:
            return " | ".join(_value_shape(item, definitions) for item in value[union])
    if "const" in value:
        return json.dumps(value["const"])
    if "enum" in value:
        return " | ".join(json.dumps(item) for item in value["enum"])
    kind = value.get("type")
    if kind == "array":
        return "Array<" + _value_shape(value.get("items", {}), definitions) + ">"
    if kind == "object":
        required = value.get("required", [])
        return (
            "{"
            + ",".join(
                key + ("" if key in required else "?") + ":" + _value_shape(item, definitions)
                for key, item in value.get("properties", {}).items()
            )
            + "}"
        )
    return "number" if kind == "integer" else kind or "JSON"


def schema(name):
    description, properties, required = CONTRACTS[name]
    properties = deepcopy(properties)
    if name == "apply_changes":
        definitions = deepcopy(protocol_schema()["$defs"])
        shapes = {**DEFINITIONS, "unresolved": "issue"}
        description += (
            " Complete value shapes (closed objects; patches select fields; ? means optional): "
            + "; ".join(
                kind + ":" + _value_shape(definitions[definition], definitions)
                for kind, definition in shapes.items()
            )
            + "."
        )
        values = []
        for definition in [*DEFINITIONS.values(), "issue"]:
            values.append({"$ref": f"#/$defs/{definition}"})
        properties["changes"]["items"] = {
            "type": "object",
            "properties": {
                "op": {"enum": ["add", "update", "patch", "delete"]},
                "kind": {"enum": [*DEFINITIONS, "unresolved"]},
                "record_id": STRING,
                "target": {"type": "object", "minProperties": 1},
                "value": {"type": "object"},
                "evidence": EVIDENCE,
                "reason": STRING,
            },
            "required": ["op"],
            "additionalProperties": False,
            "allOf": [
                {
                    "if": {"properties": {"op": {"const": "patch"}}, "required": ["op"]},
                    "then": {
                        "required": ["value", "evidence"],
                        "properties": {"value": {"type": "object", "minProperties": 1}},
                    },
                    "else": {"properties": {"value": {"anyOf": values}}},
                },
                {
                    "if": {
                        "properties": {"op": {"enum": ["update", "patch", "delete"]}},
                        "required": ["op"],
                    },
                    "then": {"oneOf": [{"required": ["record_id"]}, {"required": ["target"]}]},
                },
                {
                    "if": {"required": ["target"]},
                    "then": {
                        "required": ["kind"],
                        "properties": {"op": {"enum": ["update", "patch", "delete"]}},
                    },
                },
            ],
        }
    if name == "apply_changes":
        expanded = properties["changes"]
        named_row = {
            "type": "object",
            "properties": deepcopy(expanded["items"]["properties"]),
            "additionalProperties": False,
        }
        # Defaults can supply value fields, so rows need not carry complete values.
        # Keep metadata visible to code-mode clients without weakening expansion checks.
        named_row["properties"]["evidence"]["items"]["anyOf"].append(INTEGER)
        table = {
            "type": "object",
            "required": ["groups"],
            "additionalProperties": False,
            "properties": {
                "evidence": {"type": "array", "items": EVIDENCE["items"]},
                "groups": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["op", "rows"],
                        "additionalProperties": False,
                        "properties": {
                            "op": {"enum": ["add", "update", "patch", "delete"]},
                            "kind": {"enum": [*DEFINITIONS, "unresolved"]},
                            "defaults": {"type": "object"},
                            "columns": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": STRING,
                            },
                            "rows": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "anyOf": [
                                        named_row,
                                        {
                                            "type": "array",
                                            "items": {
                                                "anyOf": [
                                                    *deepcopy(JSON_VALUE["anyOf"][:-1]),
                                                    {
                                                        "type": "array",
                                                        "items": deepcopy(JSON_VALUE),
                                                    },
                                                ]
                                            },
                                        },
                                    ]
                                },
                            },
                        },
                    },
                },
            },
        }
        properties["changes"] = {"oneOf": [expanded, table]}
        description += (
            " Compact changes={evidence?:[{batch_id,block_id}],"
            "groups:[{op,kind?,defaults?,columns?,rows}]}. "
            'Prefer named rows {value:{...},evidence:["e42"],target?:{name:...}} to avoid '
            "positional counts. defaults contains shared VALUE fields, shallow-merged with each "
            "row.value. Rows override defaults. A target selector requires kind and must match "
            "exactly one existing record; record_id remains supported, never combine both. "
            "Legacy array rows require columns; @record_id/@target and @reason are metadata, "
            "@evidence accepts source citation strings directly; no evidence pool is then needed. "
            "Integer evidence-pool indexes remain supported. Add groups require kind. "
            "Cells keep native JSON types including nested objects/arrays and null. "
            "Example group: {op:add,kind:steps,defaults:{action:send,channel_name:control,"
            'sender_name:client,receiver_name:server,condition:""},'
            'rows:[{value:{name:send_HELLO,message_name:HELLO},evidence:["e42"]}]}. '
            "Expanded arrays remain supported. "
            "Returned record_ids follow group/row order."
        )
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
    if name in {"complete_batch", "complete_review_batch"}:
        forms = [("batches",), ("batch_id", "receipt_id", "report")]
        fields = set().union(*forms)
        result["inputSchema"]["oneOf"] = [
            {
                "required": list(form),
                "not": {"anyOf": [{"required": [field]} for field in sorted(fields - set(form))]},
            }
            for form in forms
        ]
    if name in {"submit_findings", "submit_resolutions"}:
        key = "findings" if name == "submit_findings" else "resolutions"
        result["inputSchema"]["properties"][key]["items"] = {
            "type": "object",
            "properties": {"evidence": EVIDENCE},
        }
    return result


def validate_arguments(name, arguments):
    if name not in CONTRACTS:
        raise WorkflowError("unknown tool")
    contract = schema(name)["inputSchema"]
    if name == "apply_changes" and isinstance(arguments, dict):
        branches = contract["properties"]["changes"]["oneOf"]
        if isinstance(arguments.get("changes"), list):
            contract["properties"]["changes"] = branches[0]
        elif isinstance(arguments.get("changes"), dict):
            contract["properties"]["changes"] = branches[1]
    errors = list(Draft202012Validator(contract).iter_errors(arguments))
    if errors:
        messages = []
        for error in errors:
            path = list(error.absolute_path)
            if (
                name == "apply_changes"
                and error.validator == "anyOf"
                and len(path) == 5
                and path[:2] == ["changes", "groups"]
                and path[3] == "rows"
                and isinstance(error.instance, dict)
            ):
                # Report the named-row branch, not a whole-row dump plus array errors.
                messages.extend(
                    f"{'/'.join(map(str, item.absolute_path))}: {item.message}"
                    for item in error.context
                    if item.schema_path[0] == 0
                )
                continue
            if name == "apply_changes" and error.validator == "anyOf" and len(path) == 3:
                change = arguments["changes"][path[1]]
                kind = change.get("kind")
                definition = {**DEFINITIONS, "unresolved": "issue"}.get(kind)
                if definition:
                    contract = protocol_schema()["$defs"]
                    if definition == "step" and isinstance(change.get("value"), dict):
                        action = change.get("value", {}).get("action")
                        if isinstance(action, str) and action in {
                            "send",
                            "listen",
                            "connect",
                            "close",
                        }:
                            definition = action + "_step"
                    check = Draft202012Validator(
                        {"$ref": f"#/$defs/{definition}", "$defs": contract}
                    )
                    messages.extend(
                        f"changes[{path[1]}].value"
                        + "".join(
                            f"[{part}]" if isinstance(part, int) else f".{part}"
                            for part in item.absolute_path
                        )
                        + f" kind={kind}: {item.message}"
                        for item in check.iter_errors(change["value"])
                    )
                    continue
            messages.append(f"{'/'.join(map(str, error.absolute_path)) or name}: {error.message}")
        raise WorkflowError("invalid arguments: " + "; ".join(messages))
