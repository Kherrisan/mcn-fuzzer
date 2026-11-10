"""Phase timings from persisted assignments, with explicitly labelled older audit data."""

import json
import time
from collections import Counter

from .merge import conflicts
from .store import dumps


def parallel_metrics(store, events):
    """Wall-clock phases and claimed-worker occupancy, not inferred model API usage."""
    data, assignments = store.settings(), store.assignments()
    attempts = [
        (name, json.loads(body))
        for name, body in store.db.execute("SELECT assignment, body FROM attempts ORDER BY rowid")
    ]
    phases = []
    groups = [("preprocess", 0), *dict.fromkeys((a["phase"], a["round"]) for a in assignments)]
    for phase, round_ in groups:
        workers = [a for a in assignments if (a["phase"], a["round"]) == (phase, round_)]
        names = {a["name"] for a in workers}
        lives = [(name, body) for name, body in attempts if name in names]
        calls = [e for e in events if (e["phase"], e["round"]) == (phase, round_)]
        ends = [
            e["time"]
            for e in calls
            if e["role"] == "coordinator" and e["tool"] == "advance_phase" and e["status"] == "ok"
        ]
        start = min((body["created_at"] for _, body in lives), default=data["started_at"])
        end = max(ends) if ends else None
        elapsed = max(0, (end or time.time()) - start)
        agents = [e for e in calls if e["origin"] == "agent" and e["tool"] != "tools/list"]
        seen, queries, repeated, repeated_queries = set(), set(), 0, 0
        for e in agents:
            for item in e.get("delivery", {}).get("delivered_ids", []):
                key = dumps([e["tool"], item])
                repeated += key in seen
                seen.add(key)
            if e["tool"] == "query_model" and "request" in e:
                key = dumps([e["worker"], e.get("delivery", {}).get("revision"), e["request"]])
                repeated_queries += key in queries
                queries.add(key)
        occupancy = {}
        for a in workers:
            seconds = sum(
                max(
                    0,
                    body.get("submitted_at", body.get("finished_at", end or time.time()))
                    - body["claimed_at"],
                )
                for name, body in lives
                if name == a["name"] and "claimed_at" in body
            )
            occupancy[a["worker"]] = {
                "claimed_seconds": seconds,
                "phase_fraction": seconds / elapsed if elapsed else 0,
            }
        conclusions = (
            [
                json.loads(r[0])
                for r in store.db.execute(
                    "SELECT body FROM conclusions WHERE revision=?", (round_,)
                )
            ]
            if phase == "review"
            else []
        )
        initial_conflicts = 0
        if phase == "merge":
            for a in workers:
                seed = json.loads(
                    store.db.execute(
                        "SELECT body FROM history WHERE draft=? AND revision=0", (a["draft"],)
                    ).fetchone()[0]
                )
                initial_conflicts += len(conflicts(seed["initial_records"], seed["checks"]))
        phases.append(
            {
                "phase": phase,
                "initial_conflicts": initial_conflicts,
                "round": round_,
                "started_at": start,
                "finished_at": end,
                "elapsed_seconds": elapsed,
                "workers": occupancy,
                "calls_by_origin": dict(Counter(e["origin"] for e in calls)),
                "catalog_sizes": sorted({e["catalog_size"] for e in calls if "catalog_size" in e}),
                "catalog_requests": sum(e["tool"] == "tools/list" for e in calls),
                "agent_calls_by_tool": dict(Counter(e["tool"] for e in agents)),
                "agent_rejected_by_tool": dict(
                    Counter(e["tool"] for e in agents if e["status"] == "rejected")
                ),
                "agent_argument_errors": sum(
                    e.get("error", "").startswith("invalid arguments:") for e in agents
                ),
                "agent_tool_seconds": sum(e["elapsed_ms"] for e in agents) / 1000,
                "source_deliveries": sum(
                    len(e.get("delivery", {}).get("delivered_ids", []))
                    for e in agents
                    if e["tool"] == "read_batches"
                ),
                "delivered_tokens": sum(e.get("delivery", {}).get("tokens", 0) for e in agents),
                "repeated_delivered_items": repeated,
                "repeated_queries": repeated_queries,
                "batch_reports": sum(
                    e.get("batch_reports", 0) for e in agents if e["status"] == "ok"
                ),
                "fresh_conclusions": sum(not c.get("carried_from") for c in conclusions),
                "carried_conclusions": sum(bool(c.get("carried_from")) for c in conclusions),
            }
        )
    return {
        "elapsed_seconds": data.get("finished_at", time.time()) - data["started_at"],
        "phases": phases,
        "token_usage": None,
        "token_usage_note": (
            "MCP delivered_tokens measure tool payloads, not model API token usage."
        ),
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
