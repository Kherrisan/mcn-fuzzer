"""Protocol contract and extraction schemas (no provider dependencies)."""

from __future__ import annotations

from typing import Any

from rsc.artifacts import read_json
from rsc.schemas import resolve_schemas_dir

BASE = ("parties", "messages", "channels")
INTERACTIONS = ("steps", "step_dependencies")
COLLECTIONS = BASE + INTERACTIONS
DEFINITIONS = dict(
    zip(COLLECTIONS, ("party", "message", "channel", "step", "dependency"), strict=True)
)


def protocol_schema(schemas_dir=None) -> dict[str, Any]:
    return read_json(resolve_schemas_dir(schemas_dir) / "protocol.schema.json")
