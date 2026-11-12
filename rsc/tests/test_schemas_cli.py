"""Schema and CLI contract tests.

These tests are skipped when ``jsonschema`` is not installed.  They validate the
checked-in schemas, the cross-file ``$ref`` registry, one sample per stage, and
the CLI ``validate``/``schemas``/``doctor`` commands.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from rsc.cli import _validator, main

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

SPAN = {
    "source_id": "rfc8446",
    "start": 0,
    "end": 10,
    "start_line": 1,
    "end_line": 1,
}
META = {
    "rfc": "RFC 8446",
}

DOCUMENT = {
    "meta": {
        **META,
        "title": "The Transport Layer Security (TLS) Protocol Version 1.3",
        "source_id": "rfc8446",
        "source_path": "rfc8446.txt",
        "format": "txt",
    },
    "sections": [
        {
            "section_id": "s1",
            "path": ["4"],
            "title": "Handshake Protocol",
            "level": 2,
            "span": SPAN,
        }
    ],
    "blocks": [
        {
            "block_id": "b1",
            "section_path": ["4"],
            "block_type": "paragraph",
            "text": "A receiver MUST respond with a KeyUpdate.",
            "original_span": SPAN,
            "order": 0,
        }
    ],
    "span_map": [],
}

INDEX = {
    "meta": META,
    "sections": [],
    "blocks": [],
    "references": [],
}

MANIFEST = {
    "rfc": "RFC 8446",
    "created_at": "2026-01-01T00:00:00Z",
    "rsc_version": "0.1.0",
    "argv": ["rsc", "run"],
    "stages": [{"name": "ingest", "status": "complete"}],
}

BATCH = {
    "batch_id": "batch-test",
    "input_root": "/tmp/rfc",
    "created_at": "2026-01-01T00:00:00Z",
    "rsc_version": "0.1.0",
    "command": ["rsc", "ingest", "/tmp/rfc"],
    "documents": [
        {
            "doc_id": "rfc9999",
            "input_path": "rfc9999.txt",
            "format": "txt",
            "status": "ok",
            "document": "rfc9999/document.json",
            "report": "rfc9999/ingest-report.json",
            "counts": {"lines": 1, "sections": 1, "blocks": 1},
            "error": None,
        }
    ],
    "summary": {
        "total": 1,
        "ok": 1,
        "failed": 0,
        "skipped": 0,
        "unchanged": 0,
    },
}

SAMPLES = {
    "document": DOCUMENT,
    "index": INDEX,
    "manifest": MANIFEST,
    "batch": BATCH,
}


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import jsonschema
        except ImportError as exc:  # pragma: no cover - optional in the dev venv
            raise unittest.SkipTest("jsonschema is not installed") from exc
        cls.jsonschema = jsonschema

    def test_every_schema_is_valid(self) -> None:
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            with self.subTest(path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.jsonschema.Draft202012Validator.check_schema(schema)

    def test_samples_validate(self) -> None:
        for stage, sample in SAMPLES.items():
            with self.subTest(stage):
                validator = _validator(stage, SCHEMAS)
                errors = list(validator.iter_errors(sample))
                self.assertEqual(
                    [],
                    [error.message for error in errors],
                    f"{stage} sample failed validation",
                )

    def test_cli_validate_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.json"
            path.write_text(json.dumps(DOCUMENT), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["validate", "document", str(path)])
            self.assertEqual(0, code)
            self.assertIn("OK", output.getvalue())

    def test_cli_schemas_list(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["schemas", "list"])
        self.assertEqual(0, code)
        self.assertIn("common.schema.json", output.getvalue())

    def test_cli_doctor(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["doctor"])
        self.assertEqual(0, code)
        self.assertIn("schemas", output.getvalue())


if __name__ == "__main__":
    unittest.main()
