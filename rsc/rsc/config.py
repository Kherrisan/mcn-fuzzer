"""TOML workflow settings and named, immutable agent profiles."""

from __future__ import annotations

import re
import tomllib
from copy import deepcopy
from pathlib import Path


class ConfigError(ValueError):
    """Invalid workflow configuration."""


ROLES = ("parser", "merge", "reviewer")
DEFAULTS = {
    "workflow": {
        "batch_target_tokens": 8000,
        "batch_max_tokens": 12000,
        "parse_workers": "auto",
        "parse_max_workers": 4,
        "parse_context_tokens": 258400,
        "parse_fixed_tokens": 45000,
        "merge_context_tokens": 258400,
        "merge_fixed_tokens": 45000,
        "max_feedback_rounds": 5,
    },
    "agents": {
        "default_profile": "codex-luna-max",
        "profiles": {
            "codex-luna-max": {
                "provider": "codex",
                "executable": "codex",
                "model": "gpt-6-luna",
                "effort": "max",
                "args": [],
                "envs": [],
            }
        },
    },
}
PROFILE_KEYS = {"provider", "executable", "model", "effort", "args", "envs"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def managed_env(name):
    return name.startswith(("HC_", "HERDR_", "RSC_")) or name in {
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "XDG_CONFIG_HOME",
    }


def load_workflow_config(path=None):
    result = deepcopy(DEFAULTS)
    if path is None:
        return result
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() == ".json":
        raise ConfigError("JSON config is no longer supported; migrate to config.example.toml")
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read TOML config {path}: {error}") from error
    if set(data) - {"workflow", "agents"}:
        raise ConfigError("configuration supports [workflow] and [agents]")
    workflow = data.get("workflow", {})
    if isinstance(workflow, dict) and "merge_workers" in workflow:
        raise ConfigError(
            "workflow.merge_workers is removed; remove it from your config: "
            "multiple parser results now use one merge agent"
        )
    if not isinstance(workflow, dict) or set(workflow) - set(result["workflow"]):
        raise ConfigError("unknown workflow options")
    for key, value in workflow.items():
        if key == "parse_workers":
            if value == "auto" or (type(value) is int and value >= 1):
                continue
            raise ConfigError('parse_workers must be "auto" or a positive integer')
        if key in {"parse_fixed_tokens", "merge_fixed_tokens"}:
            if type(value) is not int or value < 0:
                raise ConfigError(f"{key} must be a nonnegative integer")
            continue
        if type(value) is not int or value < 1:
            raise ConfigError(f"{key} must be a positive integer")
    result["workflow"].update(workflow)
    if (
        not 128
        <= result["workflow"]["batch_target_tokens"]
        <= result["workflow"]["batch_max_tokens"]
    ):
        raise ConfigError("batch limits must satisfy 128 <= target <= maximum")
    agents = data.get("agents", {})
    if not isinstance(agents, dict) or set(agents) - {
        "default_profile",
        "parser_profile",
        "merge_profile",
        "reviewer_profile",
        "profiles",
    }:
        raise ConfigError(
            "use [agents.profiles.NAME] and role profile names; inline agents are removed"
        )
    for key in ("default_profile", *(f"{role}_profile" for role in ROLES)):
        if key in agents:
            if not isinstance(agents[key], str) or not agents[key].strip():
                raise ConfigError(f"agents.{key} must be a profile name")
            result["agents"][key] = agents[key]
    if "profiles" in agents:
        from rsc.agents import ADAPTERS

        if not isinstance(agents["profiles"], dict) or not agents["profiles"]:
            raise ConfigError("agents.profiles must be a nonempty table")
        profiles = {}
        for name, profile in agents["profiles"].items():
            if not name.strip() or not isinstance(profile, dict) or set(profile) - PROFILE_KEYS:
                raise ConfigError(f"invalid profile {name!r}; use provider/model/effort/args/envs")
            provider = profile.get("provider")
            if not isinstance(provider, str) or provider not in ADAPTERS:
                raise ConfigError(f"profile {name}: unsupported provider {provider!r}")
            value = {"executable": provider, "effort": "high", "args": [], "envs": [], **profile}
            for field in ("executable", "model", "effort"):
                if not isinstance(value.get(field), str) or not value[field].strip():
                    raise ConfigError(f"profile {name}: {field} must be a nonempty string")
            if not isinstance(value["args"], list) or any(
                not isinstance(x, str) or "\0" in x for x in value["args"]
            ):
                raise ConfigError(f"profile {name}: args must be a string list")
            seen = set()
            if not isinstance(value["envs"], list):
                raise ConfigError(f"profile {name}: envs must be a list of string pairs")
            for pair in value["envs"]:
                if (
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or any(not isinstance(x, str) or "\0" in x for x in pair)
                ):
                    raise ConfigError(f"profile {name}: envs must contain string pairs")
                key, _ = pair
                if not ENV_NAME.fullmatch(key) or key in seen or managed_env(key):
                    raise ConfigError(
                        f"profile {name}: invalid, duplicate or managed env name {key!r}"
                    )
                seen.add(key)
            value["envs"] = sorted(value["envs"])
            ADAPTERS[provider].validate(value)
            if "/" in value["executable"]:
                value["executable"] = str(
                    (path.parent / Path(value["executable"]).expanduser()).resolve()
                )
            profiles[name] = value
        result["agents"]["profiles"] = profiles
    for key in ("default_profile", *(f"{role}_profile" for role in ROLES)):
        if key in result["agents"] and result["agents"][key] not in result["agents"]["profiles"]:
            raise ConfigError(f"agents.{key} references unknown profile {result['agents'][key]!r}")
    return result


def resolve_profiles(
    config,
    *,
    profile=None,
    parser_profile=None,
    merge_profile=None,
    reviewer_profile=None,
    saved=None,
):
    agents = config["agents"]
    overrides = {"parser": parser_profile, "merge": merge_profile, "reviewer": reviewer_profile}
    roles = {}
    for role in ROLES:
        name = (
            overrides[role]
            or profile
            or (saved or {}).get(role, {}).get("name")
            or agents.get(f"{role}_profile")
            or (roles.get("parser", {}).get("name") if role != "parser" else None)
            or agents["default_profile"]
        )
        if name not in agents["profiles"]:
            raise ConfigError(f"{role} references unknown profile {name!r}")
        value = deepcopy(agents["profiles"][name])
        roles[role] = {"name": name, "profile": value}
    return roles
