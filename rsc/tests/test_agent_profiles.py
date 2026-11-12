"""Named-profile selection, provider boundaries and literal environment forwarding."""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsc.agents import ADAPTERS, AgentContext, LaunchSpec, check_profiles
from rsc.agents.profiles import bind_profiles, print_profiles, selection
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
    assert [r["name"] for r in resolve_profiles(config).values()] == ["a", "b", "a"]
    assert resolve_profiles(config, profile="a")["merge"]["name"] == "a"
    selected = resolve_profiles(config, profile="a", merge_profile="b")
    assert [selected[r]["name"] for r in ("parser", "merge", "reviewer")] == ["a", "b", "a"]
    assert resolve_profiles(config, parser_profile="b")["reviewer"]["name"] == "b"
    config["agents"]["reviewer_profile"] = "b"
    assert resolve_profiles(config)["reviewer"]["name"] == "b"
    assert resolve_profiles(config, profile="a")["reviewer"]["name"] == "a"
    assert resolve_profiles(config, profile="b", reviewer_profile="a")["reviewer"]["name"] == "a"
    assert all(set(role) == {"name", "profile"} for role in selected.values())
    with pytest.raises(ConfigError, match="unknown profile"):
        resolve_profiles(config, parser_profile="missing")
    with pytest.raises(ConfigError, match="unknown profile"):
        resolve_profiles(config, reviewer_profile="missing")


def test_workflow_defaults_and_example_match():
    defaults = load_workflow_config()["workflow"]
    assert defaults == {
        "batch_target_tokens": 8000,
        "batch_max_tokens": 12000,
        "parse_workers": "auto",
        "parse_max_workers": 4,
        "parse_context_tokens": 258400,
        "parse_fixed_tokens": 45000,
        "merge_context_tokens": 258400,
        "merge_fixed_tokens": 45000,
        "max_feedback_rounds": 5,
    }
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    assert load_workflow_config(example)["workflow"] == defaults


@pytest.mark.parametrize("workers", ['"auto"', "1", "7"])
def test_parser_worker_count_configuration(tmp_path, workers):
    path = tmp_path / "workers.toml"
    path.write_text(f"[workflow]\nparse_workers={workers}\n")
    assert load_workflow_config(path)["workflow"]["parse_workers"] == json.loads(workers)


@pytest.mark.parametrize("workers", ['"AUTO"', '"3"', "0", "-1", "true", "1.5"])
def test_invalid_parser_worker_count(tmp_path, workers):
    path = tmp_path / "workers.toml"
    path.write_text(f"[workflow]\nparse_workers={workers}\n")
    with pytest.raises(ConfigError, match='parse_workers must be "auto" or a positive integer'):
        load_workflow_config(path)


@pytest.mark.parametrize("workers", ["1", "3", '"auto"'])
def test_merge_workers_has_explicit_migration_error(tmp_path, workers):
    path = tmp_path / "legacy.toml"
    path.write_text(f"[workflow]\nmerge_workers={workers}\n")
    with pytest.raises(ConfigError, match="merge_workers is removed.*one merge agent"):
        load_workflow_config(path)


@pytest.mark.parametrize(
    "key",
    ["parse_max_workers", "parse_context_tokens", "merge_context_tokens", "max_feedback_rounds"],
)
@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5"])
def test_positive_workflow_limits(tmp_path, key, value):
    path = tmp_path / "limits.toml"
    path.write_text(f"[workflow]\n{key}={value}\n")
    with pytest.raises(ConfigError, match=f"{key} must be a positive integer"):
        load_workflow_config(path)


@pytest.mark.parametrize("key", ["parse_fixed_tokens", "merge_fixed_tokens"])
def test_fixed_token_reservations_allow_zero_but_not_negative(tmp_path, key):
    path = tmp_path / "limits.toml"
    path.write_text(f"[workflow]\n{key}=0\n")
    assert load_workflow_config(path)["workflow"][key] == 0
    path.write_text(f"[workflow]\n{key}=-1\n")
    with pytest.raises(ConfigError, match=f"{key} must be a nonnegative integer"):
        load_workflow_config(path)


def test_reviewer_profile_configuration(tmp_path):
    path, _ = configuration(tmp_path)
    path.write_text(path.read_text().replace('merge_profile = "b"', 'reviewer_profile = "b"'))
    config = load_workflow_config(path)
    assert [r["name"] for r in resolve_profiles(config).values()] == ["a", "a", "b"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--task-dir", "task"],
        ["doctor"],
        ["config", "check", "--config", "config.toml"],
    ],
)
def test_reviewer_profile_cli_selection(arguments):
    from rsc.cli import build_parser

    args = build_parser().parse_args([*arguments, "--reviewer-profile", "reviewer"])
    assert selection(args)["reviewer_profile"] == "reviewer"


