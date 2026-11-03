"""RFC ingest: input discovery, structure recovery and batch artifacts."""

from __future__ import annotations

from .detect import (
    IngestError,
    InputFile,
    decode_bytes,
    detect_format,
    discover_inputs,
    sha256_hex,
    slugify,
    unique_doc_id,
)
from .html import parse_html
from .pipeline import ingest_path
from .txt import parse_txt
from .types import (
    BatchResult,
    DocumentResult,
    IngestOptions,
    ParseResult,
)
from .validate import validate_document
from .xml import parse_xml

__all__ = [
    "BatchResult",
    "DocumentResult",
    "IngestError",
    "IngestOptions",
    "InputFile",
    "ParseResult",
    "decode_bytes",
    "detect_format",
    "discover_inputs",
    "ingest_path",
    "parse_html",
    "parse_txt",
    "parse_xml",
    "sha256_hex",
    "slugify",
    "unique_doc_id",
    "validate_document",
]
