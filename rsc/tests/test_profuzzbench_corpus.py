"""Regression tests for the ProFuzzBench protocol RFC corpus.

The corpus covers the 10 protocols in ProFuzzBench.  DAAP and DICOM have no
IETF RFC and are represented only as `no_rfc` manifest entries.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from rsc.cli import _validator
from rsc.index import build_structural_index, validate_index
from rsc.ingest import parse_html, parse_xml, validate_document

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "profuzzbench-rfc"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


class ProfuzzbenchCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        cls.entries = [
            entry for protocol in cls.manifest["protocols"] for entry in protocol["rfcs"]
        ]
        try:
            import jsonschema  # noqa: F401

            cls.validator = _validator("document", SCHEMAS)
        except ImportError:
            cls.validator = None

    def test_manifest_coverage(self) -> None:
        protocols = {item["protocol"]: item for item in self.manifest["protocols"]}
        self.assertEqual(
            set(protocols),
            {"DAAP", "DICOM", "DNS", "DTLS", "FTP", "RTSP", "SIP", "SMTP", "SSH", "TLS"},
        )
        self.assertEqual(protocols["DAAP"]["status"], "no_rfc")
        self.assertEqual(protocols["DICOM"]["status"], "no_rfc")
        self.assertEqual(protocols["DAAP"]["rfcs"], [])
        self.assertEqual(protocols["DICOM"]["rfcs"], [])
        self.assertEqual(len(self.entries), 28)

    def test_manifest_checksums(self) -> None:
        for entry in self.entries:
            with self.subTest(entry["file"]):
                path = FIXTURES / entry["file"]
                self.assertTrue(path.is_file(), entry["file"])
                data = path.read_bytes()
                self.assertEqual(len(data), entry["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])

    def test_all_documents_parse_and_validate(self) -> None:
        for entry in self.entries:
            with self.subTest(entry["file"]):
                path = FIXTURES / entry["file"]
                text = path.read_text(encoding="utf-8")
                parse = parse_html if entry["format"] == "html" else parse_xml
                result = parse(
                    text,
                    source_path=entry["file"],
                    input_sha256=entry["sha256"],
                    doc_id=path.stem,
                )
                document = result.document
                self.assertEqual(document["meta"]["rfc"], entry["rfc"])
                self.assertEqual(document["meta"]["format"], entry["format"])
                self.assertGreater(len(document["sections"]), 0)
                self.assertGreater(len(document["blocks"]), 0)
                self.assertEqual(
                    validate_document(
                        document,
                        source_text=text,
                        line_count=len(text.splitlines()),
                    ),
                    [],
                )
                self.assertEqual(result.report["unmapped_lines"], 0)
                index = build_structural_index(document, generated_at="corpus-regression")
                self.assertEqual(validate_index(index, document), [])
                if self.validator is not None:
                    self.assertEqual(
                        [error.message for error in self.validator.iter_errors(document)],
                        [],
                    )
                    self.assertEqual(
                        list(_validator("index", SCHEMAS).iter_errors(index)),
                        [],
                    )

    def test_legacy_pre_html_adapter(self) -> None:
        path = FIXTURES / "FTP" / "rfc959.html"
        text = path.read_text(encoding="utf-8")
        result = parse_html(
            text,
            source_path="FTP/rfc959.html",
            input_sha256="0" * 64,
            doc_id="rfc959",
        )
        self.assertEqual(result.report["html_mode"], "legacy-pre")
        self.assertEqual(result.document["meta"]["format"], "html")
        self.assertEqual(len(result.document["sections"]), 46)
        self.assertGreater(len(result.document["blocks"]), 600)

    def test_modern_html_adapter(self) -> None:
        path = FIXTURES / "DTLS" / "rfc9147.html"
        text = path.read_text(encoding="utf-8")
        result = parse_html(
            text,
            source_path="DTLS/rfc9147.html",
            input_sha256="0" * 64,
            doc_id="rfc9147",
        )
        self.assertNotIn("html_mode", result.report)
        self.assertGreater(len(result.document["sections"]), 100)

    def test_rfcxml_adapter(self) -> None:
        path = FIXTURES / "DTLS" / "rfc9147.xml"
        text = path.read_text(encoding="utf-8")
        result = parse_xml(
            text,
            source_path="DTLS/rfc9147.xml",
            input_sha256="0" * 64,
            doc_id="rfc9147",
        )
        self.assertEqual(result.document["meta"]["format"], "xml")
        self.assertGreater(len(result.document["sections"]), 50)


if __name__ == "__main__":
    unittest.main()
