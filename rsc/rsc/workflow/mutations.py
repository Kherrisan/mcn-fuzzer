"""Named draft edits shared by Parse and Fix; no transport or database effects."""

from copy import deepcopy

from rsc.protocol.contract import COLLECTIONS

from .model import reference_errors, validate_record
from .sources import token_count
from .store import dumps


def target(record):
    value = record["value"]
    return {"name": value["name"]} if "name" in value else deepcopy(value)


def record_key(record):
    """Internal exact key; agents select by kind and named value fields instead."""
    return dumps([record["kind"], target(record)])


def select(records, kind, fields):
    if not isinstance(fields, dict) or not fields:
        raise ValueError("target must contain exact named fields")
    matches = [
        key
        for key, record in records.items()
        if record["kind"] == kind
        and all(
            key in record["value"] and dumps(record["value"][key]) == dumps(value)
            for key, value in fields.items()
        )
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{kind} target {fields!r} has {len(matches)} matches; specify exact fields"
        )
    return matches[0]


def plan_changes(records, changes, *, evidence_check):
    """Validate every independent row, then return atomic before/after images.

    Selectors bind to the input revision. A row cannot depend on an earlier row
    having created or renamed its target. Arrays in patches replace whole arrays.
    Cross-draft references remain visible as blockers, without rejecting local
    extraction; the merge/freeze gate is responsible for resolving them.
    """
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a nonempty array of named operations")
    result, ledger, errors, touched = deepcopy(records), [], [], set()
    for index, change in enumerate(changes):
        try:
            if not isinstance(change, dict):
                raise ValueError("operation must be a named record")
            op, kind = change.get("op"), change.get("kind")
            if op not in {"add", "update", "patch", "delete"}:
                raise ValueError(
                    "expected add/update/patch/delete; merge/rename require merge phase"
                )
            if kind not in (*COLLECTIONS, "unresolved"):
                raise ValueError("unknown record kind")
            keys = (
                {"op", "kind", "reason", "target"}
                if op == "delete"
                else {
                    "op",
                    "kind",
                    "value",
                    "evidence",
                    *([] if op == "add" else ["target"]),
                }
            )
            if set(change) - keys:
                raise ValueError(f"unexpected fields: {sorted(set(change) - keys)}")
            before, old_key = None, None
            if op != "add":
                old_key = select(records, kind, change.get("target"))
                before = records[old_key]
                if old_key in touched:
                    raise ValueError("a record may be changed only once in one operation")
            if op == "delete":
                if not isinstance(change.get("reason"), str) or not change["reason"].strip():
                    raise ValueError("deletion requires a nonempty reason")
                after = None
            else:
                value = deepcopy(change.get("value"))
                if op == "patch":
                    if not isinstance(value, dict) or not value:
                        raise ValueError("patch value must be a nonempty object")
                    value = {**deepcopy(before["value"]), **value}
                validate_record(kind, value)
                if before and before["value"].get("name") != value.get("name"):
                    raise ValueError(
                        "use merge-phase rename to update names and references together"
                    )
                evidence = deepcopy(change.get("evidence"))
                evidence_check(evidence)
                if op == "patch":
                    evidence = deepcopy(before["evidence"]) + [
                        item for item in evidence if item not in before["evidence"]
                    ]
                after = {**(deepcopy(before) if before else {}),
                         "kind": kind, "value": value, "evidence": evidence}
                if token_count(after) > 8000:
                    raise ValueError("record exceeds 8000 tokens; keep it concise and cite sources")
                new_key = record_key(after)
                if new_key != old_key and new_key in result:
                    raise ValueError(f"duplicate {kind} target {target(after)!r}")
            # Nothing is changed until this entire row has validated.
            if old_key is not None:
                touched.add(old_key)
                del result[old_key]
            if after is not None:
                touched.add(new_key)
                result[new_key] = after
            ledger.append(
                {"kind": kind, "target": target(after or before), "deleted": after is None}
            )
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"changes[{index}]: {error}")
    if errors:
        raise ValueError("; ".join(errors))
    delta = {
        key: (records.get(key), result.get(key))
        for key in dict.fromkeys([*records, *result])
        if records.get(key) != result.get(key)
    }
    return delta, ledger, reference_errors(result)
