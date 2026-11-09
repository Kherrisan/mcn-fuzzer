"""Phase timings from persisted assignments, with explicitly labelled older audit data."""

from collections import Counter


def phase_metrics(data, events):
    phases = []
    saved = {p["dispatch_id"]: p for p in data.get("phase_history", [])}
    grouped = {}
    for event in events:
        grouped.setdefault(event["dispatch_id"], []).append(event)
    for dispatch, calls in grouped.items():
        completed = [
            e for e in calls if e["tool"] == "advance_phase" and e.get("status", "ok") == "ok"
        ]
        if not completed:
            continue
        end = completed[-1]
        phase = saved.get(dispatch, {})
        start = phase.get("started_at")
        observed = start is None
        start = calls[0]["time"] if observed else start
        finish = phase.get("finished_at", end["time"])
        phases.append(
            {
                "phase": end["phase"],
                "role": end["role"],
                "round": end["round"],
                "revision": end["revision"],
                "dispatch_id": dispatch,
                "started_at": start,
                "finished_at": finish,
                "elapsed_seconds": finish - start,
                "timing_origin": "first_observed_tool" if observed else "assignment_created",
                "tool_calls": len(calls),
                "rejected_calls": sum(e.get("status") == "rejected" for e in calls),
                "tool_elapsed_ms": sum(e["elapsed_ms"] for e in calls),
                "calls_by_tool": dict(Counter(e["tool"] for e in calls)),
            }
        )
    return phases
