"""Exercise the actual authenticated socket transport and MCP stdio protocol."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from rsc.workflow import Workflow, WorkflowError
from rsc.workflow.server import TaskServer, request


def test_socket_auth_and_phase_tools(tmp_path):
    w = Workflow.create(tmp_path / "task", [], "Example")
    socket = tmp_path / "service.sock"
    parser = (w.root / "parser.token").read_text()
    reviewer = (w.root / "reviewer.token").read_text()
    with TaskServer(socket, w) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tools = request(socket, {"token": reviewer, "op": "list"})
            assert [t["name"] for t in tools] == ["workflow_status"]
            with pytest.raises(WorkflowError, match="credential"):
                request(socket, {"token": "bad", "op": "list"})
            with pytest.raises(WorkflowError, match="forbidden"):
                request(socket, {"token": reviewer, "op": "call", "name": "prepare_sources"})
            with pytest.raises(WorkflowError, match="invalid arguments"):
                request(
                    socket,
                    {
                        "token": parser,
                        "op": "call",
                        "name": "advance_phase",
                        "arguments": {"phase": "published"},
                    },
                )
        finally:
            server.shutdown()
            thread.join()


def test_stdio_bridge(tmp_path):
    w = Workflow.create(tmp_path / "task", [], "Example")
    socket = tmp_path / "mcp.sock"
    with TaskServer(socket, w) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "apply_changes",
                    "arguments": {"expected_revision": 0, "operation_id": "x", "changes": []},
                },
            },
        ]
        script = (
            "from rsc.workflow.server import stdio; import sys; stdio(sys.argv[1], sys.argv[2])"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", script, str(socket), str(w.root / "parser.token")],
                input="".join(json.dumps(m) + "\n" for m in messages),
                text=True,
                capture_output=True,
                timeout=15,
                env=os.environ.copy(),
                cwd=Path(__file__).resolve().parents[1],
            )
            assert result.returncode == 0, result.stderr
            replies = {m["id"]: m for m in map(json.loads, result.stdout.splitlines()) if "id" in m}
            assert replies[1]["result"]["capabilities"]["tools"]["listChanged"]
            assert "prepare_sources" in [t["name"] for t in replies[2]["result"]["tools"]]
            assert replies[3]["result"]["isError"]
            error = json.loads(replies[3]["result"]["content"][0]["text"])
            assert "forbidden" in error["error"]
            assert error["tool"] == "apply_changes"
        finally:
            server.shutdown()
            thread.join()


def test_cached_catalog_does_not_grant_future_phase_permissions(tmp_path):
    w = Workflow.create(tmp_path / "task", [], "Example")
    socket = tmp_path / "cached.sock"
    token = (w.root / "parser.token").read_text()
    with TaskServer(socket, w) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tools = request(socket, {"token": token, "op": "list", "catalog": "role"})
            assert "apply_changes" in [t["name"] for t in tools]
            assert "submit_findings" not in [t["name"] for t in tools]
            with pytest.raises(WorkflowError, match="forbidden"):
                request(
                    socket,
                    {
                        "token": token,
                        "op": "call",
                        "name": "apply_changes",
                        "arguments": {
                            "expected_revision": 0,
                            "operation_id": "future",
                            "changes": [],
                        },
                    },
                )
        finally:
            server.shutdown()
            thread.join()


def test_service_recovers_owned_stale_socket_and_rejects_duplicate_server(tmp_path):
    import time

    w = Workflow.create(tmp_path / "task", [], "Recovery fixture")
    socket = tmp_path / "recovery.sock"
    token = (w.root / "parser.token").read_text()
    argv = [
        sys.executable,
        "-m",
        "rsc.workflow.cli",
        "serve",
        "--task-dir",
        str(w.root),
        "--socket",
        str(socket),
    ]

    def start():
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[1],
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode())
            try:
                result = request(socket, {"token": token, "op": "call", "name": "workflow_status"})
                return process, result
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.02)
        process.kill()
        process.wait()
        raise AssertionError("service readiness timeout")

    first, status = start()
    current = first
    try:
        dispatch = status["assignment"]["dispatch_id"]
        request(
            socket,
            {
                "token": token,
                "op": "call",
                "name": "claim_assignment",
                "arguments": {"dispatch_id": dispatch},
            },
        )
        # A second service cannot acquire the task even with a different socket.
        duplicate = subprocess.run(
            [*argv[:-1], str(tmp_path / "other.sock")],
            text=True,
            capture_output=True,
            timeout=10,
            cwd=Path(__file__).resolve().parents[1],
        )
        assert duplicate.returncode == 2
        assert "already running" in duplicate.stderr
        first.kill()
        first.wait(timeout=5)
        assert socket.exists()
        current, recovered = start()
        assert recovered["assignment"]["dispatch_id"] == dispatch
        assert recovered["assignment"]["claimed"] is True
        assert recovered["round"] == 0
    finally:
        if current.poll() is None:
            current.terminate()
            current.wait(timeout=5)
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
    assert not socket.exists()


def test_console_bootstraps_service_without_pythonpath_from_checkout_parent(tmp_path):
    project = Path(__file__).resolve().parents[1]
    console = tmp_path / "rsc-console"
    # Same import/bootstrap behavior as the installed console entry point.
    console.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(project)!r})\n"
        "from rsc.cli import main\nsys.exit(main())\n"
    )
    console.chmod(0o700)
    hc = tmp_path / "fixture-hc"
    hc.write_text(
        f"#!{sys.executable}\nimport sys\n"
        'print("fixture stopped at HC startup", file=sys.stderr)\nsys.exit(7)\n'
    )
    hc.chmod(0o700)
    source = tmp_path / "rfc9999.txt"
    source.write_text("RFC 9999\n\n1. Test\n\nThe client sends HELLO.\n")
    root = tmp_path / "task"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(console),
            "run",
            "--task-dir",
            str(root),
            "--rfc",
            str(source),
            "--protocol",
            "Bootstrap fixture",
            "--hc",
            str(hc),
        ],
        cwd=project.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "fixture stopped at HC startup" in result.stderr
    assert "ImportError" not in (root / "server.stderr.log").read_text()
    assert not (root / "workflow.sock").exists()
