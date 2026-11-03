"""Behavioral contracts at the shared artifact, stage and provider boundaries."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rsc.artifacts import read_jsonl, write_json, write_jsonl
from rsc.cli import _validator
from rsc.config import ProviderConfig
from rsc.ingest import IngestOptions, ingest_path
from rsc.llm import AnthropicClient, OpenAIClient
from rsc.llm.errors import (
    LLMAuthError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    StructuredOutputUnsupported,
)
from rsc.schemas import validator


class ArtifactAndStageTests(unittest.TestCase):
    def test_ingest_duplicate_statuses_and_dry_run(self) -> None:
        fixture = Path(__file__).parent / "fixtures/rfc/sample-rfc.txt"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            for name in ("first.txt", "second.txt"):
                (inputs / name).write_bytes(fixture.read_bytes())
            out = root / "out"
            dry = ingest_path(inputs, IngestOptions(out_dir=out, dry_run=True))
            self.assertEqual([row.status for row in dry.documents], ["planned", "unchanged"])
            self.assertFalse(out.exists())
            for force in (False, True):
                result = ingest_path(inputs, IngestOptions(out_dir=out, force=force))
                self.assertEqual([row.status for row in result.documents], ["ok", "unchanged"])
                self.assertEqual(result.documents[0].counts, result.documents[1].counts)
                self.assertEqual(result.documents[0].doc_id, result.documents[1].doc_id)

    def test_ingest_failed_files_keep_identity_and_allow_later_files(self) -> None:
        fixture = Path(__file__).parent / "fixtures/rfc/sample-rfc.txt"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            data = fixture.read_bytes()
            for name in ("first.txt", "second.txt"):
                (inputs / name).write_bytes(data)
            with patch.object(Path, "read_bytes", side_effect=[OSError("unreadable"), data]):
                result = ingest_path(inputs, IngestOptions(out_dir=root / "read-failure"))
            failed, good = result.documents
            self.assertEqual((failed.status, good.status), ("failed", "ok"))
            self.assertEqual(failed.doc_id, "first")
            self.assertEqual(failed.error, "cannot read input: unreadable")
            self.assertIsNone(failed.document_path)
            self.assertEqual(result.exit_code(), 1)
            with patch("rsc.ingest.pipeline._parse", side_effect=ValueError("parse failed")):
                result = ingest_path(inputs, IngestOptions(out_dir=root / "parse-failure"))
            self.assertEqual([row.status for row in result.documents], ["failed", "failed"])
            self.assertEqual(result.documents[0].doc_id, result.documents[1].doc_id)
            self.assertEqual(result.documents[0].error, "ValueError: parse failed")
            self.assertIsNotNone(result.documents[0].document_path)

    def test_serialization_and_optional_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.jsonl"
            self.assertEqual(read_jsonl(path, missing_ok=True), [])
            with self.assertRaises(FileNotFoundError):
                read_jsonl(path)
            write_jsonl(path, [{"text": "协议", "status": "unknown"}])
            self.assertEqual(path.read_bytes(), '{"text": "协议", "status": "unknown"}\n'.encode())
            write_jsonl(path, [])
            self.assertEqual(path.read_bytes(), b"")
            path.write_text('\n{"ok": true}\n\n', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"ok": True}])
            write_json(path, {"text": "协议"})
            self.assertEqual(path.read_bytes(), '{\n  "text": "协议"\n}\n'.encode())

    def test_stage_imports_do_not_import_cli(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import rsc.pipeline, sys; assert 'rsc.cli' not in sys.modules"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(_validator, validator)


class SharedProviderTests(unittest.TestCase):
    def test_sdk_constructor_overrides(self) -> None:
        for cls, api, constructor in (
            (OpenAIClient, "openai", "OpenAI"),
            (AnthropicClient, "anthropic", "Anthropic"),
        ):
            config = ProviderConfig(
                name="p",
                api=api,
                model="m",
                api_key="local",
                base_url="https://example.test",
                timeout_s=20,
                max_retries=2,
                default_headers={"x-test": "1"},
                extra_client_kwargs={"timeout": 7},
            )
            with self.subTest(api=api), patch(f"{api}.{constructor}") as build:
                cls(config)
                build.assert_called_once_with(
                    api_key="local",
                    base_url="https://example.test",
                    timeout=7,
                    max_retries=2,
                    default_headers={"x-test": "1"},
                )

    def test_sdk_error_mapping_and_metadata(self) -> None:
        cases = (
            ("AuthenticationError", "bad key", LLMAuthError),
            ("RateLimitError", "slow down", LLMRateLimitError),
            ("APITimeoutError", "timeout", LLMTimeoutError),
            ("APIConnectionError", "connection failed", LLMConnectionError),
            ("BadRequestError", "maximum context length", LLMContextLengthError),
            ("BadRequestError", "invalid payload", LLMProviderError),
            ("APIStatusError", "server error", LLMProviderError),
        )
        sdk = SimpleNamespace(**{name: type(name, (Exception,), {}) for name, _, _ in cases})
        for cls, api in ((OpenAIClient, "openai"), (AnthropicClient, "anthropic")):
            client = cls(ProviderConfig(name="p", api=api, model="m"), sdk_client=object())
            client._sdk = sdk
            for name, message, expected in cases:
                with self.subTest(api=api, name=name, message=message):
                    error = getattr(sdk, name)(message)
                    error.status_code, error.request_id = 400, "req-test"
                    mapped = client._map_error(error)
                    self.assertIsInstance(mapped, expected)
                    self.assertEqual((mapped.status_code, mapped.request_id), (400, "req-test"))
            unsupported = client._map_error(sdk.BadRequestError("response_format unsupported"))
            expected = StructuredOutputUnsupported if api == "openai" else LLMProviderError
            self.assertIsInstance(unsupported, expected)
            existing = LLMProviderError("already mapped")
            self.assertIs(client._map_error(existing), existing)


if __name__ == "__main__":
    unittest.main()
