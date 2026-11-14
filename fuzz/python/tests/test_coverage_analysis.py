import json
import tempfile
import unittest
from pathlib import Path

from coverage_analysis.graphs import export_graphs, load_graphs, read_graphs


class CoverageAnalysisTests(unittest.TestCase):
    def test_reads_current_dot_without_writing_an_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dot = root / "callgraph.dot"
            cfg = root / "cfg"
            cfg.mkdir()
            (cfg / "handler.dot").write_text('digraph { entry -> exit; }\n')
            dot.write_text('digraph { a [label="{handler}"]; b [label="{other}"]; a -> b; }\n')
            first = read_graphs(dot, cfg)
            self.assertEqual({f["symbol"] for f in first["functions"]}, {"handler", "other"})
            self.assertEqual(first["edges"], [{"source": "a", "target": "b", "kind": "direct"}])
            self.assertEqual(first["cfgs"]["handler"]["edges"], [{"src": "entry", "dst": "exit"}])
            dot.write_text('digraph { a [label="{handler}"]; }\n')
            second = read_graphs(dot, cfg)
            self.assertEqual(len(second["functions"]), 1)
            self.assertEqual(second["edges"], [])
            self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()},
                             {"callgraph.dot", "cfg/handler.dot"})

    def test_separate_json_graphs_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = root / "cfg"
            cfg.mkdir()
            dot = root / "callgraph.dot"
            dot.write_text('digraph { a [label="{f}"]; }')
            (cfg / "f.dot").write_text('digraph { a -> b; }')
            expected = export_graphs(dot, cfg, root / "graphs")
            calls, cfgs = root / "graphs/callgraph.json", root / "graphs/cfg.json"
            self.assertEqual(load_graphs(calls, cfgs), expected)
            self.assertIsInstance(json.loads(calls.read_text())["functions"], list)
            self.assertIsInstance(json.loads(cfgs.read_text())["functions"], dict)
            dot.unlink()
            (cfg / "f.dot").unlink()
            self.assertEqual(load_graphs(calls, cfgs), expected)


if __name__ == "__main__":
    unittest.main()
