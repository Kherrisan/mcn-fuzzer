"""The thirteen agent contracts. Coordinator operations have no MCP schema."""

import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from rsc.protocol.contract import COLLECTIONS, DEFINITIONS, protocol_schema

from .store import StoreError as WorkflowError

STRING = {"type": "string", "minLength": 1}
INTEGER = {"type": "integer", "minimum": 0}
OBJECT = {"type": "object"}
STRINGS = {"type": "array", "items": STRING}
TOKEN = {"type": "integer", "minimum": 1, "maximum": 30000}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def array(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


REF = obj(
    {"kind": {"enum": [*COLLECTIONS, "unresolved"]}, "target": OBJECT, "scope": STRING},
    ["kind", "target"],
)
EVIDENCE = array(
    {"oneOf": [STRING, obj({"batch_id": STRING, "block_id": STRING}, ["batch_id", "block_id"])]}, 1
)
QUERY = obj({"kind": STRING, "filters": OBJECT}, ["kind", "filters"])
REQUIREMENT = obj(
    {
        "requirement": STRING,
        "objects": array(REF),
        "result": {"enum": ["pass", "finding"]},
        "findings": STRINGS,
    },
    ["requirement", "objects", "result", "findings"],
)
OBLIGATION = obj(
    {"description": STRING, "objects": array(REF), "evidence": EVIDENCE},
    ["description", "objects", "evidence"],
)
REPORT = obj(
    {
        "summary": STRING,
        "objects": array(REF),
        "requirements": array(REQUIREMENT),
        "obligations": array(OBLIGATION),
        "queries": array(QUERY),
    },
    ["summary", "objects"],
)
PAGING = {"cursor": INTEGER, "revision": INTEGER, "max_tokens": TOKEN}
FINDING = obj(
    {
        "name": STRING,
        "requirement": STRING,
        "description": STRING,
        "objects": array(REF),
        "evidence": EVIDENCE,
        "acceptance": STRING,
        "batch_id": STRING,
        "check_name": STRING,
    },
    ["name", "requirement", "description", "objects", "evidence", "acceptance"],
)
RESOLUTION = obj(
    {
        "name": STRING,
        "status": {"enum": ["fixed", "not_applicable", "unresolved"]},
        "description": STRING,
        "objects": array(REF),
        "evidence": EVIDENCE,
    },
    ["name", "status", "description", "objects", "evidence"],
)

READ = {"read_batches", "list_batches", "search_source", "query_model"}
PHASE_TOOLS = {
    "parse": READ | {"apply_changes", "complete_batch", "advance_phase"},
    "merge": READ | {"apply_changes", "get_changes", "advance_phase"},
    "review": READ
    | {
        "complete_batch",
        "submit_findings",
        "list_findings",
        "get_changes",
        "review_resolutions",
        "advance_phase",
    },
    "fix": READ
    | {"apply_changes", "list_findings", "get_changes", "submit_resolutions", "advance_phase"},
}
CONTRACTS = {
    "claim_assignment": (
        "Claim the named assignment and attempt supplied by the coordinator.",
        {"name": STRING, "attempt": {"type": "integer", "minimum": 1}},
        ["name", "attempt"],
    ),
    "advance_phase": (
        "Submit only your assignment after its gate passes. End this turn; "
        "the coordinator alone advances the global phase.",
        {},
        [],
    ),
    "list_batches": (
        "List immutable source ranges, ownership and your progress; no reading receipt is issued.",
        {**PAGING, "assigned_only": {"type": "boolean"}},
        [],
    ),
    "read_batches": (
        "Read complete named source batches in source order within max_tokens. "
        "Follow remaining; undelivered batches have no receipt. Supplementary reads "
        "do not transfer batch ownership. Cite segment.citation or batch_id/block_id.",
        {"batch_ids": array(STRING, 1), "max_tokens": TOKEN},
        ["batch_ids"],
    ),
    "search_source": (
        "Find supplementary context; search never replaces complete source reading.",
        {"query": STRING, "batch_ids": STRINGS, **PAGING},
        ["query"],
    ),
    "query_model": (
        "Read named records and evidence at a fixed revision. Default view is your "
        "draft, or the frozen review model. sealed excludes active drafts; conflicts "
        "lists merge decisions still needed. reports exposes immutable extraction reports "
        "with current name associations, only during merge. index omits definitions and does not "
        "count as full model delivery. Batch filtering includes the reference closure. "
        "Continue sealed pages with both revision and view_revision from the first page.",
        {
            "view": {"enum": ["current", "sealed", "index", "conflicts", "reports"]},
            "kind": STRING,
            "filters": OBJECT,
            "batch_ids": STRINGS,
            "scope": STRING,
            "check_name": STRING,
            "view_revision": {"type": "integer", "minimum": 1},
            **PAGING,
        },
        [],
    ),
    "get_changes": (
        "Read complete before/after images since a model or local draft revision.",
        {"from_revision": INTEGER, **PAGING},
        ["from_revision"],
    ),
    "apply_changes": (
        "Atomically apply changes=[named operations] with a unique operation_id. "
        "Targets are exact named value fields. Patch replaces supplied fields, including "
        "whole arrays; update replaces the complete value. Local forward references "
        "are allowed until merge. Only merge assignments may merge or rename scoped "
        "candidates; the server rewrites structured references and report mappings.",
        {"expected_revision": INTEGER, "operation_id": STRING, "changes": {}},
        ["expected_revision", "operation_id", "changes"],
    ),
    "complete_batch": (
        "Submit actual source extraction or independent review reports in assigned "
        "source order. Each source report uses its current attempt's receipt. "
        "Review reports include requirements, compared objects, outcomes and absence "
        "query scopes. Cross-range checks use check_name and the same report shape.",
        {
            "revision": INTEGER,
            "reports": array(
                {
                    "oneOf": [
                        obj(
                            {"batch_id": STRING, "receipt": STRING, "report": REPORT},
                            ["batch_id", "receipt", "report"],
                        ),
                        obj({"check_name": STRING, "report": REPORT}, ["check_name", "report"]),
                    ]
                },
                1,
            ),
        },
        ["revision", "reports"],
    ),
    "list_findings": (
        "Read assigned findings, resolutions and independent acceptance history.",
        PAGING,
        [],
    ),
    "submit_findings": (
        "Report source-backed discrepancies against the frozen revision. "
        "Names are local to your assignment; do not reuse a name for different content.",
        {"revision": INTEGER, "findings": array(FINDING, 1)},
        ["revision", "findings"],
    ),
    "submit_resolutions": (
        "Propose resolutions for your assigned findings; only a reviewer can accept them.",
        {"revision": INTEGER, "resolutions": array(RESOLUTION, 1)},
        ["revision", "resolutions"],
    ),
    "review_resolutions": (
        "Independently accept or reject fixes against the new frozen model and sources.",
        {
            "revision": INTEGER,
            "reviews": array(
                obj(
                    {
                        "name": STRING,
                        "accepted": {"type": "boolean"},
                        "reason": STRING,
                        "evidence": EVIDENCE,
                    },
                    ["name", "accepted", "reason", "evidence"],
                ),
                1,
            ),
        },
        ["revision", "reviews"],
    ),
}


def schema(name):
    description, properties, required = CONTRACTS[name]
    contract = obj(deepcopy(properties), required)
    if name == "apply_changes":
        definitions = protocol_schema()["$defs"]
        description += "\nValue shapes (field names and native JSON types):\n" + "\n".join(
            f"{kind}: {shape(definitions[definition], definitions)}"
            for kind, definition in {**DEFINITIONS, "unresolved": "issue"}.items()
        )
        contract["$defs"] = definitions
        variants = []
        for kind, definition in {**DEFINITIONS, "unresolved": "issue"}.items():
            for op in ("add", "update", "patch", "delete"):
                props = {"op": {"const": op}, "kind": {"const": kind}}
                if op != "add":
                    props["target"] = OBJECT
                if op == "delete":
                    props["reason"] = STRING
                else:
                    props["value"] = OBJECT if op == "patch" else {"$ref": f"#/$defs/{definition}"}
                    props["evidence"] = EVIDENCE
                props["scope"] = STRING
                variants.append(obj(props, set(props) - {"scope"}))
        variants.extend(
            [
                obj(
                    {
                        "op": {"const": "rename"},
                        "source": REF,
                        "name": STRING,
                        "evidence": EVIDENCE,
                    },
                    ["op", "source", "name", "evidence"],
                ),
                obj(
                    {
                        "op": {"const": "merge"},
                        "sources": array(REF, 1),
                        "value": OBJECT,
                        "evidence": EVIDENCE,
                    },
                    ["op", "sources", "value", "evidence"],
                ),
                obj(
                    {
                        "op": {"const": "resolve"},
                        "name": STRING,
                        "decision": STRING,
                        "evidence": EVIDENCE,
                    },
                    ["op", "name", "decision", "evidence"],
                ),
            ]
        )
        contract["properties"]["changes"] = array({"oneOf": variants}, 1)
    return {"name": name, "description": description, "inputSchema": contract}


def shape(node, definitions):
    """Short code-mode help, derived from the same schema as validation."""
    if "$ref" in node:
        return shape(definitions[node["$ref"].rsplit("/", 1)[1]], definitions)
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(value) for value in node["enum"])
    if "oneOf" in node or "anyOf" in node:
        return " | ".join(shape(item, definitions) for item in node.get("oneOf", node.get("anyOf")))
    if node.get("type") == "array":
        return f"Array<{shape(node['items'], definitions)}>"
    if node.get("type") == "object":
        return (
            "{"
            + ",".join(
                f"{key}{'' if key in node.get('required', []) else '?'}:{shape(value, definitions)}"
                for key, value in node.get("properties", {}).items()
            )
            + "}"
        )
    return node.get("type", "object")


