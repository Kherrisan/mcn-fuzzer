"""HTML RFC adapter."""

from __future__ import annotations

from .html_legacy import is_legacy_pre_html, parse_legacy_html
from .markup import parse_markup
from .types import ParseResult


def parse_html(
    text: str,
    *,
    source_path: str,
    input_sha256: str,
    doc_id: str,
) -> ParseResult:
    if is_legacy_pre_html(text):
        legacy = parse_legacy_html(
            text,
            source_path=source_path,
            input_sha256=input_sha256,
            doc_id=doc_id,
        )
        if legacy is not None:
            return legacy
    return parse_markup(
        text,
        format_name="html",
        source_path=source_path,
        input_sha256=input_sha256,
        doc_id=doc_id,
    )
