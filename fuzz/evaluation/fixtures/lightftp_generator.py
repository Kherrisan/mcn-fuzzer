"""Hand-written LightFTP step handlers for the evaluation cases."""

import re
from collections.abc import Mapping

from generator_server import InteractionError, step_handler


def _scope(ctx, params):
    scope = params.get(ctx.step_name)
    return scope if isinstance(scope, Mapping) else {}


def _read(ctx, expected=None):
    reply = ctx.recv(until=b"\n", max_bytes=4096)
    if expected is not None and not reply.startswith(expected):
        raise InteractionError("generator", f"expected {expected!r}, got {reply!r}")
    return reply


def _send(ctx, text):
    ctx.send((text + "\r\n").encode("ascii"))


@step_handler("service greeting")
def greeting(ctx, params):
    _read(ctx, b"220")


@step_handler("send USER")
def user(ctx, params):
    _send(ctx, "USER " + _scope(ctx, params).get("user", "ubuntu"))


@step_handler("USER intermediate")
def user_reply(ctx, params):
    _read(ctx, b"331")


@step_handler("send PASS")
def password(ctx, params):
    _send(ctx, "PASS " + _scope(ctx, params).get("password", "ubuntu"))


@step_handler("login accepted")
def login(ctx, params):
    _read(ctx, b"230")


@step_handler("send PWD")
def pwd(ctx, params):
    _send(ctx, "PWD")


@step_handler("pathname reply")
def pathname(ctx, params):
    _read(ctx, b"257")


@step_handler("send NOOP")
def noop(ctx, params):
    _send(ctx, "NOOP")


@step_handler("send QUIT")
def quit(ctx, params):
    _send(ctx, "QUIT")


@step_handler("ordinary command reply")
def command_reply(ctx, params):
    _read(ctx, b"221" if ctx.step_instance.get("name", "").lower().startswith("quit") else None)


@step_handler("send TYPE")
def send_type(ctx, params):
    _send(ctx, "TYPE I")


@step_handler("TYPE accepted")
def type_reply(ctx, params):
    _read(ctx, b"200")


@step_handler("send PASV")
def pasv(ctx, params):
    _send(ctx, "PASV")


@step_handler("PASV endpoint")
def pasv_reply(ctx, params):
    reply = _read(ctx)
    match = re.search(br"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", reply)
    if match is None:
        raise InteractionError("generator", f"PASV reply was {reply!r}")
    numbers = [int(part) for part in match.groups()]
    ctx._eval_endpoint = (".".join(str(part) for part in numbers[:4]), numbers[4] * 256 + numbers[5])


@step_handler("send LIST")
def list_command(ctx, params):
    _send(ctx, "LIST")


@step_handler("send RETR")
def retr(ctx, params):
    _send(ctx, "RETR " + _scope(ctx, params).get("download", "hello.txt"))


@step_handler("transfer preliminary")
def preliminary(ctx, params):
    _read(ctx, b"150")


@step_handler("LIST data via passive")
def passive_data(ctx, params):
    channel = ctx.connect(*ctx._eval_endpoint)
    ctx.recv(channel=channel, max_bytes=65536)
    channel.close()


@step_handler("retrieve stream bytes via passive")
def retrieve_data(ctx, params):
    passive_data(ctx, params)


@step_handler("transfer completion")
def completion(ctx, params):
    _read(ctx, b"226")


@step_handler("send PORT")
def port(ctx, params):
    ctx._eval_listener = ctx.accept("127.0.0.1", 0)
    number = ctx._eval_listener.port
    _send(ctx, "PORT " + ",".join(str(part) for part in [127, 0, 0, 1, number // 256, number % 256]))


@step_handler("PORT accepted")
def port_reply(ctx, params):
    _read(ctx, b"200")


@step_handler("send STOR")
def stor(ctx, params):
    _send(ctx, "STOR " + _scope(ctx, params)["upload_name"])


@step_handler("store stream bytes via active")
def active_data(ctx, params):
    channel = ctx._eval_listener.take()
    ctx.send(b"evaluation-upload\n", channel=channel)
    channel.close()
    ctx._eval_listener.close()
