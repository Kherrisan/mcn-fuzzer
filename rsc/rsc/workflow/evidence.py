"""Short source citations expand to canonical evidence before normal validation."""

from copy import deepcopy


def source_citations(batches):
    result, seen = {}, set()
    for batch in batches:
        for segment in batch["segments"]:
            pair = (batch["batch_id"], segment["block_id"])
            if pair not in seen:
                seen.add(pair)
                result[f"e{len(result) + 1}"] = {"batch_id": pair[0], "block_id": pair[1]}
    return result


def expand_evidence(entries, citations, *, path="evidence"):
    if not isinstance(entries, list):
        raise ValueError(f"{path} must be an array")
    result = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            if entry not in citations:
                raise ValueError(
                    f"{path}[{index}] unknown citation {entry!r}; copy citation from a source read"
                )
            result.append(deepcopy(citations[entry]))
        elif (
            isinstance(entry, dict)
            and set(entry) == {"batch_id", "block_id"}
            and all(isinstance(v, str) and v for v in entry.values())
        ):
            result.append(deepcopy(entry))
        else:
            raise ValueError(f"{path}[{index}] requires a citation or batch_id/block_id strings")
    return result


def validate_evidence(entries, batches, *, delivered=None):
    """Validate canonical source pairs; live calls and audit replay share this check."""
    if not entries:
        raise ValueError("source evidence is required")
    errors = []
    for entry in entries:
        batch = batches.get(entry.get("batch_id"))
        if batch is None or entry.get("block_id") not in {s["block_id"] for s in batch["segments"]}:
            errors.append(f"invalid source pair {entry!r}")
        elif delivered:
            try:
                delivered(entry["batch_id"])
            except ValueError as error:
                errors.append(str(error))
    if errors:
        raise ValueError("; ".join(errors))
