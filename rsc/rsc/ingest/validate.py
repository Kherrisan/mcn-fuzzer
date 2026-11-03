"""Structural invariants for an ingest ``document.json``.

JSON Schema validation is available through ``rsc validate``.  This module
checks the invariants that are hard to express in JSON Schema alone: section
trees, block references, order, span bounds and evidence-map consistency.
"""

from __future__ import annotations

import re

BLOCK_TYPES = frozenset(
    {
        "heading",
        "paragraph",
        "list_item",
        "table",
        "grammar",
        "diagram",
        "blockquote",
        "preamble",
        "other",
    }
)
FORMATS = frozenset({"txt", "html", "xml"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_span(
    span: object,
    *,
    source_id: str,
    source_length: int,
    line_count: int,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(span, dict):
        errors.append(f"{label}: span must be an object")
        return
    missing = [
        key
        for key in ("source_id", "start", "end", "start_line", "end_line")
        if key not in span
    ]
    if missing:
        errors.append(f"{label}: span missing {', '.join(repr(key) for key in missing)}")
        return
    if span.get("source_id") != source_id:
        errors.append(f"{label}: span source_id does not match document")
    start = span.get("start")
    end = span.get("end")
    start_line = span.get("start_line")
    end_line = span.get("end_line")
    if not all(_is_int(value) for value in (start, end, start_line, end_line)):
        errors.append(f"{label}: span offsets and line numbers must be integers")
        return
    assert isinstance(start, int) and isinstance(end, int)
    assert isinstance(start_line, int) and isinstance(end_line, int)
    if start < 0 or end < 0:
        errors.append(f"{label}: span offsets must be >= 0")
    if start > end:
        errors.append(f"{label}: span start is after end")
    if end > source_length:
        errors.append(f"{label}: span end {end} exceeds source length {source_length}")
    if start_line < 1 or end_line < 1:
        errors.append(f"{label}: span line numbers must be >= 1")
    if start_line > end_line:
        errors.append(f"{label}: span start_line is after end_line")
    if end_line > line_count:
        errors.append(
            f"{label}: span end_line {end_line} exceeds line count {line_count}"
        )


def validate_document(
    document: dict[str, object],
    *,
    source_text: str,
    line_count: int,
) -> list[str]:
    errors: list[str] = []
    for key in ("meta", "sections", "blocks"):
        if key not in document:
            errors.append(f"document missing {key!r}")
    if errors:
        return errors

    meta = document["meta"]
    sections = document["sections"]
    blocks = document["blocks"]
    if not isinstance(meta, dict):
        errors.append("meta must be an object")
        return errors
    if not isinstance(sections, list):
        errors.append("sections must be an array")
        return errors
    if not isinstance(blocks, list):
        errors.append("blocks must be an array")
        return errors

    source_id = meta.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        errors.append("meta.source_id must be a non-empty string")
        source_id = ""
    if not isinstance(meta.get("rfc"), str) or not meta.get("rfc"):
        errors.append("meta.rfc must be a non-empty string")
    if not isinstance(meta.get("title"), str) or not meta.get("title"):
        errors.append("meta.title must be a non-empty string")
    if meta.get("format") not in FORMATS:
        errors.append("meta.format must be txt, html or xml")
    digest = meta.get("input_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        errors.append("meta.input_sha256 must be a 64-character lowercase hex digest")

    source_length = len(source_text)
    section_by_id: dict[str, dict[str, object]] = {}
    section_paths: set[tuple[str, ...]] = set()
    for index, section in enumerate(sections):
        label = f"sections[{index}]"
        if not isinstance(section, dict):
            errors.append(f"{label}: must be an object")
            continue
        section_id = section.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            errors.append(f"{label}: section_id must be a non-empty string")
            continue
        if section_id in section_by_id:
            errors.append(f"{label}: duplicate section_id {section_id!r}")
        section_by_id[section_id] = section
        path = section.get("path")
        if not isinstance(path, list) or not path or not all(
            isinstance(part, str) and part for part in path
        ):
            errors.append(f"{label}: path must be a non-empty array of strings")
            path = []
        else:
            key = tuple(str(part) for part in path)
            # Some RFCs contain duplicate section numbers (a known numbering
            # typo).  section_id remains unique because the parser adds a
            # suffix; do not reject the whole document for this.
            section_paths.add(key)
        if not isinstance(section.get("title"), str):
            errors.append(f"{label}: title must be a string")
        level = section.get("level")
        if not _is_int(level) or int(level) < 1:
            errors.append(f"{label}: level must be a positive integer")
        _check_span(
            section.get("span"),
            source_id=source_id,
            source_length=source_length,
            line_count=line_count,
            label=f"{label}.span",
            errors=errors,
        )
        parent_id = section.get("parent_section_id")
        if parent_id is not None:
            if not isinstance(parent_id, str) or parent_id not in section_by_id:
                errors.append(f"{label}: parent_section_id does not exist")
            elif path:
                parent = section_by_id[parent_id]
                parent_path = parent.get("path")
                if isinstance(parent_path, list):
                    if list(path[: len(parent_path)]) != list(parent_path):
                        errors.append(f"{label}: path is not prefixed by parent path")

    block_ids: set[str] = set()
    last_order = -1
    for index, block in enumerate(blocks):
        label = f"blocks[{index}]"
        if not isinstance(block, dict):
            errors.append(f"{label}: must be an object")
            continue
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            errors.append(f"{label}: block_id must be a non-empty string")
        elif block_id in block_ids:
            errors.append(f"{label}: duplicate block_id {block_id!r}")
        else:
            block_ids.add(block_id)
        block_type = block.get("block_type")
        if block_type not in BLOCK_TYPES:
            errors.append(f"{label}: unknown block_type {block_type!r}")
        text = block.get("text")
        if not isinstance(text, str):
            errors.append(f"{label}: text must be a string")
        elif not text and block_type not in ("table", "diagram"):
            errors.append(f"{label}: text must not be empty")
        order = block.get("order")
        if not _is_int(order):
            errors.append(f"{label}: order must be an integer")
        else:
            if int(order) <= last_order:
                errors.append(f"{label}: order must strictly increase")
            last_order = int(order)
        section_path = block.get("section_path")
        if not isinstance(section_path, list) or not all(
            isinstance(part, str) for part in section_path
        ):
            errors.append(f"{label}: section_path must be an array of strings")
        elif section_path and tuple(section_path) not in section_paths:
            errors.append(f"{label}: section_path does not match any section")
        parent = block.get("parent_block")
        if parent is not None and parent not in block_ids:
            errors.append(f"{label}: parent_block does not exist")
        lead_in = block.get("lead_in_block_id")
        if lead_in is not None and lead_in not in block_ids:
            errors.append(f"{label}: lead_in_block_id does not exist")
        _check_span(
            block.get("original_span"),
            source_id=source_id,
            source_length=source_length,
            line_count=line_count,
            label=f"{label}.original_span",
            errors=errors,
        )

    span_map = document.get("span_map", [])
    if not isinstance(span_map, list):
        errors.append("span_map must be an array")
    else:
        for index, entry in enumerate(span_map):
            label = f"span_map[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{label}: must be an object")
                continue
            block_id = entry.get("block_id")
            if block_id is not None and block_id not in block_ids:
                errors.append(f"{label}: block_id does not exist")
            for key in (
                "normalized_start",
                "normalized_end",
                "source_start",
                "source_end",
            ):
                if not _is_int(entry.get(key)):
                    errors.append(f"{label}: {key} must be an integer")
            start = entry.get("normalized_start")
            end = entry.get("normalized_end")
            source_start = entry.get("source_start")
            source_end = entry.get("source_end")
            if all(_is_int(value) for value in (start, end, source_start, source_end)):
                if int(start) > int(end):
                    errors.append(f"{label}: normalized_start is after normalized_end")
                if int(source_start) > int(source_end):
                    errors.append(f"{label}: source_start is after source_end")
                if int(source_end) > source_length:
                    errors.append(f"{label}: source_end exceeds source length")

    return errors
