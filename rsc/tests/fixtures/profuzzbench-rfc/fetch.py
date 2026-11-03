#!/usr/bin/env python3
"""Refresh the ProFuzzBench RFC corpus from the URLs in manifest.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def entries() -> list[dict[str, object]]:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    result: list[dict[str, object]] = []
    for protocol in manifest.get("protocols", []):
        for entry in protocol.get("rfcs", []):
            result.append(entry)
    return result


def check() -> int:
    failures = 0
    for entry in entries():
        path = ROOT / str(entry["file"])
        if not path.is_file():
            print(f"MISSING {path.relative_to(ROOT)}")
            failures += 1
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != entry["bytes"] or digest != entry["sha256"]:
            print(f"CHANGED {path.relative_to(ROOT)}")
            failures += 1
        else:
            print(f"OK      {path.relative_to(ROOT)}")
    return 1 if failures else 0


def download() -> int:
    failures = 0
    for entry in entries():
        path = ROOT / str(entry["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(str(entry["url"]), timeout=60) as response:
                data = response.read()
        except Exception as exc:  # network failures are reported per entry
            print(f"FAIL    {entry['file']}: {exc}", file=sys.stderr)
            failures += 1
            continue
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        print(
            f"WROTE   {entry['file']}  {len(data)} bytes  sha256={digest[:16]}…"
        )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing files against manifest.json instead of downloading",
    )
    args = parser.parse_args(argv)
    return check() if args.check else download()


if __name__ == "__main__":
    raise SystemExit(main())
