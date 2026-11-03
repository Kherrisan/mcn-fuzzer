"""User-facing ctx API.

``Channel`` is an opaque handle. Scripts can pass it back to ``send`` and
``recv``; they cannot read its socket or file descriptor. Bytes received past
a requested delimiter stay on that channel for the next ``recv``.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Optional

from .transport import SocketEndpoint, SocketTransport, Transport, TransportError


class InteractionError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class Channel:
    """Script-facing connection handle. The socket stays private."""

    def __init__(self, endpoint: SocketEndpoint, timeout_s: float) -> None:
        self._endpoint = endpoint
        self.proto = endpoint.proto
        self.peer = endpoint.peer
        self.timeout_s = timeout_s

    def close(self) -> None:
        self._endpoint.close()


class Session:
    """Default TCP connection retained for one in-process client.

    Forked case workers create a new session and drop it when the case ends.
    """

    def __init__(self) -> None:
        self.endpoint: Optional[SocketEndpoint] = None
        self.host: Optional[str] = None
        self.port: Optional[int] = None

    def close(self) -> None:
        if self.endpoint is not None:
            self.endpoint.close()
        self.endpoint = None
        self.host = None
        self.port = None


class Ctx:
    def __init__(
        self,
        session: Session,
        host: str,
        port: int,
        timeout_s: float,
        transport: Optional[Transport] = None,
        connect_timeout_s: Optional[float] = None,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.connect_timeout_s = timeout_s if connect_timeout_s is None else float(connect_timeout_s)
        self.bytes_sent = 0
        self.bytes_recv = 0
        self._transport: Transport = transport if transport is not None else SocketTransport()
        self._extra: list[Channel] = []
        self._listeners: list[Listener] = []

    def _timeout(self, timeout: Optional[float]) -> float:
        if timeout is None:
            return self.timeout_s
        if timeout < 0:
            raise InteractionError("generator", "timeout must be >= 0")
        return float(timeout)

    def connect(
        self,
        host: str,
        port: int,
        proto: str = "tcp",
        timeout: Optional[float] = None,
    ) -> Channel:
        timeout_s = self.connect_timeout_s if timeout is None else self._timeout(timeout)
        try:
            endpoint = self._transport.connect(host, int(port), proto, timeout_s)
        except TransportError as exc:
            raise InteractionError(exc.status, str(exc)) from exc
        channel = Channel(endpoint, timeout_s)
        self._extra.append(channel)
        return channel

    def accept(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        proto: str = "tcp",
        timeout: Optional[float] = None,
    ) -> "Listener":
        """Listen for inbound TCP connections.

        A background thread accepts until this ``generate()`` call ends.
        ``Listener.take()`` returns each connection as a channel.
        """
        timeout_s = self._timeout(timeout)
        if proto.lower() != "tcp":
            raise InteractionError("generator", "accept() only supports tcp")
        try:
            waiting = self._transport.listen(host, int(port), timeout_s)
        except TransportError as exc:
            raise InteractionError(exc.status, str(exc)) from exc
        listener = Listener(waiting, timeout_s)
        self._listeners.append(listener)
        return listener

    def close_extra(self) -> None:
        for listener in self._listeners:
            listener.close()
        self._listeners.clear()
        for channel in self._extra:
            channel.close()
        self._extra.clear()

    def close_all(self) -> None:
        """Close the default session and every extra channel."""
        self.close_extra()
        self.session.close()

    def _default(self) -> Channel:
        session = self.session
        if (
            session.endpoint is not None
            and session.host == self.host
            and session.port == self.port
        ):
            return Channel(session.endpoint, self.timeout_s)
        session.close()
        try:
            session.endpoint = self._transport.connect(
                self.host, self.port, "tcp", self.connect_timeout_s
            )
        except TransportError as exc:
            raise InteractionError(exc.status, str(exc)) from exc
        session.host = self.host
        session.port = self.port
        return Channel(session.endpoint, self.timeout_s)

    def send(self, data: bytes, channel: Optional[Channel] = None) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise InteractionError("generator", "send() data must be bytes")
        payload = bytes(data)
        target = channel if channel is not None else self._default()
        try:
            target._endpoint.send(payload)
        except OSError as exc:
            raise InteractionError("conn", f"send failed: {exc}") from exc
        self.bytes_sent += len(payload)

    def recv(
        self,
        channel: Optional[Channel] = None,
        until: Optional[bytes] = None,
        max_bytes: int = 4096,
    ) -> bytes:
        """Read from a channel.

        ``until`` stops at the first delimiter. Bytes after it stay buffered
        on the channel. ``max_bytes`` caps the returned slice. A closed peer
        raises ``eof`` when the delimiter is still missing, and returns any
        bytes already buffered when ``until`` is omitted. A deadline with no
        usable bytes raises ``timeout``.
        """
        if until is not None and not isinstance(until, (bytes, bytearray)):
            raise InteractionError("generator", "recv() until must be bytes")
        if max_bytes <= 0:
            raise InteractionError("generator", "recv() max_bytes must be positive")
        target = channel if channel is not None else self._default()
        try:
            data = _recv_buffered(target, None if until is None else bytes(until), max_bytes)
        except InteractionError:
            raise
        except socket.timeout as exc:
            raise InteractionError("timeout", "recv timed out") from exc
        except OSError as exc:
            raise InteractionError("conn", f"recv failed: {exc}") from exc
        self.bytes_recv += len(data)
        return data


def _recv_buffered(channel: Channel, until: Optional[bytes], max_bytes: int) -> bytes:
    buf = channel._endpoint.pending
    deadline = time.monotonic() + channel.timeout_s
    while True:
        if until is not None:
            found = buf.find(until)
            if found != -1:
                end = found + len(until)
                take = min(end, max_bytes)
                out = bytes(buf[:take])
                del buf[:take]
                return out
        if until is None and len(buf) >= max_bytes:
            out = bytes(buf[:max_bytes])
            del buf[:max_bytes]
            return out
        if until is not None and len(buf) >= max_bytes:
            out = bytes(buf[:max_bytes])
            del buf[:max_bytes]
            return out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if until is None and buf:
                out = bytes(buf[:max_bytes])
                del buf[: len(out)]
                return out
            raise InteractionError("timeout", "recv timed out")
        want = 4096 if until is not None else min(4096, max_bytes - len(buf))
        try:
            chunk = channel._endpoint.recv(max(want, 1), remaining)
        except socket.timeout as exc:
            if until is None and buf:
                out = bytes(buf[:max_bytes])
                del buf[: len(out)]
                return out
            raise InteractionError("timeout", "recv timed out") from exc
        if not chunk:
            if until is not None and until not in buf:
                raise InteractionError("eof", "peer closed before delimiter")
            if not buf:
                raise InteractionError("eof", "peer closed")
            out = bytes(buf[:max_bytes])
            del buf[: len(out)]
            return out
        buf.extend(chunk)


class Listener:
    """Background TCP accept loop. ``take`` returns the next connection."""

    def __init__(self, waiting, timeout_s: float) -> None:
        self._waiting = waiting
        self._timeout_s = timeout_s
        self.host = waiting.host
        self.port = waiting.port
        self._queue: queue.Queue[Channel] = queue.Queue()
        self._channels: list[Channel] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="generator-accept", daemon=True)
        self._thread.start()

    def take(self, timeout: Optional[float] = None) -> Channel:
        wait = self._timeout_s if timeout is None else float(timeout)
        if wait < 0:
            raise InteractionError("generator", "timeout must be >= 0")
        try:
            return self._queue.get(timeout=wait)
        except queue.Empty as exc:
            raise InteractionError("timeout", f"accept {self.host}:{self.port} timed out") from exc

    def close(self) -> None:
        self._stop.set()
        self._waiting.close()
        self._thread.join(timeout=1.0)
        for channel in self._channels:
            channel.close()
        self._channels.clear()

    def _run(self) -> None:
        import select

        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([self._waiting.fileno()], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                endpoint = self._waiting.accept()
            except OSError:
                break
            channel = Channel(endpoint, self._timeout_s)
            self._channels.append(channel)
            self._queue.put(channel)
