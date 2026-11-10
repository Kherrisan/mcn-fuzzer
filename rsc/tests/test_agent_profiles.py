"""Named-profile selection, provider boundaries and literal environment forwarding."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsc.agents import ADAPTERS, AgentContext, LaunchSpec
from rsc.agents.profiles import bind_profiles, print_profiles
from rsc.config import ConfigError, load_workflow_config, resolve_profiles


def configuration(tmp_path, extra=""):
    path = tmp_path / "agents.toml"
    path.write_text(
        """[agents]
default_profile = "a"
merge_profile = "b"
[agents.profiles.a]
provider = "codex"
model = "gpt-6-luna"
effort = "max"
[agents.profiles.b]
provider = "pi"
model = "deepseek/deepseek-flash"
effort = "high"
"""
        + extra
    )
    return path, load_workflow_config(path)


def test_profile_precedence_and_toml(tmp_path):
    _, config = configuration(tmp_path)
    assert resolve_profiles(config)["merge"]["name"] == "b"
    assert resolve_profiles(config, profile="a")["merge"]["name"] == "a"
    selected = resolve_profiles(config, profile="a", merge_profile="b")
    assert [selected[r]["name"] for r in ("parser", "merge")] == ["a", "b"]
    with pytest.raises(ConfigError, match="unknown profile"):
        resolve_profiles(config, parser_profile="missing")


@pytest.mark.parametrize(
    "body",
    [
        '[agents.parser]\nmodel="x"',
        '[agents]\ndefault_profile="missing"',
        "[workflow]\nmax_review_rounds=0",
        '[agents.profiles.x]\nprovider="unknown"\nmodel="x"',
        '[agents.profiles.x]\nprovider=[]\nmodel="x"',
        "[[ invalid TOML",
    ],
)
def test_invalid_configuration(tmp_path, body):
    path = tmp_path / "bad.toml"
    path.write_text(body)
    with pytest.raises(ConfigError):
        load_workflow_config(path)
    legacy = tmp_path / "old.json"
    legacy.write_text("{}")
    with pytest.raises(ConfigError, match="JSON config"):
        load_workflow_config(legacy)


@pytest.mark.parametrize(
    "envs",
    [
        '[["A"]]',
        '[["A", 1]]',
        '[["A", "one"], ["A", "two"]]',
        '[["BAD=KEY", "x"]]',
        '[["RSC_PI_RUNTIME", "x"]]',
        '[["HC_SESSION", "x"]]',
        '[["XDG_STATE_HOME", "x"]]',
        '{ A="x" }',
    ],
)
def test_invalid_envs(tmp_path, envs):
    with pytest.raises(ConfigError, match="env"):
        configuration(tmp_path, f"envs = {envs}\n")


def test_frozen_profiles_survive_resume_and_reject_mutations(tmp_path):
    path, config = configuration(tmp_path)
    root = tmp_path / "task"
    root.mkdir()
    args = SimpleNamespace(config=str(path), profile="b", parser_profile=None, merge_profile="a")
    original = bind_profiles(root, config, existing=False, args=args)
    assert (root / "agent-profiles.json").stat().st_mode & 0o777 == 0o600
    args = SimpleNamespace(config=None, profile=None, parser_profile=None, merge_profile=None)
    assert bind_profiles(root, config, existing=True, args=args) == original
    args.config = str(path)
    assert bind_profiles(root, config, existing=True, args=args) == original
    changed = copy.deepcopy(config)
    changed["agents"]["profiles"]["b"]["effort"] = "max"
    with pytest.raises(ConfigError, match="immutable"):
        bind_profiles(root, changed, existing=True, args=args)
    args.profile = "a"
    with pytest.raises(ConfigError, match="immutable"):
        bind_profiles(root, config, existing=True, args=args)
    other = tmp_path / "legacy"
    other.mkdir()
    with pytest.raises(ConfigError, match="no profile snapshot"):
        bind_profiles(other, config, existing=True, args=args)


def test_provider_launch_specs_are_bound_and_do_not_embed_env_in_argv(tmp_path, capsys):
    _, config = configuration(tmp_path, 'envs = [["SECRET", "private value"]]\n')
    roles = resolve_profiles(config)
    print_profiles(roles)
    output = capsys.readouterr().out
    assert "SECRET" in output and "private value" not in output
    for role, selected in roles.items():
        context = AgentContext(tmp_path, "/tmp/test.sock", "abcd1234", role)
        spec = ADAPTERS[selected["profile"]["provider"]].prepare(selected["profile"], context)
        spec.write(context.directory / "launch.json")
        assert "private value" not in json.dumps(spec.argv)
        bridge = json.loads((context.directory / "mcp-launch.json").read_text())
        assert str(tmp_path / f"{role}.token") in bridge["argv"]
        assert (context.directory / "launch.json").stat().st_mode & 0o777 == 0o600
        assert bridge["env"].get("SECRET") == ("private value" if role == "merge" else None)
        if role == "parser":
            assert "--no-daemon" in spec.argv and "--thinking" not in spec.argv
            from rsc.workflow.tools import READ

            limited = {
                arg.split(".tools.")[1].split(".")[0]
                for arg in spec.argv
                if ".output_token_limit=" in arg
            }
            assert limited == READ
            assert all(arg.endswith("=31024") for arg in spec.argv if ".output_token_limit=" in arg)
        else:
            assert "--thinking" in spec.argv and "--no-daemon" not in spec.argv


def test_launch_exec_inherits_env_and_preserves_literal_values(tmp_path):
    value = "spaces $() `literal` = \\ unicode 协议\nnext line"
    spec = LaunchSpec(
        [
            sys.executable,
            "-c",
            "import os,json;print(json.dumps([os.environ['LITERAL'],os.environ['AMBIENT']]))",
        ],
        {"LITERAL": value},
        str(tmp_path),
    )
    path = tmp_path / "launch.json"
    spec.write(path)
    launcher = Path(__file__).resolve().parents[1] / "rsc/agents/launch.py"
    result = subprocess.run(
        [sys.executable, str(launcher), str(path)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "AMBIENT": "inherited-only", "LITERAL": "overridden"},
    )
    assert json.loads(result.stdout) == [value, "inherited-only"]
    assert "inherited-only" not in path.read_text()


@pytest.mark.parametrize(
    "provider,args",
    [
        ("codex", ["--model", "other"]),
        ("pi", ["--thinking", "max"]),
        ("codex", ["-c", "model=other"]),
        ("codex", ["--config=model_reasoning_effort=low"]),
        ("codex", ["-cmodel=other"]),
    ],
)
def test_profile_args_cannot_override_frozen_identity(provider, args):
    profile = {"args": args, "effort": "high"}
    with pytest.raises(ConfigError, match="provider-managed"):
        ADAPTERS[provider].validate(profile)


def test_capability_check_uses_profile_path_override(tmp_path, monkeypatch):
    import shutil
    import subprocess

    executable = tmp_path / "fixture-agent"
    shutil.copyfile("/bin/true", executable)
    executable.chmod(0o700)
    calls = []

    def check(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "--no-daemon --model --config", "")

    monkeypatch.setattr(subprocess, "run", check)
    ADAPTERS["codex"].check(
        {
            "provider": "codex",
            "executable": executable.name,
            "model": "fixture",
            "effort": "high",
            "args": [],
            "envs": [["PATH", str(tmp_path)]],
        }
    )
    assert calls == [[str(executable), "--help"]]


def test_codex_script_wrapper_is_rejected_without_execution(tmp_path):
    path = tmp_path / "wrapper"
    path.write_text("#!/bin/sh\nexit 99\n")
    path.chmod(0o700)
    with pytest.raises(ConfigError, match="not a wrapper"):
        ADAPTERS["codex"].executable({"executable": str(path), "envs": []})
