"""End-to-end ingest pipeline for one file or a directory of RFC files."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rsc.artifacts import read_json, write_json

from .detect import (
    IngestError,
    decode_bytes,
    detect_format,
    discover_inputs,
    sha256_hex,
    slugify,
    unique_doc_id,
)
from .metadata import SCHEMA_VERSION, detect_rfc_number
from .types import BatchResult, DocumentResult, IngestOptions, ParseResult
from .validate import validate_document

try:
    from .version import __version__ as RSC_VERSION
except Exception:  # pragma: no cover
    RSC_VERSION = "0.1.0"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, data)
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _counts_from_document(document: dict[str, Any]) -> dict[str, int]:
    sections = document.get("sections", [])
    blocks = document.get("blocks", [])
    return {
        "sections": len(sections) if isinstance(sections, list) else 0,
        "blocks": len(blocks) if isinstance(blocks, list) else 0,
    }


def _existing_sha(document_path: Path) -> str | None:
    document = _read_json(document_path)
    if not document:
        return None
    meta = document.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("input_sha256")
    return value if isinstance(value, str) else None


def _existing_counts(document_path: Path, report_path: Path) -> dict[str, int]:
    report = _read_json(report_path)
    if report:
        counts: dict[str, int] = {}
        for key in ("lines", "sections", "blocks"):
            value = report.get(key)
            if isinstance(value, int):
                counts[key] = value
        if counts:
            return counts
    document = _read_json(document_path)
    if document:
        return _counts_from_document(document)
    return {}


def _parse(
    format_name: str,
    text: str,
    *,
    source_path: str,
    input_sha256: str,
    doc_id: str,
) -> ParseResult:
    if format_name == "txt":
        from .txt import parse_txt

        return parse_txt(
            text,
            source_path=source_path,
            input_sha256=input_sha256,
            doc_id=doc_id,
        )
    if format_name == "html":
        from .html import parse_html

        return parse_html(
            text,
            source_path=source_path,
            input_sha256=input_sha256,
            doc_id=doc_id,
        )
    if format_name == "xml":
        from .xml import parse_xml

        return parse_xml(
            text,
            source_path=source_path,
            input_sha256=input_sha256,
            doc_id=doc_id,
        )
    raise IngestError(f"unsupported format: {format_name!r}")


def _batch_id(input_root: Path) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(str(input_root).encode("utf-8")).hexdigest()[:6]
    return f"batch-{stamp}-{digest}"


def ingest_path(input_path: str | Path, options: IngestOptions) -> BatchResult:
    """Ingest one RFC file or every supported RFC file under a directory."""

    root = Path(input_path).expanduser()
    if not root.exists():
        raise IngestError(f"input path does not exist: {root}")
    root = root.resolve()
    out_dir = options.out_dir.expanduser().resolve()

    inputs = discover_inputs(root, options)
    batch_id = _batch_id(root)
    documents: list[DocumentResult] = []
    used_doc_ids: dict[str, str] = {}
    processed: dict[str, str] = {}

    for input_file in inputs:
        result = DocumentResult(
            doc_id=slugify(input_file.path.stem).lower(),
            input_path=input_file.path,
            relative_path=input_file.relative_path,
            format="unknown",
            input_sha256="",
            status="failed",
        )
        documents.append(result)
        try:
            data = input_file.path.read_bytes()
        except OSError as exc:
            result.error = f"cannot read input: {exc}"
            continue

        digest = sha256_hex(data)
        format_name = detect_format(input_file.path, data, options.format)
        text = decode_bytes(data).text
        rfc_number = detect_rfc_number(text, input_file.relative_path)
        base_id = f"rfc{rfc_number}" if rfc_number is not None else slugify(input_file.path.stem)
        doc_id = unique_doc_id(base_id, digest, used_doc_ids)
        document_path = out_dir / doc_id / "document.json"
        report_path = out_dir / doc_id / "ingest-report.json"
        result.doc_id, result.format, result.input_sha256 = doc_id, format_name, digest
        result.document_path, result.report_path = document_path, report_path

        duplicate = processed.get(doc_id) == digest
        existing_sha = (
            _existing_sha(document_path) if not duplicate and document_path.is_file() else None
        )
        if duplicate or (existing_sha == digest and not options.force):
            result.status = "unchanged"
            result.counts = _existing_counts(document_path, report_path)
            processed[doc_id] = digest
            continue
        if options.dry_run:
            result.status = "planned"
            processed[doc_id] = digest
            continue

        try:
            parsed = _parse(
                format_name, text, source_path=input_file.relative_path,
                input_sha256=digest, doc_id=doc_id,
            )
            line_count = int(parsed.report.get("lines", len(text.splitlines())))
            errors = validate_document(parsed.document, source_text=text, line_count=line_count)
            if errors:
                raise IngestError("; ".join(errors[:8]))
            parsed.report["valid"] = True
            _write_json(document_path, parsed.document)
            _write_json(report_path, parsed.report)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            continue

        processed[doc_id] = digest
        result.status = "ok"
        result.counts = {
            key: value for key, value in parsed.report.items()
            if key in ("lines", "sections", "blocks") and isinstance(value, int)
        }

    batch_path: Path | None = None
    if not options.dry_run:
        batch = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "input_root": str(root),
            "created_at": datetime.now(UTC).isoformat(),
            "rsc_version": RSC_VERSION,
            "command": list(options.command) or ["rsc", "ingest", str(root)],
            "documents": [document.to_batch_entry(out_dir) for document in documents],
            "summary": {
                "total": len(documents),
                "ok": sum(1 for item in documents if item.status == "ok"),
                "failed": sum(1 for item in documents if item.status == "failed"),
                "skipped": sum(1 for item in documents if item.status == "skipped"),
                "unchanged": sum(1 for item in documents if item.status == "unchanged"),
            },
        }
        batch_path = out_dir / "batch.json"
        _write_json(batch_path, batch)

    result = BatchResult(
        batch_id=batch_id,
        input_root=root,
        out_dir=out_dir,
        documents=documents,
        batch_path=batch_path,
    )
    return result
