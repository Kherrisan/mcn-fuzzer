"""FTP command generator for the LibAFLstar FTP cases.

Rust mutates `verb` and `arg`. This script builds one FTP command with scapy
and reads the reply. A 227 PASV reply opens a second TCP channel.
"""

import re

from scapy.fields import StrField
from scapy.packet import Packet

from generator_server.context import Ctx


class FtpCommand(Packet):
    name = "FtpCommand"
    fields_desc = [StrField("verb", "NOOP"), StrField("arg", "")]

    def post_build(self, pkt, pay):
        verb = self.verb.encode() if isinstance(self.verb, str) else bytes(self.verb)
        arg = self.arg.encode() if isinstance(self.arg, str) else bytes(self.arg)
        line = verb + (b" " + arg if arg else b"") + b"\r\n"
        return line + pay


def parse_pasv(reply: bytes) -> tuple[str, int]:
    match = re.search(br"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)", reply)
    if match is None:
        raise ValueError(f"PASV reply has no address: {reply!r}")
    numbers = [int(part) for part in match.groups()]
    host = ".".join(str(part) for part in numbers[:4])
    port = numbers[4] * 256 + numbers[5]
    return host, port


def generate(ctx: Ctx, params: dict) -> None:
    verb = params.get("verb", "NOOP")
    arg = params.get("arg", "")
    ctx.send(bytes(FtpCommand(verb=str(verb), arg=str(arg))))
    reply = ctx.recv(until=b"\r\n")
    if reply.startswith(b"227"):
        host, port = parse_pasv(reply)
        data = ctx.connect(host, port)
        ctx.recv(channel=data, until=b"\r\n")
