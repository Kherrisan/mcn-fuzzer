import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import cbor2

from generator_server.context import Ctx, InteractionError, Session
from generator_server.registry import Registry
from generator_server.server import handle_payload
from generator_server.worker import run_isolated

ROOT = Path(__file__).resolve().parents[1]
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
    def test_legacy_generate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.py"
            path.write_text("def generate(ctx, params): pass\n")
            with self.assertRaisesRegex(ValueError, "no step handlers"):
                Registry().get(str(path))

    def test_import_is_cached(self):
        path = Path(tempfile.gettempdir()) / f"libaflstar_gen_once_{os.getpid()}.py"
        path.write_text(
            "COUNT = 0\n"
            "COUNT += 1\n"
            "from generator_server import step_handler\n@step_handler('test')\ndef handler(ctx, params):\n"
            "    return COUNT\n"
        )
        registry = Registry()
        first = registry.get(str(path))
        second = registry.get(str(path))
        self.assertIs(first, second)
        self.assertEqual(registry.loads, 1)
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        first(ctx, [{"name": "one", "step": "test"}])

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
            "step_trace": [{"name": "one", "step": "ftp command"}],
            "params": {"one": {"verb": "USER", "arg": "ubuntu"}},
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
            "step_trace": [{"name": "one", "step": "ftp command"}],
            "params": {"one": {"verb": "PASV", "arg": ""}},
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

        seen = {}
        first = threading.Thread(target=lambda: seen.update(one=dial(b"one\r\n")))
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
        self.assertFalse(hasattr(channel, "sock"))
        self.assertFalse(hasattr(channel, "fileno"))
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
        session = Session()
        sessions = {9: session}
        ctx = Ctx(session, "127.0.0.1", port, 1.0)
        ctx.send(b"NOOP\r\n")
        self.assertIsNotNone(session.endpoint)
        reset = {"op": "reset", "client": 9, "id": 2}
        second = cbor2.loads(handle_payload(cbor2.dumps(reset), sessions))
        self.assertEqual(second["status"], "ok")
        self.assertNotIn(9, sessions)


class BufferIsolationTests(unittest.TestCase):
    def test_recv_until_keeps_tail(self):
        payload = b"220 ready\r\n331 next\r\n"

        def handler(conn):
            conn.sendall(payload)

        port = _listen(handler)
        ctx = Ctx(Session(), "127.0.0.1", port, 1.0)
        first = ctx.recv(until=b"\r\n")
        second = ctx.recv(until=b"\r\n")
        self.assertEqual(first, b"220 ready\r\n")
        self.assertEqual(second, b"331 next\r\n")
        ctx.close_all()

    def test_recv_reports_eof(self):
        def handler(conn):
            conn.sendall(b"partial")

        port = _listen(handler)
        ctx = Ctx(Session(), "127.0.0.1", port, 1.0)
        with self.assertRaises(InteractionError) as caught:
            ctx.recv(until=b"\r\n", max_bytes=64)
        self.assertEqual(caught.exception.status, "eof")
        ctx.close_all()

    def test_worker_globals_do_not_leak(self):
        directory = Path(tempfile.mkdtemp(prefix="mcn-gen-"))
        script = directory / "counter.py"
        script.write_text(
            "COUNT = 0\n"
            "from generator_server import step_handler\n@step_handler('test')\ndef handler(ctx, params):\n"
            "    global COUNT\n"
            "    COUNT += 1\n"
            "    open(params['one']['out'], 'w').write(str(COUNT))\n"
        )
        outs = []
        for index in range(2):
            out = directory / f"run{index}.txt"
            request = {
                "op": "run",
                "client": 1,
                "id": index + 1,
                "generator": str(script),
                "step_trace": [{"name": "one", "step": "test"}],
                "params": {"one": {"out": str(out)}},
                "host": "127.0.0.1",
                "port": 1,
                "timeout_ms": 500,
            }
            response = cbor2.loads(run_isolated(cbor2.dumps(request)))
            self.assertEqual(response["status"], "ok", response)
            outs.append(out.read_text())
        self.assertEqual(outs, ["1", "1"])

    def test_case_timeout_closes_connection(self):
        held = {}
        closed = threading.Event()

        def handler(conn):
            held["conn"] = conn
            try:
                while conn.recv(16):
                    pass
            except OSError:
                pass
            closed.set()

        port = _listen(handler)
        script = Path(tempfile.mkdtemp(prefix="mcn-block-")) / "block.py"
        script.write_text(
            "from generator_server import step_handler\n@step_handler('test')\ndef handler(ctx, params):\n"
            "    ctx.recv(max_bytes=16)\n"
        )
        request = {
            "op": "run",
            "client": 3,
            "id": 9,
            "generator": str(script),
            "step_trace": [{"name": "one", "step": "test"}],
            "params": {},
            "host": "127.0.0.1",
            "port": port,
            "timeout_ms": 300,
        }
        started = time.monotonic()
        response = cbor2.loads(run_isolated(cbor2.dumps(request)))
        elapsed = time.monotonic() - started
        self.assertEqual(response["status"], "timeout", response)
        self.assertLess(elapsed, 2.5)
        self.assertTrue(closed.wait(2.0), "worker did not close the target socket")

    def test_exception_closes_connection(self):
        closed = threading.Event()

        def handler(conn):
            try:
                while conn.recv(16):
                    pass
            except OSError:
                pass
            closed.set()

        port = _listen(handler)
        script = Path(tempfile.mkdtemp(prefix="mcn-boom-")) / "boom.py"
        script.write_text(
            "from generator_server import step_handler\n@step_handler('test')\ndef handler(ctx, params):\n"
            "    ctx.send(b'hello')\n"
            "    raise RuntimeError('boom')\n"
        )
        request = {
            "op": "run",
            "client": 4,
            "id": 10,
            "generator": str(script),
            "step_trace": [{"name": "one", "step": "test"}],
            "params": {},
            "host": "127.0.0.1",
            "port": port,
            "timeout_ms": 1000,
        }
        response = cbor2.loads(run_isolated(cbor2.dumps(request)))
        self.assertEqual(response["status"], "generator", response)
        self.assertIn("boom", response.get("error", ""))
        self.assertTrue(closed.wait(2.0), "exception path left the socket open")


