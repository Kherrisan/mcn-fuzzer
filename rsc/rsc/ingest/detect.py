"""Input discovery, format detection, decoding and stable document IDs."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .types import IngestOptions, InputFile

SUPPORTED_SUFFIXES: dict[str, str] = {
    ".txt": "txt",
    ".text": "txt",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
}
_SKIP_DIR_NAMES = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv"})
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class IngestError(ValueError):
    """Invalid input, format or output configuration."""


@dataclass(frozen=True)
class DecodedInput:
    text: str
    encoding: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(value: str) -> str:
    slug = _SAFE_ID.sub("-", value.strip()).strip("-._")
    return slug or "document"


def decode_bytes(data: bytes) -> DecodedInput:
    """Decode RFC text; UTF-8 is preferred, Latin-1 is the fallback."""

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return DecodedInput(data.decode(encoding), encoding)
        except UnicodeDecodeError:
            continue
    return DecodedInput(data.decode("latin-1"), "latin-1")


def detect_format(path: Path, data: bytes, override: str = "auto") -> str:
    """Return ``txt``/``html``/``xml``."""

    if override != "auto":
        return override
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raise IngestError(
            "PDF ingest is not implemented yet; convert the RFC to text, HTML or XML"
        )
    if suffix in SUPPORTED_SUFFIXES:
        return SUPPORTED_SUFFIXES[suffix]
    head = data[:4096].lstrip().lower()
    if head.startswith(b"<?xml") or head.startswith(b"<rfc") or b"<rfc " in head:
        return "xml"
    if head.startswith(b"<!doctype html") or b"<html" in head or b"<body" in head:
        return "html"
    # A file without a useful suffix is still accepted as plain RFC text.
    return "txt"


def _hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts if part not in (".", ".."))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _matches(relative: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _candidate_files(input_path: Path, options: IngestOptions) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise IngestError(f"input path does not exist: {input_path}")

    iterator = input_path.rglob("*") if options.recursive else input_path.iterdir()
    candidates: list[Path] = []
    for path in iterator:
        if path.is_symlink() and not options.follow_symlinks:
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(input_path)
        if _hidden(relative):
            continue
        if any(part in _SKIP_DIR_NAMES for part in relative.parts[:-1]):
            continue
        if _inside(path, options.out_dir):
            continue
        candidates.append(path)
    return candidates


def discover_inputs(input_path: str | Path, options: IngestOptions) -> list[InputFile]:
    """Discover one file or a directory of RFC files.

    Directory mode uses supported suffixes by default.  ``--format`` does not
    broaden discovery to arbitrary files; it only overrides parsing for files
    that are already selected.
    """

    root = Path(input_path).expanduser()
    if not root.exists():
        raise IngestError(f"input path does not exist: {root}")
    root = root.resolve()
    files = _candidate_files(root, options)
    inputs: list[InputFile] = []

    for path in files:
        relative = path.relative_to(root) if root.is_dir() else Path(path.name)
        rel_posix = relative.as_posix()
        if options.include and not _matches(rel_posix, options.include):
            continue
        if options.exclude and _matches(rel_posix, options.exclude):
            continue
        if root.is_dir() and path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        inputs.append(InputFile(path=path, relative_path=rel_posix))

    inputs.sort(key=lambda item: item.relative_path)
    if not inputs:
        raise IngestError(f"no supported RFC files found under {root}")
    return inputs


def unique_doc_id(base: str, digest: str, used: dict[str, str]) -> str:
    """Return a stable, collision-free document ID.

    ``used`` maps an existing doc_id to the input sha256 that produced it.  The
    same content reused under the same base ID is allowed; different content
    gets a short hash suffix.
    """

    base = slugify(base).lower()
    if base not in used or used[base] == digest:
        used[base] = digest
        return base
    candidate = f"{base}-{digest[:8]}"
    index = 2
    while candidate in used and used[candidate] != digest:
        candidate = f"{base}-{digest[:8]}-{index}"
        index += 1
    used[candidate] = digest
    return candidate
