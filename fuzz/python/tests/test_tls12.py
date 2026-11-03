"""TLS 1.2 ClientHello builder and record reader."""

import importlib.util
import socket
import threading
import unittest
from pathlib import Path

from generator_server.context import Ctx, InteractionError, Session

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "targets" / "openssl" / "generators" / "tls12.py"


def _load():
    spec = importlib.util.spec_from_file_location("openssl_tls12", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TLS = _load()


def _listen(handler):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept():
        conn, _ = server.accept()
        try:
            handler(conn)
        finally:
            conn.close()
            server.close()

    threading.Thread(target=accept, daemon=True).start()
    return port


def _ctx(port, timeout=0.5):
    return Ctx(Session(), "127.0.0.1", port, timeout, connect_timeout_s=timeout)


def _read_client(conn):
    data = b""
    conn.settimeout(1.0)
    while len(data) < 5:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


class ClientHelloTests(unittest.TestCase):
    def test_default_hello_recomputes_lengths(self):
        record = TLS.build_client_hello({})
        self.assertGreater(len(record), 5)
        self.assertEqual(record[0], 22)
        self.assertEqual(record[1:3], b"\x03\x03")
        body_len = int.from_bytes(record[3:5], "big")
        self.assertEqual(body_len, len(record) - 5)
        handshake = record[5:]
        self.assertEqual(handshake[0], 1)
        hello_len = int.from_bytes(handshake[1:4], "big")
        self.assertEqual(hello_len, len(handshake) - 4)
        self.assertEqual(handshake[4:6], b"\x03\x03")
        self.assertIn(b"localhost", record)

    def test_bounds_and_truncate(self):
        full = TLS.build_client_hello({"truncate": 0, "version": -5, "cipher": 999999})
        self.assertEqual(full[5 + 4: 5 + 6], b"\x00\x00")
        cipher_at = full.find(b"\x00\x02")
        self.assertGreater(cipher_at, 0)
        cut = TLS.build_client_hello({"truncate": 8})
        self.assertEqual(len(cut), 8)
        self.assertTrue(cut)
        long_sni = TLS.build_client_hello({"sni": "a" * 400})
        self.assertEqual(long_sni.count(b"a"), 253)
        custom = TLS.build_client_hello({"extensions_hex": "zz0001"})
        self.assertNotIn(b"localhost", custom)

    def test_server_hello_alert_and_eof(self):
        def hello(conn):
            _read_client(conn)
            body = b"\x02\x00\x00\x03\x03\x03\x03"
            conn.sendall(b"\x16\x03\x03" + len(body).to_bytes(2, "big") + body)

        ctx = _ctx(_listen(hello))
        TLS.generate(ctx, {"truncate": 0})
        self.assertGreater(ctx.bytes_sent, 0)

        def alert(conn):
            _read_client(conn)
            conn.sendall(b"\x15\x03\x03\x00\x02\x02\x28")

        ctx = _ctx(_listen(alert))
        with self.assertRaises(InteractionError) as raised:
            TLS.generate(ctx, {"version": 769, "cipher": 5})
        self.assertEqual(raised.exception.status, "alert")

        def closed(conn):
            _read_client(conn)

        ctx = _ctx(_listen(closed))
        with self.assertRaises(InteractionError) as raised:
            TLS.generate(ctx, {"truncate": 8})
        self.assertEqual(raised.exception.status, "eof")

    def test_declared_length_is_bounded(self):
        def huge(conn):
            _read_client(conn)
            conn.sendall(b"\x16\x03\x03\xff\xff")

        ctx = _ctx(_listen(huge))
        with self.assertRaises(InteractionError) as raised:
            TLS.generate(ctx, {})
        self.assertEqual(raised.exception.status, "alert")
        self.assertIn(str(TLS.MAX_RECORD_BODY), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
