#!/usr/bin/env python3
"""One LightFTP coverage build: forkserver binary, bitcode, callgraph, CFGs, manifest.

The compiler flags match targets/lightftp/build-coverage.sh. Graph generation
uses the same patched sources. This script does not start a campaign.
"""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

FUZZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FUZZ / "python"))
from coverage_analysis.manifest import write_manifest  # noqa: E402
from coverage_analysis.cfg import graph_inputs, write_source_map  # noqa: E402


UNITS = ["main.c", "ftpserv.c", "cfgparse.c", "x_malloc.c"]


def run(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"command failed: {shlex.join(argv)}\n{result.stdout}{result.stderr}")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=FUZZ / ".deps/LightFTP/Source")
    parser.add_argument("--manifest-only", action="store_true",
                        help="hash an existing binary, sources, and DOT files")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.manifest_only:
        script = FUZZ / "targets/lightftp/build-coverage.sh"
        env = os.environ.copy()
        env["LIGHTFTP_COVERAGE_BUILD"] = str(output)
        run(["bash", str(script)], env=env)
    binary = output / "fftp"
    sources = [args.source_dir / name for name in UNITS]
    graphs = ["callgraph.dot"]
    cfg = output / "cfg"
    if cfg.is_dir():
        if (output / "lightftp.bc").is_file():
            write_source_map(output / "lightftp.bc", cfg)
        graphs.extend(f"cfg/{path.name}" for path in graph_inputs(cfg))
    missing = [str(path) for path in [binary, *sources, output / "callgraph.dot"] if not path.is_file()]
    if missing:
        raise SystemExit("missing build artifacts:\n" + "\n".join(missing))
    # source_sha256 covers the whole source directory, not only UNITS.
    payload = write_manifest(output / "manifest.json", binary, args.source_dir, output, graphs)
    print(json_dumps(payload))


def json_dumps(payload):
    import json
    return json.dumps(payload, indent=2)


if __name__ == "__main__":
    main()
