"""Compact model-facing views, separate from immutable source and extraction artifacts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contract import COLLECTIONS
from .merge import canonical, semantic


def object_view(value: Any) -> Any:
    """Copy semantic fields and Python-assigned IDs for model-facing history."""

    if isinstance(value, dict):
        return {key: object_view(item) for key, item in value.items()}
    if isinstance(value, list):
        return [object_view(item) for item in value]
    return deepcopy(value)


def block_view(block: dict) -> dict:
    """Keep source identity, kind and exact text; retain coordinates only for excerpts."""

    keys = ("doc_id", "block_id", "block_type", "text")
    result = {key: deepcopy(block[key]) for key in keys if key in block}
    if "text_start" in block:
        result.update(text_start=block["text_start"], text_end=block["text_end"])
        for key in ("text_length", "next_start"):
            if key in block:
                result[key] = deepcopy(block[key])
    return result


def artifact_views(pairs: list[dict]) -> list[dict]:
    """Render unique block pairs and include each identical object definition only once."""

    unique = {}
    for pair in pairs:
        block = pair["block"]
        key = block["doc_id"], block["block_id"]
        if key not in unique:
            unique[key] = deepcopy(pair)
            continue
        previous = unique[key]
        if block_view(previous["block"]) != block_view(block):
            raise ValueError("duplicate context block must be coalesced before projection")
        previous["roles"] = sorted(set(previous["roles"]) | set(pair["roles"]))
        history = previous.get("compile_result") or {}
        for stage, record in (pair.get("compile_result") or {}).items():
            if stage in history and history[stage] != record:
                raise ValueError("duplicate context block has conflicting extraction snapshots")
            history[stage] = deepcopy(record)
        previous["compile_result"] = history or None
    result, objects = [], {}
    for pair in unique.values():
        block = pair["block"]
        history = {}
        for stage, record in (pair.get("compile_result") or {}).items():
            items = {}
            reused = set(record.get("reused_names", []))
            for kind in COLLECTIONS:
                for row in record.get("items", {}).get(kind, []):
                    value = object_view(row)
                    if kind != "steps" and isinstance(value, dict):
                        value.pop("id", None)
                    key = canonical(semantic(value))
                    label = value.get("name")
                    if not label and kind == "step_dependencies":
                        prior = value.get("prior_step", "")
                        dependent = value.get("dependent_step", "")
                        label = f"{prior} -> {dependent}"
                    if objects.get(key) == value and label:
                        reused.add(label)
                    else:
                        items.setdefault(kind, []).append(value)
                        objects[key] = value
            history[stage] = {"status": record["status"], "items": items}
            if reused:
                history[stage]["reused_names"] = sorted(reused)
        view = {
            "block": block_view(block),
            "roles": list(pair["roles"]),
            "compile_result": history or None,
        }
        result.append(view)
    return result
