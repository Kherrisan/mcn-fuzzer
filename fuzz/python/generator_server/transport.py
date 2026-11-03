"""Native socket transport.

``Transport`` is the seam a future Nyx-Net backend can replace. Script code
talks to ``Channel`` and never sees this module's sockets.
"""

from __future__ import annotations

import socket
import time
from typing import Optional, Protocol


class TransportError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class SocketEndpoint:
    """One connected socket plus the bytes already pulled past a delimiter."""

    def __init__(self, sock: socket.socket, proto: str, peer: tuple[str, int]) -> None:
        self._sock = sock
        self.proto = proto
        self.peer = peer
        self.pending = bytearray()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def send(self, payload: bytes) -> None:
        if self.proto == "udp":
            self._sock.sendto(payload, self.peer)
        else:
            self._sock.sendall(payload)

    def recv(self, nbytes: int, timeout_s: float) -> bytes:
        self._sock.settimeout(timeout_s)
        if self.proto == "udp":
            while True:
                chunk, addr = self._sock.recvfrom(nbytes)
                if addr[0] != self.peer[0] or int(addr[1]) != int(self.peer[1]):
                    continue
                return chunk
        return self._sock.recv(nbytes)


class AcceptQueue:
    def __init__(self, sock: socket.socket, timeout_s: float) -> None:
        self._sock = sock
        self.timeout_s = timeout_s
        self.host, self.port = sock.getsockname()[:2]

    def fileno(self) -> int:
        return self._sock.fileno()

    def accept(self) -> SocketEndpoint:
        conn, addr = self._sock.accept()
        conn.settimeout(self.timeout_s)
        return SocketEndpoint(conn, "tcp", (addr[0], int(addr[1])))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Transport(Protocol):
    def connect(
        self, host: str, port: int, proto: str, timeout_s: float
    ) -> SocketEndpoint: ...

    def listen(self, host: str, port: int, timeout_s: float) -> AcceptQueue: ...


def _tcp_connect(host: str, port: int, timeout_s: float) -> socket.socket:
    last: Optional[OSError] = None
    deadline = time.monotonic() + max(timeout_s, 0.2)
    while True:
        try:
            return socket.create_connection((host, port), timeout=timeout_s)
        except OSError as exc:
            last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise TransportError("conn", f"tcp connect {host}:{port} failed: {last}")


class SocketTransport:
    """Process-local TCP and UDP sockets."""

    def connect(self, host: str, port: int, proto: str, timeout_s: float) -> SocketEndpoint:
        proto = proto.lower()
        if proto == "tcp":
            sock = _tcp_connect(host, int(port), timeout_s)
            return SocketEndpoint(sock, "tcp", (host, int(port)))
        if proto == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout_s)
            return SocketEndpoint(sock, "udp", (host, int(port)))
        raise TransportError("generator", f"unknown proto {proto!r}")

    def listen(self, host: str, port: int, timeout_s: float) -> AcceptQueue:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
            sock.listen(8)
        except OSError as exc:
            sock.close()
            raise TransportError("conn", f"accept bind {host}:{port} failed: {exc}") from exc
        return AcceptQueue(sock, timeout_s)
