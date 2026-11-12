"""Tests for the document-local structural index."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from rsc.cli import _validator, main
from rsc.index import (
    RSCIndex,
    StructuralIndexError,
    build_structural_index,
    index_summary,
    validate_index,
)
from rsc.ingest import parse_html, parse_xml
from rsc.protocol.context import Corpus

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def span(start: int, end: int) -> dict[str, object]:
    return {
        "source_id": "rfc9999",
        "start": start,
        "end": end,
        "start_line": 1,
        "end_line": 1,
    }


def make_document() -> dict[str, object]:
    sections = [
        {
            "section_id": "sec-1",
            "path": ["1"],
            "title": "Introduction",
            "level": 1,
            "span": span(0, 100),
            "parent_section_id": None,
        },
        {
            "section_id": "sec-2",
            "path": ["2"],
            "title": "Conventions",
            "level": 1,
            "span": span(100, 200),
            "parent_section_id": None,
        },
        {
            "section_id": "sec-3",
            "path": ["3"],
            "title": "Messages",
            "level": 1,
            "span": span(200, 400),
            "parent_section_id": None,
        },
        {
            "section_id": "sec-4",
            "path": ["4"],
            "title": "References",
            "level": 1,
            "span": span(400, 500),
            "parent_section_id": None,
        },
    ]

    def block(
        block_id: str,
        path: list[str],
        text: str,
        *,
        block_type: str = "paragraph",
        start: int = 0,
        end: int = 1,
    ) -> dict[str, object]:
        return {
            "block_id": block_id,
            "section_path": path,
            "block_type": block_type,
            "text": text,
            "original_span": span(start, end),
            "parent_block": None,
            "lead_in_block_id": None,
            "order": int(block_id[1:]) - 1,
            "attrs": {},
        }

    return {
        "meta": {
            "rfc": "RFC 9999",
            "title": "Synthetic Protocol",
            "source_id": "rfc9999",
            "generator": "test",
        },
        "sections": sections,
        "blocks": [
            block("b1", ["1"], "The frobnicator handshake is used to negotiate a session."),
            block(
                "b2",
                ["2"],
                'The key words "MUST", "MUST NOT", "SHOULD", and "MAY" in this '
                "document are to be interpreted as described in BCP 14 "
                "[RFC2119] [RFC8174] when, and only when, they appear in all capitals.",
                start=110,
                end=180,
            ),
            block(
                "b3",
                ["3"],
                "enum { update_not_requested(0), update_requested(1) } KeyUpdateRequest;",
                block_type="grammar",
                start=210,
                end=260,
            ),
            block(
                "b4",
                ["3"],
                "struct { KeyUpdateRequest request_update; } KeyUpdate;",
                block_type="grammar",
                start=270,
                end=320,
            ),
            block(
                "b5",
                ["3"],
                "frobnicator: Indicates whether the recipient should respond.",
                start=330,
                end=390,
            ),
        ],
        "span_map": [],
    }


class StructuralIndexTests(unittest.TestCase):
    def test_index_covers_source_blocks_and_keeps_references(self) -> None:
        document = make_document()
        index = build_structural_index(document, generated_at="x")
        self.assertEqual(set(index), {"meta", "sections", "blocks", "references"})
        self.assertEqual(index_summary(index)["blocks"], 5)
        self.assertEqual(validate_index(index, document), [])
        self.assertEqual(
            [b["block_id"] for b in index["blocks"]], [b["block_id"] for b in document["blocks"]]
        )
        self.assertEqual(
            {ref["target_rfc"] for ref in index["references"]}, {"RFC 2119", "RFC 8174", "BCP 14"}
        )

    def test_parent_and_neighbors_respect_scope_and_source_order(self) -> None:
        index = build_structural_index(make_document())
        query = RSCIndex(index)
        self.assertEqual(query.get_parent_section("b3")["section_id"], "sec-3")
        self.assertEqual(query.get_block_index("b3")["prev_block_id"], "b2")
        self.assertEqual(query.get_block_index("b3")["next_block_id"], "b4")
        self.assertEqual(
            [b["block_id"] for b in query.get_neighbor_blocks("b3", 20, 20)["before"]], ["b1", "b2"]
        )
        neighbors = query.get_neighbor_blocks("b3", 20, 20, scope="section")
        self.assertEqual(neighbors["before"], [])
        self.assertEqual([b["block_id"] for b in neighbors["after"]], ["b4", "b5"])
        self.assertEqual(query.get_neighbor_blocks("b1")["before"], [])
        self.assertEqual(query.get_neighbor_blocks("b5")["after"], [])
        self.assertEqual(query.get_neighbor_blocks("b3", 0, 0), {"before": [], "after": []})
        with self.assertRaises(KeyError):
            query.get_parent_section("unknown")
        with self.assertRaises(ValueError):
            query.get_neighbor_blocks("b3", before=-1)
        with self.assertRaises(ValueError):
            query.get_neighbor_blocks("b3", scope="all")

    def test_preamble_and_nested_section_ancestry(self) -> None:
        document = make_document()
        document["blocks"][0]["section_path"] = []
        document["sections"][2]["path"] = ["2", "1"]
        document["sections"][2]["parent_section_id"] = "sec-2"
        for block in document["blocks"][2:]:
            block["section_path"] = ["2", "1"]
        query = RSCIndex(build_structural_index(document))
        self.assertIsNone(query.get_parent_section("b1"))
        self.assertEqual([s["section_id"] for s in query.get_section_ancestors("sec-3")], ["sec-2"])
        self.assertEqual(query.get_neighbor_blocks("b3", scope="section")["before"], [])
        result = query.get_parent_section("b3")
        result["title"] = "changed"
        self.assertEqual(query.get_parent_section("b3")["title"], "Messages")

    def test_duplicate_ids_orders_and_unknown_paths_are_rejected(self) -> None:
        for key, value in (
            ("order", 0),
            ("order", True),
            ("block_id", "b1"),
            ("section_path", ["missing"]),
        ):
            with self.subTest(key=key, value=value):
                document = make_document()
                document["blocks"][1][key] = value
                with self.assertRaises(StructuralIndexError):
                    build_structural_index(document)

    def test_repeated_section_labels_use_source_containment(self) -> None:
        document = make_document()
        duplicate = deepcopy(document["sections"][2])
        duplicate.update(section_id="sec-3-again", span=span(330, 400))
        document["sections"][2]["span"] = span(200, 330)
        document["sections"].append(duplicate)
        query = RSCIndex(build_structural_index(document))
        self.assertEqual(query.get_parent_section("b3")["section_id"], "sec-3")
        self.assertEqual(query.get_parent_section("b5")["section_id"], "sec-3-again")
        duplicate["span"] = span(200, 400)
        with self.assertRaisesRegex(StructuralIndexError, "ambiguous section path"):
            build_structural_index(document)

    def test_validation_rejects_missing_blocks_and_broken_neighbors(self) -> None:
        document = make_document()
        index = build_structural_index(document)
        broken = deepcopy(index)
        broken["blocks"].pop()
        self.assertIn(
            "index block coverage differs from document", validate_index(broken, document)
        )
        broken = deepcopy(index)
        broken["blocks"][0]["next_block_id"] = "b3"
        self.assertTrue(any("source order" in e for e in validate_index(broken, document)))
        broken = deepcopy(index)
        broken["sections"][0]["parent_section_id"] = "sec-1"
        self.assertTrue(any("cyclic" in e for e in validate_index(broken, document)))

    def test_corpus_queries_keep_duplicate_block_ids_in_separate_documents(self) -> None:
        first = make_document()
        second = make_document()
        second["blocks"][1]["text"] = "Second document"
        corpus = Corpus({"one": first, "two": second})
        self.assertEqual(corpus.get_block_index("one", "b1")["section_id"], "sec-1")
        self.assertEqual(corpus.get_neighbor_blocks("one", "b1")["after"][0]["doc_id"], "one")
        self.assertEqual(
            corpus.get_neighbor_blocks("two", "b1")["after"][0]["text"], "Second document"
        )
        with self.assertRaises(KeyError):
            corpus.get_neighbor_blocks("missing", "b1")

    def test_cli_writes_structural_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document_path = root / "document.json"
            document_path.write_text(json.dumps(make_document()), encoding="utf-8")
            out = root / "out"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["index", str(document_path), "--out", str(out), "--force", "--json"])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertTrue((out / "structural-index.json").is_file())
            self.assertFalse((out / "index.json").exists())
            summary = json.loads(stdout.getvalue())
            expected = index_summary(build_structural_index(make_document()))
            self.assertEqual(summary["blocks"], expected["blocks"])
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["validate", "index", str(out / "structural-index.json")])
            self.assertEqual(code, 0)

    def test_empty_document(self) -> None:
        document = {
            "meta": {
                "rfc": "RFC 0000",
                "generator": "test",
            },
            "sections": [],
            "blocks": [],
            "span_map": [],
        }
        index = build_structural_index(document, generated_at="x")
        self.assertEqual(index["sections"], [])
        self.assertEqual(index["blocks"], [])
        self.assertEqual(validate_index(index, document), [])


class ProFuzzBenchStructuralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(__file__).resolve().parent / "fixtures" / "profuzzbench-rfc"
        cls.manifest = json.loads((cls.base / "manifest.json").read_text(encoding="utf-8"))
        cls.entries = {
            (protocol["protocol"], entry["rfc"]): entry
            for protocol in cls.manifest["protocols"]
            for entry in protocol["rfcs"]
        }

    def _build(self, protocol: str, rfc: str) -> dict[str, object]:
        entry = self.entries[(protocol, rfc)]
        text = (self.base / entry["file"]).read_text(encoding="utf-8")
        parse = parse_html if entry["format"] == "html" else parse_xml
        parsed = parse(
            text,
            source_path=entry["file"],
            doc_id=Path(entry["file"]).stem,
        )
        return build_structural_index(parsed.document, generated_at="x")

    def test_subset(self) -> None:
        cases = (
            ("TLS", "RFC 8446"),
            ("SIP", "RFC 3261"),
            ("DNS", "RFC 1035"),
            ("SMTP", "RFC 5321"),
        )
        for protocol, rfc in cases:
            with self.subTest(protocol=protocol, rfc=rfc):
                index = self._build(protocol, rfc)
                self.assertGreaterEqual(index_summary(index)["sections"], 1)
                self.assertEqual(validate_index(index), [])
                validator = _validator("index", SCHEMAS)
                self.assertEqual([error.message for error in validator.iter_errors(index)], [])


if __name__ == "__main__":
    unittest.main()
