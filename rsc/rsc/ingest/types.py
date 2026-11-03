"""Shared datatypes for the ingest stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FORMATS = frozenset({"txt", "html", "xml"})
STATUSES = frozenset({"ok", "failed", "skipped", "unchanged", "planned"})


@dataclass(frozen=True)
class IngestOptions:
    """User-facing options for one ingest invocation."""

    out_dir: Path
    recursive: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    format: str = "auto"
    force: bool = False
    dry_run: bool = False
    follow_symlinks: bool = False
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.format not in FORMATS | {"auto"}:
            raise ValueError(f"unsupported format: {self.format!r}")


@dataclass(frozen=True)
class InputFile:
    """One discovered input file."""

    path: Path
    relative_path: str


@dataclass
class ParseResult:
    """A parsed document plus its ingest report."""

    document: dict[str, object]
    report: dict[str, object]


@dataclass
class DocumentResult:
    """Outcome for one input file."""

    doc_id: str
    input_path: Path
    relative_path: str
    format: str
    input_sha256: str
    status: str
    document_path: Path | None = None
    report_path: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def to_batch_entry(self, out_dir: Path) -> dict[str, object]:
        def relative(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return path.resolve().relative_to(out_dir.resolve()).as_posix()
            except ValueError:
                return str(path)

        return {
            "doc_id": self.doc_id,
            "input_path": self.relative_path,
            "format": self.format,
            "input_sha256": self.input_sha256,
            "status": self.status,
            "document": relative(self.document_path),
            "report": relative(self.report_path),
            "counts": dict(self.counts),
            "error": self.error,
        }


@dataclass
class BatchResult:
    """Outcome for a file or directory ingest invocation."""

    batch_id: str
    input_root: Path
    out_dir: Path
    documents: list[DocumentResult]
    batch_path: Path | None = None

    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self.documents),
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "unchanged": 0,
            "planned": 0,
        }
        for document in self.documents:
            if document.status in counts:
                counts[document.status] += 1
        return counts

    def exit_code(self) -> int:
        if any(document.status == "failed" for document in self.documents):
            return 1
        if not self.documents:
            return 2
        return 0

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "input_root": str(self.input_root),
            "out_dir": str(self.out_dir),
            "documents": [doc.to_batch_entry(self.out_dir) for doc in self.documents],
            "summary": self.summary(),
        }
