"""Artifact checker behavior and pre-publication CLI, without executing a case."""

import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


PYTHON_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PYTHON_ROOT.parent / "skills/protocol-modules/scripts/validate_generator.py"
VALIDATOR = runpy.run_path(str(SCRIPT))
ValidationError = VALIDATOR["ValidationError"]


class ArtifactValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcn-artifact-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "request with spaces"
        self.root.mkdir()
        self.write("generator.py", "def generate(ctx, params):\n    raise RuntimeError('must not execute')\n")
        self.write_json("params.schema.json", {"type": "object"})
        self.write_json("params.json", {"values": [True, -1, "text", [2]]})
        self.write_json("result.draft.json", {
            "request_id": "request-1",
            "status": "ok", "added_steps": [],
        })

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def write_json(self, name, value):
        self.write(name, json.dumps(value))

    def validate(self, **kwargs):
        return VALIDATOR["validate"](
            self.root, "request-1",
            self.root / "result.draft.json", **kwargs,
        )

    def test_draft_cli_imports_without_executing_or_publishing(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        run = subprocess.run([
            sys.executable, str(SCRIPT), "--output-dir", str(self.root),
            "--request-id", "request-1",
            "--result", str(self.root / "result.draft.json"),
            "--check-imports", "--pythonpath", str(PYTHON_ROOT),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["imports_checked"])
        self.assertFalse(report["runtime_executed"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_production_subset_accepts_optional_properties_and_ignores_unsupported_keywords(self):
        self.write_json("params.schema.json", {
            "type": "object", "properties": {
                "required_value": {"type": "integer", "minimum": 10, "enum": [11]},
                "optional_value": {"type": "string"},
            }, "required": ["required_value"], "additionalProperties": False,
        })
        self.write_json("params.json", {"required_value": 1})
        self.assertEqual(self.validate()["status"], "ok")

    def test_signed_integer_boundaries_and_nested_lists(self):
        self.write_json("params.json", {"bounds": [-(1 << 63), (1 << 63) - 1, [False, ""]]})
        self.validate()
        for value in [-(1 << 63) - 1, 1 << 63, 1.5, None, {"nested": True}, [None]]:
            with self.subTest(value=value):
                self.write_json("params.json", {"value": value})
                with self.assertRaises(ValidationError):
                    self.validate()

    def test_non_json_numbers_are_rejected(self):
        self.write("params.json", '{"value": NaN}')
        with self.assertRaisesRegex(ValidationError, "non-JSON number"):
            self.validate()

    def test_required_unknown_fields_and_boolean_integer_mismatch(self):
        self.write_json("params.schema.json", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False,
        })
        for params, error in [({}, "missing required"), ({"value": 1, "extra": 2}, "not in the schema"),
                              ({"value": True}, "must be an integer")]:
            with self.subTest(params=params):
                self.write_json("params.json", params)
                with self.assertRaisesRegex(ValidationError, error):
                    self.validate()

    def test_remote_refs_rejected_even_in_nested_schema(self):
        self.write_json("params.schema.json", {
            "type": "object", "properties": {"unused": {"items": {"$ref": "https://invalid/schema"}}},
        })
        with self.assertRaisesRegex(ValidationError, "remote \\$ref"):
            self.validate()

    def test_completion_ids_status_and_added_steps(self):
        base = json.loads((self.root / "result.draft.json").read_text())
        for key, value, error in [
            ("request_id", "different", "does not match"),
            ("status", "error", "synthesis failed"),
            ("added_steps", [1], "array of strings"),
        ]:
            with self.subTest(key=key):
                self.write_json("result.draft.json", dict(base, **{key: value}))
                with self.assertRaisesRegex(ValidationError, error):
                    self.validate()

    def test_entrypoint_syntax_async_signature_and_yield_are_rejected(self):
        for source in [
            "def generate(:\n pass\n", "return 1\n", "def other(ctx, params): pass\n",
            "async def generate(ctx, params): pass\n", "def generate(ctx): pass\n",
            "def generate(ctx, params, required): pass\n", "def generate(ctx, params, *, required): pass\n",
            "def generate(ctx, params): yield 1\n",
        ]:
            with self.subTest(source=source):
                self.write("generator.py", source)
                with self.assertRaises(ValidationError):
                    self.validate()

    def test_nested_yield_helper_and_optional_argument_are_accepted(self):
        self.write("generator.py", "def generate(ctx, params, optional=None):\n"
                   "    def iterator():\n        yield 1\n    return None\n")
        self.validate()

    def test_import_errors_overwritten_entrypoint_and_resources_are_reported(self):
        for source, error in [
            ("import missing_mcn_dependency\ndef generate(ctx, params): pass\n", "ModuleNotFoundError"),
            ("def generate(ctx, params): pass\ngenerate = 1\n", "no generate"),
            ("import socket\ns = socket.socket()\ndef generate(ctx, params): pass\n", "case resources"),
            ("import threading\nthreading.Thread(target=lambda: None).start()\n"
             "def generate(ctx, params): pass\n", "start threads"),
            ("import subprocess\nsubprocess.Popen(['true'])\ndef generate(ctx, params): pass\n", "case resources"),
            (f"open({str(self.root / 'side-effect')!r}, 'w').write('bad')\n"
             "def generate(ctx, params): pass\n", "write case files"),
        ]:
            with self.subTest(error=error):
                self.write("generator.py", source)
                with self.assertRaisesRegex(ValidationError, error):
                    self.validate(import_check=True, pythonpath=str(PYTHON_ROOT))
        self.assertFalse((self.root / "side-effect").exists())

    def test_import_probe_has_a_deadline(self):
        self.write("generator.py", "import time\ntime.sleep(5)\ndef generate(ctx, params): pass\n")
        with self.assertRaisesRegex(ValidationError, "exceeded"):
            self.validate(import_check=True, pythonpath=str(PYTHON_ROOT), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
