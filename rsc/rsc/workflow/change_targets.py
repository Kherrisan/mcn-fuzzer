"""Resolve exact field selectors against a transaction snapshot, preserving retries."""

import json
from copy import deepcopy


def resolve_targets(changes, records, previous, history, revision):
    resolved, bindings = deepcopy(changes), {}
    for index, change in enumerate(resolved):
        if change["op"] == "add":
            continue
        if "target" in change:
            kind, target = change.get("kind"), change["target"]
            if not kind or "record_id" in change or not isinstance(target, dict) or not target:
                raise ValueError(
                    f"changes[{index}] target requires kind, nonempty fields, no record_id"
                )
            key = json.dumps([kind, target], ensure_ascii=False, sort_keys=True)
            wanted = {
                k: json.dumps(v, ensure_ascii=False, sort_keys=True) for k, v in target.items()
            }

            def matches(record, kind=kind, wanted=wanted):
                return (
                    record
                    and record["kind"] == kind
                    and all(
                        k in record["value"]
                        and json.dumps(record["value"][k], ensure_ascii=False, sort_keys=True) == v
                        for k, v in wanted.items()
                    )
                )

            candidates = set()
            if previous:
                cached = previous.get("target_bindings", {}).get(key)
                if cached:
                    candidates.add(cached)
                else:
                    # An older ID-based operation may be retried after a rename/delete.
                    for entry in history:
                        if entry["revision"] != previous["result"]["revision"]:
                            continue
                        for item in entry["changes"]:
                            if any(matches(record) for record in (item["before"], item["after"])):
                                candidates.add(item["record_id"])
            if not candidates:
                candidates = {rid for rid, record in records.items() if matches(record)}
            if len(candidates) != 1:
                raise ValueError(
                    f"changes[{index}] {kind} target {target!r} has {len(candidates)} matches; "
                    f"revision={revision}. Use exact fields or an explicit record_id."
                )
            rid = candidates.pop()
            bindings[key] = rid
            change["record_id"] = rid
            del change["target"]
        elif change.get("record_id") not in records and previous is None:
            raise ValueError(
                f"invalid operation or missing record: changes[{index}] "
                f"record_id={change.get('record_id')!r}; revision={revision}. "
                "Use the returned ledger or exact kind/target fields."
            )
    return resolved, bindings
