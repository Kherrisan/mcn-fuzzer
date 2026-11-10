"""Truncated display names never select another campaign's native conversation."""

import sqlite3
from dataclasses import replace

import pytest

from rsc.agents import ADAPTERS, AgentContext
from rsc.config import ConfigError


@pytest.fixture
def native(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name, cwd, created_at)")
    profile = {
        "provider": "codex",
        "executable": "/bin/true",
        "model": "gpt-6-luna",
        "effort": "high",
        "args": [],
        "envs": [["CODEX_HOME", str(home)]],
    }
    yield ADAPTERS["codex"], profile, db
    db.close()


def insert(db, context, thread, created=100):
    db.execute(
        "INSERT INTO threads VALUES (?,?,?,?)",
        (thread, context.terminal_name, str(context.root), created),
    )
    db.commit()


def test_identical_display_names_restore_exact_sessions_in_different_campaigns(tmp_path, native):
    adapter, profile, db = native
    contexts = [
        AgentContext(
            tmp_path / suffix,
            "/tmp/service.sock",
            "a-long-common-campaign-prefix-" + suffix,
            "parser",
            worker="parser-1",
            resume=True,
        )
        for suffix in ("one", "two")
    ]
    assert contexts[0].terminal_name == contexts[1].terminal_name
    threads = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    for context, thread in zip(contexts, threads, strict=True):
        insert(db, context, thread)
    for context, thread in zip(contexts, threads, strict=True):
        saved = adapter.session(profile, context)
        assert saved["name"] == context.terminal_name and saved["id"] == thread
        assert adapter.prepare(profile, replace(context, session_id=thread)).argv[-2:] == [
            "resume",
            thread,
        ]
    # A persisted ID from another campaign cannot override directory ownership.
    with pytest.raises(ConfigError, match="unavailable"):
        adapter.prepare(profile, replace(contexts[0], session_id=threads[1]))


def test_known_identity_beats_same_directory_name_collision_and_missing_identity_never_guesses(
    tmp_path, native
):
    adapter, profile, db = native
    context = AgentContext(tmp_path / "task", "/tmp/sock", "task", "parser", worker="parser-1")
    insert(db, context, "old", 100)
    insert(db, context, "current", 200)
    assert adapter.session(profile, context, started_at=150)["id"] == "current"
    assert adapter.session(profile, context, started_at=300) is None
    with pytest.raises(ConfigError, match="ambiguous"):
        adapter.prepare(profile, replace(context, resume=True))
    resumed = replace(context, resume=True, session_id="current")
    assert adapter.prepare(profile, resumed).argv[-2:] == ["resume", "current"]
    # Resume retains the original creation time; process restart is not a new conversation.
    assert adapter.session(profile, resumed, started_at=300)["id"] == "current"


def test_absent_native_state_waits_for_initialization_without_creating_a_database(tmp_path, native):
    adapter, profile, _ = native
    missing = tmp_path / "not-created"
    profile["envs"] = [["CODEX_HOME", str(missing)]]
    context = AgentContext(tmp_path, "/tmp/sock", "task", "parser")
    assert adapter.session(profile, context) is None and not missing.exists()
    with pytest.raises(ConfigError, match="unavailable"):
        adapter.prepare(profile, replace(context, resume=True))
    assert not missing.exists()
