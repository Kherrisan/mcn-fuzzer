#!/usr/bin/env python3
"""CLI for RFC protocol interaction extraction and structural inspection."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rsc import pipeline, schemas
from rsc.agents import check_profiles
from rsc.agents.profiles import print_profiles, selection, selection_arguments
from rsc.config import ConfigError, load_workflow_config, resolve_profiles
from rsc.schemas import SchemaValidationError
from rsc.version import __version__

# Compatibility aliases: the implementation lives in schemas, never in the CLI.
STAGE_FILES = schemas.STAGE_FILES
JSONL_STAGES = schemas.JSONL_STAGES
default_schemas_dir = schemas.default_schemas_dir
_build_registry = schemas.build_registry
_schema_path = schemas.schema_path
_schemas_dir = schemas.resolve_schemas_dir
_read_json = schemas.read_schema_json
_validator = schemas.validator
_format_error = schemas.format_error
_validate_instance = schemas.validate_instance
_validate_rows = schemas.validate_rows


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    schemas_dir = _schemas_dir(args.schemas_dir)
    schema_files = sorted(schemas_dir.glob("*.schema.json"))
    print(f"rsc {__version__}")
    print(f"python {sys.version.split()[0]} ({sys.executable})")
    print(f"schemas {schemas_dir} ({len(schema_files)} files)")

    for module in ("jsonschema", "pydantic", "tiktoken"):
        try:
            importlib.import_module(module)
            print(f"{module}: available")
        except ImportError:
            print(f"{module}: MISSING")

    if not schema_files:
        print("no schemas found", file=sys.stderr)
        return 1
    for path in schema_files:
        try:
            _read_json(path)
        except ConfigError as exc:
            print(f"invalid schema {path.name}: {exc}", file=sys.stderr)
            return 1

    roles = resolve_profiles(load_workflow_config(args.config), **selection(args))
    check_profiles(roles)
    print_profiles(roles)
    return 0


def cmd_schemas_list(args: argparse.Namespace) -> int:
    schemas_dir = _schemas_dir(args.schemas_dir)
    paths = sorted(schemas_dir.glob("*.schema.json"))
    if not paths:
        print(f"no schemas in {schemas_dir}", file=sys.stderr)
        return 1
    for path in paths:
        data = _read_json(path)
        title = data.get("title", "(untitled)") if isinstance(data, dict) else "(not an object)"
        print(f"{path.name:28} {title}")
    return 0


def cmd_schemas_show(args: argparse.Namespace) -> int:
    schemas_dir = _schemas_dir(args.schemas_dir)
    name = args.name
    if not name.endswith(".schema.json"):
        if name.endswith(".json"):
            name = name[:-5]
        name = f"{name}.schema.json"
    path = schemas_dir / name
    if not path.is_file():
        print(f"schema not found: {path}", file=sys.stderr)
        return 2
    print(json.dumps(_read_json(path), indent=2, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    file_path = Path(args.file).expanduser()
    if not file_path.is_file():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2
    if args.stage == "protocol":
        from rsc.protocol import validate_protocol_file

        schemas.check_errors(validate_protocol_file(file_path, args.schemas_dir))
        print(f"OK {file_path}: protocol schema and references verified")
        return 0
    validator = _validator(args.stage, _schemas_dir(args.schemas_dir))

    jsonl = bool(args.jsonl) or (not args.json and file_path.suffix == ".jsonl")
    errors: list[str] = []
    count = 0

    if jsonl:
        try:
            stream = file_path.open(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read {file_path}: {exc}", file=sys.stderr)
            return 2
        with stream:
            for lineno, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {lineno}: invalid JSON: {exc}")
                    continue
                count += 1
                errors.extend(
                    f"line {lineno} {message}" for message in _validate_instance(validator, record)
                )
    else:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"invalid JSON in {file_path}: {exc}", file=sys.stderr)
            return 2
        count = 1
        errors.extend(_validate_instance(validator, data))

    if errors:
        for message in errors[:50]:
            print(f"{file_path}: {message}", file=sys.stderr)
        if len(errors) > 50:
            print(f"... {len(errors) - 50} more error(s)", file=sys.stderr)
        print(f"FAILED {file_path}: {len(errors)} error(s)", file=sys.stderr)
        return 1

    kind = "JSONL" if jsonl else "JSON"
    print(f"OK {file_path}: {count} {kind} record(s) match {args.stage}.schema.json")
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    config = load_workflow_config(args.config)
    print_profiles(resolve_profiles(config, **selection(args)))
    return 0


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #


def cmd_ingest(args: argparse.Namespace) -> int:
    from rsc.ingest import IngestError, IngestOptions, ingest_path

    out_dir = Path(args.out).expanduser().resolve()
    options = IngestOptions(
        out_dir=out_dir,
        recursive=args.recursive,
        include=tuple(args.include or ()),
        exclude=tuple(args.exclude or ()),
        format=args.format,
        force=args.force,
        dry_run=args.dry_run,
        command=("rsc", "ingest", args.input),
    )
    try:
        result = ingest_path(args.input, options)
    except (IngestError, OSError) as exc:
        print(f"rsc: error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return result.exit_code()

    for document in result.documents:
        line = f"{document.status:9} {document.doc_id:24} {document.relative_path}"
        if document.error:
            line += f"  error: {document.error}"
        print(line)
    summary = result.summary()
    print(
        f"{summary['total']} document(s): {summary['ok']} ok, "
        f"{summary['failed']} failed, {summary['skipped']} skipped, "
        f"{summary['unchanged']} unchanged, {summary['planned']} planned"
    )
    if result.batch_path is not None:
        print(f"batch: {result.batch_path}")
    return result.exit_code()


def cmd_stats(args: argparse.Namespace) -> int:
    from rsc.protocol import validate_protocol_file
    from rsc.stats import collect_protocol, compute_stats, format_stats

    path, model = collect_protocol(args.path)
    if args.validate:
        schemas.check_errors(validate_protocol_file(path, args.schemas_dir))
    stats = compute_stats(model)
    print(json.dumps(stats, indent=2, ensure_ascii=False) if args.json else format_stats(stats))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    summary = pipeline.index_document(
        args.input, out=args.out, force=args.force, schemas_dir=args.schemas_dir
    )
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"structural index: {summary['path']}")
        print(
            f"sections: {summary['sections']}, blocks: {summary['blocks']}, "
            f"references: {summary['references']}"
        )
    return 0


def _print_validation_errors(errors: list[str]) -> None:
    for message in errors[:50]:
        print(message, file=sys.stderr)
    if len(errors) > 50:
        print(f"... {len(errors) - 50} more error(s)", file=sys.stderr)
    print(f"FAILED: {len(errors)} validation error(s)", file=sys.stderr)


def cmd_compile(args: argparse.Namespace) -> int:
    print("rsc compile has been replaced by the MCP workflow; use rsc run --help", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsc",
        description="RFC protocol interaction model compiler",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    from rsc.workflow.cli import register

    register(sub)

    p = sub.add_parser("doctor", help="check environment, dependencies and schemas")
    selection_arguments(p)
    p.add_argument("--config", help="optional workflow config to validate")
    p.add_argument("--schemas-dir", help="override the schema directory")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ingest", help="ingest one RFC file or a directory")
    p.add_argument("input", help="RFC file or directory")
    p.add_argument("--out", default="work", help="output root (default: work)")
    p.add_argument("--recursive", action="store_true", help="recurse into subdirectories")
    p.add_argument("--include", action="append", default=[], metavar="GLOB")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    p.add_argument(
        "--format",
        choices=["auto", "txt", "html", "xml"],
        default="auto",
        help="force the input format",
    )
    p.add_argument("--force", action="store_true", help="regenerate even if unchanged")
    p.add_argument("--dry-run", action="store_true", help="list work without writing")
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("stats", help="summarize protocol interaction output")
    p.add_argument("path", help="protocol.json or its output directory")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument(
        "--validate",
        action="store_true",
        help="validate schema and graph before summarizing",
    )
    p.add_argument("--schemas-dir", help="override the schema directory")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("index", help="build the M2 structural index")
    p.add_argument("input", help="document.json or a run directory containing it")
    p.add_argument("--out", help="output directory or structural-index.json path")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")
    p.add_argument("--schemas-dir", help="override the schema directory")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("compile", help="removed; use rsc run")
    p.add_argument("legacy_arguments", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("schemas", help="inspect JSON Schema contracts")
    schemas_sub = p.add_subparsers(dest="schemas_command", required=True)

    q = schemas_sub.add_parser("list", help="list available schemas")
    q.add_argument("--schemas-dir")
    q.set_defaults(func=cmd_schemas_list)

    q = schemas_sub.add_parser("show", help="print one schema")
    q.add_argument("name", help="stage name or schema file name")
    q.add_argument("--schemas-dir")
    q.set_defaults(func=cmd_schemas_show)

    p = sub.add_parser("validate", help="validate a JSON/JSONL artifact")
    p.add_argument("stage", choices=sorted(STAGE_FILES))
    p.add_argument("file")
    p.add_argument("--schemas-dir")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--jsonl", action="store_true", help="force JSON Lines")
    mode.add_argument("--json", action="store_true", help="force a single JSON value")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("config", help="inspect workflow configuration")
    config_sub = p.add_subparsers(dest="config_command", required=True)
    q = config_sub.add_parser("check", help="validate workflow and agent settings")
    selection_arguments(q)
    q.add_argument("--config", required=True)
    q.set_defaults(func=cmd_config_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except pipeline.PipelineError as exc:
        print(f"rsc: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except SchemaValidationError as exc:
        _print_validation_errors(exc.errors)
        return 1
    except ConfigError as exc:
        print(f"rsc: error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"rsc: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("rsc: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
