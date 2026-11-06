import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_analysis import catalog as ids
from coverage_analysis import coverage as coverage_mod
from coverage_analysis import proposal as proposal_mod
from coverage_analysis import protocol as protocol_mod
from coverage_analysis.handlers import load_handlers
from coverage_analysis.manifest import graphs_digest, write_manifest


def model_document():
    step = {
        "id": "step-001",
        "name": "hello",
        "message_name": "hello",
        "channel_name": "control",
        "sender_name": "client",
        "receiver_name": "server",
        "condition": "start",
    }
    return {
        "meta": {"schema_version": "7.0.0", "protocol": "demo", "complete": True,
                 "documents": []},
        "parties": [{"name": "client", "description": "c"},
                    {"name": "server", "description": "s"}],
        "messages": [{"name": "hello", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP",
                      "connection": "connection_oriented",
                      "endpoint_party_names": ["client", "server"],
                      "initiator_party_name": "client",
                      "acceptor_party_name": "server", "endpoint_sources": []}],
        "steps": [step],
        "step_dependencies": [],
        "unresolved": [],
    }


def coverage_document(source="a.c"):
    branch = [10, 3, 10, 9, 1, 0, 0, 0, 4]
    return {
        "data": [{
            "functions": [{"name": "foo", "count": 3, "filenames": [source],
                           "branches": [branch], "regions": []}],
            "files": [{"filename": source, "branches": [branch]}],
            "totals": {"branches": {"count": 2, "covered": 1}},
        }],
        "type": "llvm.coverage.json.export",
        "version": "2.0.1",
    }


def fixture(root):
    root = Path(root)
    source = root / "source"
    source.mkdir()
    (source / "a.c").write_text("int foo(int x) { return x ? 1 : 0; }\n")
    protocol = root / "protocol.json"
    protocol.write_text(json.dumps(model_document()))
    catalog = protocol_mod.build_catalog(model_document())
    catalog_path = root / "catalog.json"
    catalog_path.write_text(json.dumps(catalog))
    step_id = catalog["steps"][0]["step_id"]
    handlers = {
        "version": 1,
        "session": "s",
        "mappings": [{
            "message": "hello",
            "handlers": [{"symbol": "foo", "location": "a.c:10",
                          "evidence": "dispatch", "uncertain": False}],
            "step_ids": [step_id],
            "reason": None,
        }],
    }
    handlers_path = root / "handlers.json"
    handlers_path.write_text(json.dumps(handlers))
    cfg = root / "cfg"
    cfg.mkdir()
    (cfg / "source-map.json").write_text(json.dumps({"version": 1, "functions": {"foo": {"blocks": {}}}}))
    (cfg / ".foo.dot").write_text(
        'digraph "CFG for \'foo\' function" {\n'
        '\tNodeA [shape=record,label="{entry:\\l|{<s0>T|<s1>F}}"];\n'
        '\tNodeA:s0 -> NodeB;\n\tNodeA:s1 -> NodeC;\n'
        '\tNodeB [shape=record,label="{yes:\\l|}"];\n\tNodeC [shape=record,label="{no:\\l|}"];\n}\n'
    )
    callgraph = root / "callgraph.dot"
    callgraph.write_text(
        'digraph "Call graph" {\n'
        '\tNode1 [shape=record,label="{foo}"];\n\tNode2 [shape=record,label="{bar}"];\n'
        '\tNode1 -> Node2;\n}\n'
    )
    binary = root / "fftp"
    binary.write_bytes(b"binary")
    manifest = root / "manifest.json"
    write_manifest(manifest, binary, source, root,
                   ["callgraph.dot", "cfg/.foo.dot", "cfg/source-map.json"])
    snapshot = root / "snapshots" / "000001"
    snapshot.mkdir(parents=True)
    (snapshot / "metadata.json").write_text(json.dumps({
        "fingerprint": "build1", "binary": str(binary),
        "binary_sha256": hashlib.sha256(b"binary").hexdigest(),
    }))
    (snapshot / "coverage.json").write_text(json.dumps(coverage_document(str(source / "a.c"))))
    (snapshot / "coverage.profdata.zlib").write_bytes(b"not-really-zlib")
    out = root / "out"
    out.mkdir()
    return {
        "root": root, "source": source, "protocol": protocol, "catalog": catalog_path,
        "handlers": handlers_path, "cfg": cfg, "callgraph": callgraph,
        "manifest": manifest, "snapshot": snapshot, "out": out, "step_id": step_id,
        "binary": binary,
    }


class IdTests(unittest.TestCase):
    def test_step_trace_and_protocol_ids_are_stable_and_short(self):
        step = model_document()["steps"][0]
        digest = ids.step_id(step)
        self.assertEqual(len(digest), 8)
        self.assertEqual(digest, "55f21230")
        trace = ids.trace_id([step])
        self.assertEqual(len(trace), 8)
        self.assertEqual(trace, "d21f4f84")
        self.assertEqual(len(ids.protocol_id(model_document())), 8)


