"""Worker/profile configuration persists with the independent reviewer CLI."""

import pytest

from rsc.cli import build_parser
from rsc.config import ConfigError, load_workflow_config
from rsc.workflow import cli, supervisor


def test_worker_settings_and_profiles_survive_resume(tmp_path, monkeypatch):
    launches = []

    class Runner:
        def __init__(self, root, socket, config, hc, retry_worker=None):
            launches.append(config)

        def run(self):
            pass

    monkeypatch.setattr(supervisor, "Supervisor", Runner)
    monkeypatch.setattr(cli, "request", lambda *_: {})
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Example\n\nThe client sends HELLO.\n")
    config = tmp_path / "config.toml"
    config.write_text("[workflow]\nparse_workers=1\nparse_max_workers=2\nmax_feedback_rounds=5\n")
    root = tmp_path / "task"
    args = build_parser().parse_args(
        [
            "run",
            "--task-dir",
            str(root),
            "--rfc",
            str(source),
            "--protocol",
            "Example",
            "--config",
            str(config),
        ]
    )
    assert args.func(args) == 0
    config.write_text("[workflow]\nmax_feedback_rounds=2\n")
    resumed = build_parser().parse_args(["run", "--task-dir", str(root)])
    assert resumed.func(resumed) == 0
    assert launches[-1]["workflow"]["parse_workers"] == 1
    assert launches[-1]["workflow"]["parse_max_workers"] == 2
    assert launches[-1]["workflow"]["max_feedback_rounds"] == 5
    assert launches[-1]["resolved_agents"]["reviewer"] == launches[-1]["resolved_agents"]["parser"]


@pytest.mark.parametrize("key", ["review_workers", "max_review_rounds", "merge_workers"])
def test_removed_configuration_rejected(tmp_path, key):
    config = tmp_path / "config.toml"
    config.write_text(f"[workflow]\n{key}=3\n")
    with pytest.raises(ConfigError, match="unknown workflow|merge_workers"):
        load_workflow_config(config)
