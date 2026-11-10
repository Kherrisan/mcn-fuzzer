"""Real socket and stdio bridge expose only the result workflow."""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_result_workflow import run as run

from rsc.workflow.server import TaskServer, request


def test_six_tools_real_bridge_and_no_receipts(run, tmp_path):
    sock = tmp_path / "mcp.sock"
    token_path = run.root / "parser-1.token"
    token = token_path.read_text().strip()
    with TaskServer(sock, run) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            listed = request(sock, {"op": "list", "token": token, "catalog": "role"})
            names = {t["name"] for t in listed}
            assert names == {
                "read_source_batches",
                "search_source",
                "query_model",
                "submit_parse_result",
                "submit_clustering_result",
            }
            for name in (
                "claim_assignment",
                "apply_changes",
                "advance_phase",
                "workflow_status",
                "submit_merge_result",
            ):
                with pytest.raises(ValueError, match="forbidden"):
                    request(sock, {"op": "call", "token": token, "name": name, "arguments": {}})
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "read_source_batches",
                        "arguments": {"batch_ids": [run.batches[0]["batch_id"]]},
                    },
                },
            ]
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "rsc.workflow.cli",
                    "mcp",
                    "--socket",
                    str(sock),
                    "--token-file",
                    str(token_path),
                ],
                input="".join(json.dumps(m) + "\n" for m in messages),
                text=True,
                capture_output=True,
                timeout=10,
                cwd=Path(__file__).resolve().parents[1],
            )
            assert result.returncode == 0, result.stderr
            replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()) if "id" in r}
            content = json.loads(replies[3]["result"]["content"][0]["text"])
            assert content["batches"][0]["batch_id"] == run.batches[0]["batch_id"]
            assert "receipt" not in json.dumps(content)
            assert run.store.assignments()[0]["status"] == "running"
        finally:
            server.shutdown()
            thread.join()
