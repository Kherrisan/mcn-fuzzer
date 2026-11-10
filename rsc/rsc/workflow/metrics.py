"""Persisted phase timings; worker occupancy is not model inference utilization."""

import json
import time
from collections import Counter


def result_metrics(store, events):
    data = store.settings()
    transitions = [e for e in events if e["kind"] == "phase"]
    intervals, start, phase, round_ = [], data["started_at"], "preprocess", 0
    for e in transitions:
        intervals.append((phase, round_, start, e["time"]))
        start, phase, round_ = e["time"], e["phase"], e["round"]
    if phase not in {"published", "failed"}:
        intervals.append((phase, round_, start, time.time()))
    rows = []
    attempts = [dict(r) for r in store.db.execute("SELECT * FROM attempts")]
    for phase, round_, start, end in intervals:
        calls = [
            e
            for e in events
            if e["kind"] == "call" and e.get("phase") == phase and e.get("round", 0) == round_
        ]
        seen, repeated, delivered = set(), 0, 0
        for call in calls:
            for item in call.get("delivery", {}).get("items", []):
                key = (call["worker"], call["tool"], str(item))
                repeated += key in seen
                delivered += 1
                seen.add(key)
        busy = 0
        for attempt in attempts:
            body = json.loads(attempt["body"])
            begin = body.get("running_at")
            if begin is not None:
                finish = body.get("submitted_at", body.get("failed_at", end))
                busy += max(0, min(end, finish) - max(start, begin))
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
                "delivered_items": delivered,
                "repeated_delivered_items": repeated,
                "worker_occupancy_seconds": busy,
            }
        )
    return {
        "elapsed_seconds": data.get("finished_at", time.time()) - data["started_at"],
        "phases": rows,
        "retries": sum(a["attempt"] > 1 for a in attempts),
        "correction_rounds": data["round"],
        "semantic_review": False,
    }


def phase_metrics(data, events):
    phases = []
    saved = {(p["dispatch_id"], p["phase"]): p for p in data.get("phase_history", [])}
    known_phases = {e["dispatch_id"]: e["phase"] for e in events if "phase" in e}
    grouped = {}
    for event in events:
        grouped.setdefault(
            (event["dispatch_id"], event.get("phase", known_phases.get(event["dispatch_id"]))), []
        ).append(event)
    for (dispatch, phase_name), calls in grouped.items():
        completed = [
            e for e in calls if e["tool"] == "advance_phase" and e.get("status", "ok") == "ok"
        ]
        if not completed:
            continue
        end = completed[-1]
        phase = saved.get((dispatch, phase_name), {})
        if not phase.get("finished_at") and all(
            e.get("next_phase") == phase_name for e in completed
        ):
            # A valid parser submission can wait for background source preparation.
            # That is not a completed global phase until the saved handoff exists.
            continue
        start = phase.get("started_at")
        observed = start is None
        start = calls[0]["time"] if observed else start
        finish = phase.get("finished_at", end["time"])
        seen = set()
        repeated = 0
        delivered_items = 0
        for event in calls:
            family = "source" if event["tool"] in {"read_batch", "read_batches"} else event["tool"]
            for item in event.get("delivery", {}).get("delivered_ids", []):
                key = (family, item)
                repeated += key in seen
                delivered_items += 1
                seen.add(key)
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
                "calls_by_origin": dict(Counter(e.get("origin", "unclassified") for e in calls)),
                "delivery_audited_calls": sum("delivery" in e for e in calls),
                "delivered_items": delivered_items,
                "repeated_delivered_items": repeated,
                "delivered_tokens": sum(e.get("delivery", {}).get("tokens", 0) for e in calls),
                "non_tool_wall_seconds": max(
                    0, finish - start - sum(e["elapsed_ms"] for e in calls) / 1000
                ),
                "rejected_calls": sum(e.get("status") == "rejected" for e in calls),
                "tool_elapsed_ms": sum(e["elapsed_ms"] for e in calls),
                "calls_by_tool": dict(Counter(e["tool"] for e in calls)),
            }
        )
    return phases
