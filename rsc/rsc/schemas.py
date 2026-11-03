"""JSON Schema loading and artifact validation, independent of the CLI."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from rsc.artifacts import read_json
from rsc.config import ConfigError

STAGE_FILES: dict[str, str] = {
    "document": "document.schema.json",
    "index": "index.schema.json",
    "protocol": "protocol.schema.json",
    "manifest": "manifest.schema.json",
    "batch": "batch.schema.json",
}
JSONL_STAGES = frozenset()


def default_schemas_dir() -> Path:
    """Locate the schema directory in a checkout or an installed wheel."""

    env = os.environ.get("RSC_SCHEMAS_DIR")
    if env:
        path = Path(env).expanduser()
        if not path.is_dir():
            raise ConfigError(f"RSC_SCHEMAS_DIR is not a directory: {path}")
        return path.resolve()

    checkout = Path(__file__).resolve().parents[1] / "schemas"
    if checkout.is_dir():
        return checkout

    with suppress(Exception):
        import sysconfig

        data = sysconfig.get_path("data")
        if data:
            installed = Path(data) / "share" / "rsc" / "schemas"
            if installed.is_dir():
                return installed

    raise ConfigError("cannot locate RSC schemas; run from a checkout or set RSC_SCHEMAS_DIR")


def resolve_schemas_dir(value: str | Path | None) -> Path:
    return Path(value).expanduser().resolve() if value else default_schemas_dir()


def read_schema_json(path: Path) -> Any:
    try:
        return read_json(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def schema_path(stage: str, schemas_dir: Path) -> Path:
    if stage not in STAGE_FILES:
        raise ConfigError(f"unknown stage {stage!r}; choose from {sorted(STAGE_FILES)}")
    path = schemas_dir / STAGE_FILES[stage]
    if not path.is_file():
        raise ConfigError(f"schema not found: {path}")
    return path


def build_registry(schemas_dir: Path):
    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ConfigError(
            "jsonschema/referencing is required for validation; "
            "install with `pip install jsonschema`"
        ) from exc

    registry = Registry()
    for path in sorted(schemas_dir.glob("*.schema.json")):
        data = read_schema_json(path)
        if not isinstance(data, dict):
            raise ConfigError(f"schema must be a JSON object: {path}")
        resource = Resource.from_contents(data, default_specification=DRAFT202012)
        registry = registry.with_resource(uri=path.as_uri(), resource=resource)
        schema_id = data.get("$id")
        if isinstance(schema_id, str) and schema_id:
            registry = registry.with_resource(uri=schema_id, resource=resource)
    return registry


def validator(stage: str, schemas_dir: Path):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ConfigError(
            "jsonschema is required for validation; install with `pip install jsonschema`"
        ) from exc
    schema = read_schema_json(schema_path(stage, schemas_dir))
    registry = build_registry(schemas_dir)
    return Draft202012Validator(schema, registry=registry)


def format_error(error: Any) -> str:
    if isinstance(error, str):
        return error
    path = getattr(error, "json_path", "$")
    return f"{path}: {error.message}"


def validate_instance(validator: Any, instance: Any) -> list[str]:
    return [format_error(error) for error in validator.iter_errors(instance)]


def validate_rows(
    rows: list[dict[str, Any]],
    stage: str,
    schemas_dir: Path,
) -> list[str]:
    schema_validator = validator(stage, schemas_dir)
    return [
        f"{stage}[{index}]: {error}"
        for index, row in enumerate(rows)
        for error in validate_instance(schema_validator, row)
    ]


class SchemaValidationError(ValueError):
    """One or more artifacts failed their schema checks."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def check_errors(errors: list[str]) -> None:
    if errors:
        raise SchemaValidationError(errors)
