"""RFCXML adapter (RFC 7991-style XML, parsed with the shared markup parser)."""

from __future__ import annotations

from .markup import parse_markup
from .types import ParseResult


def parse_xml(
    text: str,
    *,
    source_path: str,
    doc_id: str,
) -> ParseResult:
    return parse_markup(
        text,
        format_name="xml",
        source_path=source_path,
        doc_id=doc_id,
    )
