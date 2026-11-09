"""Task-local Unix service and MCP JSONL stdio bridge."""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import socketserver
import stat
import sys
import threading
from pathlib import Path

from .locking import exclusive
from .state import Workflow, WorkflowError
from .tools import schema

MAX_LINE = 32 * 1024 * 1024


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def request(path, payload):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(60)
        connection.connect(str(path))
        connection.sendall(encode(payload))
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            raise WorkflowError("invalid service response length")
        result = json.loads(line)
        if not result.get("ok"):
            raise WorkflowError(result.get("error", "service error"))
        return result["result"]


class TaskServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, workflow):
        self.workflow = workflow
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(60)
        try:
            line = self.rfile.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                raise WorkflowError("invalid request length")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise WorkflowError("request must be an object")
            workflow = self.server.workflow
            with workflow.lock:
                workflow.data = workflow._load()
                role = workflow.authenticate(payload.get("token", ""))
                if payload["op"] == "list":
                    names = (
                        workflow.catalog(role)
                        if payload.get("catalog") == "role"
                        else workflow.allowed(role)
                    )
                    result = [schema(name) for name in sorted(names)]
                elif payload["op"] == "call":
                    name, arguments = payload["name"], payload.get("arguments", {})
                    result = workflow.call(role, name, arguments)
                    if name in {"workflow_status", "claim_assignment", "advance_phase"}:
                        result["allowed_tools"] = sorted(workflow.allowed(role))
                else:
                    raise WorkflowError("unknown operation")
            response = {"ok": True, "result": result}
        except (ValueError, KeyError, TypeError, OSError) as error:
            response = {"ok": False, "error": str(error)}
        self.wfile.write(encode(response))


def serve(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if len(os.fsencode(path)) > 100:
        raise WorkflowError("Unix socket path is too long; supply a shorter --socket path")
    with exclusive(root / "service.lock"):
        marker = root / "service.json"
        if path.exists():
            owned = json.loads(marker.read_text()).get("socket") if marker.exists() else None
            if owned != str(path) or not stat.S_ISSOCK(path.stat().st_mode):
                raise WorkflowError(f"socket path is not owned by this task: {path}")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise WorkflowError(
                            "cannot establish whether existing service is alive"
                        ) from error
                else:
                    raise WorkflowError("task service is already running")
            path.unlink()
        workflow = Workflow(root)
        if workflow.data is None:
            raise WorkflowError("task has not been initialized")
        marker.write_text(json.dumps({"socket": str(path), "pid": os.getpid()}))
        if workflow.data["phase"] == "published":
            workflow.export()
        bound_inode = None
        try:
            with TaskServer(path, workflow) as service:
                bound_inode = path.stat().st_ino
                previous = signal.getsignal(signal.SIGTERM)
                signal.signal(
                    signal.SIGTERM,
                    lambda *_: threading.Thread(target=service.shutdown, daemon=True).start(),
                )
                try:
                    service.serve_forever()
                finally:
                    signal.signal(signal.SIGTERM, previous)
        finally:
            if bound_inode is not None and path.exists() and path.stat().st_ino == bound_inode:
                path.unlink()
            workflow.db.close()


def stdio(path, token_file, catalog="phase"):
    token = Path(token_file).read_text().strip()
    last_tools = None

    def rpc(op, **args):
        return request(path, {"token": token, "op": op, "catalog": catalog, **args})

    def emit(value):
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    while line := sys.stdin.buffer.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE:
            raise WorkflowError("MCP input exceeds maximum")
        msg = {}
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                msg = {}
                raise WorkflowError("MCP request must be an object")
            method = msg.get("method")
            if "id" not in msg:
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "rsc-workflow", "version": "1.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = rpc("list")
                last_tools = [t["name"] for t in tools]
                result = {"tools": tools}
            elif method == "tools/call":
                params = msg["params"]
                try:
                    value = rpc("call", name=params["name"], arguments=params.get("arguments", {}))
                    result = {
                        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
                    }
                except WorkflowError as error:
                    result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            else:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
                continue
            emit({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            if method == "tools/call":
                names = [t["name"] for t in rpc("list")]
                if names != last_tools:
                    emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                    last_tools = names
        except (ValueError, KeyError, TypeError, OSError) as error:
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(error)},
                }
            )
