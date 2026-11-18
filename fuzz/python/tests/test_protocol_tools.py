"""Protocol, source, and replay-log tools added for coverage analysis."""

import json
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from coverage_analysis import coverage as coverage_mod
from coverage_analysis import protocol as protocol_mod
from coverage_analysis import tools
from coverage_analysis.state import State, StateError

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "targets/lightftp/protocol.json"
SOURCE = ROOT / "targets/lightftp/source"
RUN = ROOT / "targets/lightftp/runs/mcn-fuzzer-20261116-1229"
SNAPSHOT = RUN / "source-coverage/snapshots/000003"
BINARY = ROOT / "targets/lightftp/build-llvm-cov/fftp"


def make_state():
    state = State(Path(tempfile.mkdtemp()), source_root=SOURCE.resolve())
    graph = protocol_mod.load_protocol(MODEL)
    graph.catalog["binding"] = {
        "Server-PI": "sut", "Server-DTP": "sut",
        "User-PI": "generator", "User-DTP": "generator",
    }
    state.latest["protocol"] = SimpleNamespace(value=graph)
    return state, graph


class ProtocolToolTests(unittest.TestCase):
    def test_list_messages_pages_and_filters_direction(self):
        state, _ = make_state()
        inbound = tools.call(state, "list_messages", {"direction": "sut_inbound", "limit": 5})
        self.assertGreater(inbound["total"], 5)
        self.assertEqual(len(inbound["items"]), 5)
        self.assertTrue(all(item["direction"] == "sut_inbound" for item in inbound["items"]))
        second = tools.call(state, "list_messages",
                            {"direction": "sut_inbound", "offset": 5, "limit": 5})
        self.assertNotEqual([i["message"] for i in inbound["items"]],
                            [i["message"] for i in second["items"]])
        every = tools.call(state, "list_messages", {"direction": "all", "limit": 50})
        self.assertEqual(every["total"], 58)

    def test_show_message_reports_fields_and_carrier(self):
        state, _ = make_state()
        row = tools.call(state, "show_message", {"message": "MLSD command"})
        self.assertEqual(row["fields"], ["pathname"])
        self.assertEqual(row["steps"][0]["step"], "Send MLSD")
        self.assertEqual(row["steps"][0]["direction"], "sut_inbound")
        with self.assertRaises(StateError):
            tools.call(state, "show_message", {"message": "no such message"})

    def test_show_step_includes_processing_and_bounded_needs(self):
        state, _ = make_state()
        row = tools.call(state, "show_step", {"step": "Send MLSD"})
        self.assertEqual(row["action"], "send")
        self.assertEqual(row["channel"], "control")
        self.assertLessEqual(len(row["needs"]), 8)
        self.assertTrue(all(len(item["condition"]) <= 160 for item in row["needs"]))
        with self.assertRaises(ValueError):
            tools.call(state, "show_step", {"step": "no such step"})

    def test_show_function_source_window_stays_bounded(self):
        document = export_snapshot()
        coverage = coverage_mod.load_coverage_document(document, SOURCE.resolve())
        state, _ = make_state()
        state.latest["coverage"] = SimpleNamespace(value=coverage)
        row = tools.call(state, "show_function_source", {"function": "ftpMLSD", "radius": 40})
        self.assertLessEqual(len(row["lines"]), 40)
        self.assertLessEqual(len(json.dumps(row)), 12000)
        self.assertEqual(row["stats"]["missed_branches"], 12)
        with self.assertRaises(StateError):
            tools.call(state, "show_function_source", {"function": "no_such_function"})

    def test_show_replay_logs_pages_and_locates_case(self):
        state, _ = make_state()
        request = SimpleNamespace(
            request_id="000003-x", snapshot=3, snapshot_dir=SNAPSHOT,
            output_dir=RUN / "source-coverage/analysis/requests/000003-18e8885e957153c5",
            session="s", build="b", model=MODEL, handlers_path=Path("x"),
            sut_parties=[], generator_parties=[], unavailable_parties=[])
        state.requests["000003-x"] = request
        first = tools.call(state, "show_replay_logs",
                           {"request_id": "000003-x", "max_lines": 30})
        self.assertEqual(first["snapshot"], "000003")
        self.assertLessEqual(len(first["lines"]), 30)
        self.assertEqual(first["lines"][0]["line"], 1)
        case = first["cases"][0]["case_id"]
        scoped = tools.call(state, "show_replay_logs",
                            {"request_id": "000003-x", "case_id": case, "max_lines": 40})
        self.assertTrue(scoped["lines"])
        self.assertTrue(all(item["line"] >= 1 for item in scoped["lines"]))
        with self.assertRaises(StateError):
            tools.call(state, "show_replay_logs",
                       {"request_id": "000003-x", "case_id": "corpus:missing"})
        with self.assertRaises(StateError):
            tools.call(state, "show_replay_logs", {"request_id": "unknown"})


