"""Field and duplicate mutations over an ordered proposal trace."""

from __future__ import annotations

FORBIDDEN = set(":[]")
MAX_COUNT = 32
MAX_STEPS = 4096


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


def _count(value):
    if type(value) is int and not isinstance(value, bool):
        if value < 0 or value > MAX_COUNT:
            raise MutationError(f"duplicate count {value} is outside 0..{MAX_COUNT}")
        return value
    return 0


def validate_mutations(trace, mutations, model_steps):
    names = _names(trace)
    by_name = {item["name"]: item for item in trace}
    seen_fields = set()
    duplicate_spans = []
    for mutation in mutations or []:
        op = mutation.get("op")
        kind, *rest = selection(mutation.get("steps"), names)
        if op == "field":
            field = str(mutation.get("field") or "").strip()
            if not field:
                raise MutationError("field mutation needs a field name")
            if not str(mutation.get("suggestion") or "").strip():
                raise MutationError(f"field {field} needs a suggestion")
            if not str(mutation.get("evidence") or "").strip():
                raise MutationError(f"field {field} needs source evidence")
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
        elif op == "duplicate":
            if not str(mutation.get("count_param") or "").strip():
                raise MutationError("duplicate mutation needs count_param")
            if kind == "each":
                spans = [(index, index) for index in rest[0]]
            else:
                spans = [(rest[0], rest[1])]
            for span in spans:
                for previous in duplicate_spans:
                    if not (span[1] < previous[0] or previous[1] < span[0]):
                        raise MutationError("duplicate selections overlap")
                duplicate_spans.append(span)
        else:
            raise MutationError(f"unknown mutation op {op!r}")
    return by_name


def expand_trace(trace, mutations, counts):
    """Expand duplicate mutations. `counts` maps count_param to a runtime value."""

    names = _names(trace)
    expanded = [dict(item) for item in trace]
    # Apply from the right so earlier indexes stay valid.
    jobs = []
    for mutation in mutations or []:
        if mutation.get("op") != "duplicate":
            continue
        kind, *rest = selection(mutation.get("steps"), names)
        count = _count((counts or {}).get(mutation["count_param"], 0))
        if kind == "each":
            for index in rest[0]:
                jobs.append((index, index, count, False))
        else:
            jobs.append((rest[0], rest[1], count, True))
    for start, end, count, _block in sorted(jobs, key=lambda item: item[0], reverse=True):
        block = [dict(item) for item in expanded[start : end + 1]]
        extra = block * count
        expanded[end + 1 : end + 1] = extra
        if len(expanded) > MAX_STEPS:
            raise MutationError(f"expanded trace has {len(expanded)} steps; limit is {MAX_STEPS}")
    return expanded
