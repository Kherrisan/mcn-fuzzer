"""Read-only reconstruction of the parallel workflow's authoritative evidence.

Reuse mutation, merge, evidence and review gates; never treat a submitted flag
or an exported report as proof that its inputs were actually delivered.
"""

import json
from collections import Counter
from copy import deepcopy
from functools import partial

from rsc.agents import AgentContext
from rsc.protocol.validate import validate_protocol

from . import merge
from .evidence import expand_evidence, source_citations, validate_evidence
from .mutations import plan_changes, select
from .review import absence_queries, closure, query_delivered, source_context
from .state import Workflow, WorkflowError, delta
from .store import Identity
from .tools import validate_arguments


def require(condition, message):
    if not condition:
        raise WorkflowError(f"audit failed: {message}")


def audit_handoffs(w):
    path = w.root / "supervisor.json"
    if not path.exists():
        return {"verified": False, "reason": "no supervisor artifacts; programmatic workflow"}
    saved = json.loads(path.read_text())
    terminals = saved.get("retired", []) + [
        {**terminal, "worker": worker} for worker, terminal in saved["terminals"].items()
    ]
    checked = 0
    for a in w.store.assignments():
        for attempt, status, encoded in w.db.execute(
            "SELECT attempt,status,body FROM attempts WHERE assignment=? ORDER BY attempt",
            (a["name"],),
        ):
            body = json.loads(encoded)
            if "claimed_at" not in body:
                proofs = [
                    json.loads(r[0])
                    for r in w.db.execute(
                        "SELECT body FROM conclusions WHERE json_extract(body,'$.assignment')=?",
                        (a["name"],),
                    )
                ]
                require(
                    status == "failed"
                    or (
                        a["phase"] == "review"
                        and len(proofs) == len(a["batches"]) + len(a["checks"])
                        and all(p["attempt"] == 0 and p.get("carried_from") for p in proofs)
                    ),
                    "HC assignment completed without a claim or carried conclusions",
                )
                continue
            end = body.get("submitted_at", body.get("finished_at"))
            require(
                body["created_at"] <= body.get("send_intent_at", 0) <= body["claimed_at"] <= end,
                "HC claim lacks prior persisted send intent",
            )
            matches = [
                t for t in terminals if t["assignment"] == a["name"] and t["attempt"] == attempt
            ]
            require(len(matches) == 1, "HC assignment has no unique recorded terminal")
            terminal = matches[0]
            expected = AgentContext(w.root, "", w.data["name"], a["role"], worker=a["worker"])
            require(
                terminal["worker"] == a["worker"]
                and terminal["name"] == expected.terminal_name
                and terminal["ready"]
                and terminal["address"],
                "HC terminal identity or readiness differs",
            )
            session = saved["sessions"][a["worker"]]
            require(
                session["name"] == terminal["name"]
                and json.loads((expected.directory / "session.json").read_text()) == session,
                "HC terminal and saved agent session names differ",
            )
            checked += 1
    return {
        "verified": True,
        "claimed_attempts": checked,
        "cleanup_complete": saved.get("cleanup_complete", False),
    }


def audit_run(root):
    w = Workflow(root, readonly=True)
    try:
        # All SQL reads use one consistent snapshot, even if a diagnostic opens
        # the database while a service is finishing its export.
        w.db.execute("BEGIN")
        return _audit(w)
    except (KeyError, TypeError, IndexError, StopIteration) as error:
        raise WorkflowError(f"audit failed: incomplete or malformed evidence: {error}") from error
    finally:
        w.db.close()


