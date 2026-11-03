import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import cbor2

from generator_server.context import Ctx, InteractionError, Session
from generator_server.registry import Registry
from generator_server.server import handle_payload

ROOT = Path(__file__).resolve().parents[2]
FTP_SCRIPT = ROOT / "generators" / "ftp_cmd.py"


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


class RegistryTests(unittest.TestCase):
    def test_import_is_cached(self):
        directory = Path(self.id().replace(".", "_"))
        path = Path("/tmp") / "libaflstar_gen_once.py"
        path.write_text(
            "COUNT = 0\n"
            "COUNT += 1\n"
            "def generate(ctx, params):\n"
            "    return COUNT\n"
        )
        registry = Registry()
        first = registry.get(str(path))
        second = registry.get(str(path))
        self.assertIs(first, second)
        self.assertEqual(registry.loads, 1)
        self.assertEqual(first(None, {}), 1)

    def test_relative_path_rejected(self):
        registry = Registry()
        with self.assertRaises(ValueError):
            registry.get("ftp_cmd.py")


class FtpGeneratorTests(unittest.TestCase):
    def test_user_command_roundtrip(self):
        seen = {}

        def handler(conn):
            data = b""
            while b"\r\n" not in data:
                chunk = conn.recv(64)
                if not chunk:
                    break
                data += chunk
            seen["data"] = data
            conn.sendall(b"331 need password\r\n")

        port = _listen(handler)
        request = {
            "op": "run",
            "client": 11,
            "id": 3,
            "generator": str(FTP_SCRIPT),
            "params": {"verb": "USER", "arg": "ubuntu"},
            "host": "127.0.0.1",
            "port": port,
            "timeout_ms": 1000,
        }
        raw = handle_payload(cbor2.dumps(request), {})
        response = cbor2.loads(raw)
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(seen["data"], b"USER ubuntu\r\n")
        self.assertGreater(response["recv"], 0)

    def test_pasv_opens_second_channel(self):
        data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        data_sock.bind(("127.0.0.1", 0))
        data_sock.listen(1)
        data_port = data_sock.getsockname()[1]
        p1, p2 = divmod(data_port, 256)
        reply = f"227 Entering Passive Mode (127,0,0,1,{p1},{p2})\r\n".encode()

        def data_handler():
            conn, _ = data_sock.accept()
            try:
                conn.sendall(b"listing\r\n")
            finally:
                conn.close()
                data_sock.close()

        threading.Thread(target=data_handler, daemon=True).start()

        def control(conn):
            data = b""
            while b"\r\n" not in data:
                data += conn.recv(64)
            conn.sendall(reply)

        port = _listen(control)
        request = {
            "op": "run",
            "client": 12,
            "id": 4,
            "generator": str(FTP_SCRIPT),
            "params": {"verb": "PASV", "arg": ""},
            "host": "127.0.0.1",
            "port": port,
            "timeout_ms": 1000,
        }
        response = cbor2.loads(handle_payload(cbor2.dumps(request), {}))
        self.assertEqual(response["status"], "ok", response)
        self.assertGreaterEqual(response["recv"], len(reply) + len(b"listing\r\n"))


class AcceptTests(unittest.TestCase):
    def test_accept_thread_receives_two_connections(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        listener = ctx.accept("127.0.0.1", 0)
        self.assertGreater(listener.port, 0)

        def dial(payload: bytes) -> bytes:
            sock = socket.create_connection(("127.0.0.1", listener.port), timeout=1)
            sock.sendall(payload)
            data = sock.recv(64)
            sock.close()
            return data

        first = threading.Thread(target=lambda: seen.update(one=dial(b"one\r\n")))
        seen = {}
        first.start()
        channel = listener.take()
        ctx.send(b"ack-one\r\n", channel=channel)
        got = ctx.recv(channel=channel, until=b"\r\n")
        first.join(timeout=2)
        self.assertEqual(got, b"one\r\n")
        self.assertEqual(seen["one"], b"ack-one\r\n")

        second = threading.Thread(target=lambda: seen.update(two=dial(b"two\r\n")))
        second.start()
        other = listener.take()
        self.assertIsNot(other, channel)
        ctx.send(b"ack-two\r\n", channel=other)
        got = ctx.recv(channel=other, until=b"\r\n")
        second.join(timeout=2)
        self.assertEqual(got, b"two\r\n")
        self.assertEqual(seen["two"], b"ack-two\r\n")
        ctx.close_extra()

    def test_accept_timeout_uses_argument(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 5.0)
        listener = ctx.accept("127.0.0.1", 0, timeout=0.2)
        started = time.monotonic()
        with self.assertRaises(InteractionError) as caught:
            listener.take()
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.status, "timeout")
        self.assertLess(elapsed, 1.5)
        ctx.close_extra()

    def test_connect_timeout_overrides_default(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 5.0)
        started = time.monotonic()
        with self.assertRaises(InteractionError) as caught:
            ctx.connect("127.0.0.1", 1, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.status, "conn")
        self.assertLess(elapsed, 1.5)
        ctx.close_extra()


