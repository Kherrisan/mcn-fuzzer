"""Protocol contract and extraction schemas (no provider dependencies)."""

from __future__ import annotations

from typing import Any

from rsc.artifacts import read_json
from rsc.schemas import resolve_schemas_dir

BASE = ("parties", "messages", "channels")
INTERACTIONS = ("steps", "step_dependencies")
COLLECTIONS = BASE + ("states",) + INTERACTIONS
DEFINITIONS = dict(
    zip(COLLECTIONS, ("party", "message", "channel", "state", "step", "dependency"), strict=True)
)


def record_identity(kind, value):
    if kind == "step_dependencies":
        return None
    name = value["name"]
    return kind, (value["party_name"], name) if kind == "states" else name


def protocol_schema(schemas_dir=None) -> dict[str, Any]:
    return read_json(resolve_schemas_dir(schemas_dir) / "protocol.schema.json")
