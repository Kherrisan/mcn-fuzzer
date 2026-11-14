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

from coverage_analysis import coverage as coverage_mod
from coverage_analysis import proposal as proposal_mod
from coverage_analysis import protocol as protocol_mod
from coverage_analysis.handlers import load_handlers


def model_document():
    step = {
        "name": "hello",
        "action": "send", "message_name": "hello",
        "channel_name": "control",
        "sender_name": "client",
        "receiver_name": "server",
        "condition": "start",
    }
    return {
        "meta": {"protocol": "demo", "complete": True,
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
    step_name = catalog["steps"][0]["name"]
    handlers = {
        "version": 2,
        "session": "s",
        "mappings": [{
            "message": "hello",
            "handlers": [{"symbol": "foo", "location": "a.c:10",
                          "evidence": "dispatch", "uncertain": False}],
            "step_names": [step_name],
            "reason": None,
        }],
    }
    handlers_path = root / "handlers.json"
    handlers_path.write_text(json.dumps(handlers))
    cfg = root / "cfg"
    cfg.mkdir()
    (cfg / "source-map.json").write_text(json.dumps({"functions": {"foo": {"blocks": {}}}}))
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
    snapshot = root / "snapshots" / "000001"
    snapshot.mkdir(parents=True)
    (snapshot / "metadata.json").write_text(json.dumps({
        "binary": str(binary),
    }))
    (snapshot / "coverage.json").write_text(json.dumps(coverage_document(str(source / "a.c"))))
    out = root / "out"
    out.mkdir()
    return {
        "root": root, "source": source, "protocol": protocol, "catalog": catalog_path,
        "handlers": handlers_path, "cfg": cfg, "callgraph": callgraph,
        "snapshot": snapshot, "out": out, "step_name": step_name,
        "binary": binary,
    }


class CoverageTests(unittest.TestCase):
    def test_dependency_fields_are_state_keys(self):
        keys = ["size", "token"]
        dependency = {"prior": "a", "dependent": "c", "kind": "data", "fields": keys}
        self.assertEqual(proposal_mod._dependencies([dependency], ["a", "c"])[0]["fields"], keys)
        for invalid in ([{"prior_field": "size", "dependent_field": "offset"}], [""], []):
            with self.subTest(fields=invalid), self.assertRaises(proposal_mod.ProposalError):
                proposal_mod._dependencies([{**dependency, "fields": invalid}], ["a", "c"])
        with self.assertRaises(proposal_mod.ProposalError):
            proposal_mod._dependencies([dependency, {**dependency, "prior": "b"}], ["a", "b", "c"])
        with self.assertRaisesRegex(proposal_mod.ProposalError, "not in trace order"):
            proposal_mod._dependencies([dependency], ["c", "a"])

    def test_model_field_relation_uses_prior_field_as_state_key(self):
        model = model_document()
        model["steps"].append({**model["steps"][0], "name": "next"})
        model["step_dependencies"] = [{
            "prior_step": "hello", "dependent_step": "next", "condition": "",
            "field_relations": [{"prior_field": "transfer size", "dependent_field": "restart marker",
                                 "derive": "subtract acknowledged bytes"}],
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            path.write_text(json.dumps(model))
            graph = protocol_mod.load_protocol(path)
            relation = graph.dependencies("next")["before"][0]["field_relations"][0]
            self.assertEqual(relation["derive"], "subtract acknowledged bytes")
            parties = {"client_1": "client", "server_1": "server"}
            channels = {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}}
            trace = [{"name": name, "step": step, "channel": "control_1", "sender": "client_1",
                      "receiver": "server_1", "reason": "test"}
                     for name, step in [("first", "hello"), ("second", "next")]]
            proposal = proposal_mod.build_proposal(graph, "autonomous", "s", "", "",
                                                   {"reason": "test"}, trace, None, parties, channels)
            self.assertEqual(proposal["dependencies"][0]["fields"], ["transfer size"])

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

    def test_alias_mapping_is_computed_once_per_session(self):
        from types import SimpleNamespace

        from coverage_analysis import state as state_mod

        with tempfile.TemporaryDirectory() as raw:
            state = state_mod.State(raw)
            graph = SimpleNamespace(successors={"foo": []}, digest="graph-1")
            state.latest["callgraph"] = state_mod.Loaded(
                "callgraph", 1, "graph-1", {}, graph)
            calls = []
            original = coverage_mod.compute_aliases

            def counting(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)

            coverage_mod.compute_aliases = counting
            try:
                first = coverage_mod.load_coverage_document(coverage_document("/s/a.c"))
                first.functions["a.c:foo"] = first.functions.pop("foo")
                self.assertFalse(state._alias_join(first))
                second = coverage_mod.load_coverage_document(coverage_document("/s/a.c"))
                second.functions["a.c:foo"] = second.functions.pop("foo")
                self.assertTrue(state._alias_join(second))
                self.assertEqual(len(calls), 1)
                self.assertEqual(second.aliases["a.c:foo"], "foo")
                # An already aliased model is not re-keyed a second time.
                self.assertTrue(state._alias_join(second))
                self.assertEqual(second.function_stats("foo")["total_branches"], 2)
            finally:
                coverage_mod.compute_aliases = original

    def test_apply_aliases_is_idempotent(self):
        data = coverage_mod.load_coverage_document(coverage_document("/s/a.c"))
        data.functions["a.c:foo"] = data.functions.pop("foo")
        aliases, diagnostics = coverage_mod.compute_aliases(data.functions, ["foo"])
        coverage_mod.apply_aliases(data, aliases)
        coverage_mod.apply_aliases(data, aliases)
        self.assertEqual(data.aliases["a.c:foo"], "foo")
        self.assertEqual(data.function_stats("foo")["total_branches"], 2)
        self.assertEqual(diagnostics, [])




class HandlerMappingTests(unittest.TestCase):
    def test_shared_symbol_across_messages_is_one_handler(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "handlers.json"
            path.write_text(json.dumps({
                "version": 2, "session": "s",
                "mappings": [
                    {"message": "one", "handlers": [
                        {"symbol": "shared", "location": "server.c:10"}]},
                    {"message": "two", "handlers": [
                        {"symbol": "shared", "location": "server.c:10"}]},
                ],
            }))
            loaded = load_handlers(path)
            self.assertEqual(len(loaded.by_id), 1)
            self.assertEqual(len(loaded.for_message("one")), 1)
            self.assertEqual(len(loaded.for_message("two")), 1)
            self.assertEqual(loaded.for_message("one")[0].id,
                             loaded.for_message("two")[0].id)

    def test_same_name_with_different_identity_still_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "handlers.json"
            path.write_text(json.dumps({
                "version": 2, "session": "s",
                "mappings": [
                    {"message": "one", "handlers": [
                        {"symbol": "shared", "location": "server.c:10"}]},
                    {"message": "two", "handlers": [
                        {"symbol": "shared", "location": "server.c:20"}]},
                ],
            }))
            with self.assertRaises(Exception):
                load_handlers(path)


class ProposalTests(unittest.TestCase):
    def test_proposal_round_trip_and_content_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            graph = protocol_mod.load_protocol(files["protocol"], files["catalog"])
            handlers = load_handlers(files["handlers"])
            handler_id = handlers.rows[0].handlers[0].id
            self.assertEqual(handler_id, "foo")
            trace = [{"name": "hello_once", "step": files["step_name"],
                      "channel": "control_1", "sender": "client_1", "receiver": "server_1",
                      "reason": "target", "constraints": ["needs login"]}]
            parties = {"client_1": "client", "server_1": "server"}
            channels = {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}}
            proposal = proposal_mod.build_proposal(
                graph, "coverage", "s", "build1", "000001",
                {"handler_ids": [handler_id], "reason": "reach foo"}, trace, handlers,
                parties, channels)
            proposal_mod.validate_proposal(proposal, graph, handlers,
                                           {"session": "s", "build": "build1", "snapshot": "000001"})
            self.assertNotIn("proposal_id", proposal)
            self.assertIn("dependencies", proposal)
            self.assertIn("mutable_fields", proposal["trace"][0])
            changed = json.loads(json.dumps(proposal))
            changed["trace"][0]["constraints"] = ["different"]
            proposal_mod.validate_proposal(changed, graph, handlers)
            missing_reason = json.loads(json.dumps(proposal))
            missing_reason["goal"]["reason"] = "  "
            with self.assertRaises(proposal_mod.ProposalError):
                proposal_mod.validate_proposal(missing_reason, graph, handlers)


def _network_model():
    def step(action, name, **fields):
        row = {
            "name": name,
            "action": action,
            "channel_name": "control",
            "condition": "",
        }
        row.update(fields)
        return row
    return {
        "meta": {"protocol": "demo", "complete": True,
                 "documents": []},
        "parties": [{"name": "client", "description": "c"},
                    {"name": "server", "description": "s"}],
        "messages": [{"name": "hello", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP",
                      "connection": "connection_oriented",
                      "endpoint_party_names": ["client", "server"],
                      "initiator_party_name": "client",
                      "acceptor_party_name": "server", "endpoint_sources": []}],
        "steps": [
            step("send", "send hello", message_name="hello",
                 sender_name="client", receiver_name="server"),
            step("listen", "server listens", party_name="server"),
            step("connect", "client connects", party_name="client"),
            step("close", "server closes", party_name="server"),
        ],
        "step_dependencies": [{
            "prior_step": "server listens",
            "dependent_step": "client connects",
            "condition": "listener is ready",
            "field_relations": [],
        }],
        "unresolved": [],
    }


class ProtocolLoadTests(unittest.TestCase):
    def test_current_catalog_loads_every_action(self):
        model = _network_model()
        catalog = protocol_mod.build_catalog(model)
        self.assertNotIn("protocol_id", catalog)
        actions = {step["action"] for step in catalog["steps"]}
        self.assertEqual(actions, {"send", "listen", "connect", "close"})
        for raw, published in zip(model["steps"], catalog["steps"]):
            self.assertEqual(published["name"], raw["name"])
            self.assertNotIn("step_id", published)
            self.assertEqual(published["party_name"], raw.get("party_name"))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            protocol = root / "protocol.json"
            catalog_path = root / "catalog.json"
            protocol.write_text(json.dumps(model))
            catalog_path.write_text(json.dumps(catalog))
            graph = protocol_mod.load_protocol(protocol, catalog_path)
            self.assertEqual({step["action"] for step in graph.catalog["steps"]}, actions)
            listen = next(step for step in graph.catalog["steps"] if step["action"] == "listen")
            context = protocol_mod.context_for(graph, [listen["name"]], None)
            self.assertEqual(context["parties"], ["server"])
            self.assertEqual(context["handlers"], [])
            self.assertIsNone(graph.view(listen["name"])["message"])


    def test_model_shape_validation_cannot_be_bypassed_by_omitting_version(self):
        for invalid in (None, [], {}, {"meta": {}}, {**_network_model(), "steps": "bad"}):
            with self.subTest(model=invalid), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "protocol.json"
                path.write_text(json.dumps(invalid))
                with self.assertRaises(protocol_mod.ProtocolError):
                    protocol_mod.load_protocol(path)

    def test_proposal_rejects_removed_version_and_request_id(self):
        model = _network_model()
        catalog = protocol_mod.build_catalog(model)
        graph = protocol_mod.ProtocolGraph({"protocol_name": "demo", "model": model}, catalog)
        base = {
            "origin": "autonomous", "session": "s", "build": "", "snapshot": "",
            "goal": {"handler_ids": [], "reason": "x"},
            "parties": {}, "channels": {}, "trace": [], "mutations": [],
            "dependencies": [],
        }
        for field in ("version", "request_id", "graph_revision", "global_context"):
            with self.subTest(field=field):
                bad = dict(base)
                bad[field] = 1 if field != "request_id" else "r"
                with self.assertRaisesRegex(proposal_mod.ProposalError, field):
                    proposal_mod.validate_proposal(bad, graph, None)

    def test_proposal_rejects_trace_handler_ids(self):
        model = _network_model()
        catalog = protocol_mod.build_catalog(model)
        graph = protocol_mod.ProtocolGraph(
            {"protocol_name": "demo", "model": model},
            catalog,
        )
        listen = next(step["name"] for step in catalog["steps"] if step["action"] == "listen")
        with self.assertRaises(proposal_mod.ProposalError) as raised:
            proposal_mod.build_proposal(
                graph, "autonomous", "s", "", "",
                {"handler_ids": [], "reason": "open"},
                [{"name": "listen_once", "step": listen, "channel": "control_1",
                  "party": "server_1", "handler_ids": ["h1"], "reason": "listen",
                  "constraints": []}],
                None,
                {"server_1": "server"},
                {"control_1": {"channel": "control", "endpoints": ["server_1"]}},
            )
        self.assertIn("cannot carry handler ids", str(raised.exception))


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
                    "snapshot": "000001",
                    "output_dir": str(files["out"]),
                    "snapshot_dir": str(files["snapshot"]),
                    "model": str(files["protocol"]),
                    "catalog": str(files["catalog"]),
                    "handlers": str(files["handlers"]),
                    "source_dir": str(files["source"]),
                }
                call({"op": "register_request", "request": request})
                tool("load_protocol", {"protocol_path": str(files["protocol"]),
                                       "catalog_path": str(files["catalog"])})
                tool("load_handlers", {"handlers_path": str(files["handlers"])})
                tool("load_cfg", {"cfg_dir": str(files["cfg"])})
                tool("load_callgraph", {"callgraph_dot": str(files["callgraph"])})
                loaded = tool("load_coverage_json", {"request_id": "req-1",
                                                     "snapshot_dir": str(files["snapshot"])})
                self.assertFalse((files["snapshot"] / "coverage.json").exists())
                (files["snapshot"] / "coverage.json").write_text("not-json")
                again = tool("load_coverage_json", {"request_id": "req-1",
                                                     "snapshot_dir": str(files["snapshot"])})
                self.assertEqual(again["version"], loaded["version"])
                self.assertTrue(again["reused"])
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
                    "parties": {"client_1": "client", "server_1": "server"},
                    "channels": {"control_1": {"channel": "control",
                                               "endpoints": ["client_1", "server_1"]}},
                    "trace": [{"name": "hello_once", "step": files["step_name"],
                               "channel": "control_1", "sender": "client_1",
                               "receiver": "server_1",
                               "reason": "target", "constraints": []}],
                    "mutations": [{"op": "field", "steps": ["hello_once"],
                                   "field": "username", "suggestion": "empty",
                                   "evidence": "a.c:10"}],
                })
                self.assertEqual(proposal["request_id"], "req-1")
                self.assertNotIn("proposal_id", proposal)
                path = files["out"] / "proposal.json"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                committed = call({"op": "commit_analysis", "request_id": "req-1",
                                  "proposal_sha256": digest})
                self.assertEqual(committed["graph_revision"], 1)
                again = call({"op": "commit_analysis", "request_id": "req-1",
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

    def test_stdio_bridge_lists_tools_and_loads_snapshot(self):
        try:
            from mcp import Client
            from mcp.client.stdio import StdioServerParameters
        except ImportError:
            self.skipTest("mcp package is not installed")
        import asyncio

        python_root = Path(__file__).resolve().parents[1]
        server_py = python_root / "coverage_analysis" / "server.py"
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            socket_path = str(files["root"] / "mcp.sock")
            proc = subprocess.Popen(
                [sys.executable, "-m", "coverage_analysis.server", "serve",
                 "--socket", socket_path, "--state-dir", str(files["root"] / "state"),
                 "--source-root", str(files["source"])],
                cwd=str(python_root),
                env={**os.environ, "PYTHONPATH": str(python_root)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if os.path.exists(socket_path):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("coverage service did not create its socket")

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

                call({"op": "register_request", "request": {
                    "request_id": "req-1", "session": "s", "build": "build1",
                    "snapshot": "000001",
                    "output_dir": str(files["out"]),
                    "snapshot_dir": str(files["snapshot"]),
                    "model": str(files["protocol"]),
                    "catalog": str(files["catalog"]),
                    "handlers": str(files["handlers"]),
                    "source_dir": str(files["source"]),
                }})

                async def drive():
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=[str(server_py), "stdio", "--socket", socket_path],
                        cwd=str(python_root),
                        env={
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": os.environ.get("HOME", "/tmp"),
                            "PYTHONPATH": str(python_root),
                        },
                    )
                    async with Client(params, mode="legacy") as client:
                        listed = await client.list_tools()
                        loaded = await client.call_tool("load_coverage_json", {
                            "request_id": "req-1",
                            "snapshot_dir": str(files["snapshot"]),
                        })
                        overview = await client.call_tool(
                            "show_overview_code_coverage", {})
                        return listed, loaded, overview

                listed, loaded, overview = asyncio.run(asyncio.wait_for(drive(), 40))
                names = {tool.name for tool in listed.tools}
                for required in ("load_coverage_json", "begin_analysis",
                                 "show_overview_code_coverage", "submit_proposal"):
                    self.assertIn(required, names)
                self.assertFalse(loaded.is_error, loaded)
                body = json.loads(loaded.content[0].text)
                self.assertEqual(body["snapshot"], "000001")
                self.assertEqual(body["coverage"], "coverage.json")
                self.assertFalse(overview.is_error, overview)
                summary = json.loads(overview.content[0].text)
                self.assertEqual(summary["total_branches"], 2)
                self.assertEqual(summary["covered_branches"], 1)
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
