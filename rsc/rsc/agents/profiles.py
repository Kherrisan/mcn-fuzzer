"""Bind a new task to resolved profiles; ordinary resume never reselects defaults."""

import json

from rsc.config import ROLES, ConfigError, resolve_profiles

from .files import private_json


def selection_arguments(parser):
    for name in ("profile", *(f"{role}-profile" for role in ROLES)):
        parser.add_argument(f"--{name}", help="select a named agent profile")


def selection(args):
    return {
        key: getattr(args, key, None)
        for key in ("profile", *(f"{role}_profile" for role in ROLES))
    }


def bind_profiles(root, config, *, existing, args):
    path = root / "agent-profiles.json"
    if existing and not path.exists():
        raise ConfigError(
            "task has no profile snapshot; use a new task directory (artifacts remain readable)"
        )
    saved = json.loads(path.read_text()) if path.exists() else None
    if path.exists() and (
        not isinstance(saved, dict)
        or set(saved) != {"roles"}
        or not isinstance(saved["roles"], dict)
        or set(saved["roles"]) != set(ROLES)
        or any(
            not isinstance(selected, dict)
            or set(selected) != {"name", "profile"}
            or not isinstance(selected["name"], str)
            or not isinstance(selected["profile"], dict)
            for selected in saved["roles"].values()
        )
    ):
        raise ConfigError("unsupported profile snapshot; use a new task directory")
    # Config locates definitions; saved selections survive ordinary resumes.
    roles = resolve_profiles(config, **selection(args), saved=saved["roles"] if saved else None)
    if saved is not None and saved["roles"] != roles:
        raise ConfigError(
            "task profiles are immutable; use a new task directory to change profiles"
        )
    if saved is None:
        private_json(path, {"roles": roles})
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
