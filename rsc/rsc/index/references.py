"""Extract section, RFC and BCP references from document blocks."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_SECTION_RE = re.compile(
    r"(?i)\b(?:Section|Sections|§)\s*([0-9]+(?:\.[0-9]+)*|[A-Z](?:\.[0-9]+)*)\b"
)
_APPENDIX_RE = re.compile(r"(?i)\bAppendix\s+([A-Z])(?:\.([0-9]+))?\b")
_RFC_RE = re.compile(r"(?i)\bRFC\s*([0-9]{1,5})\b")
_BCP_RE = re.compile(r"(?i)\bBCP\s*([0-9]{1,4})\b")
_BRACKET_RFC_RE = re.compile(r"(?i)\[RFC\s*([0-9]{1,5})\]")


def _reference_id(block_id: str, ref_type: str, raw: str, start: int, end: int) -> str:
    digest = hashlib.sha1(f"{block_id}|{ref_type}|{raw}|{start}|{end}".encode()).hexdigest()[:10]
    return f"ref-{ref_type}-{digest}"


def _resolve_section(
    raw: str,
    by_path: dict[tuple[str, ...], str],
) -> str | None:
    value = raw.strip()
    appendix = re.fullmatch(r"(?i)Appendix\s+([A-Z])(?:\.([0-9]+))?", value)
    if appendix:
        path = (f"appendix-{appendix.group(1).lower()}",)
        if appendix.group(2):
            path = path + (appendix.group(2),)
        return by_path.get(path)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        return by_path.get(tuple(value.split(".")))
    return None


def extract_references(
    document: dict[str, Any],
    by_path: dict[tuple[str, ...], str],
) -> list[dict[str, Any]]:
    """Extract references with exact block-level provenance."""

    references: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()

    for block in document.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("block_id") or "")
        text = str(block.get("text") or "")
        if not block_id or not text:
            continue
        span = block.get("original_span") or {}

        def add(
            ref_type: str,
            raw: str,
            start: int,
            end: int,
            *,
            target_section_id: str | None = None,
            target_rfc: str | None = None,
            resolved: bool,
            block_id: str = block_id,
            span: dict[str, Any] = span,
        ) -> None:
            key = (block_id, start, end, ref_type)
            if key in seen:
                return
            seen.add(key)
            references.append(
                {
                    "reference_id": _reference_id(block_id, ref_type, raw, start, end),
                    "from_block": block_id,
                    "ref_type": ref_type,
                    "raw": raw,
                    "target_section_id": target_section_id,
                    "target_rfc": target_rfc,
                    "resolved": resolved,
                    "span": span,
                }
            )

        for match in _SECTION_RE.finditer(text):
            raw = match.group(0)
            section_id = _resolve_section(match.group(1), by_path)
            add(
                "section",
                raw,
                match.start(),
                match.end(),
                target_section_id=section_id,
                resolved=section_id is not None,
            )

        for match in _APPENDIX_RE.finditer(text):
            raw = match.group(0)
            section_id = _resolve_section(raw, by_path)
            add(
                "section",
                raw,
                match.start(),
                match.end(),
                target_section_id=section_id,
                resolved=section_id is not None,
            )

        for match in _BRACKET_RFC_RE.finditer(text):
            number = match.group(1)
            raw = match.group(0)
            add(
                "rfc",
                raw,
                match.start(),
                match.end(),
                target_rfc=f"RFC {number}",
                resolved=True,
            )

        for match in _RFC_RE.finditer(text):
            number = match.group(1)
            raw = match.group(0)
            add(
                "rfc",
                raw,
                match.start(),
                match.end(),
                target_rfc=f"RFC {number}",
                resolved=True,
            )

        for match in _BCP_RE.finditer(text):
            number = match.group(1)
            raw = match.group(0)
            add(
                "rfc",
                raw,
                match.start(),
                match.end(),
                target_rfc=f"BCP {number}",
                resolved=True,
            )

    return references


def reference_key(reference: dict[str, Any]) -> tuple[str, str, int]:
    """Sort key used to keep generated indexes stable."""

    span = reference.get("span") or {}
    return (
        str(reference.get("from_block") or ""),
        str(reference.get("ref_type") or ""),
        int(span.get("start", 0) or 0),
    )
