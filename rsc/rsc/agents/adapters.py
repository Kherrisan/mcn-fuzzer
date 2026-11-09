"""Provider-specific command construction and executable capability checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from rsc.config import ConfigError

from .files import private_json


@dataclass(frozen=True)
class LaunchSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: str

    def write(self, path):
        private_json(path, asdict(self))


@dataclass(frozen=True)
class AgentContext:
    root: Path
    socket: str
    task_id: str
    role: str
    batch_max_tokens: int = 12000

    @property
    def directory(self):
        return self.root / "agents" / self.role

    @property
    def namespace(self):
        return f"rsc_{self.task_id}_{self.role}"

    def bridge(self, profile):
        environment = dict(profile["envs"])
        environment["PYTHONPATH"] = (
            str(Path(__file__).resolve().parents[2])
            + os.pathsep
            + environment.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
        )
        path = self.directory / "mcp-launch.json"
        LaunchSpec(
            [
                sys.executable,
                "-m",
                "rsc.workflow.cli",
                "mcp",
                "--socket",
                self.socket,
                "--token-file",
                str(self.root / f"{self.role}.token"),
                "--catalog",
                "role",
            ],
            environment,
            str(self.root),
        ).write(path)
        return {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("launch.py")), str(path)],
            "env": {},
        }


class Adapter:
    required_flags = ()
    reserved = {"--model", "-m", "--print", "exec", "--resume", "--continue", "--api-key"}

    def ready(self, context):
        return True

    def validate(self, profile):
        for arg in profile["args"]:
            if arg.split("=", 1)[0] in self.reserved:
                raise ConfigError("profile args override a provider-managed option")
        if profile["effort"] not in {
            "off",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ConfigError(f"unsupported effort {profile['effort']!r}")

    def check(self, profile):
        self.validate(profile)
        if not shutil.which(
            profile["executable"], path=dict(profile["envs"]).get("PATH", os.environ.get("PATH"))
        ):
            raise ConfigError(
                f"{profile['provider']} executable is not available: {profile['executable']}"
            )
        with tempfile.TemporaryDirectory(prefix="rsc-agent-check-") as directory:
            result = subprocess.run(
                [profile["executable"], "--help"],
                capture_output=True,
                text=True,
                timeout=20,
                env={
                    **os.environ,
                    **dict(profile["envs"]),
                    "PI_OFFLINE": "1",
                    "PI_CODING_AGENT_DIR": directory,
                },
            )
        if result.returncode or any(flag not in result.stdout for flag in self.required_flags):
            raise ConfigError(
                f"{profile['provider']} is missing required interactive/MCP capabilities"
            )


class CodexAdapter(Adapter):
    required_flags = ("--no-daemon", "--model", "--config")
    reserved = Adapter.reserved | {"--remote", "--profile", "-p"}

    def validate(self, profile):
        super().validate(profile)
        if profile["effort"] == "off":
            raise ConfigError("Codex uses effort=none rather than off")
        args = profile["args"]
        for index, arg in enumerate(args):
            setting = arg
            if arg in {"-c", "--config"}:
                setting = args[index + 1] if index + 1 < len(args) else ""
            elif arg.startswith("--config="):
                setting = arg.removeprefix("--config=")
            elif arg.startswith("-c"):
                setting = arg[2:]
            key = setting.split("=", 1)[0].strip()
            if key in {
                "model",
                "model_reasoning_effort",
                "check_for_update_on_startup",
            } or key.startswith(("mcp_servers", "features.multi_agent")):
                raise ConfigError("profile args override a provider-managed Codex setting")

    def prepare(self, profile, context):
        self.validate(profile)
        argv = [
            profile["executable"],
            "--no-daemon",
            "--disable",
            "multi_agent",
            "--disable",
            "multi_agent_v2",
            "--model",
            profile["model"],
            "-c",
            f'model_reasoning_effort="{profile["effort"]}"',
            "-c",
            "check_for_update_on_startup=false",
        ]
        prefix = f"mcp_servers.{context.namespace}"
        bridge = context.bridge(profile)
        for key in ("command", "args"):
            argv += ["-c", f"{prefix}.{key}={json.dumps(bridge[key])}"]
        for key, value in bridge["env"].items():
            argv += ["-c", f"{prefix}.env.{key}={json.dumps(value)}"]
        argv += [
            "-c",
            f"{prefix}.required=true",
            "-c",
            f'{prefix}.default_tools_approval_mode="approve"',
        ]
        for tool in (
            "read_batch",
            "read_batches",
            "query_model",
            "get_changes",
            "list_batches",
            "search_source",
            "list_findings",
        ):
            limit = 31024 if tool in {"read_batch", "read_batches"} else 31024
            argv += ["-c", f"{prefix}.tools.{tool}.output_token_limit={limit}"]
        return LaunchSpec(argv + profile["args"], dict(profile["envs"]), str(context.root))


class PiAdapter(Adapter):
    required_flags = (
        "--model",
        "--thinking",
        "--extension",
        "--no-extensions",
        "--offline",
        "--no-approve",
        "--tools",
        "--session-dir",
        "--name",
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
    )
    reserved = Adapter.reserved | {
        "-p",
        "--mode",
        "--thinking",
        "--extension",
        "-e",
        "--no-mcp",
        "--no-extensions",
        "--tools",
        "--exclude-tools",
        "--session",
        "--session-id",
        "--provider",
        "--name",
        "--session-dir",
    }

    def validate(self, profile):
        super().validate(profile)
        if profile["effort"] == "none":
            raise ConfigError("Pi uses effort=off rather than none")
        if "/" not in profile.get("model", ""):
            raise ConfigError("Pi model must explicitly name provider/model")

    def check(self, profile):
        super().check(profile)
        version = subprocess.run(
            [profile["executable"], "--version"], capture_output=True, text=True, timeout=10
        )
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < (1, 5, 0):
            raise ConfigError("Pi >= 1.5.0 with native MCP and OSC 7501 is required")

    def prepare(self, profile, context):
        self.validate(profile)
        context.directory.mkdir(parents=True, exist_ok=True)
        runtime = context.directory / "pi-runtime.json"
        ready = context.directory / "ready.json"
        ready.unlink(missing_ok=True)
        private_json(
            runtime,
            {
                "namespace": context.namespace,
                "bridge": context.bridge(profile),
                "model": profile["model"],
                "effort": profile["effort"],
                "events": str(context.directory / "pi-events.jsonl"),
                "fatal": str(context.directory / "fatal.json"),
                "ready": str(ready),
            },
        )
        argv = [
            profile["executable"],
            "--model",
            profile["model"],
            "--thinking",
            profile["effort"],
            "--offline",
            "--no-extensions",
            "--extension",
            "builtin:mcp",
            "--extension",
            str(Path(__file__).with_name("pi.mjs")),
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--tools",
            f"mcp__{context.namespace}__*",
            "--session-dir",
            str(context.directory / "sessions"),
            "--name",
            f"rsc-{context.role}-{context.task_id}",
        ]
        return LaunchSpec(
            argv + profile["args"],
            {**dict(profile["envs"]), "RSC_PI_RUNTIME": str(runtime)},
            str(context.root),
        )

    def ready(self, context):
        path = context.directory / "ready.json"
        return path.exists() and json.loads(path.read_text()).get("namespace") == context.namespace


ADAPTERS = {"codex": CodexAdapter(), "pi": PiAdapter()}


def check_profiles(roles):
    checked = set()
    for selected in roles.values():
        if selected["fingerprint"] not in checked:
            ADAPTERS[selected["profile"]["provider"]].check(selected["profile"])
            checked.add(selected["fingerprint"])
