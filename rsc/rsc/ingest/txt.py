"""Plain-text RFC parser.

Classic RFC text is paginated: every page may carry a header and footer,
paragraphs are hard-wrapped, and lists/tables/ABNF/artwork must survive.  The
parser is deterministic and does not call an LLM.
"""

from __future__ import annotations

import re
from collections import Counter

from .detect import slugify
from .metadata import build_meta
from .spans import FOOTER_RE, Line, is_blank, span_for_lines, split_lines
from .types import ParseResult

_STRICT_HEADER_RE = re.compile(
    r"(?i)^\s*(Request for Comments|Category|Obsoletes|Updates|STD|BCP|ISSN)\s*:"
)
_NETWORK_HEADER_RE = re.compile(
    r"(?i)^\s*(Network Working Group|Internet Engineering Task Force|"
    r"Internet Research Task Force)\s*$"
)
_RFC_HEADER_LINE_RE = re.compile(r"(?i)^\s*RFC\s+\d{1,5}\b")
_MONTH_YEAR_RE = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{4}\b"
)
_TOC_HEADING_RE = re.compile(r"(?i)^\s*(Table of Contents|Contents)\s*$")
_TOC_ENTRY_RE = re.compile(r"\.{3,}\s*\d+\s*$")
_TOC_DOTTED_ENTRY_RE = re.compile(r"(?:\.\s*){3,}\d{1,4}\s*$")
_TOC_PAGE_ENTRY_RE = re.compile(r"\s{2,}\d{1,4}\s*$")
_TOC_NUMBERED_ENTRY_RE = re.compile(
    r"^\s*\d+(?:\.\d+)*\.?\s+.*\s+\d{1,4}\s*$"
)

_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-Z][^\n]{0,140})$")
_APPENDIX_HEADING_RE = re.compile(r"^\s*(Appendix\s+[A-Z](?:\.\d+)*)\.?\s*(.*)$")
_UNNUMBERED_HEADINGS = {
    "references",
    "acknowledgements",
    "acknowledgments",
    "security considerations",
    "iana considerations",
    "authors' addresses",
    "author's address",
}
_KNOWN_SECTION_TITLES = _UNNUMBERED_HEADINGS | {
    "introduction",
    "conclusions",
    "normative references",
    "informative references",
}
_STOP_LABELS = {
    "abstract",
    "status of this memo",
    "table of contents",
    "contents",
    "copyright notice",
}

_LIST_RE = re.compile(r"^\s*(o|[*+-]|\(?\d+\)|\d+\.)\s+(.*)$")
_GRAMMAR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*=\s*")
_STRUCTURE_RE = re.compile(
    r"^\s*(?:struct|enum|union|select)\s*(?:\([^)]*\))?\s*\{"
)


class TxtParser:
    """Parse one plain-text RFC into a ``document.json`` object."""

    def parse(
        self,
        text: str,
        *,
        source_path: str,
        doc_id: str,
    ) -> ParseResult:
        lines = split_lines(text)
        meta = build_meta(
            lines,
            source_path=source_path,
            format="txt",
            doc_id=doc_id,
            text=text,
        )
        removed = _strip_page_artifacts(lines)
        removed["toc"] = _strip_toc(lines)
        removed["page_break"] = _collapse_page_break_blanks(lines)
        sections, blocks, span_map = _build_structure(lines, meta, text)
        document = {
            "meta": meta,
            "sections": sections,
            "blocks": blocks,
            "span_map": span_map,
        }
        report = _build_report(document, lines, removed)
        return ParseResult(document=document, report=report)


def parse_txt(
    text: str,
    *,
    source_path: str,
    doc_id: str,
) -> ParseResult:
    return TxtParser().parse(
        text,
        source_path=source_path,
        doc_id=doc_id,
    )


# --------------------------------------------------------------------------- #
# page artifacts
# --------------------------------------------------------------------------- #


def _normalize_line(text: str) -> str:
    return " ".join(text.split())


def _looks_like_repeated_header(text: str) -> bool:
    if _RFC_HEADER_LINE_RE.match(text):
        return True
    if _MONTH_YEAR_RE.search(text):
        return True
    stripped = text.strip()
    return bool(stripped) and len(stripped) < 90 and stripped.isupper()


