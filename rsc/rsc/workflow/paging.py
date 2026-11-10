"""Token-budget pagination and compact model records.

Each page is self-contained. Compact rows decode to the same records as the
records format, including nested values and evidence.
"""

from __future__ import annotations

import json

from .sources import token_count

DEFAULT_BUDGET = 24_000
HARD_BUDGET = 30_000


class PageError(ValueError):
    pass


def resolve_budget(requested) -> int:
    if requested is None:
        return DEFAULT_BUDGET
    if type(requested) is not int or isinstance(requested, bool) or requested < 1:
        raise PageError("max_tokens must be a positive integer")
    if requested > HARD_BUDGET:
        raise PageError(f"max_tokens {requested} exceeds the hard limit {HARD_BUDGET}")
    return requested


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Marks that the original record had a `value` object, including `{}`.
_VALUE_MARK = "#value"


def encode_compact(records: list[dict]) -> dict:
    evidence: list = []
    indexes: dict[str, int] = {}

    def evidence_index(item) -> int:
        key = _dumps(item)
        if key not in indexes:
            indexes[key] = len(evidence)
            evidence.append(item)
        return indexes[key]

    groups: dict[tuple, dict] = {}
    for record in records:
        flat = {"record_id": record["record_id"], "kind": record["kind"]}
        if "value" in record:
            flat[_VALUE_MARK] = 1
            value = record["value"]
            if isinstance(value, dict):
                flat.update(value)
            else:
                flat["value"] = value
        else:
            for key, item in record.items():
                if key not in {"record_id", "kind", "evidence"}:
                    flat[key] = item
        if "evidence" in record:
            flat["evidence"] = [evidence_index(item) for item in record["evidence"]]
        columns = tuple(sorted(flat))
        group = groups.setdefault(
            (flat["kind"], columns),
            {"kind": flat["kind"], "columns": list(columns), "rows": []},
        )
        group["rows"].append([flat[column] for column in columns])
    return {"evidence": evidence, "groups": list(groups.values())}


def decode_compact(payload: dict) -> list[dict]:
    evidence = payload.get("evidence") or []
    records = []
    for group in payload.get("groups") or []:
        columns = group["columns"]
        for row in group["rows"]:
            flat = dict(zip(columns, row, strict=True))
            record = {"record_id": flat.pop("record_id"), "kind": flat.pop("kind")}
            if "evidence" in flat:
                record["evidence"] = [evidence[index] for index in flat.pop("evidence")]
            has_value = flat.pop(_VALUE_MARK, None)
            if has_value:
                record["value"] = flat
            else:
                record.update(flat)
            records.append(record)
    return records


def delivered_ids(rows) -> list:
    return [
        row.get("record_id")
        or row.get("change_id")
        or row.get("batch_id")
        or row.get("finding_id")
        or row.get("name")
        or (
            {
                "kind": row["kind"],
                "target": {"name": row["value"]["name"]}
                if "name" in row["value"]
                else row["value"],
            }
            if "kind" in row and "value" in row
            else None
        )
        for row in rows
    ]


def seal_response(payload: dict) -> dict:
    """Set `tokens` to the token count of the payload that callers receive."""

    sealed = dict(payload)
    sealed["tokens"] = 0
    for _ in range(8):
        counted = token_count(sealed)
        if sealed["tokens"] == counted:
            return sealed
        sealed["tokens"] = counted
    return sealed


def minimum_budget(render) -> int:
    """Smallest budget whose rendered response reports no more tokens than that budget."""

    guess = 1
    for _ in range(12):
        payload = render(guess)
        if payload["tokens"] <= guess:
            return payload["tokens"]
        guess = payload["tokens"]
    return guess


def fit_page(rows, cursor, max_tokens, revision, build):
    """Return the longest prefix whose final response fits in the budget."""

    budget = resolve_budget(max_tokens)
    cursor = int(cursor or 0)
    if cursor < 0 or cursor > len(rows):
        raise PageError("invalid cursor")

    def finish_at(items, candidate):
        end = cursor + len(items)
        nxt = end if end < len(rows) else None
        payload = build(items, nxt, revision, candidate)
        payload["delivered_ids"] = delivered_ids(items)
        payload["max_tokens"] = candidate
        payload["revision"] = revision
        return seal_response(payload)

    def finish(items):
        return finish_at(items, budget)

    if cursor == len(rows) and finish([])["tokens"] > budget:
        needed = minimum_budget(lambda candidate: finish_at([], candidate))
        raise PageError(f"page metadata requires {needed} tokens; budget is {budget}")
    chosen: list = []
    for row in rows[cursor:]:
        payload = finish([*chosen, row])
        if payload["tokens"] > budget:
            if not chosen:
                needed = minimum_budget(
                    lambda candidate: finish_at(rows[cursor : cursor + 1], candidate)
                )
                raise PageError(f"single record requires {needed} tokens; budget is {budget}")
            break
        chosen.append(row)
    return finish(chosen)


def records_page(rows, cursor, max_tokens, revision, *, metadata=None):
    def build(items, nxt, rev, budget):
        return {
            "format": "records",
            "items": items,
            "next_cursor": nxt,
            "revision": rev,
            "max_tokens": budget,
            **(metadata or {}),
        }

    return fit_page(rows, cursor, max_tokens, revision, build)


def compact_page(rows, cursor, max_tokens, revision):
    def build(items, nxt, rev, budget):
        body = encode_compact(items)
        body.update(
            format="compact",
            next_cursor=nxt,
            revision=rev,
            max_tokens=budget,
        )
        return body

    return fit_page(rows, cursor, max_tokens, revision, build)
