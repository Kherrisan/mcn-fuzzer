#!/usr/bin/env python3
"""CLI for RFC protocol interaction extraction and structural inspection."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rsc import pipeline, schemas
from rsc.config import ConfigError, load_compile_options, load_llm_config
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

    for module in ("jsonschema", "pydantic", "pydantic_ai", "anthropic", "openai"):
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

    if args.config:
        try:
            config = load_llm_config(args.config)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 1
        print(
            f"config {args.config}: default={config.default_provider} "
            f"providers={len(config.providers)} stages={len(config.stages)}"
        )
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


def _key_status(provider: Any) -> str:
    try:
        provider.resolve_api_key()
        return "set"
    except ConfigError:
        return "missing"


def cmd_config_check(args: argparse.Namespace) -> int:
    config = load_llm_config(args.config)
    load_compile_options(args.config)
    missing: list[str] = []
    providers: list[dict[str, Any]] = []

    for name, provider in sorted(config.providers.items()):
        key = "not_checked" if args.no_key_check else _key_status(provider)
        if key == "missing":
            missing.append(name)
        providers.append(
            {
                "name": name,
                "api": provider.api,
                "model": provider.model,
                "base_url": provider.base_url,
                "structured_mode": provider.structured_mode,
                "max_tokens_param": provider.max_tokens_param,
                "api_key": key,
            }
        )

    stages = {
        stage: {
            "provider": config.for_stage(stage).name,
            "model": config.for_stage(stage).model,
        }
        for stage in sorted(config.stages)
    }

    print(
        json.dumps(
            {
                "default_provider": config.default_provider,
                "cache_dir": str(config.cache_dir) if config.cache_dir else None,
                "providers": providers,
                "stages": stages,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 1 if missing else 0


def cmd_llm_check(args: argparse.Namespace) -> int:
    from rsc.llm import create_client_for_stage

    config = load_llm_config(args.config)
    stages = [args.stage] if args.stage else (sorted(config.stages) or ["default"])
    results: list[dict[str, Any]] = []
    failed = False

    for stage in stages:
        try:
            provider = config.for_stage(stage)
            client = create_client_for_stage(config, stage)
            key = _key_status(provider)
            results.append(
                {
                    "stage": stage,
                    "provider": provider.name,
                    "api": provider.api,
                    "model": provider.model,
                    "client": type(client).__name__,
                    "api_key": key,
                    "status": "ok" if key == "set" else "missing_key",
                }
            )
            if key != "set":
                failed = True
        except Exception as exc:  # construction failures are reported, not raised
            results.append(
                {
                    "stage": stage,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            failed = True

    print(json.dumps({"stages": results}, indent=2, ensure_ascii=False))
    return 1 if failed else 0


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


def _default_llm_config() -> Path | None:
    """Find a local LLM config without hard-coding a user-specific path."""

    root = Path(__file__).resolve().parents[1]
    for candidate in (
        Path.cwd() / "config.local.json",
        root / "config.local.json",
        Path.cwd() / "config.json",
        root / "config.json",
    ):
        if candidate.is_file():
            return candidate
    return None


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


def _build_llm_client(args: argparse.Namespace, stage: str) -> Any:
    config_path = Path(args.config).expanduser() if args.config else _default_llm_config()
    if config_path is None or not config_path.is_file():
        raise ConfigError("LLM config not found; use --config or config.local.json")
    from rsc.llm import LLMCache, create_client_for_stage

    llm_config = load_llm_config(config_path)
    cache = LLMCache(llm_config.cache_dir) if llm_config.cache_dir else None
    return create_client_for_stage(llm_config, stage, cache=cache)


def _print_validation_errors(errors: list[str]) -> None:
    for message in errors[:50]:
        print(message, file=sys.stderr)
    if len(errors) > 50:
        print(f"... {len(errors) - 50} more error(s)", file=sys.stderr)
    print(f"FAILED: {len(errors)} validation error(s)", file=sys.stderr)


def cmd_compile(args: argparse.Namespace) -> int:
    from rsc.protocol.compiler import resolve_options

    config_path = Path(args.config).expanduser() if args.config else _default_llm_config()
    repair = {
        key: getattr(args, key)
        for key in ("objects_max_rounds", "interactions_max_rounds")
        if getattr(args, key) is not None
    }
    if args.no_complete:
        repair["enabled"] = False
    context = {
        key: getattr(args, key)
        for key in ("max_tokens", "compact_at", "compact_to")
        if getattr(args, key) is not None
    }
    options = resolve_options(
        config_path,
        workers=args.workers,
        objects_workers=args.objects_workers,
        interactions_workers=args.interactions_workers,
        block_lists=args.block_lists,
        repair=repair,
        context=context,
    )
    summary = pipeline.compile_protocol(
        args.input,
        args.protocol,
        args.out,
        config=config_path,
        workers=options.workers,
        objects_workers=options.objects_workers,
        interactions_workers=options.interactions_workers,
        block_lists=options.block_lists,
        force=args.force,
        schemas_dir=args.schemas_dir,
        repair_options=options.repair,
        request_limits=options.request_limits,
        context_options=options.context,
        agent_options=options.agent,
        link_options=options.link,
    )
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"{'complete' if summary['complete'] else 'incomplete'}: {summary['path']}")
        print(
            f"calls: {summary['calls']}, failures: {summary['failures']}, "
            f"unresolved: {summary['unresolved']}, model written: {summary['model_written']}"
        )
    return summary["exit_code"]


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsc",
        description="RFC protocol interaction model compiler",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check environment, SDKs and schemas")
    p.add_argument("--config", help="optional LLM config to validate")
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

    p = sub.add_parser("compile", help="compile all documents in an ingest batch")
    p.add_argument("input", help="directory containing batch.json")
    p.add_argument("--protocol", required=True, help="caller-selected protocol and version")
    p.add_argument("--out", required=True, help="model output directory")
    p.add_argument("--config", help="LLM config path")
    p.add_argument(
        "--workers", type=int, help="extraction concurrency fallback (config or 8); link is serial"
    )
    p.add_argument("--objects-workers", type=int, help="concurrent object extraction lists")
    p.add_argument(
        "--interactions-workers", type=int, help="concurrent interaction extraction lists"
    )
    p.add_argument("--block-lists", type=int, help="fixed source partitions (config or 8)")
    p.add_argument(
        "--context-limit-tokens", dest="max_tokens", type=int,
        help="model context capacity; overrides SDK metadata"
    )
    p.add_argument(
        "--context-compact-at", dest="compact_at", type=float,
        help="input capacity ratio that triggers compaction (config or 0.8)"
    )
    p.add_argument(
        "--context-compact-to", dest="compact_to", type=float,
        help="target input capacity ratio after compaction (config or 0.5)"
    )
    p.add_argument(
        "--no-complete",
        action="store_true",
        help="disable both completion stages while retaining validation and unresolved issues",
    )
    p.add_argument(
        "--objects-repair-max-rounds",
        dest="objects_max_rounds",
        type=int,
        help="object repair round limit (default: config or 5)",
    )
    p.add_argument(
        "--interactions-repair-max-rounds",
        dest="interactions_max_rounds",
        type=int,
        help="interaction repair round limit (default: config or 5)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true", help="print a machine-readable summary")
    p.add_argument("--schemas-dir")
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

    p = sub.add_parser("config", help="inspect LLM configuration")
    config_sub = p.add_subparsers(dest="config_command", required=True)
    q = config_sub.add_parser("check", help="validate providers and key presence")
    q.add_argument("--config", required=True)
    q.add_argument("--no-key-check", action="store_true")
    q.set_defaults(func=cmd_config_check)

    p = sub.add_parser("llm", help="inspect LLM clients")
    llm_sub = p.add_subparsers(dest="llm_command", required=True)
    q = llm_sub.add_parser("check", help="build clients without sending requests")
    q.add_argument("--config", required=True)
    q.add_argument("--stage", help="check one stage instead of all configured stages")
    q.set_defaults(func=cmd_llm_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (pipeline.PipelineError, pipeline.CompileError) as exc:
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
