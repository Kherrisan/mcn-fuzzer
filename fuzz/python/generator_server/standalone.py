"""Run one generator in this process.

This module does not import the resident server, its worker, CBOR, or iceoryx2.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

from generator_server.context import Ctx, Session
from generator_server.registry import Registry


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: standalone.py REQUEST.json", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as handle:
        request = json.load(handle)
    result_path = request["result"]
    started = time.monotonic()
    status = "ok"
    exception_type = None
    error_text = None
    ctx = None
    try:
        registry = Registry()
        generate = registry.get(request["generator"])
        ctx = Ctx(
            Session(),
            str(request["host"]),
            int(request["port"]),
            float(request["timeout_s"]),
            connect_timeout_s=float(request["connect_timeout_s"]),
        )
        ctx.generator_params = request.get("params") or {}
        ctx.global_context = ctx.generator_params
        trace = request.get("step_trace")
        if trace or getattr(generate, "_mcn_dispatch", False):
            generate(ctx, trace or [])
        else:
            generate(ctx, request.get("params") or {})
    except Exception as exc:
        status = "error"
        exception_type = type(exc).__name__
        error_text = traceback.format_exc()
    finally:
        if ctx is not None:
            ctx.close_all()
    payload = {
        "status": status,
        "exception_type": exception_type,
        "traceback": error_text,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "bytes_sent": 0 if ctx is None else ctx.bytes_sent,
        "bytes_recv": 0 if ctx is None else ctx.bytes_recv,
        "bytes_note": (
            "Ctx counters can miss native sockets and other Python modules"
        ),
    }
    temporary = f"{result_path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    os.replace(temporary, result_path)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