def _audit(w):
    data, assignments = w.data, w.store.assignments()
    require(data["phase"] == "published", "task is not published")
    require(1 <= data["round"] <= data["max_review_rounds"], "invalid review round")
    require(data["revision"] == data["round"], "frozen revision/round mismatch")
    w.check_sources()
    batches = {b["batch_id"]: b for b in w.batches}
    citations = source_citations(w.batches)
    by_name = {a["name"]: a for a in assignments}
    sequence = list(dict.fromkeys((a["phase"], a["round"]) for a in assignments))
    expected = [("parse", 0), ("merge", 0), ("review", 1)]
    for round_ in range(1, data["round"]):
        expected.extend([("fix", round_), ("merge", round_), ("review", round_ + 1)])
    require(sequence == expected, "missing or reordered workflow phase")
    require(all(a["status"] == "submitted" for a in assignments), "unfinished assignment")
    require(
        [r[0] for r in w.db.execute("SELECT revision FROM snapshots ORDER BY revision")]
        == list(range(1, data["revision"] + 1)),
        "missing frozen revision",
    )

    def receipt(a, attempt, batch):
        require(batch in batches, "unknown receipt batch")
        who = Identity(a["worker"], a["role"], a["name"], attempt)
        w.store.receipt(who, batch, f"{a['name']}/{attempt}/{batch}")

    def evidence(entries, a, attempt):
        validate_evidence(entries, batches, delivered=lambda b: receipt(a, attempt, b))

    # A history entry is reconstructed from the actual named operation and its
    # receipts, not just accepted because its before/after images form a chain.
    versions = {}
    for a in assignments:
        attempts = w.db.execute(
            "SELECT attempt, status, body FROM attempts WHERE assignment=? ORDER BY attempt",
            (a["name"],),
        ).fetchall()
        require(
            [r[0] for r in attempts] == list(range(1, a["attempt"] + 1)) and a["attempt"] <= 3,
            "invalid attempt history",
        )
        require(attempts[-1][1] == "submitted", "final attempt was not submitted")
        for _, status, encoded in attempts[:-1]:
            terminal = json.loads(encoded).get("terminal", {})
            require(
                status == "failed"
                and terminal.get("name")
                and terminal.get("state") in {"exited", "missing"},
                "retry lacks authoritative terminal evidence",
            )
        if not a["draft"]:
            require(
                a["phase"] == "review" and a["role"] == "reviewer",
                "non-review assignment has no draft",
            )
            continue
        require(a["role"] == "parser", "reviewer owns a mutable draft")
        draft, final = w.store.draft(a["draft"])
        require(
            draft["sealed"] and draft["base_revision"] == a["base_revision"],
            "unsealed or incorrectly based draft",
        )
        rows = w.db.execute(
            "SELECT revision, body FROM history WHERE draft=? ORDER BY revision", (a["draft"],)
        ).fetchall()
        require(
            [r[0] for r in rows] == list(range(draft["revision"] + 1)),
            "non-contiguous draft history",
        )
        checks = []
        if a["phase"] == "merge":
            inputs = [
                x
                for x in assignments
                if x["round"] == a["round"]
                and x["phase"] == ("parse" if a["round"] == 0 else "fix")
            ]
            records, checks = w.merge_inputs(inputs, a["base_revision"])
        else:
            records = w.snapshot(a["base_revision"]) if a["phase"] == "fix" else {}
        require(
            json.loads(rows[0][1]) == {"initial_records": records, "checks": checks},
            "draft seed differs from its sealed inputs",
        )
        versions[a["draft"], 0] = deepcopy(records)
        operations = [
            dict(row)
            for row in w.db.execute("SELECT * FROM operations WHERE assignment=?", (a["name"],))
        ]
        require(len(operations) == draft["revision"], "missing or duplicate mutation operation")
        decisions = []
        for revision, encoded in rows[1:]:
            history = json.loads(encoded)
            matches = [op for op in operations if json.loads(op["result"])["revision"] == revision]
            require(len(matches) == 1, "history has no unique named operation")
            op = matches[0]
            args = json.loads(op["arguments"])
            validate_arguments("apply_changes", args)
            require(
                args["operation_id"] == op["name"]
                and args["expected_revision"] == revision - 1
                and history["assignment"] == a["name"]
                and history["attempt"] == op["attempt"]
                and 1 <= op["attempt"] <= a["attempt"],
                "operation ownership/revision mismatch",
            )
            changes = deepcopy(args["changes"])
            for change in changes:
                if "evidence" in change:
                    change["evidence"] = expand_evidence(change["evidence"], citations)
            validate = partial(evidence, a=a, attempt=op["attempt"])
            if a["phase"] == "merge":
                after, checks, decided = merge.apply(
                    records, changes, checks, evidence_check=validate
                )
                edits = delta(records, after)
                require(
                    history["checks"] == checks and history["decisions"] == decided,
                    "merge decisions/checks differ from operation replay",
                )
                decisions.extend(decided)
            else:
                edits, _, _ = plan_changes(records, changes, evidence_check=validate)
                after = deepcopy(records)
                for key, (_, value) in edits.items():
                    if value is None:
                        after.pop(key)
                    else:
                        after[key] = value
                require(
                    history["checks"] is None and history["decisions"] is None,
                    "ordinary parser recorded merge decisions",
                )
            require(
                history["changes"]
                == [{"key": k, "before": b, "after": v} for k, (b, v) in edits.items()],
                "mutation history differs from operation replay",
            )
            records = after
            versions[a["draft"], revision] = deepcopy(records)
        require(
            records == final and checks == a["checks"] and decisions == a.get("decisions", []),
            "draft/history mismatch",
        )
        if a["phase"] == "merge":
            require(not merge.conflicts(records, checks), "unresolved merge conflict")
            snapshot = json.loads(
                w.db.execute(
                    "SELECT body FROM snapshots WHERE revision=?", (a["base_revision"] + 1,)
                ).fetchone()[0]
            )
            require(
                snapshot
                == {
                    "records": merge.final_records(records),
                    "name_map": w.name_map(records, a["name"]),
                    "merge_assignment": a["name"],
                },
                "frozen model/name mapping differs from sealed merge",
            )

    parsing = [a for a in assignments if a["phase"] == "parse"]
    expected = w.source_assignments("parse", data["parse_workers"], 0)
    require(
        [{k: a[k] for k in wanted} for a, wanted in zip(parsing, expected, strict=False)]
        == expected
        and len(parsing) == len(expected),
        "parse ownership differs from source partition",
    )
    for a in parsing:
        reports = w.db.execute("SELECT * FROM reports WHERE assignment=?", (a["name"],)).fetchall()
        require(
            Counter(r["batch"] for r in reports) == Counter(a["batches"]),
            "missing or duplicate source extraction report",
        )
        for r in reports:
            require(
                r["phase"] == "parse" and 1 <= r["attempt"] <= a["attempt"],
                "invalid extraction report owner",
            )
            receipt(a, r["attempt"], r["batch"])
            report = json.loads(r["body"])
            validate_arguments(
                "complete_batch",
                {
                    "revision": r["revision"],
                    "reports": [
                        {
                            "batch_id": r["batch"],
                            "receipt": f"{a['name']}/{r['attempt']}/{r['batch']}",
                            "report": report,
                        }
                    ],
                },
            )
            model = versions[a["draft"], r["revision"]]
            for obj in report["objects"]:
                select(model, obj["kind"], obj["target"])
            for obligation in report.get("obligations", []):
                evidence(obligation["evidence"], a, r["attempt"])

    reviews = []
    for revision in range(1, data["revision"] + 1):
        active = [
            a for a in assignments if a["phase"] == "review" and a["base_revision"] == revision
        ]
        errors = w.review_gate(active, revision)
        require(not errors, "; ".join(errors))
        conclusions = [
            json.loads(r[0])
            for r in w.db.execute("SELECT body FROM conclusions WHERE revision=?", (revision,))
        ]
        reviews.append(
            {
                "revision": revision,
                "workers": len(active),
                "conclusions": len(conclusions),
                "carried": sum(bool(c.get("carried_from")) for c in conclusions),
            }
        )

    findings = w.findings()
    for name, f in findings.items():
        author = by_name[f["assignment"]]
        require(
            author["phase"] == "review"
            and author["worker"] == f["reviewer"]
            and author["base_revision"] == f["revision"],
            "finding lacks independent author",
        )
        evidence(f["evidence"], author, f["attempt"])
        model = w.snapshot(f["revision"])
        absence_queries(f["objects"], model, f["queries"])
        delivered = {
            json.loads(r[0])
            for r in w.db.execute(
                "SELECT name FROM deliveries WHERE assignment=? AND attempt=? AND revision=? "
                "AND family='model'",
                (author["name"], f["attempt"], f["revision"]),
            )
        }
        for query in f["queries"]:
            require(
                query_delivered(
                    w.store, author["name"], f["attempt"], f["revision"], query, model, delivered
                ),
                "finding lacks complete negative-query delivery proof",
            )
        for decision in f["history"]:
            reviewer, fixer = by_name[decision["assignment"]], by_name[decision["fix_assignment"]]
            require(
                reviewer["phase"] == "review"
                and fixer["phase"] == "fix"
                and reviewer["worker"] == f["reviewer"]
                and decision["reviewer"] == reviewer["worker"]
                and reviewer["base_revision"] == decision["revision"]
                and fixer["base_revision"] + 1 == decision["revision"]
                and name in reviewer["findings"]
                and name in fixer["findings"],
                "resolution lacks assigned independent acceptance",
            )
            resolution = decision["resolution"]
            evidence(resolution["evidence"], fixer, decision["fix_attempt"])
            evidence(
                decision["evidence"] + f["evidence"] + resolution["evidence"],
                reviewer,
                decision["attempt"],
            )
            patch = versions[fixer["draft"], decision["fix_revision"]]
            require(
                decision["fix_revision"] == w.store.draft(fixer["draft"])[0]["revision"],
                "resolution does not describe final Fix draft",
            )
            for obj in resolution["objects"]:
                select(patch, obj["kind"], obj["target"])
            model = w.snapshot(decision["revision"])
            required = source_context(
                model, [e["batch_id"] for e in f["evidence"] + resolution["evidence"]]
            )
            associations = w.resolution_associations(decision, decision["revision"])
            required |= closure(
                model,
                {
                    select(model, a["final"]["kind"], a["final"]["target"])
                    for a in associations
                    if a["final"] is not None
                },
            )
            delivered = {
                json.loads(r[0])
                for r in w.db.execute(
                    "SELECT name FROM deliveries WHERE assignment=? AND attempt=? AND revision=? "
                    "AND family='model'",
                    (reviewer["name"], decision["attempt"], decision["revision"]),
                )
            }
            require(required <= delivered, "resolution reviewed without changed model context")
        require(f["status"] != "submitted", "unreviewed resolution")
        if f["status"] == "accepted":
            require(f["history"] and f["history"][-1]["accepted"], "finding accepted by its fixer")
    pending = any(f["status"] != "accepted" for f in findings.values())
    require(
        data["review_limit_reached"] == pending
        and (not pending or data["round"] == data["max_review_rounds"]),
        "premature partial publication",
    )
    model = w.publication_model()
    require(not validate_protocol(model, w.documents), "invalid published protocol")
    cutoff = json.loads((w.root / "report.json").read_text())["export_event_seq"]
    events = [
        (seq, json.loads(body))
        for seq, body in w.db.execute(
            "SELECT seq, body FROM events WHERE seq<=? ORDER BY seq", (cutoff,)
        )
    ]
    require(events and events[-1][0] == cutoff, "invalid exported event boundary")
    require(
        any(
            e["role"] == "coordinator"
            and e["tool"] == "advance_phase"
            and e["status"] == "ok"
            and e["phase"] == "review"
            and e["round"] == data["round"]
            for _, e in events
        ),
        "export omits the final publication transition",
    )
    for filename, expected in w.artifacts(events).items():
        require(
            json.loads((w.root / filename).read_text()) == expected,
            f"exported {filename} differs from authoritative state",
        )
    require(
        [json.loads(line) for line in (w.root / "events.jsonl").read_text().splitlines()]
        == [{"seq": seq, **body} for seq, body in events],
        "exported events differ from the recorded prefix",
    )
    return {
        "verified": True,
        "handoffs": audit_handoffs(w),
        "name": data["name"],
        "revision": data["revision"],
        "source_batches": len(batches),
        "assignments": len(assignments),
        "reviews": reviews,
        "findings": dict(Counter(f["status"] for f in findings.values())),
        "complete": model["meta"]["complete"],
        "limitation": "Verifies delivery and workflow invariants, not semantic completeness.",
    }