class CoverageTests(unittest.TestCase):
    def test_aliases_join_file_prefixed_names(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "coverage.json"
            path.write_text(json.dumps(coverage_document("/s/ftpserv.c")))
            data = coverage_mod.load_coverage(path, "/s")
            data.functions["ftpserv.c:foo"] = data.functions.pop("foo")
            diagnostics = coverage_mod.build_aliases(data, ["foo"])
            self.assertEqual(data.aliases["ftpserv.c:foo"], "foo")
            self.assertEqual(data.function_stats("foo")["total_branches"], 2)
            self.assertEqual(data.function_stats("foo")["covered_branches"], 1)
            self.assertEqual(diagnostics, [])

    def test_missing_alias_is_diagnostic_not_guessed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "coverage.json"
            path.write_text(json.dumps(coverage_document("/s/a.c")))
            data = coverage_mod.load_coverage(path)
            diagnostics = coverage_mod.build_aliases(data, ["bar"])
            self.assertNotIn("a.c:foo", data.aliases)
            self.assertTrue(diagnostics)


class ProposalTests(unittest.TestCase):
    def test_proposal_round_trip_and_content_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            graph = protocol_mod.load_protocol(files["protocol"], files["catalog"])
            handlers = load_handlers(files["handlers"])
            handler_id = handlers.rows[0].handlers[0].id
            trace = [{"step_id": files["step_id"], "handler_ids": [handler_id],
                      "reason": "target", "constraints": ["needs login"]}]
            proposal = proposal_mod.build_proposal(
                graph, "coverage", "s", "req", "build1", "000001",
                {"handler_ids": [handler_id], "reason": "reach foo"}, trace, handlers)
            proposal_mod.validate_proposal(proposal, graph, handlers,
                                           {"session": "s", "request_id": "req",
                                            "build": "build1", "snapshot": "000001",
                                            "protocol_id": graph.base["protocol_id"]})
            self.assertEqual(len(proposal["proposal_id"]), 8)
            changed = json.loads(json.dumps(proposal))
            changed["trace"][0]["constraints"] = ["different"]
            with self.assertRaises(proposal_mod.ProposalError):
                proposal_mod.validate_proposal(changed, graph, handlers)


class ServiceTests(unittest.TestCase):
    def test_service_loads_queries_and_commits(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            socket_path = str(files["root"] / "mcp.sock")
            proc = subprocess.Popen(
                [sys.executable, "-m", "coverage_analysis.server", "serve",
                 "--socket", socket_path, "--state-dir", str(files["root"] / "state"),
                 "--source-root", str(files["source"])],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if os.path.exists(socket_path):
                        break
                    time.sleep(0.05)
                def call(payload):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(20)
                        client.connect(socket_path)
                        client.sendall(json.dumps(payload).encode() + b"\n")
                        data = b""
                        while b"\n" not in data:
                            chunk = client.recv(1 << 20)
                            if not chunk:
                                break
                            data += chunk
                    reply = json.loads(data.split(b"\n")[0])
                    self.assertTrue(reply.get("ok"), reply)
                    return reply["result"]

                def tool(name, arguments):
                    return call({"op": "tool", "name": name, "arguments": arguments})

                request = {
                    "request_id": "req-1", "session": "s", "build": "build1",
                    "snapshot": "000001", "protocol_id": "unused",
                    "output_dir": str(files["out"]),
                    "snapshot_dir": str(files["snapshot"]),
                    "manifest": str(files["manifest"]),
                    "model": str(files["protocol"]),
                    "catalog": str(files["catalog"]),
                    "handlers": str(files["handlers"]),
                    "source_dir": str(files["source"]),
                }
                call({"op": "register_request", "request": request})
                tool("load_protocol", {"protocol_path": str(files["protocol"]),
                                       "catalog_path": str(files["catalog"])})
                catalog = json.loads(files["catalog"].read_text())
                request["protocol_id"] = catalog["protocol_id"]
                call({"op": "register_request", "request": request})
                tool("load_handlers", {"handlers_path": str(files["handlers"])})
                tool("load_cfg", {"cfg_dir": str(files["cfg"]),
                                  "manifest_path": str(files["manifest"])})
                tool("load_callgraph", {"callgraph_dot": str(files["callgraph"]),
                                        "manifest_path": str(files["manifest"])})
                tool("load_profdata", {"request_id": "req-1",
                                       "snapshot_dir": str(files["snapshot"]),
                                       "manifest_path": str(files["manifest"])})
                overview = tool("show_overview_code_coverage", {})
                self.assertEqual(overview["total_branches"], 2)
                self.assertEqual(overview["covered_branches"], 1)
                handlers = tool("show_handler_code_coverage", {})["handlers"]["items"]
                self.assertEqual(len(handlers), 1)
                tool("begin_analysis", {"request_id": "req-1"})
                proposal = tool("submit_proposal", {
                    "request_id": "req-1", "origin": "coverage",
                    "goal": {"handler_ids": [handlers[0]["handler"]["id"]],
                             "reason": "reach foo"},
                    "trace": [{"step_id": files["step_id"],
                               "handler_ids": [handlers[0]["handler"]["id"]],
                               "reason": "target", "constraints": []}],
                })
                path = files["out"] / "proposal.json"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                committed = call({"op": "commit_analysis", "request_id": "req-1",
                                  "proposal_id": proposal["proposal_id"],
                                  "proposal_sha256": digest})
                self.assertEqual(committed["graph_revision"], 1)
                again = call({"op": "commit_analysis", "request_id": "req-1",
                              "proposal_id": proposal["proposal_id"],
                              "proposal_sha256": digest})
                self.assertEqual(again["graph_revision"], 1)
                call({"op": "shutdown"})
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                stderr = proc.stderr.read().decode()
                if proc.returncode not in (0, None):
                    self.fail(stderr)


if __name__ == "__main__":
    unittest.main()
