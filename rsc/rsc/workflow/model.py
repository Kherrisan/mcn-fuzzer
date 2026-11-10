"""Protocol structure, reference and endpoint gates for workflow drafts."""

from jsonschema import Draft202012Validator

from rsc.protocol.contract import DEFINITIONS, protocol_schema
from rsc.protocol.references import reference_errors as reference_errors


def record_schema(kind, value):
    schema = protocol_schema()
    definition = ({**DEFINITIONS, "unresolved": "issue"})[kind]
    if (
        kind == "steps"
        and isinstance(value, dict)
        and value.get("action")
        in {
            "send",
            "listen",
            "connect",
            "close",
        }
    ):
        definition = value["action"] + "_step"
    return {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}


def validate_record(kind, value):
    check = Draft202012Validator(record_schema(kind, value))
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or 'value'}: {e.message}"
        for e in check.iter_errors(value)
    ]
    if errors:
        raise ValueError("; ".join(errors))