def _iceoryx2():
    try:
        import iceoryx2 as iox2
    except ImportError:
        return None
    return iox2


def _iox_run(client, node, iox2, body: bytes, timeout_s: float = 5):
    sample = client.loan_slice_uninit(len(body))
    view = sample.payload()
    for index, byte in enumerate(body):
        view[index] = byte
    pending = sample.assume_init().send()
    messages = []
    deadline = time.time() + timeout_s
    while time.time() < deadline and len(messages) < 2:
        response = pending.receive()
        if response is None:
            node.wait(iox2.Duration.from_millis(1))
            continue
        payload = response.payload()
        messages.append(cbor2.loads(bytes(payload[index] for index in range(payload.len()))))
        if messages[-1].get("kind") == "result":
            break
    if len(messages) < 2:
        raise TimeoutError(f"generator server did not send Started and Result: {messages}")
    return messages[0], messages[1]


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



class StepTraceTests(unittest.TestCase):
    def test_dependency_resolves_the_bound_instance_after_duplication(self):
        with tempfile.TemporaryDirectory(prefix="mcn-instance-dependency-") as directory:
            script = Path(directory) / "generator.py"
            script.write_text(
                "from generator_server import step_handler\n"
                "@step_handler('receive')\n"
                "def receive(ctx, params):\n"
                "    s = ctx.step_instance\n"
                "    assert ctx.step_name == s['name']\n"
                "    ctx.save_state('token', params[s['name']]['token'])\n"
                "    assert ctx.load_state('token') == params[s['name']]['token']\n"
                "@step_handler('send')\n"
                "def send(ctx, params):\n"
                "    ctx.observed.append((ctx.step_instance['name'], ctx.load_depending_state('token')))\n"
            )
            generate = Registry().get(str(script))
            ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
            ctx.observed = []
            ctx.generator_params = {
                "a": {"token": "original"},
                "a_copy": {"token": "copy"},
            }
            trace = [
                {"name": "a", "step": "receive"},
                {"name": "b", "step": "send", "inputs": {"token": "a.token"}},
                {"name": "a_copy", "step": "receive"},
                {"name": "b_copy", "step": "send", "inputs": {"token": "a_copy.token"}},
                {"name": "b_later", "step": "send", "inputs": {"token": "a.token"}},
            ]
            generate(ctx, trace)
            self.assertEqual(ctx.observed, [("b", "original"), ("b_copy", "copy"),
                                             ("b_later", "original")])
            fresh = Ctx(Session(), "127.0.0.1", 1, 1.0)
            fresh.observed = []
            with self.assertRaisesRegex(InteractionError, "missing state 'a.token'"):
                generate(fresh, [trace[1]])
            with self.assertRaisesRegex(InteractionError, "no dependency binding"):
                generate(fresh, [{"name": "missing", "step": "send", "inputs": {}}])

    def test_decorated_dispatch_initializes_and_preserves_inputs(self):
        directory = Path(tempfile.mkdtemp(prefix="mcn-handler-generator-"))
        script = directory / "generator.py"
        script.write_text(
            "from generator_server import step_handler, global_init\n"
            "@global_init\n"
            "def setup(ctx, params):\n"
            "    assert ctx.step_instance is None\n"
            "    assert ctx.step_name is None\n"
            "    ctx.events = [('init', 'ready')]\n"
            "@step_handler('send USER')\n"
            "def user(ctx, params):\n"
            "    ctx.events.append((ctx.step_name, params[ctx.step_name]['username']))\n"
            "    try: params[ctx.step_name]['username'] = 'changed'\n"
            "    except TypeError: pass\n"
            "    else: raise AssertionError('params must be read-only')\n"
            "    try: ctx.step_instance['name'] = 'changed'\n"
            "    except TypeError: pass\n"
            "    else: raise AssertionError('instance must be read-only')\n"
        )
        registry = Registry()
        generate = registry.get(str(script))
        self.assertIs(generate, registry.get(str(script)))
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        ctx.generator_params = {
            "one": {"username": "a"},
            "two": {"username": "b"},
        }
        generate(ctx, [
            {"name": "one", "step": "send USER"},
            {"name": "two", "step": "send USER"},
        ])
        self.assertEqual(ctx.events, [("init", "ready"), ("one", "a"), ("two", "b")])
        self.assertIsNone(ctx.step_instance)
        self.assertIsNone(ctx.step_name)
        with self.assertRaises(InteractionError):
            generate(ctx, [{"name": "unknown", "step": "other"}])

    def test_duplicate_decorated_handlers_rejected_on_load(self):
        directory = Path(tempfile.mkdtemp(prefix="mcn-duplicate-handler-"))
        script = directory / "generator.py"
        script.write_text(
            "from generator_server import step_handler\n"
            "@step_handler('x')\n"
            "def first(ctx, params): pass\n"
            "@step_handler('x')\n"
            "def second(ctx, params): pass\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicate step handler"):
            Registry().get(str(script))

    def test_server_dispatches_read_only_instance_params(self):
        directory = Path(tempfile.mkdtemp(prefix="mcn-handler-request-"))
        script = directory / "generator.py"
        script.write_text(
            "from generator_server import step_handler, global_init\n"
            "@global_init\n"
            "def setup(ctx, params): ctx.ready = True\n"
            "@step_handler('hello')\n"
            "def hello(ctx, params):\n"
            "    assert ctx.ready is True\n"
            "    assert ctx.step_name == 'one'\n"
            "    assert params['one']['username'] == 'mutated'\n"
            "    try: params['one']['username'] = 'changed'\n"
            "    except TypeError: pass\n"
            "    else: raise AssertionError('params must be read-only')\n"
        )
        try:
            request = {"op": "run", "client": 42, "id": 1, "generator": str(script),
                       "step_trace": [{"name": "one", "step": "hello"}],
                       "params": {"one": {"username": "mutated"}}, "host": "127.0.0.1", "port": 1}
            response = cbor2.loads(handle_payload(cbor2.dumps(request), {}))
            self.assertEqual(response["status"], "ok", response)
        finally:
            shutil.rmtree(directory)

    def test_state_access_is_explicit(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        missing = object()
        with self.assertRaises(InteractionError):
            ctx.save_state("challenge", None)
        with self.assertRaises(InteractionError):
            ctx.load_state("challenge")
        ctx.step_instance = {"name": "greet"}
        ctx.save_state("challenge", None)
        self.assertIsNone(ctx.load_state("challenge", missing))
        ctx.step_instance = {"name": "other"}
        self.assertIs(ctx.load_state("challenge", missing), missing)
        self.assertFalse(hasattr(ctx, "field"))

    def test_depending_state_accepts_dots_in_instance_and_key(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        ctx.step_instance = {"name": "a.1"}
        ctx.save_state("state.key", 42)
        ctx.step_instance = {"name": "b", "inputs": {"state.key": "a.1.state.key"}}
        self.assertEqual(ctx.load_depending_state("state.key"), 42)

    def test_states_survive_close_all(self):
        ctx = Ctx(Session(), "127.0.0.1", 1, 1.0)
        ctx.step_instance = {"name": "greet"}
        ctx.save_state("challenge", b"kept")
        ctx.close_all()
        self.assertEqual(ctx.load_state("challenge"), b"kept")
        self.assertEqual(ctx.states[("greet", "challenge")], b"kept")

    def test_server_accepts_versioned_step_trace(self):
        directory = Path(tempfile.mkdtemp(prefix="mcn-trace-generator-"))
        script = directory / "generator.py"
        marker = directory / "seen.txt"
        script.write_text(
            "from generator_server import step_handler\n@step_handler('hello')\ndef handler(ctx, params):\n"
            "    assert params['hello_1']['username'] == 'ubuntu'\n"
            "    assert ctx.get_global('x', 'missing') == 'missing'\n"
            f"    open({str(marker)!r}, 'w').write('1')\n"
        )
        try:
            request = {
                "op": "run",
                "client": 21,
                "id": 7,
                "generator": str(script),
                "version": 3,
                "step_trace": [{
                    "name": "hello_1", "step": "hello", "action": "send",
                    "channel": "control",
                }],
                "params": {"hello_1": {"username": "ubuntu"}},
                "host": "127.0.0.1", "port": 1, "timeout_ms": 1000,
            }
            response = cbor2.loads(handle_payload(cbor2.dumps(request), {}))
            self.assertEqual(response["status"], "ok", response)
            self.assertEqual(marker.read_text(), "1")
        finally:
            shutil.rmtree(directory)


class ServerProcessTests(unittest.TestCase):
    def test_server_process_ftp_roundtrip(self):
        iox2 = _iceoryx2()
        if iox2 is None:
            self.fail("iceoryx2 is not installed")

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
        service_name = f"mcn-fuzz/generator-test-{os.getpid()}"
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
                        .request_response(iox2.Slice[ctypes.c_uint8], iox2.Slice[ctypes.c_uint8])
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

            started, result = _iox_run(
                client,
                node,
                iox2,
                cbor2.dumps(
                    {
                        "op": "run",
                        "client": 7,
                        "id": 2,
                        "worker_id": 7,
                        "case_id": 2,
                        "generator": str(FTP_SCRIPT),
                        "step_trace": [{"name": "one", "step": "ftp command"}],
                        "params": {"one": {"verb": "USER", "arg": "ubuntu"}},
                        "host": "127.0.0.1",
                        "port": port,
                        "timeout_ms": 1000,
                    }
                ),
            )
            self.assertEqual(started.get("kind"), "started", started)
            self.assertGreater(started.get("pid", 0), 0, started)
            self.assertNotIn("pid", result)
            self.assertEqual(result.get("code"), 0, result)
            self.assertEqual(result.get("worker_id"), 7, result)
            self.assertEqual(result.get("case_id"), 2, result)
            self.assertEqual(seen.get("data"), b"USER ubuntu\r\n")

            reset = _iox_request(
                client,
                node,
                iox2,
                cbor2.dumps({"op": "reset", "client": 1, "id": 3}),
            )
            self.assertEqual(reset["status"], "ok", reset)

            unknown = _iox_request(
                client,
                node,
                iox2,
                cbor2.dumps(
                    {
                        "op": "cancel",
                        "client": 1,
                        "id": 4,
                        "worker_id": 1,
                        "case_id": 99,
                    }
                ),
            )
            self.assertEqual(unknown.get("status"), "generator", unknown)
            self.assertIn("unknown op", unknown.get("error", ""), unknown)
        finally:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
