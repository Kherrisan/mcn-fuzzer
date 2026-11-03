"""RFC metadata extraction from the first page/header area."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .spans import Line

SCHEMA_VERSION = "0.1.0"
GENERATOR = "rsc-ingest/0.1.0"

_REQUEST_RE = re.compile(r"(?i)Request for Comments:\s*(\d{1,5})")
_RFC_LINE_RE = re.compile(r"(?i)^\s*RFC\s+(\d{1,5})\b")
_RFC_NAME_RE = re.compile(r"(?i)\brfc[-_ ]?(\d{1,5})\b")
_STEM_DIGITS_RE = re.compile(r"^(?:rfc)?[-_ ]?(\d{1,5})$", re.IGNORECASE)
_XML_RFC_RE = re.compile(r"(?i)<rfc\b[^>]*\bnumber\s*=\s*[\"'](\d{1,5})")
_DOCNAME_RE = re.compile(r"(?i)<docName>\s*(?:RFC\s*)?(\d{1,5})")
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
_DATE_RE = re.compile(rf"(?i)\b({_MONTHS})\s+(\d{{4}})\b")
_AUTHOR_INITIALS_RE = re.compile(r"^[A-Z]\.\s+[A-Z]")
_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")
_STOP_LABELS = {
    "abstract",
    "status of this memo",
    "table of contents",
    "contents",
    "copyright notice",
    "1. introduction",
}
_AFFILIATION_MARKERS = (
    "@",
    "university",
    "institute",
    "inc.",
    "corporation",
    "laboratory",
    "department",
    "research",
    "cisco",
    "google",
    "microsoft",
    "mozilla",
    "mit/lcs",
    "isi",
)


def detect_rfc_number(text: str, source_path: str | Path) -> int | None:
    """Best-effort RFC number from filename, header, or first page."""

    path = Path(source_path)
    stem = path.stem
    match = _STEM_DIGITS_RE.match(stem)
    if match:
        return int(match.group(1))
    match = _RFC_NAME_RE.search(stem)
    if match:
        return int(match.group(1))

    match = _XML_RFC_RE.search(text) or _DOCNAME_RE.search(text)
    if match:
        return int(match.group(1))

    lines = text.splitlines()
    for line in lines[:80]:
        match = _REQUEST_RE.search(line)
        if match:
            return int(match.group(1))
    for line in lines[:20]:
        match = _RFC_LINE_RE.match(line)
        if match:
            return int(match.group(1))
    return None


def extract_title(lines: list[Line], source_path: str | Path) -> str:
    """Pick the most plausible title from the header/title area.

    The heuristic stops at the first recognizable preamble label ("Abstract",
    "Status of this Memo", ...), so body text cannot win.  Among the remaining
    candidates it prefers long lines, especially ones containing ``--`` or a
    subtitle colon, and filters obvious author/affiliation/header lines.
    """

    candidates: list[str] = []
    for line in lines[:80]:
        if line.removed or not line.text.strip():
            continue
        text = line.text.strip()
        lowered = text.lower()
        if lowered in _STOP_LABELS:
            break
        if _HEADING_RE.match(text):
            break
        if _looks_like_header(text) or _looks_like_author(text) or _DATE_RE.search(text):
            continue
        candidates.append(text)

    if not candidates:
        return Path(source_path).stem

    def score(text: str) -> tuple[int, int]:
        value = len(text)
        if "--" in text:
            value += 20
        if ":" in text:
            value += 10
        if 10 <= len(text) <= 120:
            value += 10
        return value, len(text)

    return max(candidates, key=score)


def _looks_like_header(text: str) -> bool:
    lowered = text.lower()
    if re.match(r"(?i)^\s*rfc\s+\d+", text):
        return True
    return any(
        lowered.startswith(prefix)
        for prefix in (
            "request for comments:",
            "category:",
            "obsoletes:",
            "updates:",
            "std:",
            "bcp:",
            "issn:",
            "network working group",
            "internet engineering task force",
            "internet research task force",
            "[page ",
        )
    )


def _looks_like_author(text: str) -> bool:
    lowered = text.lower()
    if _AUTHOR_INITIALS_RE.match(text):
        return True
    if any(marker in lowered for marker in _AFFILIATION_MARKERS):
        return True
    if text.endswith(",") and len(text) < 40:
        return True
    return False


def _field(lines: list[Line], name: str) -> str | None:
    pattern = re.compile(rf"(?i)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$")
    for line in lines[:80]:
        if line.removed:
            continue
        match = pattern.match(line.text)
        if match:
            return re.split(r"\s{2,}", match.group(1).strip())[0].strip()
    return None


def _rfc_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [f"RFC {number}" for number in re.findall(r"\d{1,5}", value)]


def _published(lines: list[Line]) -> str | None:
    for line in lines[:80]:
        if line.removed:
            continue
        match = _DATE_RE.search(line.text)
        if match:
            return f"{match.group(1).title()} {match.group(2)}"
    return None


def build_meta(
    lines: list[Line],
    *,
    source_path: str | Path,
    input_sha256: str,
    format: str,
    doc_id: str,
    text: str,
) -> dict[str, object]:
    """Build the ``document.meta`` object."""

    rfc_number = detect_rfc_number(text, source_path)
    rfc_label = f"RFC {rfc_number}" if rfc_number is not None else Path(source_path).stem
    series = _field(lines, "Category")
    updates = _rfc_list(_field(lines, "Updates"))
    obsoletes = _rfc_list(_field(lines, "Obsoletes"))
    return {
        "schema_version": SCHEMA_VERSION,
        "rfc": rfc_label,
        "title": extract_title(lines, source_path),
        "source_id": doc_id,
        "source_path": str(source_path),
        "input_sha256": input_sha256,
        "format": format,
        "input_set": [rfc_label] if rfc_number is not None else [],
        "series": series,
        "updates": updates,
        "obsoletes": obsoletes,
        "published": _published(lines),
        "normative_references": [],
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": GENERATOR,
    }
