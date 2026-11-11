"""Resident generator server.

Scripts call ``generate(ctx, step_trace)``. Socket work goes through a
``Transport`` so a later Nyx-Net backend can replace it.
"""

from .context import Channel, Ctx, InteractionError, Listener, Session
from .registry import Registry
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
]
