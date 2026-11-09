import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coverage_analysis.graphs import read_graphs
from svf_to_llvm import icfg_identity, merge_callgraph, merge_icfg


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def fixture(self, text):
        path = self.root / "input.dot"
        path.write_text(text)
        return path

    def test_fold_calls_deduplicate_edges_drop_auxiliaries_keep_real_self_loop(self):
        mapping = {}
        descriptions = [(0, "global", None, None, False),
                        (1, "entry", "f", None, False),
                        (2, "intra", "f", ("f", 1), True),
                        (3, "call", "f", ("f", 1), False),
                        (4, "return", "f", ("f", 1), False),
                        (5, "intra", "f", ("f", 2), True),
                        (6, "intra", "f", ("f", 2), False),
                        (7, "exit", "f", None, False),
                        (8, "entry", "g", None, False),
                        (9, "exit", "g", None, False),
                        (10, "intra", "f", None, False)]
        lines = ["digraph ICFG {"]
        for ident, kind, symbol, owner, entry in descriptions:
            mapping[ident] = {"kind": kind, "function": symbol, "owner": owner,
                              "block_entry": entry}
            label = f"{ident} {kind}" + (f"\n{symbol}" if symbol else "")
            lines.append(f'n{ident} [label={json.dumps(label)}, kind="{kind}"];')
        edges = [(0, 1, "intra"), (1, 2, "intra"), (2, 3, "intra"),
                 (3, 8, "call"), (9, 4, "return"), (4, 5, "intra"),
                 (4, 5, "intra"), (5, 6, "intra"), (6, 5, "intra"),
                 (6, 10, "intra"), (10, 7, "intra")]
        lines.extend(f'n{a} -> n{b} [kind="{kind}"];' for a, b, kind in edges)
        lines.append("}")
        functions = {"f": {"blocks": [{"name": "entry"}, {"name": "loop"}],
                            "edges": {(1, 2), (2, 2)}}}
        path = self.fixture("\n".join(lines))
        merged, calls, stats = merge_icfg(path, mapping, functions)
        self.assertEqual(merged["f"], {(1, 2), (2, 2)})
        self.assertEqual(calls, [(3, "f", 1, "g", "call")])
        self.assertEqual(stats["discarded_auxiliary_nodes"], 6)
        self.assertEqual(stats["duplicate_projected_edges"], 1)
        # An internal instruction edge must not fabricate a missing loop.
        path.write_text(path.read_text().replace('n6 -> n5 [kind="intra"];', ""))
        with self.assertRaisesRegex(ValueError, "Merged CFG differs"):
            merge_icfg(path, mapping, functions)

    def test_callgraph_preserves_indirect_kind_and_removes_model_functions(self):
        path = self.fixture('''digraph CallGraph {
n1 [label="f"];
n2 [label="g"];
n3 [label="svf.model"];
n1 -> n2 [kind="direct"];
n1 -> n2 [kind="direct"];
n1 -> n2 [kind="direct+indirect"];
n1 -> n3 [kind="direct"];
}
''')
        symbols, edges, stats = merge_callgraph(path, {"f", "g"})
        self.assertEqual(symbols, ["f", "g"])
        self.assertEqual(edges, [("f", "g", "direct"), ("f", "g", "indirect")])
        self.assertEqual(stats["discarded_auxiliary_nodes"], 1)

    def test_native_node_and_callgraph_labels(self):
        self.assertEqual(icfg_identity("Node0xabc", {"label": r'{CallICFGNode17 {fun: f {line: 7}}\l}'}),
                         (17, "call", "f"))
        path = self.fixture('''digraph CallGraph {
Node0x1 [shape=record,label="{CallGraphNode ID: 1 {fun: f}}"];
Node0x2 [shape=record,label="{CallGraphNode ID: 2 {fun: g}}"];
Node0x1 -> Node0x2 [color=red];
}
''')
        self.assertEqual(merge_callgraph(path, {"f", "g"})[1], [("f", "g", "indirect")])


class ConversionIntegrationTests(unittest.TestCase):
    def test_real_pta_to_existing_graph_reader_and_mismatched_bitcode_rejection(self):
        clang = Path.home() / "SVF/llvm-21.1.0.obj/bin/clang"
        if not clang.is_file():
            self.skipTest("LLVM 21 clang is needed for the end-to-end test")
        scripts = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "test.c"
            source.write_text('''
static int target(int n) { if (n > 3) return 3; return n + 1; }
int (*volatile callback)(int) = target;
int main(int argc, char **argv) {
    int n = callback(argc);
    while (n < 10) { n = target(n); if (n == 3) break; }
    return target(n) + target(argc);
}
''')
            bitcode, graphs, output = root / "test.bc", root / "svf", root / "converted"
            env = {**os.environ, "PYTHONPATH": str(scripts)}

            def run(*args, check=True):
                result = subprocess.run([str(arg) for arg in args], env=env,
                                        capture_output=True, text=True)
                if check:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            run(clang, "-g", "-O0", "-emit-llvm", "-c", source, "-o", bitcode)
            run(sys.executable, scripts / "svf_pta_graphs.py", bitcode,
                "--output-dir", graphs, "--pta", "steensgaard")
            arguments = [sys.executable, scripts / "svf_to_llvm.py", "--bitcode", bitcode,
                         "--icfg", graphs / "icfg.post-pta.dot", "--callgraph",
                         graphs / "callgraph.post-pta.dot", "--output-dir", output]
            run(*arguments)
            parsed = read_graphs(output / "callgraph.dot", output / "cfg")
            main = parsed["cfgs"]["main"]
            self.assertEqual(main["entry"], "entry")
            self.assertGreater(len(main["blocks"]), 3)
            self.assertTrue(any(block["locations"] for block in main["blocks"].values()))
            self.assertTrue(any(call["target"] == "target" for block in main["blocks"].values()
                                for call in block["calls"]))
            self.assertIn("indirect", {edge["kind"] for edge in parsed["edges"]})
            self.assertTrue(json.loads((output / "conversion.json").read_text())[
                "all_cfg_edges_verified_against_original_llvm"])
            native = root / "native"
            run(sys.executable, scripts / "svf_pta_graphs.py", bitcode,
                "--output-dir", native, "--pta", "steensgaard", "--dot-style", "native")
            native_output = root / "native-converted"
            run(sys.executable, scripts / "svf_to_llvm.py", "--bitcode", bitcode,
                "--icfg", native / "icfg.post-pta.dot", "--callgraph",
                native / "callgraph.post-pta.dot", "--output-dir", native_output)
            native_graphs = read_graphs(native_output / "callgraph.dot", native_output / "cfg")
            self.assertEqual(native_graphs, parsed)
            bitcode.write_bytes(bitcode.read_bytes() + b"changed")
            arguments[-1] = root / "rejected"
            rejected = run(*arguments, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("bitcode hash mismatch", rejected.stderr)
            self.assertFalse((root / "rejected").exists())


if __name__ == "__main__":
    unittest.main()
