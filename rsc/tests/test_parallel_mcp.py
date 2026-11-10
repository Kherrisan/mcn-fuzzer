"""Real socket authentication, phase catalogs and MCP list-changed notifications."""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import test_parallel_workflow as helpers

from rsc.workflow.server import TaskServer, request

run = helpers.run


def test_phase_catalog_notifications_and_cached_catalog_permissions(run, tmp_path):
    socket = tmp_path / "mcp.sock"
    token = (run.root / "parser-1.token").read_text()
    a = run.store.assignment("parse-0/parser-1")
    with TaskServer(socket, run) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cached = request(socket, {"op": "list", "token": token, "catalog": "role"})
            assert "submit_resolutions" in {t["name"] for t in cached}
            with pytest.raises(ValueError, match="forbidden"):
                request(
                    socket,
                    {
                        "op": "call",
                        "token": token,
                        "name": "submit_resolutions",
                        "arguments": {"revision": 0, "resolutions": []},
                    },
                )
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "claim_assignment",
                        "arguments": {"name": a["name"], "attempt": 1},
                    },
                },
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            ]
            script = (
                "from rsc.workflow.server import stdio; import sys; stdio(sys.argv[1], sys.argv[2])"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(socket), str(run.root / "parser-1.token")],
                input="".join(json.dumps(m) + "\n" for m in messages),
                capture_output=True,
                text=True,
                timeout=10,
                cwd=Path(__file__).resolve().parents[1],
            )
            assert result.returncode == 0, result.stderr
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            replies = {r["id"]: r for r in responses if "id" in r}
            assert [t["name"] for t in replies[2]["result"]["tools"]] == ["claim_assignment"]
            assert "apply_changes" in {t["name"] for t in replies[4]["result"]["tools"]}
            assert any(r.get("method") == "notifications/tools/list_changed" for r in responses)
            for name in (
                "prepare_sources",
                "workflow_status",
                "check_gate",
                "read_batch",
                "complete_review_batch",
            ):
                with pytest.raises(ValueError, match="forbidden"):
                    request(socket, {"op": "call", "token": token, "name": name})
        finally:
            server.shutdown()
            thread.join()