class UdpTests(unittest.TestCase):
    def test_udp_connect_sends_without_handshake(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        channel = ctx.connect("127.0.0.1", port, proto="udp")
        self.assertEqual(channel.proto, "udp")
        ctx.send(b"ping", channel=channel)
        sock.settimeout(1)
        data, _addr = sock.recvfrom(16)
        self.assertEqual(data, b"ping")
        ctx.close_extra()
        sock.close()


class ResetTests(unittest.TestCase):
    def test_reset_closes_default_connection(self):
        held = {}

        def handler(conn):
            held["conn"] = conn
            conn.sendall(b"220 ready\r\n")
            try:
                while conn.recv(16):
                    pass
            except OSError:
                pass

        port = _listen(handler)
        sessions = {}
        run = {
            "op": "run",
            "client": 9,
            "id": 1,
            "generator": str(FTP_SCRIPT),
            "params": {"verb": "NOOP", "arg": ""},
            "host": "127.0.0.1",
            "port": port,
            "timeout_ms": 1000,
        }
        first = cbor2.loads(handle_payload(cbor2.dumps(run), sessions))
        self.assertEqual(first["status"], "ok", first)
        self.assertIsNotNone(sessions[9].sock)
        reset = {"op": "reset", "client": 9, "id": 2}
        second = cbor2.loads(handle_payload(cbor2.dumps(reset), sessions))
        self.assertEqual(second["status"], "ok")
        self.assertNotIn(9, sessions)


def _iceoryx2():
    try:
        import iceoryx2 as iox2
    except ImportError:
        return None
    return iox2


def _iox_request(client, node, iox2, body: bytes, timeout_s: float = 5):
    sample = client.loan_slice_uninit(len(body))
    view = sample.payload()
    for index, byte in enumerate(body):
        view[index] = byte
    pending = sample.assume_init().send()
    deadline = time.time() + timeout_s
    response = None
    while time.time() < deadline and response is None:
        response = pending.receive()
        if response is None:
            node.wait(iox2.Duration.from_millis(1))
    if response is None:
        raise TimeoutError("generator server sent no response")
    payload = response.payload()
    raw = bytes(int(payload[index]) for index in range(payload.len()))
    return cbor2.loads(raw)


class ServerProcessTests(unittest.TestCase):
    def test_server_process_ftp_roundtrip(self):
        iox2 = _iceoryx2()
        if iox2 is None:
            self.skipTest("iceoryx2 is not installed")

        import ctypes

        seen = {}

        def handler(conn):
            data = b""
            while b"\r\n" not in data:
                chunk = conn.recv(64)
                if not chunk:
                    break
                data += chunk
            seen["data"] = data
            conn.sendall(b"331 need password\r\n")

        port = _listen(handler)
        service_name = f"libaflstar/generator-test-{os.getpid()}"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        server = subprocess.Popen(
            [sys.executable, "-m", "generator_server", "--service", service_name],
            env=env,
            cwd=str(ROOT),
        )
        try:
            node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
            opened = None
            deadline = time.time() + 10
            while time.time() < deadline:
                if server.poll() is not None:
                    self.fail(f"generator server exited early with {server.returncode}")
                try:
                    opened = (
                        node.service_builder(iox2.ServiceName.new(service_name))
                        .request_response(
                            iox2.Slice[ctypes.c_uint8], iox2.Slice[ctypes.c_uint8]
                        )
                        .open()
                    )
                    break
                except Exception:
                    time.sleep(0.05)
            self.assertIsNotNone(opened, "server did not open the iceoryx2 service")
            client = (
                opened.client_builder()
                .initial_max_slice_len(65536)
                .allocation_strategy(iox2.AllocationStrategy.PowerOfTwo)
                .create()
            )

            ping = _iox_request(
                client,
                node,
                iox2,
                cbor2.dumps({"op": "ping", "client": 1, "id": 1}),
            )
            self.assertEqual(ping["status"], "ok", ping)

            run = _iox_request(
                client,
                node,
                iox2,
                cbor2.dumps(
                    {
                        "op": "run",
                        "client": 1,
                        "id": 2,
                        "generator": str(FTP_SCRIPT),
                        "params": {"verb": "USER", "arg": "ubuntu"},
                        "host": "127.0.0.1",
                        "port": port,
                        "timeout_ms": 1000,
                    }
                ),
            )
            self.assertEqual(run["status"], "ok", run)
            self.assertEqual(seen.get("data"), b"USER ubuntu\r\n")
            self.assertGreater(run["recv"], 0)

            reset = _iox_request(
                client,
                node,
                iox2,
                cbor2.dumps({"op": "reset", "client": 1, "id": 3}),
            )
            self.assertEqual(reset["status"], "ok", reset)
        finally:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