def _strip_page_artifacts(lines: list[Line]) -> dict[str, int]:
    counts = {"headers": 0, "footers": 0}

    for line in lines:
        if FOOTER_RE.search(line.text):
            line.removed = "footer"
            counts["footers"] += 1

    for line in lines:
        if line.removed:
            continue
        if _STRICT_HEADER_RE.match(line.text) or _NETWORK_HEADER_RE.match(line.text):
            line.removed = "header"
            counts["headers"] += 1

    # Group non-empty lines by page and inspect the top of each page.  A line
    # that repeats at the top of at least two pages is a page header.
    pages: dict[int, list[Line]] = {}
    for line in lines:
        if line.removed or is_blank(line):
            continue
        pages.setdefault(line.page, []).append(line)

    top_counts: Counter[str] = Counter()
    for page_lines in pages.values():
        for line in page_lines[:5]:
            top_counts[_normalize_line(line.text)] += 1

    for page_lines in pages.values():
        for line in page_lines[:5]:
            if line.removed:
                continue
            normalized = _normalize_line(line.text)
            repeated = top_counts[normalized] >= 2 and _looks_like_repeated_header(
                line.text
            )
            at_page_top = _RFC_HEADER_LINE_RE.match(line.text) or (
                _MONTH_YEAR_RE.search(line.text) and line.page > 1
            )
            if repeated or at_page_top:
                line.removed = "header"
                counts["headers"] += 1

    return counts


def _is_toc_entry(text: str) -> bool:
    return bool(
        _TOC_ENTRY_RE.search(text)
        or _TOC_DOTTED_ENTRY_RE.search(text)
        or _TOC_PAGE_ENTRY_RE.search(text)
        or _TOC_NUMBERED_ENTRY_RE.match(text)
    )


def _strip_toc(lines: list[Line]) -> int:
    start: int | None = None
    for index, line in enumerate(lines[:300]):
        if line.removed:
            continue
        if _TOC_HEADING_RE.match(line.text):
            start = index
            break
    if start is None:
        return 0

    removed = 0
    heading = lines[start]
    heading.removed = "toc"
    removed += 1

    def next_content(index: int) -> int:
        while index < len(lines) and (lines[index].removed or is_blank(lines[index])):
            index += 1
        return index

    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.removed:
            index += 1
            continue
        if is_blank(line):
            line.removed = "toc"
            removed += 1
            index += 1
            continue
        if _is_toc_entry(line.text):
            line.removed = "toc"
            removed += 1
            index += 1
            continue
        # TOC entries can wrap: "13.3.4 Rules ... Last-" followed by
        # "modified Dates ........ 85".  Keep the wrapped title when the next
        # content line still looks like a TOC entry.
        following = next_content(index + 1)
        if following < len(lines) and _is_toc_entry(lines[following].text):
            line.removed = "toc"
            removed += 1
            index += 1
            continue
        break
    return removed


_CONTINUATION_ENDINGS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "can",
        "for",
        "from",
        "if",
        "is",
        "may",
        "must",
        "no",
        "not",
        "of",
        "or",
        "should",
        "that",
        "the",
        "to",
        "was",
        "were",
        "when",
        "which",
        "will",
        "with",
    }
)


def _collapse_page_break_blanks(lines: list[Line]) -> int:
    """Drop blank lines that only exist because a page header/footer was removed.

    A paragraph interrupted by a page boundary looks like::

        content ...
        [Page N]
        RFC ...
        content continues ...

    The header/footer themselves are removed, but the surrounding blank lines
    would still split the paragraph.  We remove those blanks only when the
    previous content line does not end a sentence and the next line looks like
    a continuation (lowercase start, punctuation, or a continuation word).
    """

    count = 0
    for index, line in enumerate(lines):
        if line.removed or not is_blank(line):
            continue
        previous = index - 1
        while previous >= 0 and (lines[previous].removed or is_blank(lines[previous])):
            previous -= 1
        following = index + 1
        while following < len(lines) and (
            lines[following].removed or is_blank(lines[following])
        ):
            following += 1
        if previous < 0 or following >= len(lines):
            continue
        between = lines[previous + 1 : following]
        if not any(item.removed in ("header", "footer") for item in between):
            continue
        previous_text = lines[previous].text.rstrip()
        following_text = lines[following].text.lstrip()
        if not previous_text or not following_text:
            continue
        if previous_text.endswith((".", "!", "?", ":", ";")):
            continue
        last_word = previous_text.split()[-1].lower().strip(",;:")
        continues = (
            following_text[0].islower()
            or following_text[0] in ",;:)"
            or last_word in _CONTINUATION_ENDINGS
        )
        if continues:
            line.removed = "page_break"
            count += 1
    return count


