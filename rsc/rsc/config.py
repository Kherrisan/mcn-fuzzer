"""Configuration for LLM providers.

The RSC pipeline uses one provider-neutral client interface.  A provider is
described by an ``api`` discriminator (``anthropic`` or ``openai``), a model,
and optional endpoint/key settings.  Both the native Anthropic Messages API
and the OpenAI Chat Completions API are supported; any OpenAI-compatible
endpoint (vLLM, Ollama, DeepSeek, OpenRouter, ...) can be used through the
``openai`` adapter.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_DEFAULT_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STRUCTURED_MODES = frozenset({"auto", "json_schema", "json_object", "tool"})
_TOKEN_PARAMS = frozenset({"max_tokens", "max_completion_tokens"})
_STAGE_OVERRIDE_KEYS = frozenset(
    {
        "model",
        "base_url",
        "api_key",
        "api_key_env",
        "timeout_s",
        "max_retries",
        "temperature",
        "max_tokens",
        "structured_mode",
        "max_tokens_param",
        "default_headers",
        "extra_client_kwargs",
    }
)


class ConfigError(ValueError):
    """Invalid RSC configuration."""


def _expand_env(value: str, env: Mapping[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ConfigError(f"environment variable {name!r} is not set")
        return env[name]

    return _ENV_PATTERN.sub(replace_match, value)


@dataclass(frozen=True)
class ProviderConfig:
    """One configured provider/model pair."""

    name: str
    api: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_s: float = 120.0
    max_retries: int = 3
    temperature: float | None = 0.0
    max_tokens: int = 4096
    structured_mode: str = "auto"
    max_tokens_param: str = "max_tokens"
    default_headers: dict[str, str] = field(default_factory=dict)
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ConfigError("provider name must not be empty")
        if self.api not in _DEFAULT_KEY_ENV:
            raise ConfigError(
                f"provider {self.name!r}: api must be 'anthropic' or 'openai', got {self.api!r}"
            )
        if not self.model or not self.model.strip():
            raise ConfigError(f"provider {self.name!r}: model must not be empty")
        if self.base_url is not None and not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"provider {self.name!r}: base_url must be an HTTP(S) URL")
        if self.timeout_s <= 0:
            raise ConfigError(f"provider {self.name!r}: timeout_s must be positive")
        if self.max_retries < 0:
            raise ConfigError(f"provider {self.name!r}: max_retries must be >= 0")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ConfigError(f"provider {self.name!r}: temperature must be between 0 and 2")
        if self.max_tokens <= 0:
            raise ConfigError(f"provider {self.name!r}: max_tokens must be positive")
        if self.structured_mode not in _STRUCTURED_MODES:
            raise ConfigError(
                f"provider {self.name!r}: structured_mode must be one of "
                f"{sorted(_STRUCTURED_MODES)}"
            )
        if self.max_tokens_param not in _TOKEN_PARAMS:
            raise ConfigError(
                f"provider {self.name!r}: max_tokens_param must be one of {sorted(_TOKEN_PARAMS)}"
            )
        if not isinstance(self.default_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.default_headers.items()
        ):
            raise ConfigError(f"provider {self.name!r}: default_headers must be a string map")
        if not isinstance(self.extra_client_kwargs, dict):
            raise ConfigError(f"provider {self.name!r}: extra_client_kwargs must be an object")

    def resolve_api_key(self, env: Mapping[str, str] | None = None) -> str:
        """Resolve the API key from explicit config or an environment variable.

        A local OpenAI-compatible server that ignores authentication can set
        ``"api_key": "local"`` explicitly.
        """

        env = os.environ if env is None else env
        if self.api_key is not None:
            value = _expand_env(self.api_key, env) if "${" in self.api_key else self.api_key
            if value:
                return value
        key_env = self.api_key_env or _DEFAULT_KEY_ENV[self.api]
        value = env.get(key_env)
        if value:
            return value
        raise ConfigError(
            f"missing API key for provider {self.name!r}: set {key_env} "
            f"or provider.api_key / provider.api_key_env"
        )


@dataclass(frozen=True)
class LLMConfig:
    """A collection of providers plus optional per-stage overrides."""

    default_provider: str
    providers: dict[str, ProviderConfig]
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    cache_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.providers:
            raise ConfigError("at least one llm provider must be configured")
        if self.default_provider not in self.providers:
            raise ConfigError(f"default provider {self.default_provider!r} is not configured")
        for stage, overrides in self.stages.items():
            if not isinstance(overrides, dict):
                raise ConfigError(f"stage {stage!r}: overrides must be an object")
            unknown = set(overrides) - _STAGE_OVERRIDE_KEYS - {"provider"}
            if unknown:
                raise ConfigError(f"stage {stage!r}: unknown override keys {sorted(unknown)}")
            provider = overrides.get("provider", self.default_provider)
            if provider not in self.providers:
                raise ConfigError(f"stage {stage!r}: provider {provider!r} is not configured")

    def for_stage(self, stage: str) -> ProviderConfig:
        """Return the effective provider config for a pipeline stage."""

        overrides = dict(self.stages.get(stage, {}))
        provider_name = overrides.pop("provider", self.default_provider)
        base = self.providers[provider_name]
        if not overrides:
            return base
        try:
            return replace(base, **overrides)
        except TypeError as exc:
            raise ConfigError(f"stage {stage!r}: invalid override: {exc}") from exc

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        base_dir: Path | None = None,
    ) -> LLMConfig:
        providers_raw = raw.get("providers")
        if not isinstance(providers_raw, dict) or not providers_raw:
            raise ConfigError("llm.providers must be a non-empty object")
        providers: dict[str, ProviderConfig] = {}
        for name, item in providers_raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("provider names must be non-empty strings")
            if not isinstance(item, dict):
                raise ConfigError(f"provider {name!r} must be an object")
            data = dict(item)
            data["name"] = name
            if "api" not in data:
                raise ConfigError(f"provider {name!r}: api is required")
            try:
                providers[name] = ProviderConfig(**data)
            except TypeError as exc:
                raise ConfigError(f"provider {name!r}: {exc}") from exc
            except ConfigError as exc:
                raise ConfigError(f"provider {name!r}: {exc}") from exc

        default_provider = raw.get("default") or next(iter(providers))

        stages_raw = raw.get("stages") or {}
        if not isinstance(stages_raw, dict):
            raise ConfigError("llm.stages must be an object")
        stages: dict[str, dict[str, Any]] = {}
        for stage, overrides in stages_raw.items():
            if not isinstance(stage, str) or not stage.strip():
                raise ConfigError("stage names must be non-empty strings")
            if not isinstance(overrides, dict):
                raise ConfigError(f"stage {stage!r} must be an object")
            stages[stage] = dict(overrides)

        cache_raw = raw.get("cache_dir")
        cache_dir: Path | None = None
        if cache_raw is not None:
            if not isinstance(cache_raw, str) or not cache_raw.strip():
                raise ConfigError("llm.cache_dir must be a non-empty string")
            path = Path(cache_raw).expanduser()
            if not path.is_absolute():
                path = (base_dir or Path.cwd()) / path
            cache_dir = path.resolve()

        return cls(
            default_provider=default_provider,
            providers=providers,
            stages=stages,
            cache_dir=cache_dir,
        )


def load_llm_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> LLMConfig:
    """Load an ``llm`` section from a JSON file.

    The file may either be a full RSC config with a top-level ``llm`` key, or
    a standalone LLM config whose root is the ``llm`` object.  ``env`` is
    accepted for symmetry; keys are resolved lazily by the provider.
    """

    del env  # keys are resolved when a client is built
    config_path = Path(path).expanduser()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read LLM config {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in LLM config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"LLM config {config_path} must be a JSON object")
    raw = data.get("llm", data)
    if not isinstance(raw, dict):
        raise ConfigError(f"LLM config {config_path}: 'llm' must be an object")
    return LLMConfig.from_dict(raw, base_dir=config_path.resolve().parent)


def load_compile_options(path: str | Path):
    """Read the optional compile section without constructing a provider or reading keys."""
    from rsc.protocol.options import compile_options

    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        return compile_options(data.get("compile", {}))
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"invalid compile configuration: {exc}") from exc
