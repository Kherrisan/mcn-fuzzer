"""Offline compact-versus-records token benchmark. No model calls."""

import json
import time
from pathlib import Path

from rsc.workflow.paging import compact_page, decode_compact, records_page
from rsc.workflow.sources import token_count

ROOT = Path(__file__).resolve().parents[2]
MODELS = [
    ROOT / "rsc/runs/ftp-flash-1108-1607/task/protocol.json",
    ROOT / "rsc/runs/ftp-gpt-6-sol-high-20261104T031756Z/protocol.json",
]


def records_of(model):
    rows = []
    for kind in ("parties", "messages", "channels", "steps", "step_dependencies"):
        for index, value in enumerate(model.get(kind) or []):
            rows.append(
                {
                    "record_id": f"{kind}-{index}",
                    "kind": kind,
                    "value": value,
                    "evidence": [{"batch_id": "offline", "block_id": "0"}],
                }
            )
    return rows


def test_offline_ftp_models_round_trip_and_shrink():
    seen = False
    for path in MODELS:
        if not path.is_file():
            continue
        seen = True
        model = json.loads(path.read_text())
        rows = records_of(model)
        started = time.perf_counter()
        compact = compact_page(rows, 0, 24000, 1)
        records = records_page(rows, 0, 24000, 1)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert decode_compact(compact) == rows[: len(decode_compact(compact))]
        raw = token_count({"items": rows})
        print(
            f"{path}: records={len(rows)} raw_tokens={raw} "
            f"compact_tokens={compact['tokens']} compact_cursor={compact['next_cursor']} "
            f"records_tokens={records['tokens']} records_cursor={records['next_cursor']} "
            f"encode_ms={elapsed_ms:.1f}"
        )
        if compact["next_cursor"] is None and records["next_cursor"] is None:
            assert compact["tokens"] < records["tokens"]
    assert seen, "expected Flash and Sol FTP protocol artifacts"