@pytest.mark.parametrize(
    "body",
    [
        '[agents.parser]\nmodel="x"',
        '[agents]\ndefault_profile="missing"',
        '[agents]\nreviewer_profile="missing"',
        '[agents]\nreviewer_profile=4',
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
    assert json.loads((root / "agent-profiles.json").read_text()) == {"roles": original}
    assert original["reviewer"]["name"] == "b"
    args = SimpleNamespace(config=None, profile=None, parser_profile=None, merge_profile=None)
    assert bind_profiles(root, config, existing=True, args=args) == original
    args.config = str(path)
    assert bind_profiles(root, config, existing=True, args=args) == original
    changed = copy.deepcopy(config)
    changed["agents"]["profiles"]["b"]["effort"] = "max"
    with pytest.raises(ConfigError, match="immutable"):
        bind_profiles(root, changed, existing=True, args=args)
    args.reviewer_profile = "a"
    with pytest.raises(ConfigError, match="immutable"):
        bind_profiles(root, config, existing=True, args=args)
    args.reviewer_profile = None
    args.profile = "a"
    with pytest.raises(ConfigError, match="immutable"):
        bind_profiles(root, config, existing=True, args=args)
    other = tmp_path / "legacy"
    other.mkdir()
    with pytest.raises(ConfigError, match="no profile snapshot"):
        bind_profiles(other, config, existing=True, args=args)


@pytest.mark.parametrize("metadata", ["version", "fingerprint"])
def test_legacy_profile_snapshot_metadata_is_rejected(tmp_path, metadata):
    _, config = configuration(tmp_path)
    snapshot = {"roles": resolve_profiles(config)}
    if metadata == "version":
        snapshot["version"] = 1
    else:
        snapshot["roles"]["parser"]["fingerprint"] = "old-digest"
    (tmp_path / "agent-profiles.json").write_text(json.dumps(snapshot))
    with pytest.raises(ConfigError, match="unsupported profile snapshot"):
        bind_profiles(tmp_path, config, existing=True, args=SimpleNamespace())


@pytest.mark.parametrize("snapshot", [None, [], {}, {"roles": {}}, {"roles": []}])
def test_malformed_profile_snapshot_is_not_replaced(tmp_path, snapshot):
    _, config = configuration(tmp_path)
    path = tmp_path / "agent-profiles.json"
    contents = json.dumps(snapshot)
    path.write_text(contents)
    with pytest.raises(ConfigError, match="unsupported profile snapshot"):
        bind_profiles(tmp_path, config, existing=True, args=SimpleNamespace())
    assert path.read_text() == contents


def test_provider_launch_specs_keep_private_env_out_of_argv(tmp_path, capsys, monkeypatch):
    _, config = configuration(tmp_path, 'envs = [["SECRET", "private value"]]\n')
    monkeypatch.setattr(ADAPTERS["codex"], "executable", lambda _: "/bin/true")
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
        if selected["profile"]["provider"] == "codex":
            assert "--no-daemon" in spec.argv and "--thinking" not in spec.argv
            assert spec.argv[0] == "/bin/true"
            assert "check_for_update_on_startup=false" in spec.argv
            assert spec.argv.count("--disable") == 2
            assert "multi_agent" in spec.argv and "multi_agent_v2" in spec.argv
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


@pytest.mark.parametrize("role", ["parser", "merge", "reviewer"])
def test_pi_has_source_search_read_and_file_submission_tools(tmp_path, role):
    _, config = configuration(tmp_path)
    profile = config["agents"]["profiles"]["b"]
    context = AgentContext(tmp_path, "/tmp/test.sock", "task", role, worker=f"{role}-1")
    spec = ADAPTERS["pi"].prepare(profile, context)
    tools = spec.argv[spec.argv.index("--tools") + 1].split(",")
    assert set(tools) == {"bash", "read", "write", f"mcp__{context.namespace}__*"}
    assert "--no-approve" in spec.argv and "--offline" in spec.argv
    assert spec.argv[spec.argv.index("--name") + 1] == context.terminal_name
    assert spec.argv[spec.argv.index("--session-dir") + 1] == str(context.directory / "sessions")
    bridge = json.loads((context.directory / "mcp-launch.json").read_text())
    assert str(tmp_path / f"{role}-1.token") in bridge["argv"]


def test_capability_checks_deduplicate_equal_profiles_without_fingerprints(tmp_path, monkeypatch):
    _, config = configuration(tmp_path)
    roles = resolve_profiles(config)
    roles["reviewer"]["name"] = "another-name-for-the-same-settings"
    checked = []
    for adapter in ADAPTERS.values():
        monkeypatch.setattr(adapter, "check", lambda profile: checked.append(profile))
    check_profiles(roles)
    assert checked == [roles["parser"]["profile"], roles["merge"]["profile"]]


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


def test_codex_package_launcher_resolves_native_without_running_launcher(tmp_path):
    launcher = tmp_path / "package" / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n# Unified entry point for the Codex CLI\nexit 99\n")
    launcher.chmod(0o700)
    native = tmp_path / "package" / "vendor" / "linux" / "bin" / "codex"
    native.parent.mkdir(parents=True)
    shutil.copyfile("/bin/true", native)
    native.chmod(0o700)
    profile = load_workflow_config()["agents"]["profiles"]["codex-luna-max"]
    profile["executable"] = str(launcher)
    context = AgentContext(tmp_path / "task", "/tmp/test.sock", "task", "reviewer")
    spec = ADAPTERS["codex"].prepare(profile, context)
    assert spec.argv[0] == str(native)
    assert str(launcher) not in spec.argv
    assert "check_for_update_on_startup=false" in spec.argv
