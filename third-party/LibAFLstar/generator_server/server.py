"""iceoryx2 request loop. One CBOR request, one CBOR response."""

import os
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
    host = request.get("host", "127.0.0.1")
    port = request.get("port")
    timeout_ms = request.get("timeout_ms", 1000)
    if not isinstance(path, str) or not isinstance(params, dict):
        raise InteractionError("generator", "run requires generator path and params map")
    if not isinstance(host, str) or not isinstance(port, int) or not isinstance(timeout_ms, int):
        raise InteractionError("generator", "run requires host, port and timeout_ms")
    if timeout_ms < 0:
        raise InteractionError("generator", "timeout_ms must be >= 0")
    generate = _registry.get(path)
    session = table.get(client)
    if session is None:
        session = Session()
        table[client] = session
    ctx = Ctx(session, host, int(port), timeout_ms / 1000.0)
    try:
        generate(ctx, _plain_params(params))
    finally:
        ctx.close_extra()
    return _response(request_id, "ok", None, ctx.bytes_sent, ctx.bytes_recv)


def _plain_params(params: dict) -> dict[str, Any]:
    out = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise InteractionError("generator", "param keys must be text")
        out[key] = _plain(value)
    return out


def _plain(value: Any) -> Any:
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
            {"id": request_id, "status": "generator", "error": "response too large", "sent": sent, "recv": recv}
        )
    return encoded


def _as_bytes(payload) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    length = payload.len() if hasattr(payload, "len") else len(payload)
    return bytes(int(payload[index]) for index in range(length))


def serve(service_name: str) -> None:
    import ctypes

    import iceoryx2 as iox2

    iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    service = (
        node.service_builder(iox2.ServiceName.new(service_name))
        .request_response(iox2.Slice[ctypes.c_uint8], iox2.Slice[ctypes.c_uint8])
        .open_or_create()
    )
    server = (
        service.server_builder()
        .initial_max_slice_len(65536)
        .allocation_strategy(iox2.AllocationStrategy.PowerOfTwo)
        .create()
    )
    wait = iox2.Duration.from_micros(200)
    while True:
        active = server.receive()
        if active is None:
            try:
                node.wait(wait)
            except iox2.NodeWaitFailure:
                return
            continue
        raw = _as_bytes(active.payload())
        if len(raw) > MAX_MESSAGE:
            reply = _response(0, "generator", "request exceeds 1 MiB", 0, 0)
        else:
            reply = handle_payload(raw)
        sample = active.loan_slice_uninit(len(reply))
        view = sample.payload()
        for index, byte in enumerate(reply):
            view[index] = byte
        sample.assume_init().send()
        active.delete()


def main_module_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
