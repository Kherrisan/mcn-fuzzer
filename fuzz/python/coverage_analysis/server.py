"""Coverage analysis service: background JSON-line server and MCP stdio bridge.

Run ``serve`` once per campaign. Agents connect through ``stdio``, which speaks
MCP and forwards tool calls to the Unix socket. The server owns all parsed
state; the bridge is stateless.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_analysis import proposal as proposal_mod  # noqa: E402
from coverage_analysis import protocol as protocol_mod  # noqa: E402
from coverage_analysis import tools  # noqa: E402
from coverage_analysis.state import State, StateError  # noqa: E402

MAX_LINE = 8 << 20


def _reply(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def _error(request_id, error):
    if isinstance(error, StateError):
        body = {"code": error.code, "message": str(error), "missing": error.missing}
    elif isinstance(error, (ValueError, KeyError)):
        body = {"code": "bad_request", "message": str(error), "missing": []}
    else:
        body = {"code": "internal", "message": str(error), "missing": []}
    return {"id": request_id, "ok": False, "error": body}


def _dispatch(state, message):
    request_id = message.get("id")
    op = message.get("op")
    if op == "health":
        return {"id": request_id, "ok": True,
                "result": {"pid": os.getpid(), "state_dir": str(state.state_dir)}}
    if op == "tool":
        name = message.get("name")
        with state.lock:
            result = tools.call(state, name, message.get("arguments") or {})
        return {"id": request_id, "ok": True, "result": result}
    if op == "register_request":
        with state.lock:
            result = state.register_request(message.get("request") or {})
        return {"id": request_id, "ok": True, "result": result}
    if op == "begin_analysis":
        with state.lock:
            result = state.begin_analysis(message["request_id"])
        return {"id": request_id, "ok": True, "result": result}
    if op == "submit_proposal":
        with state.lock:
            result = state.submit_proposal(message["request_id"], message)
        return {"id": request_id, "ok": True, "result": result}
    if op == "commit_analysis":
        with state.lock:
            result = state.commit_analysis(message["request_id"], message["proposal_sha256"])
        return {"id": request_id, "ok": True, "result": result}
    if op == "abort_analysis":
        with state.lock:
            result = state.abort_analysis(message["request_id"])
        return {"id": request_id, "ok": True, "result": result}
    if op == "loaded_state":
        with state.lock:
            result = state.loaded_state()
        return {"id": request_id, "ok": True, "result": result}
    if op == "eval_state":
        with state.lock:
            loaded = state.loaded_state()
            version = loaded["coverage"].get("version")
            if version is not None:
                loaded["coverage_request"] = next(
                    (request.request_id for request in reversed(state.requests.values())
                     if request.bindings.get("coverage") == version), None)
            result = {"loaded": loaded, "checkpoint": state.committed_checkpoint}
        return {"id": request_id, "ok": True, "result": result}
    if op == "shutdown":
        return {"id": request_id, "ok": True, "result": {"stopping": True}}
    raise StateError("unknown_op", f"unknown operation {op}")


async def _handle(reader, writer, state, stop_event):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            if len(line) > MAX_LINE:
                writer.write(_reply(_error(None, StateError("too_large", "request is too large"))))
                await writer.drain()
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as error:
                writer.write(_reply(_error(None, StateError("bad_json", str(error)))))
                await writer.drain()
                continue
            started = time.monotonic()
            try:
                if message.get("op") == "health":
                    reply = _dispatch(state, message)
                else:
                    reply = await asyncio.to_thread(_dispatch, state, message)
            except Exception as error:  # noqa: BLE001 - every failure becomes a tool error
                reply = _error(message.get("id"), error)
            if message.get("op") != "health":
                body = {
                    "op": message.get("op"),
                    "ok": bool(reply.get("ok")),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
                if message.get("op") == "tool":
                    body["tool"] = message.get("name")
                    body["request_id"] = (message.get("arguments") or {}).get("request_id")
                elif message.get("op") in ("register_request", "commit_analysis", "abort_analysis"):
                    body["request_id"] = (message.get("request") or {}).get("request_id") or message.get("request_id")
                if not reply.get("ok"):
                    body["error"] = reply.get("error")
                state.log("call", body)
            writer.write(_reply(reply))
            await writer.drain()
            if message.get("op") == "shutdown":
                stop_event.set()
                break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def serve(socket_path, state_dir, source_root, session_config=None):
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    state = State(state_dir, source_root)
    if session_config:
        state.load_session_config(session_config)
    stop_event = asyncio.Event()
    server = await asyncio.start_unix_server(
        lambda r, w: _handle(r, w, state, stop_event), path=str(path))
    os.chmod(path, 0o600)
    print(json.dumps({"event": "ready", "socket": str(path), "pid": os.getpid()}), flush=True)
    async with server:
        await stop_event.wait()
        server.close()
        await server.wait_closed()


def _call(socket_path, payload, timeout=600.0):
    request = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(request)
        chunks = []
        while True:
            data = client.recv(1 << 20)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    body = b"".join(chunks).split(b"\n", 1)[0]
    if not body:
        raise RuntimeError("coverage service closed the connection without a reply")
    reply = json.loads(body)
    if not reply.get("ok"):
        error = reply["error"]
        raise StateError(error["code"], error["message"], error["missing"])
    return reply.get("result")


def _mcp_server(socket_path):
    import inspect
    from typing import Annotated

    from mcp.server import MCPServer
    from mcp.types import CallToolResult, TextContent
    from pydantic import TypeAdapter

    from coverage_analysis import schemas

    async def dispatch(name, arguments):
        try:
            result = await asyncio.to_thread(
                _call, socket_path,
                {"op": "tool", "name": name, "arguments": arguments})
            return CallToolResult(
                content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
                structuredContent=result,
            )
        except StateError as error:
            return CallToolResult(isError=True, content=[TextContent(
                text=json.dumps(_error(None, error)["error"], ensure_ascii=False))])
        except Exception as error:
            return CallToolResult(isError=True, content=[TextContent(
                text=json.dumps({"code": "service_error", "message": str(error)}, ensure_ascii=False))])

    class CoverageMCPServer(MCPServer):
        async def _handle_call_tool(self, ctx, params):
            # Send raw arguments through the shared socket dispatcher so MCP
            # and Rust use the same models and structured error responses.
            return await dispatch(params.name, params.arguments or {})

    server = CoverageMCPServer("coverage-analysis")

    def register(name, description, input_model, output_model):
        async def invoke(**arguments):
            validated = input_model.model_validate(arguments)
            return await dispatch(name, validated.model_dump(exclude_none=True))

        parameters = []
        for field_name, field in input_model.model_fields.items():
            parameters.append(inspect.Parameter(
                field_name, inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if field.is_required() else field.default,
                annotation=field.rebuild_annotation(),
            ))
        invoke.__signature__ = inspect.Signature(
            parameters, return_annotation=Annotated[CallToolResult, output_model])
        server.add_tool(invoke, name=name, description=description)
        tool = server._tool_manager.get_tool(name)
        tool.parameters = input_model.model_json_schema()
        # The SDK wraps unions in {"result": ...}; all these variants already
        # are objects and must retain their established JSON shape.
        tool.fn_metadata.output_model = output_model
        tool.fn_metadata.output_schema = TypeAdapter(output_model).json_schema()
        tool.fn_metadata.output_schema.setdefault("type", "object")
        tool.fn_metadata.wrap_output = False

    for name, description in tools.TOOL_DESCRIPTIONS.items():
        register(name, description, *schemas.TOOL_MODELS[name])
    return server


async def stdio(socket_path):
    await _mcp_server(socket_path).run_stdio_async()


def _proposal_command(args):
    graph = protocol_mod.load_protocol(args.protocol, args.checkpoint)
    handlers = None
    if args.handlers:
        from coverage_analysis.handlers import load_handlers
        handlers = load_handlers(args.handlers)
    draft = json.loads(Path(args.draft).read_text())
    if draft.get("mutations"):
        raise proposal_mod.ProposalError(
            "proposal drafts declare mutable_fields per trace instance, not mutations"
        )
    proposal = proposal_mod.build_proposal(
        graph,
        origin=str(draft.get("origin") or "autonomous"),
        session=str(draft.get("session") or ""),
        build=str(draft.get("build") or ""),
        snapshot=str(draft.get("snapshot") or ""),
        goal=draft.get("goal") or {},
        trace=draft.get("trace") or [],
        handlers=handlers,
        parties=draft.get("parties") or {},
        channels=draft.get("channels") or {},
        dependencies=draft.get("dependencies"),
    )
    proposal_mod.validate_proposal(proposal, graph, handlers)
    proposal_mod.freeze(args.output, proposal)
    print(json.dumps({"output": str(args.output)}))


def _validate_command(args):
    graph = protocol_mod.load_protocol(args.protocol, args.checkpoint)
    handlers = None
    if args.handlers:
        from coverage_analysis.handlers import load_handlers
        handlers = load_handlers(args.handlers)
    proposal = proposal_mod.read_proposal(args.proposal)
    proposal_mod.validate_proposal(proposal, graph, handlers)
    print(json.dumps({"valid": True}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--socket", required=True)
    serve_parser.add_argument("--state-dir", required=True)
    serve_parser.add_argument("--source-root")
    serve_parser.add_argument("--session-config")

    stdio_parser = sub.add_parser("stdio")
    stdio_parser.add_argument("--socket", required=True)
    proposal_parser = sub.add_parser("proposal")
    proposal_parser.add_argument("--protocol", required=True)
    proposal_parser.add_argument("--handlers")
    proposal_parser.add_argument("--checkpoint")
    proposal_parser.add_argument("--draft", required=True)
    proposal_parser.add_argument("--output", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--protocol", required=True)
    validate_parser.add_argument("--handlers")
    validate_parser.add_argument("--checkpoint")
    validate_parser.add_argument("--proposal", required=True)
    args = parser.parse_args(argv)
    if args.command == "serve":
        asyncio.run(serve(args.socket, args.state_dir, args.source_root,
                          args.session_config))
    elif args.command == "stdio":
        asyncio.run(stdio(args.socket))
    elif args.command == "proposal":
        _proposal_command(args)
    else:
        _validate_command(args)


if __name__ == "__main__":
    main()
