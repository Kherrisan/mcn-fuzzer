"""Field mutations over an ordered proposal trace."""

from __future__ import annotations

FORBIDDEN = set(":[]")
FIELD_KINDS = ("string", "bytes", "integer", "boolean", "list")
BYTES_ENCODINGS = ("hex", "base64")


class MutationError(ValueError):
    pass


def _names(trace):
    names = []
    for item in trace:
        name = str(item.get("name") or "")
        if not name or any(char in name for char in FORBIDDEN):
            raise MutationError(f"instance name {name!r} must not contain :[]")
        if name in names:
            raise MutationError(f"duplicate instance name {name}")
        names.append(name)
    return names


def selection(spec, names):
    """Return ('each', [indexes]) or ('range', start, end) inclusive."""

    if isinstance(spec, list):
        indexes = []
        for name in spec:
            if name not in names:
                raise MutationError(f"unknown instance {name}")
            indexes.append(names.index(name))
        if len(indexes) != len(set(indexes)):
            raise MutationError("a mutation repeats an instance")
        return ("each", indexes)
    if isinstance(spec, str):
        text = spec.strip()
        if not (text.startswith("[") and text.endswith("]") and ":" in text):
            raise MutationError(f"selector {spec!r} must be an array or [start:end]")
        start_name, end_name = (part.strip() for part in text[1:-1].split(":", 1))
        if start_name not in names or end_name not in names:
            raise MutationError(f"unknown range endpoint in {spec}")
        start, end = names.index(start_name), names.index(end_name)
        if start > end:
            raise MutationError(f"range {spec} is reversed")
        return ("range", start, end)
    raise MutationError("mutation steps must be an array or [start:end]")


def field_type(item):
    """Validate and return (kind, encoding) for one mutable field."""
    kind = str(item.get("kind") or "string").strip()
    if kind not in FIELD_KINDS:
        raise MutationError(
            f"unknown field kind {kind!r}; expected one of {', '.join(FIELD_KINDS)}"
        )
    encoding = item.get("encoding")
    if kind == "bytes":
        encoding = str(encoding or "hex").strip()
        if encoding not in BYTES_ENCODINGS:
            raise MutationError(
                f"unknown bytes encoding {encoding!r}; expected hex or base64"
            )
    else:
        if encoding not in (None, ""):
            raise MutationError("encoding is only valid for bytes fields")
        encoding = None
    return kind, encoding


def validate_mutations(trace, mutations, model_steps):
    names = _names(trace)
    by_name = {item["name"]: item for item in trace}
    seen_fields = set()
    for mutation in mutations or []:
        op = mutation.get("op")
        if op != "field":
            raise MutationError(f"unknown mutation op {op!r}; only field mutations are supported")
        kind, *rest = selection(mutation.get("steps"), names)
        field = str(mutation.get("field") or "").strip()
        if not field:
            raise MutationError("field mutation needs a field name")
        if not str(mutation.get("suggestion") or "").strip():
            raise MutationError(f"field {field} needs a suggestion")
        if not str(mutation.get("evidence") or "").strip():
            raise MutationError(f"field {field} needs source evidence")
        field_type(mutation)
        indexes = rest[0] if kind == "each" else list(range(rest[0], rest[1] + 1))
        for index in indexes:
            instance = trace[index]
            step = model_steps[instance["step"]]
            if step.get("action", "send") != "send":
                raise MutationError(f"field mutation on non-send instance {instance['name']}")
            key = (instance["name"], field)
            if key in seen_fields:
                raise MutationError(f"field {field} is repeated on {instance['name']}")
            seen_fields.add(key)
    return by_name
