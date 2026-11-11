"""Per-case worker.

The server forks one worker per run. The child calls ``generate`` and exits
with a stable code. The parent learns the outcome from ``waitpid`` only.
Detailed diagnostics go to stderr, which the fuzzer points at ``generator.log``.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Optional

import cbor2

from .context import InteractionError
from .server import handle_payload, registry

EXIT_OK = 0
EXIT_TIMEOUT = 75
EXIT_CONN = 76
EXIT_EOF = 77
EXIT_ALERT = 78
EXIT_GENERATOR = 79
EXIT_DEPENDENCY = 80

_STATUS_CODE = {
    "timeout": EXIT_TIMEOUT,
    "conn": EXIT_CONN,
    "eof": EXIT_EOF,
    "alert": EXIT_ALERT,
    "dependency": EXIT_DEPENDENCY,
}


def execute_case(payload: bytes) -> bytes:
    """Run one request in this process and close every socket it opened."""
    from .server import sessions

    table = sessions()
    table.clear()
    try:
        return handle_payload(payload, table)
    finally:
        for session in list(table.values()):
            session.close()
        table.clear()


def status_from_exit(code: int) -> str:
    """Logical status for a worker exit code."""
    return {
        EXIT_OK: "ok",
        EXIT_TIMEOUT: "timeout",
        EXIT_CONN: "conn",
        EXIT_EOF: "eof",
        EXIT_ALERT: "alert",
        EXIT_DEPENDENCY: "dependency",
    }.get(code, "generator")


def fork_worker(payload: bytes) -> int:
    """Fork a worker and return its pid. The parent does not wait."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            request = cbor2.loads(payload)
        except Exception:
            request = None
        if isinstance(request, dict) and request.get("op") == "run":
            path = request.get("generator")
            if isinstance(path, str):
                registry().get(path)
    pid = os.fork()
    if pid == 0:
        _close_inherited_fds()
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
            _child_main(payload)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            os._exit(EXIT_GENERATOR)
    return pid


def run_isolated(payload: bytes) -> bytes:
    """Fork a worker and return a CBOR body reconstructed from its exit code.

    The production server does not use this helper. It waitpids and sends Result.
    stderr is captured here so tests can see the diagnostic line.
    """
    read_err, write_err = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_err)
        os.dup2(write_err, 2)
        os.close(write_err)
        _close_inherited_fds(keep={2})
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
            _child_main(payload)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            os._exit(EXIT_GENERATOR)
    os.close(write_err)
    _, status = os.waitpid(pid, 0)
    diagnostic = os.read(read_err, 65536).decode(errors="replace")
    os.close(read_err)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        body = {
            "id": 0,
            "status": status_from_exit(code),
            "code": code,
            "sent": 0,
            "recv": 0,
        }
        text = diagnostic.strip()
        if text:
            body["error"] = text
        return cbor2.dumps(body)
    signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
    return cbor2.dumps(
        {
            "id": 0,
            "status": "signaled",
            "signal": signal,
            "error": diagnostic.strip(),
            "sent": 0,
            "recv": 0,
        }
    )


def _child_main(payload: bytes) -> None:
    try:
        response = execute_case(payload)
    except InteractionError as exc:
        _exit_with(exc.status, str(exc))
    except Exception as exc:
        _exit_with("generator", f"worker: {exc}")
    try:
        decoded = cbor2.loads(response)
    except Exception as exc:
        _exit_with("generator", f"worker response: {exc}")
    status = decoded.get("status") if isinstance(decoded, dict) else None
    if status == "ok":
        os._exit(EXIT_OK)
    error = ""
    if isinstance(decoded, dict):
        error = str(decoded.get("error") or status or "generator")
    _exit_with(str(status or "generator"), error)


def _exit_with(status: str, message: str) -> None:
    print(f"generator {status}: {message}", file=sys.stderr, flush=True)
    os._exit(_STATUS_CODE.get(status, EXIT_GENERATOR))


def _close_inherited_fds(keep: Optional[set[int]] = None) -> None:
    retained = {0, 1, 2}
    if keep:
        retained.update(keep)
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return
    for name in names:
        try:
            number = int(name)
        except ValueError:
            continue
        if number not in retained:
            try:
                os.close(number)
            except OSError:
                pass
