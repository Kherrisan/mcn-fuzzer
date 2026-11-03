"""Compilation policy, independent of the CLI and provider client implementation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from rsc.llm.budget import RequestLimits


@dataclass(frozen=True)
class RepairOptions:
    enabled: bool = True
    objects_max_rounds: int = 5
    interactions_max_rounds: int = 5

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("repair.enabled must be a boolean")
        for key, value in asdict(self).items():
            if key == "enabled":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"repair.{key} must be a positive integer")


@dataclass(frozen=True)
class ContextOptions:
    max_tokens: int | None = None
    compact_at: float = 0.8
    compact_to: float = 0.5

    def __post_init__(self):
        import math

        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("context.max_tokens must be a positive integer or null")
        for name in ("compact_at", "compact_to"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"context.{name} must be a finite number")
        if not 0 < self.compact_to < self.compact_at <= 1:
            raise ValueError("context requires 0 < compact_to < compact_at <= 1")


@dataclass(frozen=True)
class AgentOptions:
    max_model_requests: int = 8
    max_tool_calls: int = 12
    max_block_retries: int = 3
    max_output_retries: int = 2
    max_run_seconds: float = 120
    max_tool_blocks: int = 32
    max_keywords_per_search: int = 8
    max_search_results: int = 8

    def __post_init__(self):
        import math

        for key, value in asdict(self).items():
            if key == "max_run_seconds":
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError("agent.max_run_seconds must be finite and positive")
            elif type(value) is not int or value < (
                0 if key in {"max_block_retries", "max_output_retries"} else 1
            ):
                raise ValueError(f"invalid agent.{key} limit")


@dataclass(frozen=True)
class LinkOptions:
    partition_max_input_bytes: int = 65536
    merge_max_input_bytes: int = 65536
    max_partition_calls: int = 64

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"link.{key} must be a positive integer")


STAGES = ("objects", "interactions", "repair", "link_partition", "link_merge")
QUALIFIED_STAGES = {
    f"{phase}_{stage}": stage
    for phase in ("objects", "interactions")
    for stage in ("repair", "link_partition", "link_merge")
}


def resolve_limits(values=None) -> dict[str, RequestLimits]:
    values = {} if values is None else values
    if not isinstance(values, dict) or set(values) - set(STAGES) - set(QUALIFIED_STAGES) - {
        "default"
    }:
        raise ValueError("request_limits contains an unknown stage or is not a mapping")
    result = {}
    for stage in (*STAGES, *QUALIFIED_STAGES):
        generic = QUALIFIED_STAGES.get(stage, stage)
        data = asdict(RequestLimits(max_input_bytes=16 * 1024 * 1024))
        for key in dict.fromkeys(("default", generic, stage)):
            item = values.get(key, {})
            if not isinstance(item, (dict, RequestLimits)):
                raise ValueError(f"request_limits.{key} must be a mapping or RequestLimits")
            data.update(asdict(item) if isinstance(item, RequestLimits) else item)
        result[stage] = RequestLimits(**data)
    return result


@dataclass(frozen=True)
class CompileOptions:
    workers: int = 8
    objects_workers: int | None = None
    interactions_workers: int | None = None
    block_lists: int = 8
    context: ContextOptions = field(default_factory=ContextOptions)
    agent: AgentOptions = field(default_factory=AgentOptions)
    repair: RepairOptions = field(default_factory=RepairOptions)
    link: LinkOptions = field(default_factory=LinkOptions)
    request_limits: dict = field(default_factory=resolve_limits)

    def __post_init__(self):
        for key in ("workers", "objects_workers", "interactions_workers", "block_lists"):
            value = getattr(self, key)
            if value is None and key.endswith("_workers"):
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"compile.{key} must be a positive integer")

    def resolved(self):
        return replace(
            self,
            objects_workers=self.objects_workers
            if self.objects_workers is not None
            else self.workers,
            interactions_workers=(
                self.interactions_workers if self.interactions_workers is not None else self.workers
            ),
        )


def compile_options(raw: dict) -> CompileOptions:
    if not isinstance(raw, dict):
        raise ValueError("compile must be a mapping")
    values = dict(raw)
    if "window" in values:
        raise ValueError("compile.window was removed; configure compile.context instead")
    if isinstance(values.get("agent"), dict) and "max_context_blocks" in values["agent"]:
        raise ValueError("agent.max_context_blocks was replaced by per-call max_tool_blocks")
    for name, cls in (
        ("context", ContextOptions),
        ("agent", AgentOptions),
        ("repair", RepairOptions),
        ("link", LinkOptions),
    ):
        if name in values:
            item = values[name]
            if isinstance(item, dict):
                values[name] = cls(**item)
            elif not isinstance(item, cls):
                raise ValueError(f"compile.{name} must be a mapping or {cls.__name__}")
    values["request_limits"] = resolve_limits(values.get("request_limits"))
    return CompileOptions(**values)
