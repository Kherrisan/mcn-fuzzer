"""Structural index invariants, including complete block coverage."""

from __future__ import annotations

from typing import Any

from .sections import section_for_block


def _span_errors(span: Any, prefix: str) -> list[str]:
    if not isinstance(span, dict):
        return [f"{prefix}: span must be an object"]
    errors = []
    if not isinstance(span.get("source_id"), str) or not span["source_id"]:
        errors.append(f"{prefix}: span source_id must be a nonempty string")
    for key in ("start", "end", "start_line", "end_line"):
        value = span.get(key)
        minimum = 1 if key.endswith("line") else 0
        if type(value) is not int or value < minimum:
            errors.append(f"{prefix}: span {key} must be an integer >= {minimum}")
    if not errors:
        if span["start"] > span["end"]:
            errors.append(f"{prefix}: span start > end")
        if span["start_line"] > span["end_line"]:
            errors.append(f"{prefix}: span start_line > end_line")
    return errors


def _rows(index: dict, key: str, id_key: str, errors: list[str]) -> dict[str, dict]:
    rows = {}
    for number, row in enumerate(index[key]):
        if not isinstance(row, dict) or not isinstance(row.get(id_key), str) or not row[id_key]:
            errors.append(f"{key}[{number}]: missing {id_key}")
            continue
        identity = row[id_key]
        if identity in rows:
            errors.append(f"{key}[{number}]: duplicate {id_key} {identity}")
        rows[identity] = row
    return rows


def validate_index(index: dict[str, Any], document: dict[str, Any] | None = None) -> list[str]:
    """Return invariant violations without inferring or repairing source structure."""

    if not isinstance(index, dict):
        return ["index must be an object"]
    errors: list[str] = []
    expected = {"meta", "sections", "blocks", "references"}
    for key in sorted(expected - index.keys()):
        errors.append(f"missing key: {key}")
    for key in sorted(index.keys() - expected):
        errors.append(f"unsupported key: {key}")
    if not isinstance(index.get("meta"), dict):
        errors.append("meta must be an object")
    for key in ("sections", "blocks", "references"):
        if not isinstance(index.get(key), list):
            errors.append(f"{key} must be a list")
    if errors:
        return errors
    sections = _rows(index, "sections", "section_id", errors)
    blocks = _rows(index, "blocks", "block_id", errors)
    references = _rows(index, "references", "reference_id", errors)
    for identity, section in sections.items():
        path = section.get("path")
        if not isinstance(path, list) or not all(isinstance(p, str) for p in path):
            errors.append(f"{identity}: path must be a list of strings")
        errors.extend(_span_errors(section.get("span"), identity))
        parent = section.get("parent_section_id")
        if parent is not None and parent not in sections:
            errors.append(f"{identity}: unknown parent_section_id {parent}")
        seen = {identity}
        while parent in sections:
            if parent in seen:
                errors.append(f"{identity}: cyclic section ancestry")
                break
            seen.add(parent)
            parent = sections[parent].get("parent_section_id")

    orders = set()
    valid_orders = True
    for identity, block in blocks.items():
        order = block.get("order")
        if type(order) is not int or order < 0:
            errors.append(f"{identity}: order must be a nonnegative integer")
            valid_orders = False
        elif order in orders:
            errors.append(f"{identity}: duplicate block order {order}")
        else:
            orders.add(order)
        if block.get("section_id") is not None and block["section_id"] not in sections:
            errors.append(f"{identity}: unknown section_id {block['section_id']}")
        for key in ("section_id", "prev_block_id", "next_block_id"):
            if key not in block:
                errors.append(f"{identity}: missing {key}")
        for key in ("prev_block_id", "next_block_id"):
            neighbor = block.get(key)
            if neighbor is not None and neighbor not in blocks:
                errors.append(f"{identity}: unknown {key} {neighbor}")
    if valid_orders:
        ordered = sorted(blocks, key=lambda key: blocks[key]["order"])
        if ordered != [b.get("block_id") for b in index["blocks"] if isinstance(b, dict)]:
            errors.append("blocks must be stored in source order")
        for position, identity in enumerate(ordered):
            previous = ordered[position - 1] if position else None
            following = ordered[position + 1] if position + 1 < len(ordered) else None
            if blocks[identity].get("prev_block_id") != previous:
                errors.append(f"{identity}: prev_block_id does not match source order")
            if blocks[identity].get("next_block_id") != following:
                errors.append(f"{identity}: next_block_id does not match source order")

    for identity, reference in references.items():
        if reference.get("from_block") not in blocks:
            errors.append(f"{identity}: unknown from_block {reference.get('from_block')}")
        target = reference.get("target_section_id")
        if target is not None and target not in sections:
            errors.append(f"{identity}: unknown target_section_id {target}")
        errors.extend(_span_errors(reference.get("span"), identity))

    if document is not None:
        source_blocks = document.get("blocks", [])
        source_by_id = {b["block_id"]: b for b in source_blocks}
        if len(source_by_id) != len(source_blocks):
            errors.append("document contains duplicate block IDs")
        if set(source_by_id) != set(blocks):
            errors.append("index block coverage differs from document")
        for identity, source in source_by_id.items():
            indexed = blocks.get(identity)
            if indexed is None:
                continue
            try:
                expected_section = section_for_block(source, list(sections.values()))
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if indexed.get("section_id") != expected_section:
                errors.append(f"{identity}: section_id differs from source section path")
            if indexed.get("order") != source.get("order"):
                errors.append(f"{identity}: order differs from source")
    return errors
