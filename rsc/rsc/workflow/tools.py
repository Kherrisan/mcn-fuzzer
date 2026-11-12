"""Five file-oriented tools; scheduling and feedback rounds belong to the service."""

import json

from jsonschema import Draft202012Validator

from rsc.protocol.contract import COLLECTIONS, DEFINITIONS, protocol_schema

STRING = {"type": "string", "minLength": 1}
INTEGER = {"type": "integer", "minimum": 0}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def array(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


EVIDENCE = array(obj({"batch_id": STRING, "block_id": STRING}, ["batch_id", "block_id"]), 1)
ITEM = obj(
    {"kind": {"enum": list(COLLECTIONS)}, "value": {"type": "object"}, "evidence": EVIDENCE},
    ["kind", "value", "evidence"],
)
CHECKPOINT = obj(
    {"items": array(ITEM), "unresolved": array({"type": "object"})}, ["items", "unresolved"]
)
PARSE_CHECKPOINT = obj(
    {
        **CHECKPOINT["properties"],
        "complete": {"type": "boolean"},
        "completed_batches": array(STRING),
    },
    ["items", "unresolved"],
)
FINDING = obj(
    {
        "category": {"enum": ["correctable", "source_ambiguity", "external_reference"]},
        "description": STRING,
        "batch_ids": array(STRING, 1),
        "global": {"type": "boolean"},
    },
    ["category", "description", "batch_ids", "global"],
)
REVIEW = obj(
    {"reviewed_batches": array(STRING, 1), "findings": array(FINDING),
     "complete": {"type": "boolean"}},
    ["reviewed_batches", "findings"],
)
PAGING = {"cursor": INTEGER, "max_tokens": {"type": "integer", "minimum": 1, "maximum": 30000}}
READ = {"read_source_batches", "query_model"}
PHASE_TOOLS = {
    "parse": READ | {"submit_parse_result"},
    "merge": READ | {"submit_model_result"},
    "fix": READ | {"submit_model_result"},
    "review": READ | {"submit_review_result"},
}
CONTRACTS = {
    "read_source_batches": (
        "Read complete immutable source batches. Follow remaining; read all "
        "assigned ranges using this tool or source files.",
        {"batch_ids": array(STRING, 1), "max_tokens": PAGING["max_tokens"]},
        ["batch_ids"],
    ),
    "query_model": (
        "Read frozen parser results or candidate. Follow next_cursor. Names are "
        "scoped to their input file. "
        "Agents may also read the complete input files directly.",
        {
            "view": {"enum": ["items", "index", "candidate", "inputs"]},
            "kind": {"enum": list(COLLECTIONS)},
            "name": STRING,
            "references": array(STRING, 1),
            **PAGING,
        },
        [],
    ),
    "submit_parse_result": (
        "Submit the path of your complete JSON checkpoint covering processed source ranges. "
        "File shape: "
        "{items:[{kind,value,evidence:[{batch_id,block_id}]}],"
        "unresolved:[{description,object_names}]}. "
        "For progress set complete=false and completed_batches=[fully processed "
        "batches]; final complete=true covers all assigned batches. Use names for "
        "objects and references; dependencies have no name. Write under "
        "work/<worker>/.",
        {"path": STRING},
        ["path"],
    ),
    "submit_model_result": (
        "Submit the path of a complete merged or fixed checkpoint, same file shape as Parse. "
        "Preserve all valid source evidence. Do not submit patches. Write under work/<worker>/.",
        {"path": STRING},
        ["path"],
    ),
    "submit_review_result": (
        "Submit path to {reviewed_batches:[batch "
        "names],findings:[{category,description,batch_ids,global}]}. "
        "Categories: correctable, source_ambiguity, external_reference. Review "
        "source and frozen model, "
        "never edit the candidate. Findings must identify affected source ranges; "
        "global=true needs full re-review.",
        {"path": STRING},
        ["path"],
    ),
}


def schema(name):
    description, fields, required = CONTRACTS[name]
    if name in {"submit_parse_result", "submit_model_result"}:
        definitions = protocol_schema()["$defs"]
        description += "\nProtocol value shapes: " + "; ".join(
            f"{kind}: {shape(definitions[definition], definitions)}"
            for kind, definition in DEFINITIONS.items()
        )
    return {"name": name, "description": description, "inputSchema": obj(fields, required)}


def shape(node, definitions):
    if "$ref" in node:
        return shape(definitions[node["$ref"].rsplit("/", 1)[1]], definitions)
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(value) for value in node["enum"])
    if "oneOf" in node or "anyOf" in node:
        return " | ".join(shape(n, definitions) for n in node.get("oneOf", node.get("anyOf")))
    if node.get("type") == "array":
        return "[" + shape(node["items"], definitions) + "]"
    if node.get("type") == "object":
        return (
            "{"
            + ", ".join(
                f"{k}: {shape(v, definitions)}" for k, v in node.get("properties", {}).items()
            )
            + "}"
        )
    types = node.get("type", "any")
    return " | ".join(types) if isinstance(types, list) else types


def validate_shape(contract, value):
    errors = [
        f"{e.json_path}: {e.message}" for e in Draft202012Validator(contract).iter_errors(value)
    ]
    if errors:
        raise ValueError("invalid content: " + "; ".join(errors))


def validate_arguments(name, arguments):
    validate_shape(schema(name)["inputSchema"], arguments)
