"""Line splitting, original-span helpers and source-position bookkeeping."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

FOOTER_RE = re.compile(r"\[\s*Page\s+\d+\s*\]", re.IGNORECASE)


@dataclass
class Line:
    """One physical line, with offsets into the original source text."""

    text: str
    start: int
    end: int
    number: int
    page: int = 1
    removed: str | None = None
    form_feed: bool = False


def split_lines(text: str) -> list[Line]:
    """Split ``text`` while retaining source offsets and form-feed markers."""

    lines: list[Line] = []
    offset = 0
    for number, raw in enumerate(text.splitlines(keepends=True), start=1):
        content = raw
        if content.endswith("\n"):
            content = content[:-1]
        if content.endswith("\r"):
            content = content[:-1]
        form_feed = "\f" in content
        display = content.replace("\f", "")
        lines.append(
            Line(
                text=display,
                start=offset,
                end=offset + len(content),
                number=number,
                form_feed=form_feed,
            )
        )
        offset += len(raw)
    if not lines and text:
        lines.append(Line(text=text, start=0, end=len(text), number=1))
    assign_pages(lines)
    return lines


def assign_pages(lines: list[Line]) -> None:
    """Assign page numbers using form feeds and ``[Page N]`` footers."""

    page = 1
    for line in lines:
        line.page = page
        if line.form_feed or FOOTER_RE.search(line.text):
            page += 1


def is_blank(line: Line) -> bool:
    return not line.text.strip()


def make_span(
    source_id: str,
    start: int,
    end: int,
    start_line: int,
    end_line: int,
    *,
    page: int | None = None,
    bbox: list[float] | None = None,
) -> dict[str, object]:
    span: dict[str, object] = {
        "source_id": source_id,
        "start": max(0, int(start)),
        "end": max(0, int(end)),
        "start_line": max(1, int(start_line)),
        "end_line": max(1, int(end_line)),
    }
    if page is not None:
        span["page"] = int(page)
    if bbox is not None:
        span["bbox"] = list(bbox)
    return span


def span_for_lines(
    source_id: str,
    lines: list[Line],
    start_index: int,
    end_index: int,
) -> dict[str, object]:
    """Span covering ``lines[start_index:end_index]`` (inclusive)."""

    if not lines:
        return make_span(source_id, 0, 0, 1, 1)
    first = lines[max(0, min(start_index, len(lines) - 1))]
    last = lines[max(0, min(end_index, len(lines) - 1))]
    return make_span(
        source_id,
        first.start,
        last.end,
        first.number,
        last.number,
        page=first.page,
    )


class SourceIndex:
    """Map character offsets to 1-based line numbers and locate snippets."""

    def __init__(self, source_id: str, text: str) -> None:
        self.source_id = source_id
        self.text = text
        self.line_starts = [0]
        for match in re.finditer("\n", text):
            self.line_starts.append(match.end())

    def line_for(self, offset: int) -> int:
        offset = max(0, min(offset, len(self.text)))
        return bisect_right(self.line_starts, offset)

    def span(self, start: int, end: int) -> dict[str, object]:
        start = max(0, min(start, len(self.text)))
        end = max(start, min(end, len(self.text)))
        return make_span(
            self.source_id,
            start,
            end,
            self.line_for(start),
            self.line_for(max(start, end - 1)),
        )

    def locate(self, needle: str, cursor: int = 0) -> tuple[dict[str, object] | None, int]:
        """Locate ``needle`` at/after ``cursor``; return span and next cursor."""

        if not needle:
            return None, cursor
        position = self.text.find(needle, cursor)
        if position < 0:
            return None, cursor
        return self.span(position, position + len(needle)), position + len(needle)
