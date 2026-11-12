"""Build document-local section, block, and reference indexes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rsc.schemas import legacy_metadata_errors
from rsc.version import __version__

from .references import extract_references, reference_key
from .sections import build_section_index, section_for_block
from .validate import validate_index


class StructuralIndexError(ValueError):
    """Raised when a document cannot be turned into a structural index."""


def build_structural_index(
    document: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Index every source block without deriving semantic entities."""

    if not isinstance(document, dict) or not isinstance(document.get("meta"), dict):
        raise StructuralIndexError("document and document.meta are required")
    if errors := legacy_metadata_errors(document):
        raise StructuralIndexError("; ".join(errors))
    meta = document["meta"]
    sections, by_path, _ = build_section_index(document)
    raw_blocks = document.get("blocks", [])
    if not isinstance(raw_blocks, list) or any(
        not isinstance(b, dict)
        or type(b.get("order")) is not int
        or b["order"] < 0
        or not isinstance(b.get("block_id"), str)
        or not b["block_id"]
        for b in raw_blocks
    ):
        raise StructuralIndexError("blocks require nonempty IDs and nonnegative integer orders")
    ordered = sorted(raw_blocks, key=lambda b: (b["order"], b["block_id"]))
    blocks = []
    for position, raw in enumerate(ordered):
        try:
            section_id = section_for_block(raw, sections)
        except ValueError as exc:
            raise StructuralIndexError(str(exc)) from exc
        blocks.append(
            {
                "block_id": raw["block_id"],
                "section_id": section_id,
                "order": raw["order"],
                "prev_block_id": ordered[position - 1]["block_id"] if position else None,
                "next_block_id": ordered[position + 1]["block_id"]
                if position + 1 < len(ordered)
                else None,
            }
        )
    index = {
        "meta": {
            "rfc": str(meta.get("rfc") or "RFC unknown"),
            "generated_at": generated_at or datetime.now(UTC).isoformat(),
            "generator": f"rsc-structural-index/{__version__}",
        },
        "sections": sections,
        "blocks": blocks,
        "references": sorted(extract_references(document, by_path), key=reference_key),
    }
    errors = validate_index(index, document)
    if errors:
        raise StructuralIndexError("; ".join(errors))
    return index


def index_summary(index: dict[str, Any]) -> dict[str, Any]:
    """Return counts for the supported index collections."""

    return {
        "rfc": index.get("meta", {}).get("rfc"),
        **{key: len(index.get(key, [])) for key in ("sections", "blocks", "references")},
    }
