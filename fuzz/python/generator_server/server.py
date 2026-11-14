"""iceoryx2 request loop.

The server stays single-threaded and forks each case worker itself. A run
request gets two responses on the same active request: ``Started`` with the
worker pid, then ``Result`` from ``waitpid``. There is no Cancel op and no
result pipe. Safe overflow stays off, and the response buffer holds both
messages.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Optional

import cbor2

from .context import Ctx, InteractionError, Session
from .registry import Registry

MAX_MESSAGE = 1 << 20
_registry = Registry()
_sessions: dict[int, Session] = {}


def sessions() -> dict[int, Session]:
    return _sessions


def registry() -> Registry:
    return _registry


def handle_payload(payload: bytes, client_sessions: Optional[dict[int, Session]] = None) -> bytes:
    table = _sessions if client_sessions is None else client_sessions
    try:
        request = cbor2.loads(payload)
    except Exception as exc:
        return _response(0, "generator", f"cbor: {exc}", 0, 0)
    if not isinstance(request, dict):
        return _response(0, "generator", "request must be a map", 0, 0)
    op = request.get("op")
    client = request.get("client")
    request_id = request.get("id", 0)
    if not isinstance(client, int) or not isinstance(request_id, int):
        return _response(0, "generator", "client and id must be integers", 0, 0)
    try:
        if op == "ping":
            return _response(request_id, "ok", None, 0, 0)
        if op == "reset":
            session = table.pop(int(client), None)
            if session is not None:
                session.close()
            return _response(request_id, "ok", None, 0, 0)
        if op == "run":
            return _run(table, request, int(client), int(request_id))
        return _response(request_id, "generator", f"unknown op {op!r}", 0, 0)
    except InteractionError as exc:
        return _response(request_id, exc.status, str(exc), 0, 0)
    except Exception as exc:
        return _response(request_id, "generator", str(exc), 0, 0)


def _run(table: dict[int, Session], request: dict, client: int, request_id: int) -> bytes:
    path = request.get("generator")
    params = request.get("params", {})
    step_trace = request.get("step_trace")
    host = request.get("host", "127.0.0.1")
    port = request.get("port")
    timeout_ms = request.get("timeout_ms", 1000)
    connect_ms = request.get("connect_timeout_ms", timeout_ms)
    if not isinstance(path, str) or not isinstance(params, dict):
        raise InteractionError("generator", "run requires generator path and a params map")
    if step_trace is not None and not isinstance(step_trace, list):
        raise InteractionError("generator", "step_trace must be an array")
    if not isinstance(host, str) or not isinstance(port, int) or not isinstance(timeout_ms, int):
        raise InteractionError("generator", "run requires host, port and timeout_ms")
    if not isinstance(connect_ms, int):
        raise InteractionError("generator", "connect_timeout_ms must be an integer")
    if timeout_ms < 0 or connect_ms < 0:
        raise InteractionError("generator", "timeouts must be >= 0")
    generate = _registry.get(path)
    session = table.get(client)
    if session is None:
        session = Session()
        table[client] = session
    ctx = Ctx(
        session,
        host,
        int(port),
        timeout_ms / 1000.0,
        connect_timeout_s=connect_ms / 1000.0,
    )
    ctx.generator_params = _plain_params(params)
    ctx.global_context = ctx.generator_params
    try:
        generate(ctx, _plain_step_trace(step_trace or []))
    finally:
        ctx.close_extra()
    return _response(request_id, "ok", None, ctx.bytes_sent, ctx.bytes_recv)


def _plain_step_trace(trace: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in trace:
        if not isinstance(item, dict):
            raise InteractionError("generator", "step_trace entries must be objects")
        row = {}
        for key, value in item.items():
            if key == "inputs" and isinstance(value, dict):
                row[key] = {str(name): _plain(inner) for name, inner in value.items()}
            elif key == "omitted_fields" and isinstance(value, list):
                row[key] = [str(name) for name in value]
            elif key == "broken_dependencies" and isinstance(value, list):
                row[key] = [str(name) for name in value]
            else:
                row[key] = _plain(value)
        if not isinstance(row.get("name"), str) or not isinstance(row.get("step"), str):
            raise InteractionError("generator", "step_trace entries need name and step")
        out.append(row)
    return out


def _plain_params(params: dict) -> dict[str, Any]:
    out = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise InteractionError("generator", "param keys must be text")
        out[key] = _plain(value)
    return out


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, str, bool, bytes)):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    raise InteractionError("generator", f"unsupported param value {type(value).__name__}")


def _response(request_id: int, status: str, error: Optional[str], sent: int, recv: int) -> bytes:
    body: dict[str, Any] = {
        "id": request_id,
        "status": status,
        "sent": sent,
        "recv": recv,
    }
    if error:
        body["error"] = error
    encoded = cbor2.dumps(body)
    if len(encoded) > MAX_MESSAGE:
        encoded = cbor2.dumps(
            {
                "id": request_id,
                "status": "generator",
                "error": "response too large",
                "sent": sent,
                "recv": recv,
            }
        )
    return encoded


def _as_bytes(payload) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    length = payload.len() if hasattr(payload, "len") else len(payload)
    return bytes(int(payload[index]) for index in range(length))


def _thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return 1


def _require_one_thread(where: str) -> None:
    count = _thread_count()
    if count != 1:
        raise SystemExit(f"generator server has {count} threads {where}")


def _case_key(request: dict) -> tuple:
    worker = request.get("worker_id", request.get("client"))
    case = request.get("case_id", request.get("id"))
    return (worker, case)


def _reply(active, payload: bytes) -> None:
    sample = active.loan_slice_uninit(len(payload))
    view = sample.payload()
    for index, byte in enumerate(payload):
        view[index] = byte
    sample.assume_init().send()
    active.delete()


def _identity(request: dict) -> tuple[int, int, int]:
    request_id = int(request.get("id", 0))
    worker_id = request.get("worker_id", request.get("client", 0))
    case_id = request.get("case_id", request.get("id", 0))
    return request_id, int(worker_id), int(case_id)


def _started_body(request: dict, pid: int) -> bytes:
    request_id, worker_id, case_id = _identity(request)
    return cbor2.dumps(
        {
            "kind": "started",
            "id": request_id,
            "worker_id": worker_id,
            "case_id": case_id,
            "pid": pid,
        }
    )


def _result_body(request: dict, status: int) -> bytes:
    request_id, worker_id, case_id = _identity(request)
    body: dict[str, Any] = {
        "kind": "result",
        "id": request_id,
        "worker_id": worker_id,
        "case_id": case_id,
    }
    if os.WIFEXITED(status):
        body["code"] = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        body["signal"] = os.WTERMSIG(status)
    else:
        body["code"] = 1
    return cbor2.dumps(body)


def serve(service_name: str) -> None:
    import ctypes

    import iceoryx2 as iox2

    from .worker import fork_worker

    _require_one_thread("before iceoryx2")
    iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    _require_one_thread("after iceoryx2 node create")
    builder = node.service_builder(iox2.ServiceName.new(service_name)).request_response(
        iox2.Slice[ctypes.c_uint8], iox2.Slice[ctypes.c_uint8]
    )
    if hasattr(builder, "max_active_requests_per_client"):
        builder = builder.max_active_requests_per_client(4)
    if hasattr(builder, "max_response_buffer_size"):
        builder = builder.max_response_buffer_size(2)
    if hasattr(builder, "enable_safe_overflow_for_responses"):
        builder = builder.enable_safe_overflow_for_responses(False)
    if hasattr(builder, "enable_safe_overflow_for_requests"):
        builder = builder.enable_safe_overflow_for_requests(False)
    service = builder.open_or_create()
    server_builder = service.server_builder().initial_max_slice_len(65536).allocation_strategy(
        iox2.AllocationStrategy.PowerOfTwo
    )
    server = server_builder.create()
    wait = iox2.Duration.from_micros(200)
    worker: Optional[tuple[int, dict, object]] = None
    while True:
        _require_one_thread("before fork")
        if worker is not None:
            pid, request, active = worker
            done, status = os.waitpid(pid, os.WNOHANG)
            if done != 0:
                _send(active, _result_body(request, status))
                active.delete()
                worker = None
        active = server.receive()
        if active is None:
            try:
                node.wait(wait)
            except iox2.NodeWaitFailure:
                return
            continue
        raw = _as_bytes(active.payload())
        try:
            request = cbor2.loads(raw) if len(raw) <= MAX_MESSAGE else None
        except Exception:
            request = None
        if not isinstance(request, dict):
            _reply(active, _response(0, "generator", "request must be a map", 0, 0))
            continue
        op = request.get("op")
        if op == "run":
            if worker is not None:
                _reply(
                    active,
                    _response(int(request.get("id", 0)), "generator", "a run is already active", 0, 0),
                )
                continue
            _require_one_thread("immediately before fork")
            try:
                pid = fork_worker(raw)
            except Exception as exc:
                _reply(active, _response(int(request.get("id", 0)), "generator", str(exc), 0, 0))
                continue
            try:
                _send(active, _started_body(request, pid))
            except Exception as exc:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
                _reply(active, _response(int(request.get("id", 0)), "generator", str(exc), 0, 0))
                continue
            worker = (pid, request, active)
            continue
        _reply(active, handle_payload(raw))


def _send(active, payload: bytes) -> None:
    sample = active.loan_slice_uninit(len(payload))
    view = sample.payload()
    for index, byte in enumerate(payload):
        view[index] = byte
    sample.assume_init().send()


def main_module_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
