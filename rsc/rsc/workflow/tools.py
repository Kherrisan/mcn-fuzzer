"""Six result-oriented agent contracts; scheduling is entirely server-owned."""

import json

from jsonschema import Draft202012Validator

from rsc.protocol.contract import COLLECTIONS, DEFINITIONS, protocol_schema

STRING = {"type": "string", "minLength": 1}
OBJECT = {"type": "object"}
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
    {"id": STRING, "kind": {"enum": list(COLLECTIONS)}, "value": OBJECT, "evidence": EVIDENCE},
    ["kind", "value", "evidence"],
)
DEPENDENCY = obj({"id": STRING, "value": OBJECT, "evidence": EVIDENCE}, ["id", "value", "evidence"])
TEXT_UPDATE = obj(
    {
        "id": STRING,
        "path": array({"type": ["string", "integer"]}, 1),
        "before": {"type": "string"},
        "after": {"type": "string"},
        "evidence": EVIDENCE,
    },
    ["id", "path", "before", "after", "evidence"],
)
PAGING = {
    "cursor": INTEGER,
    "revision": INTEGER,
    "max_tokens": {"type": "integer", "minimum": 1, "maximum": 30000},
}
READ = {"read_source_batches", "search_source", "query_model"}
PHASE_TOOLS = {
    "parse": READ | {"submit_parse_result"},
    "cluster": READ | {"submit_clustering_result"},
    "merge": READ | {"submit_merge_result"},
}
CONTRACTS = {
    "read_source_batches": (
        "Read complete source batches. Follow remaining. No receipt or reading report is required.",
        {"batch_ids": array(STRING, 1), "max_tokens": PAGING["max_tokens"]},
        ["batch_ids"],
    ),
    "search_source": (
        "Locate RFC text and supplementary context; returned rows identify source batches.",
        {"query": STRING, "batch_ids": array(STRING), **PAGING},
        ["query"],
    ),
    "query_model": (
        "Query own Parse items, frozen merge inputs, group membership or the checked candidate. "
        "Use references=[IDs] to find incoming references. The index contains only named items. "
        "References expose bound item IDs; unresolved references are explicit. Follow next_cursor.",
        {
            "view": {"enum": ["items", "index", "groups", "candidate"]},
            "ids": array(STRING),
            "references": array(STRING, 1),
            "kind": {"enum": list(COLLECTIONS)},
            "name": STRING,
            **PAGING,
        },
        [],
    ),
    "submit_parse_result": (
        "Submit actual items for the next assigned source batch. Omit id for new items; "
        "use returned IDs for complete updates of your existing items. References accept item IDs "
        "or unique local names. Server saves results and advances automatically. "
        "Retry identical payloads.",
        {"batch_id": STRING, "items": array(ITEM)},
        ["batch_id", "items"],
    ),
    "submit_clustering_result": (
        "Submit a complete partition of all named frozen items into same-kind concept groups. "
        "Each item ID appears exactly once, including singleton groups; exclude step_dependencies.",
        {"groups": array(array(STRING, 1))},
        ["groups"],
    ),
    "submit_merge_result": (
        "Submit one group's complete canonical value and source evidence. "
        "Server retains its smallest "
        "item ID and rewrites references. Dependencies may update only edges whose dependent step "
        "belongs to this group. Text updates are explicit compare-and-set proposals. "
        "If clustering is wrong, submit regroup_reason instead of value/evidence.",
        {
            "group_name": STRING,
            "value": OBJECT,
            "evidence": EVIDENCE,
            "dependencies": array(DEPENDENCY),
            "text_updates": array(TEXT_UPDATE),
            "regroup_reason": STRING,
        },
        ["group_name"],
    ),
}


def schema(name):
    description, fields, required = CONTRACTS[name]
    contract = obj(fields, required)
    if name.startswith("submit_") and name != "submit_clustering_result":
        definitions = protocol_schema()["$defs"]
        description += "\nProtocol value shapes: " + "; ".join(
            f"{kind}: {shape(definitions[definition], definitions)}"
            for kind, definition in DEFINITIONS.items()
        )
    if name == "submit_merge_result":
        contract["oneOf"] = [
            {"required": ["value", "evidence"], "not": {"required": ["regroup_reason"]}},
            {
                "required": ["regroup_reason"],
                "not": {
                    "anyOf": [
                        {"required": [k]}
                        for k in ("value", "evidence", "dependencies", "text_updates")
                    ]
                },
            },
        ]
    return {"name": name, "description": description, "inputSchema": contract}


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


def validate_arguments(name, arguments):
    errors = [
        f"{e.json_path}: {e.message}"
        for e in Draft202012Validator(schema(name)["inputSchema"]).iter_errors(arguments)
    ]
    if errors:
        raise ValueError("invalid arguments: " + "; ".join(errors))
