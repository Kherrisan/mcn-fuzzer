"""CLI entry point for the Pydantic AI extraction workflow."""

import asyncio
import tomllib
from pathlib import Path

from pydantic import Field, ValidationError, model_validator

from rsc.protocol.models import StrictModel
from rsc.workflow.locking import exclusive
from rsc.workflow.store import atomic_json, read_json

from .registry import LEGACY_SCAN_KINDS, SCAN_KINDS
from .sources import Sources


class ExtractConfig(StrictModel):
    model: str = "openai-codex:gpt-6-luna"
    effort: str = "high"
    context_tokens: int = Field(default=258400, ge=4096)
    output_tokens: int = Field(default=8192, ge=256)
    tool_result_reserve_tokens: int = Field(default=16384, ge=1024)
    summary_tokens: int = Field(default=2000, ge=128)
    batch_target_tokens: int = Field(default=8000, ge=128)
    batch_max_tokens: int = Field(default=12000, ge=128)
    max_requests_per_assignment: int = Field(default=96, ge=1)
    max_interaction_requests_per_assignment: int = Field(default=128, ge=1)
    max_tool_calls_per_assignment: int = Field(default=256, ge=1)
    history_tool_chars: int = Field(default=1200, ge=0)
    keep_full_turns: int = Field(default=2, ge=0)
    stub_before_drop: bool = True
    review_enabled: bool = True
    review_requests_per_second: float = Field(default=5.0, gt=0)
    review_concurrency: int = Field(default=3, ge=1)
    gap_group_max: int = Field(default=8, ge=1)
    max_tool_retries: int = Field(default=4, ge=0)
    assignment_retries: int = Field(default=2, ge=0)
    max_repair_rounds: int = Field(default=12, ge=1)
    max_global_validation_attempts: int = Field(default=6, ge=1)
    request_timeout_seconds: int = Field(default=180, ge=1)
    assignment_timeout_seconds: int = Field(default=900, ge=1)
    run_timeout_seconds: int = Field(default=7200, ge=1)
    enabled_extensions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_repair_options(cls, values):
        if not isinstance(values, dict):
            return values
        obsolete = {
            "min_repair_entities",
            "max_repair_entities",
            "repair_base_requests",
            "repair_requests_per_entity",
        }
        return {key: value for key, value in values.items() if key not in obsolete}


def register(sub):
    p = sub.add_parser("extract", help="extract a protocol model with a Pydantic AI agent")
    p.set_defaults(func=command)
    p.add_argument("--task-dir")
    p.add_argument("--protocol")
    p.add_argument("--rfc", action="append", default=[])
    p.add_argument("--config")
    p.add_argument("--probe", action="store_true", help="verify the model can call an MCP tool")


def config_from_file(path):
    try:
        values = tomllib.loads(Path(path).read_text()) if path else {}
        return ExtractConfig.model_validate(values.get("extract", {}))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"invalid extraction config: {exc}") from exc


def command(args):
    try:
        from .runner import Runner, probe
    except ImportError as exc:
        raise ValueError("install the RSC extract extra with pip install '.[extract]'") from exc

    if args.probe:
        asyncio.run(probe(config_from_file(args.config)))
        print("Model successfully executed an MCP tool.")
        return 0
    if not args.task_dir:
        raise ValueError("extract requires --task-dir")
    root = Path(args.task_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with exclusive(root / "extract.lock"):
        metadata = root / "extract-run.json"
        if metadata.is_file():
            previous = read_json(metadata)
            config = ExtractConfig.model_validate(previous["config"])
            if args.config and config_from_file(args.config) != config:
                raise ValueError("cannot change configuration on resume")
            if args.protocol is not None and args.protocol != previous["protocol"]:
                raise ValueError("cannot change protocol on resume")
            if tuple(previous.get("scan_kinds") or ()) not in (
                SCAN_KINDS,
                LEGACY_SCAN_KINDS,
            ):
                raise ValueError(
                    "cannot change scan layout on resume; start a new task directory"
                )
            sources = Sources(root, read_json(root / "source-plan.json"))
            if args.rfc and [str(Path(path).resolve()) for path in args.rfc] != [
                source["input"] for source in sources.manifest["documents"]
            ]:
                raise ValueError("cannot change source documents on resume")
            protocol = previous["protocol"]
        else:
            if (root / "state.json").exists() or (root / "protocol.json").exists():
                raise ValueError("use a new task directory for extract")
            if not args.protocol or not args.rfc:
                raise ValueError("new extraction requires --protocol and at least one --rfc")
            config = config_from_file(args.config)
            maximum = min(
                config.batch_max_tokens,
                config.context_tokens
                - config.output_tokens
                - config.tool_result_reserve_tokens
                - config.summary_tokens
                - 12000,
            )
            if maximum < 128 or config.batch_target_tokens > config.batch_max_tokens:
                raise ValueError("model budget cannot fit the batch configuration")
            protocol = args.protocol
            sources = Sources.create(
                root,
                args.rfc,
                min(config.batch_target_tokens, maximum),
                maximum,
                config.enabled_extensions,
            )
            atomic_json(
                metadata,
                {
                    "protocol": protocol,
                    "config": config.model_dump(),
                    "scan_kinds": list(SCAN_KINDS),
                },
            )
        asyncio.run(Runner(root, sources, protocol, config).run())
    return 0
