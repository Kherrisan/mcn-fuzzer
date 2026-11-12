"""FTP command generator.

Rust mutates ``verb`` and ``arg``. A 227 PASV reply opens a second TCP channel.
"""

import re

from generator_server import Ctx, step_handler


def _line(verb: str, arg: str) -> bytes:
    verb_b = verb.encode() if isinstance(verb, str) else bytes(verb)
    arg_b = arg.encode() if isinstance(arg, str) else bytes(arg)
    if arg_b:
        return verb_b + b" " + arg_b + b"\r\n"
    return verb_b + b"\r\n"


def parse_pasv(reply: bytes) -> tuple[str, int]:
    match = re.search(br"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)", reply)
    if match is None:
        raise ValueError(f"PASV reply has no address: {reply!r}")
    numbers = [int(part) for part in match.groups()]
    host = ".".join(str(part) for part in numbers[:4])
    port = numbers[4] * 256 + numbers[5]
    return host, port


@step_handler("ftp command")
def handle_command(ctx: Ctx, params: dict) -> None:
    verb = params.get("verb", "NOOP")
    arg = params.get("arg", "")
    ctx.send(_line(str(verb), str(arg)))
    reply = ctx.recv(until=b"\r\n")
    if reply.startswith(b"227"):
        host, port = parse_pasv(reply)
        data = ctx.connect(host, port)
        ctx.recv(channel=data, until=b"\r\n")
