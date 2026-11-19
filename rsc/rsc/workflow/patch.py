"""Apply Fix operations against the immutable assignment input."""

from collections import defaultdict

from rsc.protocol.contract import COLLECTIONS, record_identity

from .store import dumps
from .tools import EVIDENCE, STRING, SubmissionError, array, obj, validate_shape

VALUE = {"type": "object"}
KIND = {"enum": list(COLLECTIONS)}
PATCH = obj(
    {
        "base": STRING,
        "changes": array(
            {
                "oneOf": [
                    obj({"op": {"const": op}, "kind": KIND, **fields}, ["op", "kind", *fields])
                    for op, fields in (
                        ("add", {"value": VALUE, "evidence": EVIDENCE}),
                        ("replace", {"match": VALUE, "value": VALUE, "evidence": EVIDENCE}),
                        ("remove", {"match": VALUE}),
                    )
                ],
            }
        ),
        "unresolved": array(VALUE),
    },
    ["base", "changes"],
)


def key(kind, value):
    return record_identity(kind, value) or (kind, dumps(value))


def apply_patch(base, patch, base_path):
    validate_shape(PATCH, patch)
    if patch["base"] != base_path:
        raise ValueError("base must match this Fix assignment's candidate_path")
    indexes = defaultdict(list)
    for index, item in enumerate(base["items"]):
        indexes[key(item["kind"], item["value"])].append(index)

    replacements, additions, errors = {}, [], []
    for index, change in enumerate(patch["changes"]):
        op, kind = change["op"], change["kind"]
        item = (
            {"kind": kind, "value": change["value"], "evidence": change["evidence"]}
            if op != "remove"
            else None
        )
        if op == "add":
            additions.append(item)
            continue
        match = change["match"]
        if kind != "step_dependencies":
            fields = {"name", "party_name"} if kind == "states" else {"name"}
            if set(match) != fields or not all(isinstance(v, str) and v for v in match.values()):
                errors.append(f"changes[{index}].match: expected {', '.join(sorted(fields))}")
                continue
        matches = indexes.get(key(kind, match), [])
        if len(matches) != 1:
            errors.append(f"changes[{index}].match: expected one base item, found {len(matches)}")
        elif matches[0] in replacements:
            errors.append(f"changes[{index}].match: base item already modified")
        else:
            replacements[matches[0]] = item
    if errors:
        raise SubmissionError(
            [
                {
                    "path": error.split(": ", 1)[0],
                    "code": "patch_match",
                    "message": error.split(": ", 1)[1],
                }
                for error in errors
            ]
        )
    items = [replacements.get(index, item) for index, item in enumerate(base["items"])]
    return {
        "items": [item for item in items if item is not None] + additions,
        "unresolved": patch.get("unresolved", base["unresolved"]),
    }