def export_snapshot():
    import subprocess

    work = Path(tempfile.mkdtemp())
    profile = work / "cov.profdata"
    profile.write_bytes(zlib.decompress((SNAPSHOT / "coverage.profdata.zlib").read_bytes()))
    output = work / "cov.json"
    with output.open("w") as handle:
        subprocess.run(["llvm-cov-18", "export", str(BINARY),
                        f"-instr-profile={profile}", "-format=text"],
                       stdout=handle, check=True, stderr=subprocess.DEVNULL)
    return json.loads(output.read_text())


if __name__ == "__main__":
    unittest.main()


class FunctionSourceBundleTests(unittest.TestCase):
    def test_reads_several_whole_functions_in_one_call(self):
        document = export_snapshot()
        coverage = coverage_mod.load_coverage_document(document, SOURCE.resolve())
        state, _ = make_state()
        state.latest["coverage"] = SimpleNamespace(value=coverage)
        row = tools.call(state, "show_function_source", {
            "functions": ["ftpAPPE", "append_thread", "ftpSTOR", "stor_thread"],
            "whole": True, "max_lines": 400,
        })
        self.assertEqual([item["function"] for item in row["functions"]],
                         ["ftpAPPE", "append_thread", "ftpSTOR", "stor_thread"])
        self.assertEqual(len(row["files"]), 1)
        self.assertLessEqual(row["lines_returned"], 400)
        for item in row["functions"]:
            self.assertEqual(item["range"]["basis"], "tree_sitter")
            self.assertLessEqual(item["range"]["start"], item["lines"][0]["line"])
            self.assertGreaterEqual(item["range"]["end"], item["lines"][-1]["line"])
            self.assertEqual(item["stats"]["missed_branches"], item["stats"]["total_branches"])

    def test_budget_truncates_and_reports_skips(self):
        document = export_snapshot()
        coverage = coverage_mod.load_coverage_document(document, SOURCE.resolve())
        state, _ = make_state()
        state.latest["coverage"] = SimpleNamespace(value=coverage)
        row = tools.call(state, "show_function_source", {
            "functions": ["ftpAPPE", "append_thread", "ftpSTOR"],
            "whole": True, "max_lines": 60,
        })
        self.assertEqual(row["lines_returned"], 60)
        self.assertFalse(row["functions"][0]["truncated"])
        self.assertTrue(row["functions"][1]["truncated"])
        self.assertTrue(row["functions"][2].get("skipped"))

    def test_rejects_unknown_names_and_empty_requests(self):
        document = export_snapshot()
        coverage = coverage_mod.load_coverage_document(document, SOURCE.resolve())
        state, _ = make_state()
        state.latest["coverage"] = SimpleNamespace(value=coverage)
        with self.assertRaises(ValueError):
            tools.call(state, "show_function_source", {"functions": ["no_such_function"]})
        with self.assertRaises(StateError):
            tools.call(state, "show_function_source", {})
