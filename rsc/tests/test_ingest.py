"""Tests for the ingest stage: TXT/HTML/XML structure, file and directory mode."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rsc.cli import _validator, main
from rsc.ingest import (
    IngestOptions,
    ingest_path,
    parse_html,
    parse_txt,
    parse_xml,
    validate_document,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rfc"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _schema_validator(stage: str):
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return None
    return _validator(stage, SCHEMAS)


class TxtParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read_fixture("sample-rfc.txt")
        self.result = parse_txt(
            self.text,
            source_path="sample-rfc.txt",
            doc_id="rfc9999",
        )
        self.document = self.result.document

    def test_metadata(self) -> None:
        meta = self.document["meta"]
        self.assertEqual(meta["rfc"], "RFC 9999")
        self.assertEqual(meta["title"], "Sample Protocol Specification")
        self.assertEqual(meta["source_id"], "rfc9999")
        self.assertEqual(meta["format"], "txt")
        self.assertEqual(meta["published"], "January 2026")
        self.assertEqual(meta["input_set"], ["RFC 9999"])

    def test_section_tree(self) -> None:
        sections = self.document["sections"]
        self.assertEqual(
            [section["path"] for section in sections],
            [["1"], ["2"], ["2", "1"]],
        )
        by_id = {section["section_id"]: section for section in sections}
        child = by_id["sec-2-1"]
        self.assertEqual(child["parent_section_id"], "sec-2")
        self.assertEqual(child["level"], 2)

    def test_block_types_and_content(self) -> None:
        blocks = self.document["blocks"]
        types = [block["block_type"] for block in blocks]
        for expected in ("heading", "paragraph", "list_item", "table", "grammar", "diagram"):
            self.assertIn(expected, types)
        joined = " ".join(
            block["text"]
            for block in blocks
            if block["block_type"] == "paragraph" and "client MUST" in block["text"]
        )
        self.assertIn("contains a field", joined)
        self.assertIn("defined as follows", joined)
        self.assertFalse(any("[Page " in block["text"] for block in blocks))
        self.assertFalse(any("RFC 9999" in block["text"] for block in blocks))

    def test_list_lead_in(self) -> None:
        items = [
            block for block in self.document["blocks"] if block["block_type"] == "list_item"
        ]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["lead_in_block_id"] for item in items))
        self.assertEqual(items[0]["lead_in_block_id"], items[1]["lead_in_block_id"])
        self.assertEqual(items[0]["text"], "The first value.")
        self.assertEqual(items[1]["text"], "The second value.")

    def test_artifacts_removed(self) -> None:
        removed = self.result.report["removed"]
        self.assertGreaterEqual(removed["headers"], 2)
        self.assertGreaterEqual(removed["footers"], 2)
        self.assertGreaterEqual(removed["toc"], 1)
        self.assertGreaterEqual(removed["page_break"], 1)
        self.assertEqual(self.result.report["unmapped_lines"], 0)

    def test_span_map(self) -> None:
        block_ids = {block["block_id"] for block in self.document["blocks"]}
        self.assertEqual(len(self.document["span_map"]), len(block_ids))
        self.assertTrue(
            all(entry["block_id"] in block_ids for entry in self.document["span_map"])
        )

    def test_structural_validation(self) -> None:
        errors = validate_document(
            self.document,
            source_text=self.text,
            line_count=len(self.text.splitlines()),
        )
        self.assertEqual(errors, [])

    def test_schema_validation(self) -> None:
        validator = _schema_validator("document")
        if validator is None:
            self.skipTest("jsonschema is not installed")
        errors = list(validator.iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_indented_subsection_headings(self) -> None:
        text = (
            "1. Introduction\n"
            "\n"
            "   Body text.\n"
            "\n"
            "   1.1 Subsection\n"
            "\n"
            "   Subsection text.\n"
            "\n"
            "   1.2 Another\n"
            "\n"
            "   Another text.\n"
            "\n"
            "2. Next\n"
            "\n"
            "   Next text.\n"
        )
        result = parse_txt(
            text,
            source_path="indented.txt",
            doc_id="indented",
        )
        self.assertEqual(
            [section["path"] for section in result.document["sections"]],
            [["1"], ["1", "1"], ["1", "2"], ["2"]],
        )
        self.assertEqual(
            validate_document(
                result.document, source_text=text, line_count=len(text.splitlines())
            ),
            [],
        )

    def test_duplicate_section_path_is_a_warning(self) -> None:
        text = (
            "1. Introduction\n"
            "\n"
            "   Body text.\n"
            "\n"
            "   1.1 Duplicate\n"
            "\n"
            "   First.\n"
            "\n"
            "   1.1 Duplicate Again\n"
            "\n"
            "   Second.\n"
        )
        result = parse_txt(
            text,
            source_path="duplicate.txt",
            doc_id="duplicate",
        )
        self.assertEqual(
            validate_document(
                result.document, source_text=text, line_count=len(text.splitlines())
            ),
            [],
        )
        self.assertTrue(
            any("duplicate section path" in warning for warning in result.report["warnings"])
        )


class Tls13FixtureTests(unittest.TestCase):
    """RFC 8446 (TLS 1.3) is kept as a full-text regression fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = FIXTURES / "rfc8446.txt"
        cls.text = cls.path.read_text(encoding="utf-8")
        cls.result = parse_txt(
            cls.text,
            source_path="rfc8446.txt",
            doc_id="rfc8446",
        )
        cls.document = cls.result.document

    def test_metadata(self) -> None:
        meta = self.document["meta"]
        self.assertEqual(meta["rfc"], "RFC 8446")
        self.assertEqual(
            meta["title"],
            "The Transport Layer Security (TLS) Protocol Version 1.3",
        )
        self.assertEqual(meta["series"], "Standards Track")
        self.assertEqual(meta["published"], "August 2018")
        self.assertEqual(meta["updates"], ["RFC 5705", "RFC 6066"])
        self.assertEqual(meta["obsoletes"], ["RFC 5077", "RFC 5246", "RFC 6961"])
        self.assertEqual(meta["input_set"], ["RFC 8446"])

    def test_key_update_section(self) -> None:
        sections = {tuple(section["path"]): section for section in self.document["sections"]}
        section = sections[("4", "6", "3")]
        self.assertEqual(section["title"], "Key and Initialization Vector Update")
        self.assertEqual(section["parent_section_id"], "sec-4-6")
        paragraphs = [
            block
            for block in self.document["blocks"]
            if block["section_path"] == ["4", "6", "3"]
            and "request_update" in block["text"]
            and "MUST send a KeyUpdate" in block["text"]
        ]
        self.assertEqual(len(paragraphs), 1)

    def test_page_artifacts_removed(self) -> None:
        removed = self.result.report["removed"]
        self.assertGreater(removed["headers"], 0)
        self.assertGreater(removed["footers"], 0)
        self.assertGreater(removed["toc"], 0)
        self.assertFalse(
            any("[Page " in block["text"] for block in self.document["blocks"])
        )
        self.assertEqual(self.result.report["unmapped_lines"], 0)

    def test_structure(self) -> None:
        self.assertGreaterEqual(len(self.document["sections"]), 80)
        self.assertGreaterEqual(len(self.document["blocks"]), 1000)
        self.assertEqual(
            validate_document(
                self.document,
                source_text=self.text,
                line_count=len(self.text.splitlines()),
            ),
            [],
        )

    def test_schema_validation(self) -> None:
        validator = _schema_validator("document")
        if validator is None:
            self.skipTest("jsonschema is not installed")
        errors = list(validator.iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_ingest_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = ingest_path(self.path, IngestOptions(out_dir=out))
            self.assertEqual(result.exit_code(), 0)
            self.assertEqual(result.documents[0].doc_id, "rfc8446")
            self.assertEqual(result.documents[0].status, "ok")
            document_path = out / "rfc8446" / "document.json"
            self.assertTrue(document_path.is_file())
            batch = json.loads((out / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(batch["summary"]["ok"], 1)
            validator = _schema_validator("document")
            if validator is not None:
                document = json.loads(document_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    [error.message for error in validator.iter_errors(document)],
                    [],
                )


class MarkupParserTests(unittest.TestCase):
    def test_html_definition_list(self) -> None:
        text = (
            "<html><body><h1>1. Terms</h1>"
            "<dl><dt>Client</dt><dd>An endpoint that sends requests.</dd></dl>"
            "</body></html>"
        )
        result = parse_html(
            text,
            source_path="terms.html",
            doc_id="terms",
        )
        paragraphs = [
            block["text"]
            for block in result.document["blocks"]
            if block["block_type"] == "paragraph"
        ]
        self.assertEqual(
            paragraphs, ["Client", "An endpoint that sends requests."]
        )
        self.assertEqual(
            validate_document(
                result.document, source_text=text, line_count=len(text.splitlines())
            ),
            [],
        )

    def test_html(self) -> None:
        text = _read_fixture("sample-rfc.html")
        result = parse_html(
            text,
            source_path="sample-rfc.html",
            doc_id="sample",
        )
        document = result.document
        self.assertEqual(document["meta"]["title"], "HTML Sample Protocol")
        self.assertEqual(
            [section["path"] for section in document["sections"]],
            [["introduction"], ["introduction", "message"]],
        )
        types = [block["block_type"] for block in document["blocks"]]
        for expected in ("heading", "paragraph", "list_item", "grammar", "table"):
            self.assertIn(expected, types)
        items = [block for block in document["blocks"] if block["block_type"] == "list_item"]
        self.assertEqual(items[0]["lead_in_block_id"], items[1]["lead_in_block_id"])
        self.assertEqual(
            validate_document(
                document, source_text=text, line_count=len(text.splitlines())
            ),
            [],
        )

    def test_xml(self) -> None:
        text = _read_fixture("sample-rfc.xml")
        result = parse_xml(
            text,
            source_path="sample-rfc.xml",
            doc_id="rfc9999",
        )
        document = result.document
        self.assertEqual(document["meta"]["title"], "XML Sample Protocol")
        self.assertEqual(document["meta"]["rfc"], "RFC 9999")
        self.assertEqual(
            [section["path"] for section in document["sections"]],
            [["introduction"], ["introduction", "message"]],
        )
        self.assertEqual(
            validate_document(
                document, source_text=text, line_count=len(text.splitlines())
            ),
            [],
        )


class IngestPipelineTests(unittest.TestCase):
    def test_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = ingest_path(
                FIXTURES / "sample-rfc.txt",
                IngestOptions(out_dir=out),
            )
            self.assertEqual(result.exit_code(), 0)
            self.assertEqual(len(result.documents), 1)
            self.assertEqual(result.documents[0].status, "ok")
            self.assertTrue((out / "rfc9999" / "document.json").is_file())
            self.assertTrue((out / "rfc9999" / "ingest-report.json").is_file())
            batch = json.loads((out / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(batch["summary"], {
                "total": 1,
                "ok": 1,
                "failed": 0,
                "skipped": 0,
                "unchanged": 0,
            })
            validator = _schema_validator("batch")
            if validator is not None:
                self.assertEqual(list(validator.iter_errors(batch)), [])

    def test_directory_and_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            shutil.copy(FIXTURES / "sample-rfc.txt", corpus / "rfc9999.txt")
            shutil.copy(FIXTURES / "sample-rfc.html", corpus / "sample.html")
            nested = corpus / "nested"
            nested.mkdir()
            shutil.copy(FIXTURES / "sample-rfc.xml", nested / "sample.xml")
            # Same RFC number and different paths -> path-suffixed doc_id.
            variant = (FIXTURES / "sample-rfc.txt").read_text(encoding="utf-8")
            (corpus / "variant.txt").write_text(
                variant.replace("fixture for RSC", "variant for RSC"),
                encoding="utf-8",
            )

            out = root / "out"
            result = ingest_path(
                corpus,
                IngestOptions(out_dir=out, recursive=True),
            )
            self.assertEqual(result.exit_code(), 0)
            self.assertEqual(len(result.documents), 4)
            self.assertTrue(all(item.status == "ok" for item in result.documents))
            doc_ids = [item.doc_id for item in result.documents]
            self.assertEqual(len(set(doc_ids)), 4)
            self.assertTrue(any(doc_id.startswith("rfc9999-") for doc_id in doc_ids))
            for item in result.documents:
                self.assertIsNotNone(item.document_path)
                assert item.document_path is not None
                document = json.loads(item.document_path.read_text(encoding="utf-8"))
                errors = validate_document(
                    document,
                    source_text=item.input_path.read_text(encoding="utf-8"),
                    line_count=len(item.input_path.read_text(encoding="utf-8").splitlines()),
                )
                self.assertEqual(errors, [], item.doc_id)
                validator = _schema_validator("document")
                if validator is not None:
                    self.assertEqual(
                        [error.message for error in validator.iter_errors(document)],
                        [],
                    )

    def test_non_recursive_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            shutil.copy(FIXTURES / "sample-rfc.txt", corpus / "rfc9999.txt")
            nested = corpus / "nested"
            nested.mkdir()
            shutil.copy(FIXTURES / "sample-rfc.xml", nested / "sample.xml")

            flat = ingest_path(corpus, IngestOptions(out_dir=root / "flat"))
            self.assertEqual(len(flat.documents), 1)
            deep = ingest_path(
                corpus,
                IngestOptions(out_dir=root / "deep", recursive=True),
            )
            self.assertEqual(len(deep.documents), 2)

    def test_rerun_reparses_and_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            options = IngestOptions(out_dir=out)
            first = ingest_path(FIXTURES / "sample-rfc.txt", options)
            self.assertEqual(first.documents[0].status, "ok")
            second = ingest_path(FIXTURES / "sample-rfc.txt", options)
            self.assertEqual(second.documents[0].status, "ok")
            forced = ingest_path(
                FIXTURES / "sample-rfc.txt",
                IngestOptions(out_dir=out, force=True),
            )
            self.assertEqual(forced.documents[0].status, "ok")

    def test_include_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            shutil.copy(FIXTURES / "sample-rfc.txt", corpus / "rfc9999.txt")
            shutil.copy(FIXTURES / "sample-rfc.html", corpus / "sample.html")

            included = ingest_path(
                corpus,
                IngestOptions(out_dir=root / "included", include=("*.txt",)),
            )
            self.assertEqual(
                [item.relative_path for item in included.documents], ["rfc9999.txt"]
            )
            excluded = ingest_path(
                corpus,
                IngestOptions(out_dir=root / "excluded", exclude=("*.html",)),
            )
            self.assertEqual(
                [item.relative_path for item in excluded.documents], ["rfc9999.txt"]
            )

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = ingest_path(
                FIXTURES / "sample-rfc.txt",
                IngestOptions(out_dir=out, dry_run=True),
            )
            self.assertEqual(result.documents[0].status, "planned")
            self.assertIsNone(result.batch_path)
            self.assertFalse(out.exists())


class IngestCliTests(unittest.TestCase):
    def test_cli_ingest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    ["ingest", str(FIXTURES / "sample-rfc.txt"), "--out", str(out)]
                )
            self.assertEqual(code, 0)
            self.assertIn("1 document(s)", stdout.getvalue())
            self.assertTrue((out / "batch.json").is_file())

    def test_cli_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["ingest", str(Path(tmp) / "missing.txt"), "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertIn("does not exist", stderr.getvalue())

    def test_cli_rejects_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "rfc.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["ingest", str(pdf), "--out", str(Path(tmp) / "out")])
            self.assertEqual(code, 2)
            self.assertIn("PDF ingest is not implemented", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