def validate_arguments(name, arguments):
    if name not in CONTRACTS:
        raise WorkflowError("unknown agent tool")
    contract = schema(name)["inputSchema"]
    # Select the declared operation/kind branch for precise row diagnostics;
    # validating every other kind's oneOf branch obscures the actual problem.
    if (
        name == "apply_changes"
        and isinstance(arguments, dict)
        and isinstance(arguments.get("changes"), list)
    ):
        variants = contract["properties"]["changes"]["items"]["oneOf"]
        errors = []
        base = deepcopy(contract)
        base["properties"]["changes"]["items"] = OBJECT
        errors.extend(Draft202012Validator(base).iter_errors(arguments))
        messages = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]
        for i, row in enumerate(arguments["changes"]):
            if not isinstance(row, dict):
                continue
            branch = next(
                (
                    v
                    for v in variants
                    if v["properties"]["op"]["const"] == row.get("op")
                    and (
                        "kind" not in v["properties"]
                        or v["properties"]["kind"]["const"] == row.get("kind")
                    )
                ),
                None,
            )
            if branch is None:
                messages.append(f"changes[{i}]: unknown operation or kind")
                continue
            check = deepcopy({**branch, "$defs": contract["$defs"]})
            if row.get("op") in {"add", "update"} and row.get("kind") == "steps":
                from .model import record_schema

                check["properties"]["value"] = record_schema("steps", row.get("value"))
            messages.extend(
                f"changes[{i}]{e.json_path.removeprefix('$')}: {e.message}"
                for e in Draft202012Validator(check).iter_errors(row)
            )
    else:
        messages = [
            f"{'/'.join(map(str, e.absolute_path)) or name}: {e.message}"
            for e in Draft202012Validator(contract).iter_errors(arguments)
        ]
    if messages:
        raise WorkflowError("invalid arguments: " + "; ".join(messages))
