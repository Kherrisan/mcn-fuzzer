"""Minimal generator for the ASAN smoke campaign.

The seed uses the params-only API. Rust may mutate the `payload` string; any
value containing `CRASH` reaches the deliberate out-of-bounds read in server.c.
"""

from generator_server import InteractionError, step_handler


@step_handler("send_payload")
def send_payload(ctx, params):
    payload = params.get("payload", "hello")
    if isinstance(payload, bytes):
        wire = payload
    else:
        wire = str(payload).encode("utf-8", "replace")
    if not wire.endswith(b"\n"):
        wire += b"\n"
    ctx.send(wire)
    try:
        ctx.recv(until=b"\n", max_bytes=256)
    except InteractionError:
        # The ASAN child intentionally aborts before writing a normal reply.
        pass
