"""Hand-written LightFTP environment for the five evaluation cases."""

from __future__ import annotations

import re

from generator_server.context import Ctx, InteractionError


def generate(ctx: Ctx, params: dict) -> None:
    scenario = params["scenario"]
    try:
        reply = _read(ctx)
        if not reply.startswith(b"220"):
            raise InteractionError("generator", f"greeting was {reply!r}")
        _expect(ctx, f"USER {params.get('user', 'ubuntu')}\r\n", b"331")
        _expect(ctx, f"PASS {params.get('password', 'ubuntu')}\r\n", b"230")
        if scenario == "control-commands":
            _expect(ctx, "PWD\r\n", b"257")
            _expect(ctx, "NOOP\r\n", b"200")
        elif scenario == "passive-list":
            host, port = _pasv(ctx)
            _expect(ctx, "LIST\r\n", b"150")
            data = ctx.connect(host, port)
            ctx.recv(channel=data, max_bytes=65536)
            data.close()
            _read_code(ctx, b"226")
        elif scenario == "passive-retr":
            _expect(ctx, "TYPE I\r\n", b"200")
            host, port = _pasv(ctx)
            name = params.get("download", "hello.txt")
            _expect(ctx, f"RETR {name}\r\n", b"150")
            data = ctx.connect(host, port)
            ctx.recv(channel=data, max_bytes=65536)
            data.close()
            _read_code(ctx, b"226")
        elif scenario == "active-stor":
            _expect(ctx, "TYPE I\r\n", b"200")
            listener = ctx.accept("127.0.0.1", 0)
            port = listener.port
            octets = [127, 0, 0, 1, port // 256, port % 256]
            _expect(ctx, "PORT " + ",".join(str(part) for part in octets) + "\r\n", b"200")
            name = params["upload_name"]
            _expect(ctx, f"STOR {name}\r\n", b"150")
            data = listener.take()
            ctx.send(b"evaluation-upload\n", channel=data)
            data.close()
            listener.close()
            _read_code(ctx, b"226")
        elif scenario != "login-quit":
            raise InteractionError("generator", f"unknown scenario {scenario}")
        _expect(ctx, "QUIT\r\n", b"221")
    finally:
        ctx.close_all()


def _pasv(ctx: Ctx) -> tuple[str, int]:
    ctx.send(b"PASV\r\n")
    reply = _read(ctx)
    match = re.search(br"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", reply)
    if match is None:
        raise InteractionError("generator", f"PASV reply was {reply!r}")
    numbers = [int(part) for part in match.groups()]
    host = ".".join(str(part) for part in numbers[:4])
    return host, numbers[4] * 256 + numbers[5]


def _expect(ctx: Ctx, command: str, code: bytes) -> None:
    ctx.send(command.encode("ascii"))
    _read_code(ctx, code)


def _read_code(ctx: Ctx, code: bytes) -> None:
    reply = _read(ctx)
    if not reply.startswith(code):
        raise InteractionError("generator", f"expected {code!r}, got {reply!r}")


def _read(ctx: Ctx) -> bytes:
    return ctx.recv(until=b"\n", max_bytes=4096)
