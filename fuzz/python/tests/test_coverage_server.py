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
from coverage_analysis import tools
from coverage_analysis.handlers import load_handlers
from coverage_analysis.state import State, StateError


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
    def test_all_tool_contracts_are_valid_json_schemas(self):
        from jsonschema import Draft202012Validator
        from coverage_analysis import schemas

        self.assertEqual(set(tools.TOOL_DESCRIPTIONS), set(schemas.TOOL_MODELS))
        for definition in tools.mcp_tool_list():
            with self.subTest(tool=definition["name"]):
                Draft202012Validator.check_schema(definition["inputSchema"])
                Draft202012Validator.check_schema(definition["outputSchema"])
                self.assertEqual(definition["inputSchema"]["additionalProperties"], False)
                self.assertEqual(definition["outputSchema"]["type"], "object")

    def test_tool_input_errors_have_bad_request_code(self):
        with tempfile.TemporaryDirectory() as raw:
            state = State(Path(raw))
            for name, args in (
                ("load_protocol", {}),
                ("list_parties", {"extra": True}),
                ("list_messages", {"limit": 0}),
                ("list_messages", {"limit": "5"}),
            ):
                with self.subTest(name=name, args=args), self.assertRaises(StateError) as error:
                    tools.call(state, name, args)
                self.assertEqual(error.exception.code, "bad_request")

    def test_party_aliases_are_reciprocal_in_frozen_proposal(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            model = model_document()
            model["parties"][0]["alias"] = ["server"]
            files["protocol"].write_text(json.dumps(model))
            state = State(files["root"] / "state")
            loaded = state.load_protocol(files["protocol"])
            self.assertNotIn("party_aliases", loaded)
            graph = state.committed
            self.assertEqual(graph.catalog["party_aliases"], {"client": ["server"], "server": ["client"]})
            parties = tools.call(state, "list_parties", {})["parties"]
            self.assertEqual(parties, [
                {"name": "client", "description": "c", "alias": ["server"], "binding": None},
                {"name": "server", "description": "s", "alias": ["client"], "binding": None},
            ])
            graph.catalog["binding"] = {"client": "generator", "server": "sut"}
            self.assertEqual([row["binding"] for row in tools.call(state, "list_parties", {})["parties"]],
                             ["generator", "sut"])
            trace = [{"name": "hello_1", "step": "hello", "channel": "control_1",
                      "sender": "client_1", "receiver": "server_1", "reason": "test"}]
            result = proposal_mod.build_proposal(
                graph, "coverage", "s", "b", "000001", {"handler_ids": ["foo"], "reason": "test"},
                trace, None, {"client_1": "client", "server_1": "server"},
                {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}})
            self.assertEqual(result["party_aliases"], graph.catalog["party_aliases"])
            proposal_mod.validate_proposal(result, graph, None)
            model["parties"][0]["alias"] = ["missing"]
            files["protocol"].write_text(json.dumps(model))
            with self.assertRaisesRegex(protocol_mod.ProtocolError, "invalid alias"):
                protocol_mod.load_protocol(files["protocol"])

    def test_checkpoint_restores_removed_dependencies(self):
        model = model_document()
        model["steps"].append({**model["steps"][0], "name": "next"})
        model["step_dependencies"] = [{"prior_step": "hello", "dependent_step": "next"}]
        catalog = protocol_mod.build_catalog(model)
        base = {"protocol_name": "demo", "model": model}
        graph = protocol_mod.ProtocolGraph(base, catalog)
        graph.remove_dependency("hello", "next")
        graph.revision = 3
        restored = protocol_mod.ProtocolGraph(base, catalog)
        restored.apply_checkpoint(graph.checkpoint("request"))
        self.assertEqual(restored.dependencies("next")["before"], [])
        self.assertEqual(restored.revision, 3)
        self.assertEqual(restored.checkpoint_request_id, "request")

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

    def test_write_handlers_can_replace_a_previous_mapping(self):
        from coverage_analysis import tools
        from coverage_analysis.state import State

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = State(root / "state")
            state.load_protocol(Path(__file__).resolve().parents[2]
                                / "targets/lightftp/protocol.json")
            output = root / "init"
            first = tools.call(state, "write_handlers", {
                "output_dir": str(output), "session": "s1",
                "mappings": [{"message": "USER command",
                              "handlers": [{"symbol": "ftpUSER"}],
                              "step_names": ["Send USER"], "reason": None}]})
            second = tools.call(state, "write_handlers", {
                "output_dir": str(output), "session": "s1",
                "mappings": [{"message": "USER command",
                              "handlers": [{"symbol": "ftpUSER"}],
                              "step_names": ["Send USER"], "reason": None},
                             {"message": "PASS command",
                              "handlers": [{"symbol": "ftpPASS"}],
                              "step_names": ["Send PASS"], "reason": None}]})
            self.assertEqual(first["messages"], 1)
            self.assertEqual(second["messages"], 2)
            self.assertTrue(second["handlers_path"].endswith("handlers.json"))
            self.assertEqual(len(state.latest["handlers"].value.rows), 2)

    def test_session_config_supplies_register_defaults(self):
        from coverage_analysis.state import State

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "session.json"
            config.write_text(json.dumps({
                "session": "s1", "build": "source-coverage",
                "model": str(Path(__file__).resolve().parents[2] / "targets/lightftp/protocol.json"),
                "handlers": str(root / "handlers.json"),
                "llvm_cov": "llvm-cov-18",
                "sut_parties": ["Server-PI"], "generator_parties": ["User-PI"],
                "unavailable_parties": [],
            }))
            state = State(root / "state")
            loaded = state.load_session_config(config)
            self.assertEqual(loaded["fields"][0], "build")
            identity = state.register_request({
                "request_id": "req-x", "snapshot": 1,
                "output_dir": str(root / "requests/req-x"),
                "snapshot_dir": str(root / "snapshots/000001"),
            })
            self.assertEqual(identity["session"], "s1")
            request = state.request("req-x")
            self.assertEqual(request.session, "s1")
            self.assertEqual(request.build, "source-coverage")
            self.assertEqual(str(request.handlers_path), str(root / "handlers.json"))
            self.assertEqual(request.llvm_cov, "llvm-cov-18")
            self.assertEqual(request.sut_parties, ["Server-PI"])

    def test_register_without_session_fields_still_requires_request_facts(self):
        from coverage_analysis.state import State, StateError

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = State(root / "state")
            with self.assertRaises(StateError) as raised:
                state.register_request({"request_id": "req-x", "snapshot": 1,
                                        "output_dir": str(root / "o")})
            self.assertIn("snapshot_dir", str(raised.exception))

    def test_write_handlers_alone_satisfies_a_request_copying_its_file(self):
        from coverage_analysis import tools
        from coverage_analysis.state import State

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = State(root / "state")
            state.load_protocol(Path(__file__).resolve().parents[2]
                                / "targets/lightftp/protocol.json")
            init = root / "init"
            tools.call(state, "write_handlers", {
                "output_dir": str(init), "session": "s1",
                "mappings": [{"message": "USER command",
                              "handlers": [{"symbol": "ftpUSER",
                                            "location": "Source/ftpserv.c:412",
                                            "evidence": "dispatch", "uncertain": False}],
                              "step_names": ["Send USER"], "reason": None}]})
            # The campaign freezes a byte-identical accepted copy elsewhere.
            accepted = root / "handlers.accepted.json"
            accepted.write_bytes((init / "handlers.json").read_bytes())
            state.register_request({
                "request_id": "req-x", "session": "s1", "build": "b", "snapshot": 1,
                "output_dir": str(root / "requests"),
                "snapshot_dir": str(root / "snapshots/000001"),
                "model": str(Path(__file__).resolve().parents[2]
                             / "targets/lightftp/protocol.json"),
                "handlers": str(accepted),
            })
            resolved = state._request_handlers(state.request("req-x"))
            self.assertEqual(len(resolved.rows), 1)
            reloaded = state.load_handlers(accepted)
            self.assertEqual(reloaded["messages"], 1)

    def test_request_handlers_resolve_a_byte_identical_accepted_copy(self):
        from coverage_analysis.state import State

        run = (Path(__file__).resolve().parents[2]
               / "targets/openssl/runs/mcn-fuzzer-20261117-1741/source-coverage")
        if not (run / "analysis/init/handlers.accepted.json").is_file():
            self.skipTest("fixture run not present")
        with tempfile.TemporaryDirectory() as raw:
            state = State(Path(raw))
            state.load_protocol(Path(__file__).resolve().parents[2] / "targets/openssl/protocol.json")
            state.load_handlers(run / "analysis/init/handlers.json")
            state.register_request({
                "request_id": "req-x", "session": "s", "build": "b", "snapshot": 1,
                "output_dir": str(run / "analysis/requests"),
                "snapshot_dir": str(run / "snapshots/000001"),
                "model": str(Path(__file__).resolve().parents[2] / "targets/openssl/protocol.json"),
                "handlers": str(run / "analysis/init/handlers.accepted.json"),
            })
            resolved = state._request_handlers(state.request("req-x"))
            self.assertGreater(len(resolved.rows), 0)
            reloaded = state.load_handlers(run / "analysis/init/handlers.accepted.json")
            self.assertEqual(reloaded["messages"], len(resolved.rows))

    def test_write_handlers_serialises_validates_and_loads(self):
        from coverage_analysis import protocol as protocol_mod, tools

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = State(root / "state")
            state.load_protocol(Path(__file__).resolve().parents[2]
                                / "targets/lightftp/protocol.json")
            output = root / "out"
            result = tools.call(state, "write_handlers", {
                "output_dir": str(output), "session": "s1",
                "mappings": [{"message": "USER command",
                              "handlers": [{"symbol": "ftpUSER",
                                            "location": "Source/ftpserv.c:412",
                                            "evidence": "dispatch", "uncertain": False}],
                              "step_names": ["Send USER"], "reason": None}]})
            written = json.loads((output / "handlers.json").read_text())
            self.assertEqual(written["session"], "s1")
            self.assertEqual(result["messages"], 1)
            self.assertTrue(result["handlers_path"].endswith("handlers.json"))
            self.assertEqual(len(state.latest["handlers"].value.rows), 1)

    def test_write_handlers_rejects_a_step_from_another_message(self):
        from coverage_analysis import tools
        from coverage_analysis.state import StateError

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = State(root / "state")
            state.load_protocol(Path(__file__).resolve().parents[2]
                                / "targets/lightftp/protocol.json")
            with self.assertRaises(StateError) as raised:
                tools.call(state, "write_handlers", {
                    "output_dir": str(root / "out"), "session": "s1",
                    "mappings": [{"message": "USER command",
                                  "handlers": [{"symbol": "ftpUSER"}],
                                  "step_names": ["Send MLSD"], "reason": None}]})
            self.assertIn("of message", str(raised.exception))

    def test_party_errors_name_the_step_and_the_allowed_endpoints(self):
        from coverage_analysis import protocol as protocol_mod, proposal as proposal_mod

        graph = protocol_mod.load_protocol(
            Path(__file__).resolve().parents[2] / "targets/lightftp/protocol.json")
        parties = {"User-PI": "User-PI", "Server-PI": "Server-PI"}
        channels = {"control": {"channel": "control",
                                "endpoints": ["User-PI", "Server-PI"]}}
        trace = [{"name": "step-one", "step": "Send USER", "channel": "control",
                  "sender": None, "receiver": "Server-PI", "reason": "r"}]
        with self.assertRaisesRegex(proposal_mod.ProposalError,
                                    "step instance 'step-one'.*sender None.*endpoints"):
            proposal_mod.build_proposal(
                graph, origin="coverage", session="s", build="b", snapshot="000002",
                goal={"handler_ids": ["ftpUSER"], "reason": "r"}, trace=trace,
                handlers=None, parties=parties, channels=channels)

    def test_party_aliases_are_omitted_when_the_model_declares_none(self):
        from coverage_analysis import protocol as protocol_mod, proposal as proposal_mod

        graph = protocol_mod.load_protocol(
            Path(__file__).resolve().parents[2] / "targets/lightftp/protocol.json")
        graph.catalog["party_aliases"] = {name: [] for name in graph.catalog.get("parties") or []}
        parties = {"User-PI": "User-PI", "Server-PI": "Server-PI"}
        channels = {"control": {"channel": "control",
                                "endpoints": ["User-PI", "Server-PI"]}}
        trace = [{"name": "s1", "step": "Send USER", "channel": "control",
                  "sender": "User-PI", "receiver": "Server-PI", "reason": "r"}]
        proposal = proposal_mod.build_proposal(
            graph, origin="coverage", session="s", build="b", snapshot="000002",
            goal={"handler_ids": ["ftpUSER"], "reason": "r"}, trace=trace,
            handlers=None, parties=parties, channels=channels)
        self.assertNotIn("party_aliases", proposal)

        graph.catalog["party_aliases"] = {"Server-PI": ["Server A-PI"]}
        proposal = proposal_mod.build_proposal(
            graph, origin="coverage", session="s", build="b", snapshot="000002",
            goal={"handler_ids": ["ftpUSER"], "reason": "r"}, trace=trace,
            handlers=None, parties=parties, channels=channels)
        self.assertEqual(proposal["party_aliases"], {"Server-PI": ["Server A-PI"]})

    def test_network_steps_validate_through_the_party_helper(self):
        from coverage_analysis import protocol as protocol_mod, proposal as proposal_mod

        graph = protocol_mod.load_protocol(
            Path(__file__).resolve().parents[2] / "targets/lightftp/protocol.json")
        parties = {"User-PI": "User-PI", "Server-PI": "Server-PI"}
        channels = {"control": {"channel": "control",
                                "endpoints": ["User-PI", "Server-PI"]}}
        trace = [{"name": "setup", "step": "Listen control", "channel": "control",
                  "party": "Server-PI", "reason": "r"}]
        proposal = proposal_mod.build_proposal(
            graph, origin="coverage", session="s", build="b", snapshot="000002",
            goal={"handler_ids": ["ftpUSER"], "reason": "r"}, trace=trace,
            handlers=None, parties=parties, channels=channels)
        self.assertEqual(proposal["trace"][0]["party"], "Server-PI")

    def test_load_handlers_rejects_a_step_from_another_message(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            model = {
                "meta": {"protocol": "demo", "complete": True, "documents": []},
                "parties": [{"name": "client", "description": "c"},
                            {"name": "server", "description": "s"}],
                "messages": [
                    {"name": "Hello", "associated_fields": []},
                    {"name": "TLS 1.2 Hello", "associated_fields": []},
                ],
                "channels": [{"name": "control", "description": "c", "transport": "TCP",
                              "connection": "connection_oriented",
                              "endpoint_party_names": ["client", "server"],
                              "initiator_party_name": "client",
                              "acceptor_party_name": "server", "endpoint_sources": []}],
                "steps": [
                    {"name": "send hello", "action": "send", "message_name": "Hello",
                     "channel_name": "control", "sender_name": "client",
                     "receiver_name": "server", "condition": ""},
                    {"name": "send old hello", "action": "send",
                     "message_name": "TLS 1.2 Hello", "channel_name": "control",
                     "sender_name": "client", "receiver_name": "server", "condition": ""},
                ],
                "step_dependencies": [],
                "unresolved": [],
            }
            protocol = root / "protocol.json"
            protocol.write_text(json.dumps(model))
            handlers = root / "handlers.json"
            handlers.write_text(json.dumps({
                "version": 2, "session": "s",
                "mappings": [{"message": "Hello",
                              "handlers": [{"symbol": "foo", "location": "a.c:1",
                                            "evidence": "dispatch", "uncertain": False}],
                              "step_names": ["send old hello"], "reason": None}]}))
            state = State(root / "state", source_root=root)
            state.load_protocol(protocol)
            with self.assertRaises(StateError) as raised:
                state.load_handlers(handlers)
            self.assertIn("of message TLS 1.2 Hello", str(raised.exception))

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
            "parties": {}, "channels": {}, "trace": [],
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
    def test_descriptions_follow_active_request_and_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            (files["source"] / "a.c").write_text("\n" * 8 + "int foo(int x) {\nreturn x ? 1 : 0;\n}\n")
            model = model_document()
            model["steps"].append({**model["steps"][0], "name": "next"})
            model["step_dependencies"] = [{"prior_step": "hello", "dependent_step": "next",
                                            "condition": "initial"}]
            files["protocol"].write_text(json.dumps(model))
            state = State(files["root"] / "state")
            state.load_protocol(files["protocol"])
            state.load_handlers(files["handlers"])
            for name, args in (("describe_handler", {}), ("describe_trace_steps", {"steps": ["hello"]})):
                with self.assertRaises(StateError) as raised:
                    tools.call(state, name, args)
                self.assertEqual(raised.exception.code, "no_active_analysis")
            for request_id in ("first", "second"):
                state.register_request({
                    "request_id": request_id, "session": "s", "build": "b", "snapshot": 1,
                    "output_dir": files["out"], "snapshot_dir": files["snapshot"],
                    "model": files["protocol"], "handlers": files["handlers"],
                })
            state.begin_analysis("first")
            with self.assertRaises(StateError) as raised:
                tools.call(state, "describe_handler", {})
            self.assertEqual(raised.exception.code, "analysis_target_required")
            state.select_analysis_target("foo", "uncovered branches")
            with self.assertRaises(StateError) as raised:
                tools.call(state, "describe_handler", {})
            self.assertEqual(raised.exception.code, "not_loaded")
            data = coverage_mod.load_coverage(files["snapshot"] / "coverage.json", files["source"])
            loaded = state._store("coverage", data, "one", {"snapshot": "000001"})
            state._bind("first", "coverage", loaded)
            description = tools.call(state, "describe_handler", {})
            self.assertEqual(description["request_id"], "first")
            self.assertEqual(description["handler"]["id"], "foo")
            self.assertEqual(description["mappings"]["items"][0]["message"], "hello")
            self.assertEqual(description["coverage"]["own"]["covered_branches"], 1)
            self.assertIsNone(description["source_error"])
            self.assertEqual(description["source"]["start"], 9)
            self.assertEqual(description["source"]["end"], 11)
            self.assertEqual(description["source"]["branches"][0]["false_count"], 0)
            graph = state.txn_graph("first")
            graph.upsert_dependency("hello", "next", "verified")
            packed = tools.call(state, "describe_trace_steps", {"steps": ["hello", "next", "hello"], "limit": 2})
            self.assertEqual([step["name"] for step in packed["steps"]["items"]], ["hello", "next"])
            self.assertEqual(packed["steps"]["next_offset"], 2)
            self.assertEqual(packed["dependencies"]["items"][0]["condition"], "verified")
            self.assertEqual(list(packed["messages"]), ["hello"])
            state.begin_analysis("second")
            with self.assertRaises(StateError) as raised:
                tools.call(state, "describe_handler", {"handler_id": "foo"})
            self.assertEqual(raised.exception.code, "not_loaded")
            packed = tools.call(state, "describe_trace_steps", {"steps": ["hello", "next"]})
            self.assertEqual(packed["request_id"], "second")
            self.assertEqual(packed["dependencies"]["items"][0]["condition"], "initial")
            state.abort_analysis("second")
            with self.assertRaises(StateError) as raised:
                tools.call(state, "describe_trace_steps", {"steps": ["hello"]})
            self.assertEqual(raised.exception.code, "no_active_analysis")

    def test_target_lifecycle_and_coverage_proposal(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            model = model_document()
            model["messages"].append({"name": "next", "associated_fields": []})
            model["steps"].append({**model["steps"][0], "name": "next", "message_name": "next"})
            model["step_dependencies"] = [{"prior_step": "hello", "dependent_step": "next"}]
            files["protocol"].write_text(json.dumps(model))
            mapping = json.loads(files["handlers"].read_text())
            hello = mapping["mappings"][0]
            bar = {**hello["handlers"][0], "symbol": "bar"}
            mapping["mappings"].append({**hello, "message": "next", "step_names": ["next"],
                                        "handlers": [bar]})
            files["handlers"].write_text(json.dumps(mapping))
            other_handlers = files["root"] / "other-handlers.json"
            baz = {**bar, "symbol": "baz"}
            other_handlers.write_text(json.dumps({**mapping, "mappings": [{**hello, "handlers": [baz]}]}))
            state = State(files["root"] / "state")
            state.load_protocol(files["protocol"])
            state.load_handlers(files["handlers"])

            def tool(name, **arguments):
                return tools.call(state, name, arguments)

            def fails(code, name, **arguments):
                with self.assertRaises(StateError) as error:
                    tool(name, **arguments)
                self.assertEqual(error.exception.code, code)

            def register(request_id, handlers):
                state.register_request({
                    "request_id": request_id, "session": "s", "build": "build1",
                    "snapshot": "000001", "output_dir": files["out"] / request_id,
                    "snapshot_dir": files["snapshot"], "model": files["protocol"],
                    "handlers": handlers, "sut_parties": ["server"], "generator_parties": ["client"],
                })

            foo_target = {"handler_id": "foo", "reason": "initial coverage gap"}
            bar_target = {"handler_id": "bar", "reason": "foo's feature is blocked"}
            baz_target = {"handler_id": "baz", "reason": "next request's coverage gap"}
            for name, arguments in (("get_analysis_target", {}),
                                    ("select_analysis_target", foo_target),
                                    ("switch_analysis_target", bar_target)):
                fails("no_active_analysis", name, **arguments)
            register("req-1", files["handlers"])
            swapped = tool("load_handlers", handlers_path=str(other_handlers))
            self.assertEqual(swapped["handlers"],
                             len(json.loads(other_handlers.read_text())["mappings"]))
            # The newest mapping replaces the previous one; restore the fixture
            # mapping so the remaining target expectations still hold.
            tool("load_handlers", handlers_path=str(files["handlers"]))
            register("req-2", files["handlers"])
            tool("begin_analysis", request_id="req-1")
            self.assertEqual(tool("get_analysis_target"), {"request_id": "req-1", "target": None})
            fails("analysis_target_required", "switch_analysis_target", **bar_target)
            fails("unknown_handler", "select_analysis_target", **baz_target)
            fails("bad_request", "select_analysis_target", handler_id="foo", reason="  ")
            foo_status = {"request_id": "req-1", "target": foo_target}
            self.assertEqual(tool("select_analysis_target", **foo_target), foo_status)
            self.assertEqual(tool("select_analysis_target", **foo_target), foo_status)
            fails("target_already_selected", "select_analysis_target", **bar_target)
            fails("bad_request", "switch_analysis_target", **foo_target)
            fails("unknown_handler", "switch_analysis_target", **baz_target)
            bar_status = {"request_id": "req-1", "target": bar_target}
            self.assertEqual(tool("switch_analysis_target", **bar_target), bar_status)
            tool("begin_analysis", request_id="req-1")
            self.assertEqual(tool("get_analysis_target"), bar_status)

            tool("upsert_dependency", request_id="req-1", prior_step="hello", dependent_step="next",
                 condition="verified prerequisite")
            tool("begin_analysis", request_id="req-2")
            self.assertEqual(tool("get_analysis_target"), {"request_id": "req-2", "target": None})
            fails("analysis_target_required", "submit_proposal", request_id="req-2",
                  parties={}, channels={}, trace=[])
            fails("unknown_handler", "select_analysis_target", **baz_target)
            req2_status = {"request_id": "req-2", "target": foo_target}
            self.assertEqual(tool("select_analysis_target", **foo_target), req2_status)

            proposal = {
                "request_id": "req-1",
                "goal": {"handler_ids": ["bar"], "reason": "reach bar after hello"},
                "parties": {"client_1": "client", "server_1": "server"},
                "channels": {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}},
                "trace": [{"name": step, "step": step, "channel": "control_1", "sender": "client_1",
                           "receiver": "server_1", "reason": reason}
                          for step, reason in [("hello", "required prefix"), ("next", "primary target")]],
            }
            for ids in ([], ["foo"], ["baz"], ["bar", "foo"], ["bar", "bar"]):
                with self.subTest(handler_ids=ids):
                    fails("analysis_target_mismatch", "submit_proposal",
                          **{**proposal, "goal": {**proposal["goal"], "handler_ids": ids}})
            self.assertFalse((files["out"] / "req-1" / "proposal.json").exists())
            submitted = tool("submit_proposal", **proposal)
            frozen = json.loads(Path(submitted["path"]).read_text())
            self.assertEqual(frozen["goal"]["handler_ids"], ["bar"])
            self.assertEqual([step["step"] for step in frozen["trace"]], ["hello", "next"])
            self.assertEqual(frozen["dependencies"][0]["prior"], "hello")
            state.commit_analysis("req-1", submitted["proposal_sha256"])
            state.commit_analysis("req-1", submitted["proposal_sha256"])
            self.assertEqual(tool("get_analysis_target"), req2_status)
            register("req-3", files["handlers"])
            fails("already_loaded", "load_protocol", protocol_path=str(files["protocol"]))
            begun = tool("begin_analysis", request_id="req-3")
            self.assertEqual(begun["graph_revision"], 1)
            dependencies = tool("get_step_dependencies", request_id="req-3", step="next",
                                direction="before", trace_steps=["hello"])
            self.assertEqual(dependencies["before"], [
                {"step": "hello", "condition": "verified prerequisite", "field_relations": []},
            ])
            self.assertEqual(dependencies["after"], [])
            filtered = tool("get_step_dependencies", request_id="req-3", step="next",
                            trace_steps=["next"])
            self.assertEqual(filtered["before"], [])
            self.assertEqual(tool("show_step", request_id="req-3", step="next")["needs"][0]["condition"],
                             "verified prerequisite")
            state.abort_analysis("req-2")
            self.assertEqual(tool("select_analysis_target", **foo_target),
                             {"request_id": "req-3", "target": foo_target})
            second = tool("submit_proposal", **{**proposal, "request_id": "req-3",
                          "goal": {"handler_ids": ["foo"], "reason": "reach foo"}})
            fails("proposal_submitted", "switch_analysis_target", **bar_target)
            second_commit = state.commit_analysis("req-3", second["proposal_sha256"])
            self.assertEqual(second_commit["graph_revision"], 2)
            fails("no_active_analysis", "get_analysis_target")

    def test_source_paths_windows_and_overview_pages(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            compiled = root / "build" / "src" / "a.c"
            compiled.parent.mkdir(parents=True)
            compiled.write_text("int foo(int x) {\n" + "\n" * 8 + "if (x) x++;\n"
                                + "\n" * 239 + "if (x) x--;\nreturn x;\n}\n"
                                + "int short_fn(int x) {\nreturn x;\n}\n")
            source_root = root / "source"
            fallback = source_root / "src" / "a.c"
            fallback.parent.mkdir(parents=True)
            fallback.write_text("fallback\n" * 300)
            document = coverage_document(str(compiled))
            data = document["data"][0]
            data["functions"][0]["branches"].append([250, 1, 250, 2, 0, 1, 0, 0, 4])
            data["functions"].append({"name": "short_fn", "count": 1, "filenames": [str(compiled)],
                                      "branches": []})
            data["files"].append({"filename": str(root / "build" / "other" / "b.c"), "branches": []})
            coverage = coverage_mod.load_coverage_document(document, source_root)
            state = State(root / "state", source_root)
            state._store("coverage", coverage, "", {})

            def tool(name, **arguments):
                return tools.call(state, name, arguments)

            first = tool("show_overview_code_coverage", limit=1)
            last = tool("show_overview_code_coverage", limit=1, offset=first["next_offset"])
            self.assertEqual(len(first["modules"]), 1)
            self.assertEqual(last["total"], 2)
            self.assertIsNone(last["next_offset"])
            self.assertEqual(first["total_branches"], last["total_branches"])

            row = tool("show_source_coverage", function="foo", file="src/a.c",
                       around_line=245, max_lines=20)
            self.assertEqual(row["path"], str(compiled))
            self.assertEqual((row["start"], row["end"]), (233, 252))
            self.assertEqual(row["remaining"], {"before": {"start": 1, "end": 232}, "after": None})
            self.assertEqual(row["branches"][0]["line"], 250)
            with self.assertRaisesRegex(StateError, "outside source range"):
                tool("show_function_source", function="foo", around_line=253)
            row = tool("show_function_source", function="short_fn")
            self.assertEqual((row["start"], row["end"]), (253, 255))
            self.assertEqual(row["remaining"], {"before": None, "after": None})

            compiled.unlink()
            with self.assertRaisesRegex(StateError, "compiled source is missing"):
                tool("show_source_coverage", file=str(fallback), start_line=10, end_line=10)
            with self.assertRaisesRegex(StateError, "compiled source is missing"):
                tool("show_function_source", function="foo")
            # Relative coverage paths use only the configured compiled root.
            self.assertEqual(tools._source_path(source_root, "src/a.c"), fallback)
            fallback.unlink()
            for directory in ("left", "right"):
                candidate = source_root / directory / "a.c"
                candidate.parent.mkdir()
                candidate.touch()
            with self.assertRaisesRegex(StateError, "compiled source is missing"):
                tool("show_function_source", function="foo")

    def test_service_loads_queries_and_commits(self):
        with tempfile.TemporaryDirectory() as raw:
            files = fixture(raw)
            (files["source"] / "a.c").write_text("\n" * 8 + "int foo(int x) {\nreturn x ? 1 : 0;\n}\n")
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
                def call(payload, error_code=None):
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
                    if error_code is not None:
                        self.assertFalse(reply["ok"], reply)
                        self.assertEqual(reply["error"]["code"], error_code)
                        return
                    self.assertTrue(reply.get("ok"), reply)
                    return reply["result"]

                def tool(name, arguments):
                    from jsonschema import Draft202012Validator
                    result = call({"op": "tool", "name": name, "arguments": arguments})
                    definition = next(row for row in tools.mcp_tool_list() if row["name"] == name)
                    Draft202012Validator(definition["outputSchema"]).validate(result)
                    return result

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
                tool("load_protocol", {"protocol_path": str(files["protocol"])})
                tool("load_handlers", {"handlers_path": str(files["handlers"])})
                call({"op": "tool", "name": "load_cfg", "arguments": {
                    "cfg_dir": str(files["root"] / "missing"),
                }}, error_code="incompatible_data")
                tool("load_cfg", {"cfg_dir": str(files["cfg"])})
                tool("load_callgraph", {"callgraph_dot": str(files["callgraph"])})
                for name, arguments in (
                    ("load_protocol", {"protocol_path": str(files["protocol"])}),
                    ("load_cfg", {"cfg_dir": str(files["cfg"])}),
                    ("load_callgraph", {"callgraph_dot": str(files["callgraph"])}),
                ):
                    call({"op": "tool", "name": name, "arguments": arguments},
                         error_code="already_loaded")
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
                for name, arguments in (
                    ("get_loaded_state", {}),
                    ("show_module_code_coverage", {}),
                    ("show_file_code_coverage", {}),
                    ("show_file_code_coverage", {"file": "a.c"}),
                    ("show_func_code_coverage", {}),
                    ("show_func_code_coverage", {"function": "foo"}),
                    ("get_reachable_functions", {"function": "foo"}),
                    ("get_reaching_handlers", {"function": "foo"}),
                    ("show_source_coverage", {"function": "foo"}),
                    ("get_protocol_steps", {}),
                    ("list_parties", {}),
                    ("list_messages", {}),
                    ("show_message", {"message": "hello"}),
                    ("show_step", {"step": "hello"}),
                ):
                    with self.subTest(name=name, arguments=arguments):
                        tool(name, arguments)
                tool("begin_analysis", {"request_id": "req-1"})
                tool("select_analysis_target", {"handler_id": handlers[0]["handler"]["id"],
                                                "reason": "reach foo"})
                tool("get_step_dependencies", {"request_id": "req-1", "step": "hello"})
                tool("update_step_annotations", {"request_id": "req-1", "step": "hello",
                                                  "annotations": ["checked"]})
                tool("describe_trace_steps", {"steps": ["hello"]})
                tool("describe_handler", {})
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
                               "reason": "target", "constraints": [],
                               "mutable_fields": [{"field": "username", "kind": "string",
                                                   "suggestion": "empty",
                                                   "evidence": "a.c:10"}]}],
                })
                self.assertEqual(proposal["request_id"], "req-1")
                self.assertNotIn("proposal_id", proposal)
                path = files["out"] / "proposal.json"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                committed = call({"op": "commit_analysis", "request_id": "req-1",
                                  "proposal_sha256": digest})
                self.assertEqual(committed["graph_revision"], 1)
                call({"op": "tool", "name": "get_analysis_target"}, error_code="no_active_analysis")
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
            (files["source"] / "a.c").write_text("\n" * 8 + "int foo(int x) {\nreturn x ? 1 : 0;\n}\n")
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
                        await client.call_tool("load_protocol", {"protocol_path": str(files["protocol"])})
                        loaded = await client.call_tool("load_coverage_json", {
                            "request_id": "req-1",
                            "snapshot_dir": str(files["snapshot"]),
                        })
                        overview = await client.call_tool(
                            "show_overview_code_coverage", {})
                        target = await client.call_tool("get_analysis_target", {})
                        invalid = await client.call_tool("list_parties", {"unexpected": True})
                        parties = await client.call_tool("list_parties", {})
                        source = await client.call_tool("show_function_source", {"function": "foo"})
                        bundle = await client.call_tool("show_function_source", {"functions": ["foo"], "whole": True})
                        await client.call_tool("load_handlers", {"handlers_path": str(files["handlers"])})
                        await client.call_tool("begin_analysis", {"request_id": "req-1"})
                        await client.call_tool("select_analysis_target", {"handler_id": "foo", "reason": "test"})
                        described = await client.call_tool("describe_handler", {})
                        submitted = await client.call_tool("submit_proposal", {
                            "request_id": "req-1", "goal": {"handler_ids": ["foo"], "reason": "test"},
                            "parties": {"client_1": "client", "server_1": "server"},
                            "channels": {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}},
                            "trace": [{"name": "hello_once", "step": "hello", "channel": "control_1",
                                       "sender": "client_1", "receiver": "server_1", "reason": "test"}],
                        })
                        return listed, loaded, overview, target, invalid, parties, source, bundle, described, submitted

                listed, loaded, overview, target, invalid, parties, source, bundle, described, submitted = asyncio.run(
                    asyncio.wait_for(drive(), 40))
                names = {tool.name for tool in listed.tools}
                self.assertTrue(all(tool.output_schema for tool in listed.tools))
                self.assertTrue(invalid.is_error)
                self.assertEqual(json.loads(invalid.content[0].text)["code"], "bad_request")
                self.assertEqual(parties.structured_content, json.loads(parties.content[0].text))
                self.assertFalse(source.is_error, source)
                self.assertEqual(source.structured_content, json.loads(source.content[0].text))
                self.assertFalse(bundle.is_error, bundle)
                self.assertEqual(bundle.structured_content, json.loads(bundle.content[0].text))
                self.assertIn("functions", bundle.structured_content)
                self.assertFalse(described.is_error, described)
                self.assertEqual(described.structured_content, json.loads(described.content[0].text))
                self.assertFalse(submitted.is_error, submitted)
                self.assertEqual(submitted.structured_content, json.loads(submitted.content[0].text))
                for required in ("load_coverage_json", "begin_analysis",
                                 "show_overview_code_coverage", "submit_proposal",
                                 "select_analysis_target", "get_analysis_target", "switch_analysis_target",
                                 "describe_handler", "describe_trace_steps"):
                    self.assertIn(required, names)
                self.assertTrue(target.is_error, target)
                self.assertEqual(json.loads(target.content[0].text)["code"], "no_active_analysis")
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
