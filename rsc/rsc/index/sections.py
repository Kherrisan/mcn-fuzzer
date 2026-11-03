"""Section index construction."""

from __future__ import annotations

from typing import Any


def section_key(path: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return a hashable, normalized section-path key."""

    return tuple(str(part) for part in path)


def build_section_index(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[tuple[str, ...], str], dict[str, str]]:
    """Build the index section list plus path/title lookup maps."""

    sections: list[dict[str, Any]] = []
    by_path: dict[tuple[str, ...], str] = {}
    by_title: dict[str, str] = {}
    ambiguous_paths: set[tuple[str, ...]] = set()
    raw_sections = document.get("sections") or []

    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        section_id = str(raw.get("section_id") or "")
        if not section_id:
            continue
        path = [str(part) for part in (raw.get("path") or [])]
        item: dict[str, Any] = {
            "section_id": section_id,
            "path": path,
            "title": str(raw.get("title") or ""),
            "span": raw.get("span") or {},
        }
        parent = raw.get("parent_section_id")
        if isinstance(parent, str) and parent:
            item["parent_section_id"] = parent
        sections.append(item)
        key = section_key(path)
        if key in by_path:
            ambiguous_paths.add(key)
        by_path[key] = section_id
        by_title[str(item["title"]).casefold()] = section_id

    # Ambiguous textual references must not select an arbitrary duplicate section.
    for key in ambiguous_paths:
        by_path.pop(key)
    # Fill missing parents from the nearest path prefix.
    for item in sections:
        if "parent_section_id" in item:
            continue
        path = item["path"]
        parent_id: str | None = None
        for length in range(len(path) - 1, 0, -1):
            parent_id = by_path.get(section_key(path[:length]))
            if parent_id:
                break
        item["parent_section_id"] = parent_id

    return sections, by_path, by_title


def section_for_block(block: dict, sections: list[dict]) -> str | None:
    """Resolve an exact path, using source containment for repeated section labels."""

    path = section_key(block.get("section_path") or [])
    if not path:
        return None
    candidates = [section for section in sections if section_key(section["path"]) == path]
    if len(candidates) == 1:
        return candidates[0]["section_id"]
    if not candidates:
        raise ValueError(f"{block['block_id']}: unknown section path {path}")
    source = block.get("original_span") or {}
    containing = [
        section
        for section in candidates
        if section["span"].get("source_id") == source.get("source_id")
        and section["span"].get("start", -1) <= source.get("start", -2)
        and source.get("end", float("inf")) <= section["span"].get("end", -1)
    ]
    if len(containing) == 1:
        return containing[0]["section_id"]
    raise ValueError(f"{block['block_id']}: ambiguous section path {path}")
