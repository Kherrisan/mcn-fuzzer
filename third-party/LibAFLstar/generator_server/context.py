"""Socket helpers invoked by a generator script.

The default channel is the TCP connection to the target host and port from the
request. It stays open across runs for the same client until reset. connect()
opens an extra channel for this generate() call only. UDP connect records the
remote endpoint and does not perform a handshake. accept() listens for inbound
TCP connections on a background thread.
"""

import queue
import socket
import threading
import time
from typing import Optional


class InteractionError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class Channel:
    def __init__(
        self,
        sock: socket.socket,
        proto: str,
        peer: tuple[str, int],
        timeout_s: float,
    ) -> None:
        self.sock = sock
        self.proto = proto
        self.peer = peer
        self.timeout_s = timeout_s

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Session:
    """Default TCP connection retained for one fuzzer process."""

    def __init__(self) -> None:
        self.sock: Optional[socket.socket] = None
        self.host: Optional[str] = None
        self.port: Optional[int] = None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.host = None
        self.port = None


def _tcp_connect(host: str, port: int, timeout: float) -> socket.socket:
    last: Optional[OSError] = None
    deadline = time.monotonic() + max(timeout, 0.2)
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            return sock
        except OSError as exc:
            last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise InteractionError("conn", f"tcp connect {host}:{port} failed: {last}")


class Ctx:
    def __init__(self, session: Session, host: str, port: int, timeout_s: float) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.bytes_sent = 0
        self.bytes_recv = 0
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
        timeout_s = self._timeout(timeout)
        proto = proto.lower()
        if proto == "tcp":
            sock = _tcp_connect(host, int(port), timeout_s)
            channel = Channel(sock, "tcp", (host, int(port)), timeout_s)
        elif proto == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout_s)
            peer = (host, int(port))
            channel = Channel(sock, "udp", peer, timeout_s)
        else:
            raise InteractionError("generator", f"unknown proto {proto!r}")
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

        A background thread accepts connections until this generate() call
        ends. ``Listener.take()`` returns each new connection as a channel
        that ``send`` and ``recv`` can use. ``timeout`` is how long ``take``
        waits when it is not given its own timeout. The bound port is
        ``listener.port``; pass ``port=0`` to let the OS choose one.
        """
        timeout_s = self._timeout(timeout)
        proto = proto.lower()
        if proto != "tcp":
            raise InteractionError("generator", "accept() only supports tcp")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
            sock.listen(8)
        except OSError as exc:
            sock.close()
            raise InteractionError("conn", f"accept bind {host}:{port} failed: {exc}") from exc
        listener = Listener(sock, timeout_s)
        self._listeners.append(listener)
        return listener

    def close_extra(self) -> None:
        for listener in self._listeners:
            listener.close()
        self._listeners.clear()
        for channel in self._extra:
            channel.close()
        self._extra.clear()

    def _default(self) -> Channel:
        session = self.session
        if (
            session.sock is not None
            and session.host == self.host
            and session.port == self.port
        ):
            return Channel(session.sock, "tcp", (self.host, self.port), self.timeout_s)
        session.close()
        session.sock = _tcp_connect(self.host, self.port, self.timeout_s)
        session.host = self.host
        session.port = self.port
        return Channel(session.sock, "tcp", (self.host, self.port), self.timeout_s)

    def send(self, data: bytes, channel: Optional[Channel] = None) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise InteractionError("generator", "send() data must be bytes")
        payload = bytes(data)
        target = channel if channel is not None else self._default()
        try:
            if target.proto == "udp":
                target.sock.sendto(payload, target.peer)
            else:
                target.sock.sendall(payload)
        except OSError as exc:
            raise InteractionError("conn", f"send failed: {exc}") from exc
        self.bytes_sent += len(payload)

    def recv(
        self,
        channel: Optional[Channel] = None,
        until: Optional[bytes] = None,
        max_bytes: int = 4096,
    ) -> bytes:
        if until is not None and not isinstance(until, (bytes, bytearray)):
            raise InteractionError("generator", "recv() until must be bytes")
        if max_bytes <= 0:
            raise InteractionError("generator", "recv() max_bytes must be positive")
        target = channel if channel is not None else self._default()
        try:
            if target.proto == "udp":
                data = self._recv_udp(target, until, max_bytes, target.timeout_s)
            else:
                data = self._recv_tcp(target.sock, until, max_bytes, target.timeout_s)
        except InteractionError:
            raise
        except socket.timeout as exc:
            raise InteractionError("timeout", "recv timed out") from exc
        except OSError as exc:
            raise InteractionError("conn", f"recv failed: {exc}") from exc
        self.bytes_recv += len(data)
        return data

    def _recv_tcp(
        self,
        sock: socket.socket,
        until: Optional[bytes],
        max_bytes: int,
        timeout_s: float,
    ) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + timeout_s
        while len(buf) < max_bytes:
            if until is not None and until in buf:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if until is None and buf:
                    break
                raise InteractionError("timeout", "recv timed out")
            sock.settimeout(remaining)
            chunk = sock.recv(min(4096, max_bytes - len(buf)))
            if not chunk:
                if buf and (until is None or until in buf):
                    break
                raise InteractionError("conn", "peer closed")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_udp(
        self,
        channel: Channel,
        until: Optional[bytes],
        max_bytes: int,
        timeout_s: float,
    ) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + timeout_s
        while len(buf) < max_bytes:
            if until is not None and until in buf:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if until is None and buf:
                    break
                raise InteractionError("timeout", "recv timed out")
            channel.sock.settimeout(remaining)
            chunk, addr = channel.sock.recvfrom(min(65535, max_bytes - len(buf)))
            if addr[0] != channel.peer[0] or addr[1] != channel.peer[1]:
                continue
            if not chunk:
                break
            buf.extend(chunk)
            if until is None:
                break
        return bytes(buf)


class Listener:
    """Background TCP accept loop. ``take`` returns the next connection."""

    def __init__(self, sock: socket.socket, timeout_s: float) -> None:
        self._sock = sock
        self._timeout_s = timeout_s
        self.host, self.port = sock.getsockname()[:2]
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
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)
        for channel in self._channels:
            channel.close()
        self._channels.clear()

    def _run(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(self._timeout_s)
            channel = Channel(conn, "tcp", (addr[0], int(addr[1])), self._timeout_s)
            self._channels.append(channel)
            self._queue.put(channel)
