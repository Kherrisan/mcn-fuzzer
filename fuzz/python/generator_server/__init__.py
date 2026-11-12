"""Resident generator server.

Scripts register step handlers, which the runtime dispatches for each trace.
Socket work goes through a ``Transport`` so a later Nyx-Net backend can replace it.
"""

from .context import Channel, Ctx, InteractionError, Listener, Session
from .registry import Registry
from .handlers import step_handler, global_init
from .transport import SocketTransport, Transport

__all__ = [
    "Channel",
    "Ctx",
    "InteractionError",
    "Listener",
    "Registry",
    "Session",
    "SocketTransport",
    "Transport",
    "step_handler",
    "global_init",
]
