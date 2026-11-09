"""Protocol structure, reference and endpoint gates for workflow drafts."""

from jsonschema import Draft202012Validator

from rsc.protocol.contract import DEFINITIONS, protocol_schema
from rsc.protocol.references import reference_errors as reference_errors


def validate_record(kind, value):
    schema = protocol_schema()
    check = Draft202012Validator(
        {
            "$ref": f"#/$defs/{({**DEFINITIONS, 'unresolved': 'issue'})[kind]}",
            "$defs": schema["$defs"],
        }
    )
    errors = [e.message for e in check.iter_errors(value)]
    if errors:
        raise ValueError("; ".join(errors))