# --------------------------------------------------------------------------- #
# headings
# --------------------------------------------------------------------------- #


def _valid_heading_title(title: str) -> bool:
    """Cheap sanity checks for a numbered heading title.

    Title casing is intentionally not required: many RFCs use sentence-case
    headings ("Goals, non-goals, and limitations").  The section-number
    monotonicity check in :func:`_build_structure` rejects most numbered list
    items instead.
    """

    words = title.split()
    if not words:
        return False
    if title.rstrip().endswith((".", ";", ",")):
        return False
    if len(words) > 25:
        return False
    return True


def _path_prefix(parent: object, child: list[str]) -> bool:
    if not isinstance(parent, list):
        return False
    return child[: len(parent)] == parent


def _match_heading(text: str) -> tuple[list[str], str, int] | None:
    stripped = text.strip()
    if not stripped or len(stripped) > 180:
        return None
    indent = len(text) - len(text.lstrip())
    if indent > 20:
        return None
    if "...." in stripped or stripped.startswith(("[", "+", "|")):
        return None
    if stripped.lower() in _STOP_LABELS:
        return None

    match = _NUMBERED_HEADING_RE.match(stripped)
    if match:
        number = match.group(1)
        title = match.group(2).strip()
        if not title or not _valid_heading_title(title):
            return None
        path = number.split(".")
        return path, title, len(path)

    # Unnumbered headings and appendices are top-level and should not be
    # confused with indented body text.
    if indent > 1:
        return None
    match = _APPENDIX_HEADING_RE.match(stripped)
    if match:
        label = match.group(1).strip()
        rest = match.group(2).strip()
        title = f"{label} {rest}".strip()
        return [slugify(label).lower()], title, 1

    lowered = stripped.lower()
    if lowered in _UNNUMBERED_HEADINGS:
        return [slugify(stripped).lower()], stripped, 1
    return None


# --------------------------------------------------------------------------- #
# block construction
# --------------------------------------------------------------------------- #


def _join_paragraph_text(parts: list[str]) -> tuple[str, str]:
    result = ""
    transform = "identity"
    for raw in parts:
        piece = raw.strip()
        if not piece:
            continue
        if not result:
            result = piece
            continue
        if result.endswith("-") and piece[0].islower():
            result = result[:-1] + piece
            transform = "dehyphenate"
        else:
            result += " " + piece
            if transform == "identity":
                transform = "unwrap"
    if len([part for part in parts if part.strip()]) > 1 and transform == "identity":
        transform = "unwrap"
    return result, transform


