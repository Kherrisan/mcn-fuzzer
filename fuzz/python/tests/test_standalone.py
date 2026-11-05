"""The standalone runner stays outside the resident server."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator_server.standalone import main  # noqa: E402


class StandaloneTests(unittest.TestCase):
    def test_runs_generate_and_closes_without_importing_the_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generator = root / "generator.py"
            generator.write_text(
                "def generate(ctx, params):\n"
                "    ctx.bytes_sent += 3\n"
                "    raise RuntimeError(params['why'])\n",
                encoding="utf-8",
            )
            result = root / "result.json"
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "generator": str(generator),
                        "params": {"why": "boom"},
                        "host": "127.0.0.1",
                        "port": 9,
                        "connect_timeout_s": 0.1,
                        "timeout_s": 0.1,
                        "result": str(result),
                    }
                ),
                encoding="utf-8",
            )
            code = main([str(request)])
            self.assertEqual(code, 1)
            body = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["exception_type"], "RuntimeError")
            self.assertIn("boom", body["traceback"])
            self.assertEqual(body["bytes_sent"], 3)
            imported = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import generator_server.standalone, sys; "
                    "banned = [name for name in sys.modules if name in "
                    "{'generator_server.server','generator_server.worker','cbor2','iceoryx2'}]; "
                    "print(','.join(banned))",
                ],
                cwd=ROOT,
                env={**dict(**{k: v for k, v in __import__('os').environ.items()}), "PYTHONPATH": str(ROOT)},
                text=True,
            )
            self.assertEqual(imported.strip(), "")


if __name__ == "__main__":
    unittest.main()
