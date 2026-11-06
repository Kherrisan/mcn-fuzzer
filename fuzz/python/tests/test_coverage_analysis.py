import json
import tempfile
import unittest
from pathlib import Path

from coverage_analysis.graphs import export_graphs, load_graphs, read_graphs
from coverage_analysis.manifest import load_manifest, source_digest, write_manifest


class CoverageAnalysisTests(unittest.TestCase):
    def test_manifest_has_only_the_four_fields_and_changes_with_graphs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "fftp"
            source_dir = root / "src"
            source_dir.mkdir()
            (source_dir / "b.c").write_bytes(b"b")
            (source_dir / "a.c").write_bytes(b"a")
            graph = root / "callgraph.dot"
            binary.write_bytes(b"bin")
            graph.write_text("digraph {}")
            path = root / "manifest.json"
            write_manifest(path, binary, source_dir, root, ["callgraph.dot"])
            payload = load_manifest(path)
            self.assertEqual(set(payload), {"version", "binary_sha256", "source_sha256", "graphs_sha256"})
            self.assertEqual(payload["source_sha256"], source_digest(source_dir))
            other = root / "other"
            other.mkdir()
            (other / "a.c").write_bytes(b"a")
            (other / "b.c").write_bytes(b"b")
            self.assertEqual(source_digest(other), payload["source_sha256"])
            (source_dir / "a.c").write_bytes(b"changed")
            self.assertNotEqual(source_digest(source_dir), payload["source_sha256"])
            first = payload["graphs_sha256"]
            graph.write_text("digraph { changed }")
            write_manifest(path, binary, source_dir, root, ["callgraph.dot"])
            self.assertNotEqual(load_manifest(path)["graphs_sha256"], first)

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

    def test_separate_json_graphs_round_trip_and_reject_build_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cfg = root / "cfg"
            cfg.mkdir()
            (root / "source").mkdir()
            (root / "binary").write_bytes(b"binary")
            dot = root / "callgraph.dot"
            dot.write_text('digraph { a [label="{f}"]; }')
            (cfg / "f.dot").write_text('digraph { a -> b; }')
            manifest = write_manifest(root / "manifest.json", root / "binary", root / "source",
                                      root, ["callgraph.dot", "cfg/f.dot"])
            expected = export_graphs(dot, cfg, root / "manifest.json", root / "graphs")
            calls, cfgs = root / "graphs/callgraph.json", root / "graphs/cfg.json"
            self.assertEqual(load_graphs(calls, cfgs, manifest), expected)
            self.assertIsInstance(json.loads(calls.read_text())["functions"], list)
            self.assertIsInstance(json.loads(cfgs.read_text())["functions"], dict)
            dot.unlink()
            (cfg / "f.dot").unlink()
            self.assertEqual(load_graphs(calls, cfgs, manifest), expected)
            stale = json.loads(cfgs.read_text())
            stale["graphs_sha256"] = "another-build"
            cfgs.write_text(json.dumps(stale))
            with self.assertRaisesRegex(ValueError, "graphs_sha256"):
                load_graphs(calls, cfgs, manifest)


if __name__ == "__main__":
    unittest.main()
