"""Shared HTML/RFCXML parser.

Standard-library HTML parsing gives us tag names, attributes and line/column
positions.  We use those positions to build spans directly from the source,
which keeps HTML/XML traceability useful without a browser-grade DOM.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from .detect import slugify
from .metadata import GENERATOR, detect_rfc_number
from .spans import SourceIndex
from .types import ParseResult

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCK_TAGS = _HEADING_TAGS | {
    "p",
    "t",
    "li",
    "dt",
    "dd",
    "pre",
    "artwork",
    "table",
    "figure",
}
_GRAMMAR_RE = re.compile(r"(?m)^\s*[A-Za-z][A-Za-z0-9-]*\s*=")


def _normalize_inline(text: str) -> str:
    return " ".join(text.split())


def _normalize_pre(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


def _section_slug(title: str) -> str:
    cleaned = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", title).strip()
    return slugify(cleaned).lower()


class MarkupParser(HTMLParser):
    def __init__(self, text: str, *, source_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.text = text
        self.source = SourceIndex(source_id, text)
        self.sections: list[dict[str, object]] = []
        self.blocks: list[dict[str, object]] = []
        self.span_map: list[dict[str, object]] = []
        self.stack: list[dict[str, object]] = []
        self.current_section: dict[str, object] | None = None
        self.current: dict[str, object] | None = None
        self.section_ids: set[str] = set()
        self.order = 0
        self.last_block_id: str | None = None
        self.last_block_type: str | None = None
        self.last_list_lead_in: str | None = None
        self.title_parts: list[str] = []
        self.in_title = False
        self.rfc_number: int | None = None

    # -- positions ---------------------------------------------------------

    def _offset(self) -> int:
        line, column = self.getpos()
        starts = self.source.line_starts
        if 1 <= line <= len(starts):
            return min(len(self.text), starts[line - 1] + column)
        return len(self.text)

    # -- parser hooks ------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.split(":")[-1].lower()
        attributes = {
            key.split(":")[-1].lower(): value for key, value in attrs
        }
        offset = self._offset()

        if tag == "title":
            self.in_title = True
            return
        if tag in ("br", "hr", "img", "meta", "link"):
            return
        if tag == "rfc":
            number = attributes.get("number")
            if number and number.isdigit():
                self.rfc_number = int(number)
        if tag in ("td", "th", "tr") and self.current is not None:
            if self.current["type"] == "table":
                buffer = self.current["buffer"]
                assert isinstance(buffer, list)
                buffer.append(" ")

        if tag == "section":
            self._flush(offset)
            title = (attributes.get("title") or "section").strip()
            level = len(self.stack) + 1
            section = self._create_section(title, level, offset)
            self._add_block(
                "heading",
                title,
                offset,
                min(len(self.text), offset + len(title)),
                {"level": section["level"]},
            )
            return

        if tag in ("p", "t") and self.current is not None:
            if self.current["type"] in ("list_item", "table"):
                return

        if tag in _BLOCK_TAGS:
            self._flush(offset)
            if tag in _HEADING_TAGS:
                self.current = {
                    "tag": tag,
                    "type": "heading",
                    "start": offset,
                    "buffer": [],
                    "level": int(tag[1]),
                }
            elif tag == "li":
                self.current = {
                    "tag": tag,
                    "type": "list_item",
                    "start": offset,
                    "buffer": [],
                }
            elif tag in ("dt", "dd"):
                self.current = {
                    "tag": tag,
                    "type": "paragraph",
                    "start": offset,
                    "buffer": [],
                }
            elif tag in ("pre", "artwork"):
                self.current = {
                    "tag": tag,
                    "type": "pre",
                    "start": offset,
                    "buffer": [],
                }
            elif tag == "table":
                self.current = {
                    "tag": tag,
                    "type": "table",
                    "start": offset,
                    "buffer": [],
                }
            elif tag == "figure":
                self.current = {
                    "tag": tag,
                    "type": "diagram",
                    "start": offset,
                    "buffer": [],
                }
            else:
                self.current = {
                    "tag": tag,
                    "type": "paragraph",
                    "start": offset,
                    "buffer": [],
                }

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.split(":")[-1].lower() in ("br", "hr", "img", "meta", "link"):
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.split(":")[-1].lower()
        offset = self._offset()
        if tag == "title":
            self.in_title = False
            return

        if tag == "section":
            self._flush(offset)
            self._close_section(offset)
            if self.stack:
                self.stack.pop()
            self.current_section = self.stack[-1] if self.stack else None
            return

        if self.current is not None and self.current["tag"] == tag:
            self._flush(offset)
        elif tag in ("p", "t") and self.current is not None:
            if self.current["type"] in ("list_item", "table"):
                return

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.current is not None:
            buffer = self.current["buffer"]
            assert isinstance(buffer, list)
            buffer.append(data)

    def finish(self) -> None:
        self._flush(len(self.text))
        while self.stack:
            self._finish_section(self.stack.pop(), len(self.text))

    # -- sections ----------------------------------------------------------

    def _finish_section(self, section: dict[str, object], end_offset: int) -> None:
        span = section["span"]
        assert isinstance(span, dict)
        span["end"] = max(int(span["start"]), end_offset)
        span["end_line"] = self.source.line_for(max(int(span["start"]), end_offset - 1))
        if self.current_section is section:
            self.current_section = None

    def _close_section(self, end_offset: int) -> None:
        if self.current_section is None:
            return
        self._finish_section(self.current_section, end_offset)

    def _create_section(
        self, title: str, level: int, start_offset: int
    ) -> dict[str, object]:
        while self.stack and int(self.stack[-1]["level"]) >= level:
            self._finish_section(self.stack.pop(), start_offset)
        parent = self.stack[-1] if self.stack else None
        parent_path = list(parent["path"]) if parent else []
        path = parent_path + [_section_slug(title)]
        base_id = "sec-" + "-".join(slugify(part).lower() for part in path)
        section_id = base_id
        suffix = 2
        while section_id in self.section_ids:
            section_id = f"{base_id}-{suffix}"
            suffix += 1
        self.section_ids.add(section_id)
        section = {
            "section_id": section_id,
            "path": path,
            "title": title,
            "level": level,
            "span": self.source.span(start_offset, len(self.text)),
            "parent_section_id": parent["section_id"] if parent else None,
        }
        self.sections.append(section)
        self.stack.append(section)
        self.current_section = section
        return section

    # -- blocks ------------------------------------------------------------

    def _add_block(
        self,
        block_type: str,
        text: str,
        start: int,
        end: int,
        attrs: dict[str, object],
        lead_in: str | None = None,
    ) -> None:
        if not text:
            return
        span = self.source.span(start, end)
        block_id = f"blk-{self.order:06d}"
        block = {
            "block_id": block_id,
            "section_path": list(self.current_section["path"])
            if self.current_section
            else [],
            "block_type": block_type,
            "text": text,
            "original_span": span,
            "parent_block": None,
            "lead_in_block_id": lead_in,
            "order": self.order,
            "attrs": attrs,
        }
        self.blocks.append(block)
        self.span_map.append(
            {
                "block_id": block_id,
                "normalized_start": 0,
                "normalized_end": len(text),
                "source_start": span["start"],
                "source_end": span["end"],
                "transform": "identity",
            }
        )
        self.order += 1
        self.last_block_id = block_id
        self.last_block_type = block_type

    def _flush(self, end_offset: int) -> None:
        current = self.current
        if current is None:
            return
        raw = "".join(str(part) for part in current["buffer"])  # type: ignore[index]
        block_type = str(current["type"])
        start = int(current["start"])
        if block_type == "heading":
            title = _normalize_inline(raw)
            if title:
                self._create_section(title, int(current["level"]), start)
                self._add_block(
                    "heading", title, start, max(start, end_offset), {"level": current["level"]}
                )
            self.current = None
            return

        if block_type == "pre":
            text = _normalize_pre(raw)
            attrs: dict[str, object] = {}
            if _GRAMMAR_RE.search(text):
                block_type = "grammar"
                attrs = {"language": "abnf"}
            else:
                block_type = "diagram"
        elif block_type == "table":
            text = _normalize_inline(raw)
            attrs = {}
        elif block_type == "diagram":
            text = _normalize_pre(raw)
            attrs = {}
        else:
            text = _normalize_inline(raw)
            attrs = {}

        if not text:
            self.current = None
            return

        lead_in = None
        if block_type == "list_item":
            if self.last_block_type == "list_item":
                lead_in = self.last_list_lead_in
            elif self.last_block_type in ("paragraph", "preamble"):
                lead_in = self.last_block_id
            self.last_list_lead_in = lead_in
        elif block_type == "paragraph" and self.current_section is None:
            block_type = "preamble"

        self._add_block(block_type, text, start, max(start, end_offset), attrs, lead_in)
        self.current = None


def parse_markup(
    text: str,
    *,
    format_name: str,
    source_path: str,
    doc_id: str,
) -> ParseResult:
    parser = MarkupParser(text, source_id=doc_id)
    parser.feed(text)
    parser.close()
    parser.finish()

    rfc_number = parser.rfc_number or detect_rfc_number(text, source_path)
    title = _normalize_inline(" ".join(parser.title_parts)) or Path(source_path).stem
    rfc_label = f"RFC {rfc_number}" if rfc_number is not None else doc_id
    meta = {
        "rfc": rfc_label,
        "title": title,
        "source_id": doc_id,
        "source_path": source_path,
        "format": format_name,
        "input_set": [rfc_label] if rfc_number is not None else [],
        "series": None,
        "updates": [],
        "obsoletes": [],
        "published": None,
        "normative_references": [],
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": GENERATOR,
    }
    document = {
        "meta": meta,
        "sections": parser.sections,
        "blocks": parser.blocks,
        "span_map": parser.span_map,
    }
    by_type = Counter(str(block["block_type"]) for block in parser.blocks)
    warnings: list[str] = []
    if not parser.sections:
        warnings.append("no sections detected")
    report = {
        "doc_id": doc_id,
        "format": format_name,
        "lines": len(text.splitlines()),
        "sections": len(parser.sections),
        "blocks": len(parser.blocks),
        "blocks_by_type": dict(sorted(by_type.items())),
        "removed": {},
        "unmapped_lines": 0,
        "warnings": warnings,
        "valid": None,
    }
    return ParseResult(document=document, report=report)
