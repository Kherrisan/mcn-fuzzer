#!/usr/bin/env python3
"""Convert coverage DOT graphs to JSON and compare warm-process loading times."""

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

FUZZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FUZZ / "python"))
from coverage_analysis.cfg import graph_inputs  # noqa: E402
from coverage_analysis.graphs import read_graphs  # noqa: E402


def summarize(samples):
    return {"runs": len(samples), "seconds": samples,
            "median_seconds": statistics.median(samples),
            "min_seconds": min(samples), "max_seconds": max(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callgraph", type=Path, required=True)
    parser.add_argument("--cfg-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dot-repeat", type=int, default=3)
    parser.add_argument("--json-repeat", type=int, default=20)
    args = parser.parse_args()
    if args.dot_repeat < 1 or args.json_repeat < 1:
        parser.error("repeat counts must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dot_times = []
    expected = None
    for _ in range(args.dot_repeat):
        start = time.perf_counter()
        graphs = read_graphs(args.callgraph, args.cfg_dir)
        dot_times.append(time.perf_counter() - start)
        if expected is None:
            expected = graphs
        else:
            assert graphs == expected, "DOT reads produced different graphs"
    output = args.output_dir / "graphs.json"
    start = time.perf_counter()
    output.write_text(json.dumps(expected, separators=(",", ":")) + "\n")
    write_seconds = time.perf_counter() - start
    json_times = []
    for _ in range(args.json_repeat):
        start = time.perf_counter()
        loaded = json.loads(output.read_text())
        json_times.append(time.perf_counter() - start)
        assert loaded == expected, "JSON graph differs from DOT graph"
    inputs = [args.callgraph, *graph_inputs(args.cfg_dir)]
    dot, json_loading = summarize(dot_times), summarize(json_times)
    report = {
        "method": "same process; file read, parse, and in-memory structure construction; imports and equality checks excluded; filesystem cache not flushed",
        "callgraph": str(args.callgraph.resolve()), "cfg_dir": str(args.cfg_dir.resolve()),
        "cfg_dot_files": len(list(args.cfg_dir.glob("*.dot"))),
        "input_bytes_including_source_map": sum(p.stat().st_size for p in inputs),
        "json_bytes": output.stat().st_size, "json_path": str(output.resolve()),
        "json_write_seconds": write_seconds,
        "first_dot_parse_and_json_write_seconds": dot_times[0] + write_seconds,
        "dot_load": dot, "json_load": json_loading,
        "median_speedup": dot["median_seconds"] / json_loading["median_seconds"],
        "round_trip_equal": True,
    }
    (args.output_dir / "timing.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
