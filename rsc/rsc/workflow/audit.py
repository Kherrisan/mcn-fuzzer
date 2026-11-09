"""Recheck a published task against source receipts, revisions and independent reviews.

This proves delivery and structural workflow invariants, not semantic completeness.
Older tasks recorded fixed 50-item page offsets; accept those only after verifying
every offset and the actual payload size, and label that evidence in the report.
"""

from __future__ import annotations

import copy
import json
from collections import Counter

import tiktoken

from rsc.protocol.validate import validate_protocol

from .metrics import phase_metrics
from .model import reference_errors, validate_record
from .sources import digest
from .state import Workflow, WorkflowError


def require(condition, message):
    if not condition:
        raise WorkflowError(f"audit failed: {message}")


def delivery(rows, markers, ids):
    """Verify IDs, or old fixed page offsets plus the historical client output limit."""
    legacy = bool(markers) and all(type(m) is int for m in markers)
    if legacy:
        require(set(range(0, max(1, len(rows)), 50)).issubset(markers), "missing legacy page")
        pages = [
            {"items": rows[i : i + 50], "next_cursor": i + 50 if i + 50 < len(rows) else None}
            for i in range(0, max(1, len(rows)), 50)
        ]
    else:
        require((set(ids) or {"__empty__"}).issubset(markers), "missing delivered record/change")
        pages, cursor = [], 0
        while True:
            page = Workflow._page(rows, cursor)
            pages.append(page)
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
    encoding = tiktoken.get_encoding("cl100k_base")
    largest = max(
        len(encoding.encode(json.dumps(p, ensure_ascii=False), disallowed_special=()))
        for p in pages
    )
    require(largest < 32000, "tool payload exceeds configured delivery budget")
    return {"tracking": "legacy_offsets" if legacy else "record_ids", "max_page_tokens": largest}


def audit_run(root):
    workflow = Workflow(root)
    try:
        return _audit(workflow)
    except (KeyError, TypeError, IndexError) as error:
        raise WorkflowError(
            f"audit failed: incomplete or malformed audit evidence: {error}"
        ) from error
    finally:
        workflow.db.close()


