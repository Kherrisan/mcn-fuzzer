"""Review-limit CLI precedence and durable task configuration, without live agents."""

import pytest

from rsc.cli import build_parser
from rsc.workflow import Workflow, WorkflowError, cli, supervisor


@pytest.mark.parametrize(
    "configured,override,expected", [(None, None, 5), (2, None, 2), (2, 4, 4), (None, 7, 7)]
)
def test_review_limit_precedence_and_resume(tmp_path, monkeypatch, configured, override, expected):
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
    root = tmp_path / "task"
    args = ["run", "--task-dir", str(root), "--rfc", str(source), "--protocol", "Example"]
    if configured is not None:
        config = tmp_path / "config.toml"
        config.write_text(f"[workflow]\nmax_review_rounds = {configured}\n")
        args += ["--config", str(config)]
    if override is not None:
        args += ["--max-review-rounds", str(override)]
    parsed = build_parser().parse_args(args)
    assert parsed.func(parsed) == 0
    task = Workflow(root)
    assert task.data["max_review_rounds"] == expected
    task.db.close()
    for role in ("parser", "reviewer"):
        assert launches[0]["resolved_agents"][role]["profile"]["model"] == "gpt-6-luna"
        assert launches[0]["resolved_agents"][role]["profile"]["effort"] == "max"
    # A plain resume must preserve the original limit, including CLI overrides.
    resumed = build_parser().parse_args(["run", "--task-dir", str(root)])
    assert resumed.func(resumed) == 0
    assert launches[-1]["workflow"]["max_review_rounds"] == expected
    conflicting = build_parser().parse_args(
        ["run", "--task-dir", str(root), "--max-review-rounds", str(expected + 1)]
    )
    with pytest.raises(WorkflowError, match="cannot change the review limit"):
        conflicting.func(conflicting)
    assert len(launches) == 2


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid"])
def test_review_limit_rejects_invalid_cli_values(tmp_path, value):
    root = tmp_path / "not-created"
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--task-dir", str(root), "--max-review-rounds", value])
    assert error.value.code == 2
    assert not root.exists()
