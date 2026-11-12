"""Phase timings and tool usage from the coordinator's diagnostic journal."""

import time
from collections import Counter


def result_metrics(store, events):
    data = store.settings()
    intervals, start, phase, round_ = [], data["started_at"], "preprocess", 0
    for event in (e for e in events if e["kind"] == "phase"):
        intervals.append((phase, round_, start, event["time"]))
        start, phase, round_ = event["time"], event["phase"], event["round"]
    if phase not in {"published", "failed"}:
        intervals.append((phase, round_, start, time.time()))
    rows = []
    for phase, round_, start, end in intervals:
        calls = [
            e
            for e in events
            if e["kind"] == "call" and e.get("phase") == phase and e.get("round", 0) == round_
        ]
        busy = sum(
            max(0, min(end, a.get("submitted_at", end)) - max(start, a["running_at"]))
            for a in store.assignments()
            if "running_at" in a
        )
        rows.append(
            {
                "phase": phase,
                "round": round_,
                "started_at": start,
                "finished_at": end,
                "elapsed_seconds": end - start,
                "calls": dict(Counter(e["tool"] for e in calls if e["origin"] == "agent")),
                "rejected": dict(
                    Counter(
                        e["tool"]
                        for e in calls
                        if e["origin"] == "agent" and e["status"] == "rejected"
                    )
                ),
                "calls_by_origin": dict(Counter(e["origin"] for e in calls)),
                "tool_elapsed_seconds": sum(e["elapsed_ms"] for e in calls) / 1000,
                "delivered_tokens": sum(e.get("delivery", {}).get("tokens", 0) for e in calls),
                "worker_occupancy_seconds": busy,
            }
        )
    return {
        "elapsed_seconds": data.get("finished_at", time.time()) - data["started_at"],
        "phases": rows,
        "retries": sum(len(a.get("failures", [])) for a in store.assignments()),
        "continuations": sum(len(a.get("continuations", [])) for a in store.assignments()),
        "correction_rounds": data["round"],
        "semantic_review": data.get("semantic_review", False),
    }
