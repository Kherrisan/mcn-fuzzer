"""Adapter for legacy RFC Editor HTML (`rfc2html` pre-block pages).

Older RFC HTML pages are one large ``<pre>`` block with
``<span class="h1">``/``h2``/``h3`` headings and inline links.  A normal
tag-based HTML parser cannot recover sections/paragraphs from that layout, so
we extract the preformatted text, run the TXT structure parser, and map every
span back to the HTML source.
"""

from __future__ import annotations

from bisect import bisect_right
from html.parser import HTMLParser

from .spans import SourceIndex
from .txt import parse_txt
from .types import ParseResult


class _PreTextExtractor(HTMLParser):
    def __init__(self, html_text: str, source: SourceIndex) -> None:
        super().__init__(convert_charrefs=True)
        self.html_text = html_text
        self.source = source
        self.pre_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.segments: list[tuple[int, int, int, int]] = []
        self.search_cursor = 0
        self.plain_length = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        lowered = tag.lower()
        if lowered == "pre":
            self.pre_depth += 1
        elif lowered == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "pre" and self.pre_depth:
            self.pre_depth -= 1
        elif lowered == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.pre_depth <= 0 or not data:
            return

        plain_start = self.plain_length
        self.parts.append(data)
        self.plain_length += len(data)

        found = self.html_text.find(data, self.search_cursor)
        if found >= 0:
            source_start = found
            source_end = found + len(data)
            self.search_cursor = source_end
        else:
            line, column = self.getpos()
            starts = self.source.line_starts
            source_start = (
                starts[line - 1] + column if 1 <= line <= len(starts) else 0
            )
            source_end = source_start + len(data)
        self.segments.append(
            (plain_start, self.plain_length, source_start, source_end)
        )


def _map_offset(offset: int, starts: list[int], segments: list[tuple[int, int, int, int]]) -> int:
    if not segments:
        return max(0, offset)
    index = bisect_right(starts, offset) - 1
    if index < 0:
        return segments[0][2]
    plain_start, plain_end, source_start, source_end = segments[index]
    if offset <= plain_end:
        return source_start + max(0, offset - plain_start)
    return source_end


def _remap_span(
    span: object,
    starts: list[int],
    segments: list[tuple[int, int, int, int]],
    source: SourceIndex,
) -> dict[str, object]:
    if not isinstance(span, dict):
        return source.span(0, 0)
    start = _map_offset(int(span.get("start", 0)), starts, segments)
    end = _map_offset(int(span.get("end", 0)), starts, segments)
    return source.span(start, max(start, end))


def is_legacy_pre_html(text: str) -> bool:
    """Heuristic: rfc2html pages use a pre block with span.h1 headings."""

    lowered = text.lower()
    if '<span class="h1"' in lowered or "<span class='h1'" in lowered:
        return True
    return "<pre" in lowered and "<section" not in lowered and "<h1" not in lowered


def parse_legacy_html(
    html_text: str,
    *,
    source_path: str,
    doc_id: str,
) -> ParseResult | None:
    source = SourceIndex(doc_id, html_text)
    extractor = _PreTextExtractor(html_text, source)
    extractor.feed(html_text)
    extractor.close()
    if not extractor.parts:
        return None

    plain_text = "".join(extractor.parts)
    parsed = parse_txt(
        plain_text,
        source_path=source_path,
        doc_id=doc_id,
    )
    document = parsed.document
    segments = extractor.segments
    starts = [segment[0] for segment in segments]

    for section in document.get("sections", []):
        if isinstance(section, dict):
            section["span"] = _remap_span(section.get("span"), starts, segments, source)
    for block in document.get("blocks", []):
        if isinstance(block, dict):
            block["original_span"] = _remap_span(
                block.get("original_span"), starts, segments, source
            )
    for entry in document.get("span_map", []):
        if not isinstance(entry, dict):
            continue
        start = _map_offset(
            int(entry.get("source_start", 0)), starts, segments
        )
        end = _map_offset(int(entry.get("source_end", 0)), starts, segments)
        span = source.span(start, max(start, end))
        entry["source_start"] = span["start"]
        entry["source_end"] = span["end"]

    meta = document.get("meta")
    if isinstance(meta, dict):
        meta["format"] = "html"
    parsed.report["format"] = "html"
    parsed.report["html_mode"] = "legacy-pre"
    parsed.report["lines"] = len(html_text.splitlines())
    return parsed
