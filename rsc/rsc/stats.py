"""Counts and coverage of the protocol interaction model."""

from __future__ import annotations

from pathlib import Path

from rsc.artifacts import read_json
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.models import Protocol
from rsc.schemas import legacy_metadata_errors


def collect_protocol(path: str | Path) -> tuple[Path, dict]:
    path = Path(path).expanduser()
    if path.is_dir():
        path /= "protocol.json"
    model = read_json(path)
    if not isinstance(model, dict) or any(key not in model for key in COLLECTIONS):
        raise ValueError(f"{path} is not a protocol model")
    if errors := legacy_metadata_errors(model):
        raise ValueError("; ".join(errors))
    Protocol.model_validate(model)
    return path, model


def compute_stats(model: dict) -> dict:
    if errors := legacy_metadata_errors(model):
        raise ValueError("; ".join(errors))
    Protocol.model_validate(model)
    steps = model["steps"]
    dependencies = model["step_dependencies"]
    return {
        "protocol": model["meta"]["protocol"],
        "complete": model["meta"]["complete"],
        "counts": {key: len(model[key]) for key in (*COLLECTIONS, "unresolved")},
        "channel_coverage": {
            channel["name"]: sum(step["channel_name"] == channel["name"] for step in steps)
            for channel in model["channels"]
        },
        "self_dependencies": sum(
            edge["prior_step"] == edge["dependent_step"] for edge in dependencies
        ),
        "dependencies_with_fields": sum(bool(edge["field_relations"]) for edge in dependencies),
        "empty_field_relations": sum(not edge["field_relations"] for edge in dependencies),
        "associated_fields": sum(
            len(message["associated_fields"]) for message in model["messages"]
        ),
        "field_relation_pairs": sum(len(edge["field_relations"]) for edge in dependencies),
        "unresolved": len(model["unresolved"]),
        "unresolved_with_names": sum(bool(item["object_names"]) for item in model["unresolved"]),
    }


def format_stats(stats: dict) -> str:
    return "\n".join(f"{key}: {value}" for key, value in stats.items())
