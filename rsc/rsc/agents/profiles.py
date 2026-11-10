"""Bind a new task to resolved profiles; ordinary resume never reselects defaults."""

import json

from rsc.config import ConfigError, resolve_profiles

from .files import private_json


def selection_arguments(parser):
    for name in ("profile", "parser-profile", "merge-profile"):
        parser.add_argument(f"--{name}", help="select a named agent profile")


def selection(args):
    return {key: getattr(args, key, None) for key in ("profile", "parser_profile", "merge_profile")}


def bind_profiles(root, config, *, existing, args):
    path = root / "agent-profiles.json"
    if existing and not path.exists():
        raise ConfigError(
            "task has no profile snapshot; use a new task directory (artifacts remain readable)"
        )
    saved = json.loads(path.read_text()) if path.exists() else None
    if saved is not None and saved.get("version") != 1:
        raise ConfigError("unsupported profile snapshot")
    # Config locates definitions; saved selections survive ordinary resumes.
    roles = resolve_profiles(config, **selection(args), saved=saved["roles"] if saved else None)
    if saved is not None and saved["roles"] != roles:
        raise ConfigError(
            "task profiles are immutable; use a new task directory to change profiles"
        )
    if saved is None:
        private_json(path, {"version": 1, "roles": roles})
    return roles


def print_profiles(roles):
    for role, selected in roles.items():
        p = selected["profile"]
        envs = ", ".join(k for k, _ in p["envs"]) or "(none)"
        print(
            f"{role}: {selected['name']} provider={p['provider']} "
            f"model={p['model']} effort={p['effort']}"
        )
        print(f"  executable={p['executable']} envs={envs}")
