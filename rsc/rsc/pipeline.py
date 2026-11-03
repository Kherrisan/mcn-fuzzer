"""File entry points independent of CLI argument parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rsc.artifacts import read_json, write_json
from rsc.index import build_structural_index, index_summary
from rsc.protocol import CompileError, compile_protocol
from rsc.schemas import check_errors, resolve_schemas_dir, validate_instance, validator

__all__ = ["PipelineError", "CompileError", "compile_protocol", "index_document"]


class PipelineError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def index_document(
    input_path: str | Path,
    *,
    out: str | Path | None = None,
    force: bool = False,
    schemas_dir: str | Path | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path).expanduser().resolve()
    document_path = input_path / "document.json" if input_path.is_dir() else input_path
    if not document_path.is_file():
        raise PipelineError(f"no document.json at {document_path}", 2)
    if not out:
        out_path = document_path.parent / "structural-index.json"
    else:
        out_path = Path(out).expanduser()
        if out_path.suffix.casefold() != ".json":
            out_path /= "structural-index.json"
    if out_path.exists() and not force:
        raise PipelineError(f"{out_path} exists; use --force to overwrite", 2)
    try:
        document = read_json(document_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read {document_path}: {exc}", 2) from exc
    try:
        index = build_structural_index(document)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    check_errors(validate_instance(validator("index", resolve_schemas_dir(schemas_dir)), index))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(out_path, index)
    return {**index_summary(index), "path": str(out_path)}