def _audit(w):
    d = w.data
    require(d is not None and d["phase"] == "published", "task is not published")
    require(not w._hard_errors(), f"hard gate errors: {w._hard_errors()}")
    require(not validate_protocol(w.publication_model(), d["documents"]), "invalid protocol schema")
    require(
        json.loads((w.root / "protocol.json").read_text()) == w.publication_model(),
        "published model differs from authoritative state",
    )
    # Reconstruct every frozen revision, including deleted records.
    snapshots = {0: {}}
    records = {}
    for expected, h in enumerate(d["history"], 1):
        require(h["revision"] == expected, "non-contiguous revision history")
        for change in h["changes"]:
            rid = change["record_id"]
            require(records.get(rid) == change["before"], "inconsistent change before-image")
            if change["after"] is None:
                records.pop(rid)
            else:
                records[rid] = copy.deepcopy(change["after"])
        snapshots[expected] = copy.deepcopy(records)
    require(
        records == d["records"] and len(d["history"]) == d["revision"], "history/state mismatch"
    )
    events = [json.loads(x[0]) for x in w.db.execute("SELECT body FROM events ORDER BY seq")]
    transitions = [
        e for e in events if e["tool"] == "advance_phase" and e.get("status", "ok") == "ok"
    ]
    parses = [e for e in transitions if e["phase"] == "parse"]
    require(len(parses) == 1, "expected one completed parsing phase")
    batches = {b["batch_id"]: b for b in d["batches"]}
    encoding = tiktoken.get_encoding("cl100k_base")
    largest_source = max(
        len(
            encoding.encode(
                json.dumps({"batch": b, "receipt_id": "f" * 64}, ensure_ascii=False),
                disallowed_special=(),
            )
        )
        for b in batches.values()
    )
    require(largest_source < d["max_tokens"] + 1024, "source payload exceeds client output limit")
    for entry in d.get("response_log") or []:
        require(entry.get("format") in {"compact", "records", None}, "unknown response format")
        require(
            isinstance(entry.get("tokens"), int)
            and isinstance(entry.get("max_tokens"), int)
            and entry["tokens"] <= entry["max_tokens"] <= 30000,
            "recorded page exceeds its token budget",
        )
        require(isinstance(entry.get("delivered_ids"), list), "recorded page has no delivered ids")
        require(isinstance(entry.get("sha256"), str) and entry["sha256"], "recorded page has no digest")

    def receipt(dispatch, bid):
        require(
            d["receipts"].get(dispatch + ":" + bid)
            == digest({"dispatch": dispatch, "batch": batches[bid]}),
            f"missing or invalid receipt: {dispatch}/{bid}",
        )

    require(set(d["processed"]) == set(batches), "not all source batches were parsed")
    for bid, report in d["processed"].items():
        receipt(parses[0]["dispatch_id"], bid)
        require(bool(report.get("summary", "").strip()), "missing parsing report")
    reviews = [e for e in transitions if e["phase"] == "review"]
    require([e["round"] for e in reviews] == list(range(1, d["round"] + 1)), "missing review round")
    require(1 <= len(reviews) <= d["max_review_rounds"], "invalid review limit")
    summary = []
    base = 0
    for end in reviews:
        revision, round_, dispatch = end["revision"], end["round"], end["dispatch_id"]
        model = snapshots[revision]
        require(not reference_errors(model), f"invalid model references in round {round_}")
        for record in model.values():
            validate_record(record["kind"], record["value"])
        changes = [h for h in d["history"] if base < h["revision"] <= revision]
        affected = set(batches) if round_ == 1 else set()
        if round_ > 1:
            for h in changes:
                for change in h["changes"]:
                    for value in (change["before"], change["after"]):
                        if value:
                            affected.update(e["batch_id"] for e in value["evidence"])
            for finding in d["findings"].values():
                for resolution in finding["history"]:
                    if resolution["role"] == "parser" and resolution["round"] == round_ - 1:
                        affected.update(e["batch_id"] for e in resolution["evidence"])
        reports = {k: v for k, v in d["reviews"].items() if k.startswith(dispatch + ":")}
        require(
            affected.issubset({v["batch_id"] for v in reports.values()}),
            "unreviewed source batches",
        )
        for report in reports.values():
            require(
                report["round"] == round_
                and report["revision"] == revision
                and report["dispatch_id"] == dispatch
                and report.get("summary", "").strip(),
                "review report does not match its frozen assignment",
            )
            receipt(dispatch, report["batch_id"])
        rows = [{"record_id": rid, **record} for rid, record in model.items()]
        model_delivery = delivery(rows, d.get("model_reads", {}).get(dispatch, []), list(model))
        diff_delivery = None
        if round_ > 1:
            markers = d["diff_reads"].get(dispatch, [])
            if markers and all(type(m) is int for m in markers):
                diff_rows = changes
            else:
                diff_rows = [
                    {"change_id": f"{h['revision']}:{i}", "revision": h["revision"], "changes": [c]}
                    for h in changes
                    for i, c in enumerate(h["changes"])
                ]
            diff_delivery = delivery(diff_rows, markers, [r.get("change_id") for r in diff_rows])
        summary.append(
            {
                "round": round_,
                "revision": revision,
                "model_records": len(model),
                "required_batches": len(affected),
                "reviewed_batches": len(reports),
                "model_delivery": model_delivery,
                "diff_delivery": diff_delivery,
            }
        )
        base = revision
    for finding in d["findings"].values():
        if finding["status"] == "accepted":
            require(
                bool(finding["history"])
                and finding["history"][-1].get("role") == "reviewer"
                and finding["history"][-1].get("accepted") is True,
                "finding closed without reviewer approval",
            )
        require(finding["status"] != "submitted", "unreviewed resolution")
    pending = [f for f in d["findings"].values() if f["status"] != "accepted"]
    if pending:
        require(
            d["round"] == d["max_review_rounds"] and d["review_limit_reached"],
            "premature partial publication",
        )
    report = {
        "verified": True,
        "task_id": d["task_id"],
        "revision": d["revision"],
        "source_batches": len(batches),
        "largest_source_response_tokens": largest_source,
        "reviews": summary,
        "findings": dict(Counter(f["status"] for f in d["findings"].values())),
        "complete": w.publication_model()["meta"]["complete"],
        "phases": phase_metrics(d, events),
        "limitation": (
            "Verifies source/model delivery and workflow invariants, not semantic completeness."
        ),
    }
    supervisor_path = w.root / "supervisor.json"
    if supervisor_path.exists():
        supervisor = json.loads(supervisor_path.read_text())
        terminals = supervisor["terminals"]
        require(set(terminals) == {"parser", "reviewer"}, "missing independent agent session")
        require(terminals["parser"] != terminals["reviewer"], "roles share a terminal")
        dispatches_path = w.root / "supervisor-events.jsonl"
        sent = [
            e
            for e in map(json.loads, dispatches_path.read_text().splitlines())
            if e["kind"] == "task_sent"
        ]
        retired = {(e["role"], e["terminal"]) for e in supervisor.get("retired_terminals", [])}
        for e in sent:
            require(
                e["terminal"] == terminals[e["role"]] or (e["role"], e["terminal"]) in retired,
                "handoff uses an untracked agent session",
            )
        for transition in transitions:
            require(
                any(e["dispatch_id"] == transition["dispatch_id"] for e in sent),
                "completed phase has no HC dispatch evidence",
            )
        report["sessions"] = {"terminals": terminals, "handoffs": len(sent)}
    return report