def _is_table_start(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("+") and "-" in stripped


def _is_table_line(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(("+", "|"))


def _is_grammar_start(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith(("+", "|")):
        return False
    return bool(_GRAMMAR_RE.match(text) or _STRUCTURE_RE.match(text))


def _is_diagram_start(text: str) -> bool:
    indent = len(text) - len(text.lstrip())
    if indent < 3:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if "+-" in stripped or stripped.startswith("|"):
        return True
    return bool(re.search(r"\d[\d\s]{10,}", stripped))


def _table_rows(lines: list[Line]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.text.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells:
            rows.append(cells)
    return rows


def _grammar_rule_names(lines: list[Line]) -> list[str]:
    names: list[str] = []
    for line in lines:
        match = _GRAMMAR_RE.match(line.text)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _build_structure(
    lines: list[Line],
    meta: dict[str, object],
    text: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    source_id = str(meta["source_id"])
    sections: list[dict[str, object]] = []
    blocks: list[dict[str, object]] = []
    span_map: list[dict[str, object]] = []
    section_ids: set[str] = set()
    stack: list[dict[str, object]] = []
    current_section: dict[str, object] | None = None
    order = 0
    last_block_id: str | None = None
    last_block_type: str | None = None
    last_top_number = -1

    def close_section(end_offset: int, end_line: int) -> None:
        nonlocal current_section
        if current_section is None:
            return
        span = current_section["span"]
        assert isinstance(span, dict)
        span["end"] = max(int(span["start"]), end_offset)
        span["end_line"] = max(int(span["start_line"]), end_line)
        current_section = None

    def add_block(
        block_type: str,
        value: str,
        start_index: int,
        end_index: int,
        attrs: dict[str, object],
        transform: str,
        lead_in: str | None = None,
    ) -> dict[str, object]:
        nonlocal order, last_block_id, last_block_type
        block_id = f"blk-{order:06d}"
        span = span_for_lines(source_id, lines, start_index, end_index)
        block = {
            "block_id": block_id,
            "section_path": list(current_section["path"]) if current_section else [],
            "block_type": block_type,
            "text": value,
            "original_span": span,
            "parent_block": None,
            "lead_in_block_id": lead_in,
            "order": order,
            "attrs": attrs,
        }
        blocks.append(block)
        span_map.append(
            {
                "block_id": block_id,
                "normalized_start": 0,
                "normalized_end": len(value),
                "source_start": span["start"],
                "source_end": span["end"],
                "transform": transform,
            }
        )
        order += 1
        last_block_id = block_id
        last_block_type = block_type
        return block

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.removed or is_blank(line):
            index += 1
            continue

        heading = _match_heading(line.text)
        if heading is not None:
            path, title, level = heading
            known_title = title.lower() in _KNOWN_SECTION_TITLES
            accepted = True
            if path and path[0].isdigit():
                first = int(path[0])
                if first > 99:
                    accepted = False
                elif len(path) == 1:
                    if (
                        last_top_number >= 0
                        and first <= last_top_number
                        and not known_title
                    ):
                        accepted = False
                    elif (
                        last_top_number >= 0
                        and first > last_top_number + 3
                        and not known_title
                    ):
                        accepted = False
                elif (
                    last_top_number >= 0
                    and first < last_top_number
                    and not known_title
                ):
                    accepted = False
            if accepted:
                following = index + 1
                while following < len(lines) and lines[following].removed:
                    following += 1
                if (
                    not known_title
                    and following < len(lines)
                    and not is_blank(lines[following])
                    and lines[following].text.startswith((" ", "\t"))
                    and (
                        line.text.rstrip().endswith(".")
                        or title.split()[-1][:1].islower()
                    )
                ):
                    accepted = False
            if not accepted:
                heading = None

        if heading is not None:
            close_section(line.start, line.number - 1)
            path, title, level = heading
            if len(path) == 1 and path[0].isdigit():
                last_top_number = int(path[0])
            while stack and (
                int(stack[-1]["level"]) >= level
                or not _path_prefix(stack[-1]["path"], path)
            ):
                stack.pop()
            parent = stack[-1] if stack else None
            base_id = "sec-" + "-".join(slugify(part).lower() for part in path)
            section_id = base_id
            suffix = 2
            while section_id in section_ids:
                section_id = f"{base_id}-{suffix}"
                suffix += 1
            section_ids.add(section_id)
            section = {
                "section_id": section_id,
                "path": path,
                "title": title,
                "level": level,
                "span": span_for_lines(source_id, lines, index, len(lines) - 1),
                "parent_section_id": parent["section_id"] if parent else None,
            }
            sections.append(section)
            stack.append(section)
            current_section = section
            add_block("heading", line.text.strip(), index, index, {"level": level}, "identity")
            index += 1
            continue

        if _is_table_start(line.text):
            end = index + 1
            while (
                end < len(lines)
                and not lines[end].removed
                and not is_blank(lines[end])
                and _is_table_line(lines[end].text)
            ):
                end += 1
            value = "\n".join(item.text.rstrip() for item in lines[index:end])
            add_block(
                "table",
                value,
                index,
                end - 1,
                {"rows": _table_rows(lines[index:end])},
                "identity",
            )
            index = end
            continue

        if _is_grammar_start(line.text):
            end = index + 1
            while end < len(lines) and not lines[end].removed and not is_blank(lines[end]):
                end += 1
            value = "\n".join(item.text.rstrip() for item in lines[index:end])
            names = _grammar_rule_names(lines[index:end])
            add_block(
                "grammar",
                value,
                index,
                end - 1,
                {
                    "language": "abnf" if names else "c-like",
                    "rule_names": names,
                },
                "identity",
            )
            index = end
            continue

        if _is_diagram_start(line.text):
            end = index + 1
            while (
                end < len(lines)
                and not lines[end].removed
                and not is_blank(lines[end])
                and lines[end].text.startswith("   ")
            ):
                end += 1
            value = "\n".join(item.text.rstrip() for item in lines[index:end])
            indent = len(line.text) - len(line.text.lstrip())
            add_block("diagram", value, index, end - 1, {"indent": indent}, "identity")
            index = end
            continue

        list_match = _LIST_RE.match(line.text)
        if list_match is not None:
            lead_in = last_block_id if last_block_type in ("paragraph", "preamble") else None
            end = index
            items: list[dict[str, object]] = []
            current: dict[str, object] | None = None
            while end < len(lines):
                item_line = lines[end]
                if item_line.removed:
                    if item_line.removed in ("header", "footer", "page_break"):
                        end += 1
                        continue
                    break
                if is_blank(item_line):
                    following = end + 1
                    while following < len(lines) and (
                        lines[following].removed or is_blank(lines[following])
                    ):
                        following += 1
                    if following < len(lines) and _LIST_RE.match(lines[following].text):
                        indent = len(lines[following].text) - len(
                            lines[following].text.lstrip()
                        )
                        if indent <= 1 and _match_heading(lines[following].text) is not None:
                            break
                        end = following
                        continue
                    break
                if len(item_line.text) - len(item_line.text.lstrip()) <= 1:
                    if _match_heading(item_line.text) is not None:
                        break
                match = _LIST_RE.match(item_line.text)
                if match is not None:
                    if current is not None:
                        items.append(current)
                    current = {
                        "start": end,
                        "end": end,
                        "lines": [match.group(2).strip()],
                        "marker": match.group(1),
                        "indent": len(item_line.text) - len(item_line.text.lstrip()),
                    }
                    end += 1
                elif current is not None and item_line.text.startswith(" "):
                    current["end"] = end
                    assert isinstance(current["lines"], list)
                    current["lines"].append(item_line.text.strip())
                    end += 1
                else:
                    break
            if current is not None:
                items.append(current)
            if not items:
                index += 1
                continue
            for item in items:
                item_lines = [str(part) for part in item["lines"]]  # type: ignore[index]
                value, transform = _join_paragraph_text(item_lines)
                add_block(
                    "list_item",
                    value,
                    int(item["start"]),
                    int(item["end"]),
                    {
                        "marker": item["marker"],
                        "list_level": max(1, int(item["indent"]) // 3),
                    },
                    transform,
                    lead_in,
                )
            index = end
            continue

        end = index
        parts: list[str] = []
        while end < len(lines):
            candidate = lines[end]
            if candidate.removed:
                if candidate.removed in ("header", "footer", "page_break"):
                    end += 1
                    continue
                break
            if is_blank(candidate):
                break
            if end > index:
                if (
                    _match_heading(candidate.text) is not None
                    or _is_table_start(candidate.text)
                    or _is_grammar_start(candidate.text)
                    or _is_diagram_start(candidate.text)
                    or _LIST_RE.match(candidate.text) is not None
                ):
                    break
            parts.append(candidate.text)
            end += 1
        value, transform = _join_paragraph_text(parts)
        if value:
            block_type = "preamble" if current_section is None else "paragraph"
            add_block(block_type, value, index, end - 1, {}, transform)
        index = max(end, index + 1)

    close_section(len(text), len(lines))
    return sections, blocks, span_map


def _build_report(
    document: dict[str, object],
    lines: list[Line],
    removed: dict[str, int],
) -> dict[str, object]:
    blocks = document["blocks"]
    sections = document["sections"]
    meta = document["meta"]
    assert isinstance(blocks, list)
    assert isinstance(sections, list)
    assert isinstance(meta, dict)

    covered: set[int] = set()
    for block in blocks:
        span = block["original_span"]  # type: ignore[index]
        assert isinstance(span, dict)
        start = int(span["start_line"])
        end = int(span["end_line"])
        covered.update(range(start, end + 1))

    unmapped = sum(
        1
        for line in lines
        if not line.removed and not is_blank(line) and line.number not in covered
    )
    by_type = Counter(str(block["block_type"]) for block in blocks)  # type: ignore[index]
    warnings: list[str] = []
    if not sections:
        warnings.append("no sections detected")
    paths = [
        tuple(str(part) for part in section["path"])  # type: ignore[index]
        for section in sections
        if isinstance(section, dict) and isinstance(section.get("path"), list)
    ]
    duplicates = len(paths) - len(set(paths))
    if duplicates:
        warnings.append(f"{duplicates} duplicate section path(s)")
    if unmapped:
        warnings.append(f"{unmapped} non-empty line(s) are not covered by a block")
    if str(meta["title"]) == str(meta["source_id"]):
        warnings.append("title fell back to source id")

    return {
        "doc_id": meta["source_id"],
        "format": meta["format"],
        "lines": len(lines),
        "sections": len(sections),
        "blocks": len(blocks),
        "blocks_by_type": dict(sorted(by_type.items())),
        "removed": dict(removed),
        "unmapped_lines": unmapped,
        "warnings": warnings,
        "valid": None,
    }
