"""Canonical values, diagnostic IDs and typed reference rewrites."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def stable_id(kind: str, value) -> str:
    return kind + "-" + hashlib.sha256(canonical(value).encode()).hexdigest()[:8]


def unique(values: list) -> list:
    return sorted({canonical(value): value for value in values}.values(), key=canonical)


def issue(description: str, objects=(), **_details) -> dict:
    """Issue records stay description plus names. Diagnostics are not embedded."""

    if isinstance(objects, str):
        objects = [objects]
    names = [item for item in objects if isinstance(item, str)]
    return {"description": description, "object_names": list(dict.fromkeys(names))}


def sort_rows(kind: str, rows: list) -> list:
    """Stable presentation order. Non-step objects have no definition ID."""

    def key(row: dict):
        if kind == "steps":
            return (row.get("id") or "", row.get("name") or "")
        if kind == "step_dependencies":
            return (
                row.get("prior_step") or "",
                row.get("dependent_step") or "",
                row.get("condition") or "",
                canonical(semantic(row)),
            )
        return (row.get("name") or "", canonical(semantic(row)))

    return sorted(rows, key=key)


def semantic(value):
    """Compiler-assigned identifiers never decide semantic equivalence."""
    if isinstance(value, dict):
        return {key: semantic(item) for key, item in sorted(value.items()) if key != "id"}
    if isinstance(value, list):
        return sorted((semantic(item) for item in value), key=canonical)
    if isinstance(value, str):
        return " ".join(value.split())
    return value


REFERENCE_KEYS = {
    "message_name",
    "channel_name",
    "sender_name",
    "receiver_name",
    "party_name",
    "initiator_party_name",
    "acceptor_party_name",
    "prior_step",
    "dependent_step",
    "prior_field",
    "dependent_field",
    "endpoint_party_names",
    "object_names",
}


def rewrite(value, mapping: dict[str, str]):
    if isinstance(value, list):
        return [rewrite(item, mapping) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in REFERENCE_KEYS:
            result[key] = (
                sorted({mapping.get(v, v) for v in item})
                if isinstance(item, list)
                else mapping.get(item, item)
            )
        else:
            result[key] = rewrite(item, mapping)
    return result


def nested_objects(row: dict):
    yield row
    for key in ("associated_fields", "field_relations"):
        yield from row.get(key, [])


def rewrite_diagnostic_ids(text: str, mapping: dict[str, str]) -> str:
    """Normalize whole ID tokens for diagnostic comparison without editing source prose."""

    if not mapping:
        return text
    alternatives = "|".join(re.escape(key) for key in sorted(mapping, key=lambda x: (-len(x), x)))
    pattern = r"(?<![\w:.-])(?:" + alternatives + r")(?![\w.-]|:(?!\s))"
    return re.sub(pattern, lambda match: mapping[match[0]], text)
