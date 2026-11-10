"""Coordinator transitions and worker-scoped tools over the versioned store."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from rsc.protocol.context import document_digest
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import validate_protocol

from . import merge
from .evidence import expand_evidence, source_citations
from .mutations import plan_changes, select, target
from .paging import records_page, resolve_budget, seal_response
from .partition import partition_batches
from .review import (
    assign_checks,
    carry_forward,
    closure,
    fix_groups,
    object_ref,
    review_errors,
    source_context,
)
from .sources import prepare, validate_batches
from .store import Store, StoreError, dumps
from .tools import PHASE_TOOLS, validate_arguments

WorkflowError = StoreError
INTERNAL = {"workflow_status", "prepare_sources", "advance_phase", "record_send", "replace_attempt"}


def delta(before, after):
    return {
        k: (before.get(k), after.get(k))
        for k in dict.fromkeys([*before, *after])
        if before.get(k) != after.get(k)
    }


class Workflow:
    def __init__(self, root, *, readonly=False):
        self.store = Store(root, readonly=readonly)
        self.root, self.db, self.lock = self.store.root, self.store.db, self.store.lock
        self.documents, self.batches = self.store.sources()

    @property
    def data(self):
        return self.store.settings()

    @classmethod
    def create(
        cls,
        root,
        sources,
        protocol,
        *,
        target_tokens=8000,
        max_tokens=12000,
        max_review_rounds=5,
        parse_workers=3,
        review_workers=3,
    ):
        if not isinstance(protocol, str) or not protocol.strip():
            raise WorkflowError("protocol must be a nonempty name and version")
        for name, number in {
            "max_review_rounds": max_review_rounds,
            "parse_workers": parse_workers,
            "review_workers": review_workers,
        }.items():
            if type(number) is not int or number < 1:
                raise WorkflowError(f"{name} must be a positive integer")
        if not 128 <= target_tokens <= max_tokens:
            raise WorkflowError("batch limits must satisfy 128 <= target <= maximum")
        root = Path(root).resolve()
        Store.create(
            root,
            {
                "name": root.name,
                "phase": "preprocess",
                "revision": 0,
                "round": 0,
                "protocol": protocol,
                "sources": [str(Path(p).resolve()) for p in sources],
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
                "max_review_rounds": max_review_rounds,
                "parse_workers": parse_workers,
                "review_workers": review_workers,
                "started_at": time.time(),
            },
        ).close()
        return cls(root)

    def authenticate(self, token):
        return self.store.authenticate(token)

    def allowed(self, identity):
        if identity.role == "coordinator":
            return INTERNAL
        a = self.store.assignment(identity.assignment)
        if a["attempt"] != identity.attempt or a["status"] in {"submitted", "failed"}:
            return set()
        if a["status"] != "claimed":
            return {"claim_assignment"}
        return PHASE_TOOLS[a["phase"]] | {"claim_assignment"}

    def catalog(self, identity):
        if identity.role == "coordinator":
            return set()
        phases = ["review"] if identity.role == "reviewer" else ["parse", "merge", "fix"]
        return {"claim_assignment"}.union(*(PHASE_TOOLS[p] for p in phases))

    def status(self):
        return {
            **self.data,
            "assignments": self.store.assignments(),
            "sources_prepared": bool(self.batches),
        }

    def call(self, identity, name, arguments=None, *, origin="agent"):
        arguments = {} if arguments is None else arguments
        started = time.monotonic()
        with self.lock:
            before = self.data
            try:
                if name not in self.allowed(identity):
                    # Submission retries after a lost response are safe but cannot
                    # authorize further reads or writes on a sealed assignment.
                    if name == "advance_phase" and identity.role != "coordinator":
                        result = self.store.submit(identity)
                        return {"assignment": result, "stop_turn": True, "allowed_tools": []}
                    raise WorkflowError(f"tool {name} is forbidden for {identity.worker}")
                if identity.role == "coordinator":
                    result = self.internal(identity, name, arguments)
                else:
                    validate_arguments(name, arguments)
                    session = Worker(self, identity)
                    result = getattr(session, name)(**arguments)
                event = {"status": "ok"}
            except (ValueError, KeyError, TypeError) as error:
                event = {"status": "rejected", "error": str(error)}
                raise WorkflowError(str(error)) from error
            finally:
                if "event" in locals():
                    event.update(
                        time=time.time(),
                        worker=identity.worker,
                        role=identity.role,
                        assignment=identity.assignment,
                        attempt=identity.attempt,
                        phase=before["phase"],
                        round=before["round"],
                        tool=name,
                        origin=origin if identity.role == "coordinator" else "agent",
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                    with self.store.transaction():
                        self.db.execute("INSERT INTO events(body) VALUES (?)", (dumps(event),))
            return result

    def internal(self, identity, name, arguments):
        self.store.require_coordinator(identity)
        if name == "workflow_status":
            return self.status()
        if name == "record_send":
            self.store.record_send(identity, **arguments)
            return self.status()
        if name == "replace_attempt":
            return self.store.replace_attempt(identity, **arguments)
        if name == "prepare_sources":
            if self.data["phase"] != "preprocess":
                raise WorkflowError("sources can only be prepared before parse")
            if not self.batches:
                docs, batches = prepare(
                    self.root,
                    self.data["sources"],
                    self.data["target_tokens"],
                    self.data["max_tokens"],
                )
                self.store.install_sources(identity, docs, batches)
                self.documents, self.batches = docs, batches
            snapshots = self.root / "snapshots"
            snapshots.mkdir(exist_ok=True)
            for name, doc in self.documents.items():
                path = snapshots / f"{name}.json"
                if path.exists():
                    if json.loads(path.read_text()) != doc:
                        raise WorkflowError(f"source snapshot changed: {name}")
                else:
                    path.write_text(json.dumps(doc, ensure_ascii=False))
            validate_batches(self.documents, self.batches, self.data["max_tokens"])
            return {"documents": len(self.documents), "batches": len(self.batches)}
        if name == "advance_phase":
            return self.advance(identity)
        raise WorkflowError("unknown coordinator operation")

    def snapshot(self, revision=None):
        revision = self.data["revision"] if revision is None else revision
        if revision == 0:
            return {}
        row = self.db.execute("SELECT body FROM snapshots WHERE revision=?", (revision,)).fetchone()
        if row is None:
            raise WorkflowError("unknown frozen revision")
        return json.loads(row[0])["records"]

    def findings(self):
        return {
            name: json.loads(body)
            for name, body in self.db.execute("SELECT name, body FROM findings")
        }

    def check_sources(self):
        if not self.documents or not self.batches:
            raise WorkflowError("sources have not been prepared")
        validate_batches(self.documents, self.batches, self.data["max_tokens"])
        for name, doc in self.documents.items():
            path = self.root / "snapshots" / f"{name}.json"
            if not path.exists() or json.loads(path.read_text()) != doc:
                raise WorkflowError(f"source snapshot changed or unavailable: {name}")

    def source_assignments(self, phase, workers, round_):
        role = "reviewer" if phase == "review" else "parser"
        result = []
        for i, batches in enumerate(partition_batches(self.batches, workers), 1):
            worker = f"{role}-{i}"
            name = f"{phase}-{round_}/{worker}"
            result.append(
                {
                    "name": name,
                    "worker": worker,
                    "batches": batches,
                    **({"draft": name} if phase == "parse" else {}),
                }
            )
        return result

    def advance(self, coordinator):
        self.check_sources()
        data = self.data
        phase, round_, revision = data["phase"], data["round"], data["revision"]
        active = [
            a for a in self.store.assignments() if a["phase"] == phase and a["round"] == round_
        ]
        if phase != "preprocess" and (
            not active or any(a["status"] != "submitted" for a in active)
        ):
            raise WorkflowError("all current assignments must be submitted before advancing")
        if phase == "preprocess":
            if not self.batches:
                raise WorkflowError("prepare sources first")
            assignments = self.source_assignments("parse", data["parse_workers"], 0)
            self.store.activate(coordinator, assignments, phase="parse", round_=0, base_revision=0)
        elif phase in {"parse", "fix"}:
            drafts = [(a["name"], self.store.draft(a["draft"])[1]) for a in active]
            checks = []
            if phase == "fix":
                base = self.snapshot()
                patches = [(scope, delta(base, records)) for scope, records in drafts]
                changed = set().union(*(set(patch) for _, patch in patches))
                drafts = [("base", {k: r for k, r in base.items() if k not in changed})]
                drafts.extend(
                    (scope, {k: after for k, (_, after) in patch.items() if after})
                    for scope, patch in patches
                )
                for key in changed:
                    writes = [(scope, patch[key]) for scope, patch in patches if key in patch]
                    if len(writes) > 1 and len({dumps(after) for _, (_, after) in writes}) > 1:
                        checks.append(
                            {
                                "name": f"patch-conflict-{len(checks) + 1}",
                                "type": "patch_conflict",
                                "description": "Overlapping Fix patches need a decision",
                                "writes": writes,
                                "status": "open",
                            }
                        )
            for a in active:
                for (body,) in self.db.execute(
                    "SELECT body FROM reports WHERE assignment=? ORDER BY rowid", (a["name"],)
                ):
                    for obligation in json.loads(body).get("obligations", []):
                        checks.append(
                            {
                                "name": f"obligation-{len(checks) + 1}",
                                "type": "obligation",
                                **obligation,
                                "status": "open",
                            }
                        )
            candidates = merge.combine(drafts)
            name = f"merge-{round_}/parser-1"
            self.store.activate(
                coordinator,
                [
                    {
                        "name": name,
                        "worker": "parser-1",
                        "batches": [],
                        "draft": name,
                        "records": candidates,
                        "checks": checks,
                    }
                ],
                phase="merge",
                round_=round_,
                base_revision=revision,
            )
        elif phase == "merge":
            _, candidates = self.store.draft(active[0]["draft"])
            if merge.conflicts(candidates, active[0]["checks"]):
                raise WorkflowError("merge conflicts remain")
            records = merge.final_records(candidates)
            if not records:
                raise WorkflowError("model must not be empty")
            next_revision = revision + 1
            snapshot = {
                "records": records,
                "name_map": [{"source": origin, "final": object_ref(r)}
                             for r in candidates.values() for origin in r["origins"]],
                "merge_assignment": active[0]["name"],
            }
            encoded_snapshot = dumps(snapshot)
            with self.store.transaction():
                existing = self.db.execute(
                    "SELECT body FROM snapshots WHERE revision=?", (next_revision,)
                ).fetchone()
                if existing and existing[0] != encoded_snapshot:
                    raise WorkflowError("frozen snapshot differs from resumed merge")
                self.db.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?, ?)",
                    (next_revision, encoded_snapshot),
                )
            assignments = self.source_assignments("review", data["review_workers"], round_ + 1)
            assign_checks(records, assignments, self.batches)
            findings = self.findings()
            for a in assignments:
                a["findings"] = [
                    n
                    for n, f in findings.items()
                    if f["reviewer"] == a["worker"] and f["status"] == "submitted"
                ]
            if revision:
                previous = {
                    name: json.loads(body)
                    for name, body in self.db.execute(
                        "SELECT name, body FROM conclusions WHERE revision=?", (revision,)
                    )
                }
                carry_forward(self.snapshot(revision), records, previous, assignments, revision)
            self.store.activate(
                coordinator,
                assignments,
                phase="review",
                round_=round_ + 1,
                base_revision=next_revision,
            )
        elif phase == "review":
            errors = [
                error for a in active for error in review_errors(self.store, a, self.snapshot())
            ]
            if errors:
                raise WorkflowError("review coverage gate: " + "; ".join(errors))
            findings = self.findings()
            pending = {n: f for n, f in findings.items() if f["status"] != "accepted"}
            if any(f["status"] == "submitted" for f in findings.values()):
                raise WorkflowError("unreviewed resolutions remain")
            if pending and round_ < data["max_review_rounds"]:
                base = self.snapshot()
                assignments = []
                for i, names in enumerate(fix_groups(pending, data["parse_workers"]), 1):
                    name = f"fix-{round_}/parser-{i}"
                    batches = {e["batch_id"] for n in names for e in pending[n]["evidence"]}
                    assignments.append(
                        {
                            "name": name,
                            "worker": f"parser-{i}",
                            "draft": name,
                            "records": base,
                            "batches": [
                                b["batch_id"] for b in self.batches if b["batch_id"] in batches
                            ],
                            "findings": names,
                        }
                    )
                self.store.activate(
                    coordinator, assignments, phase="fix", round_=round_, base_revision=revision
                )
            else:
                errors = validate_protocol(self.publication_model(), self.documents)
                if errors:
                    raise WorkflowError("publication validation: " + "; ".join(errors))
                with self.store.transaction():
                    data.update(
                        phase="published",
                        review_limit_reached=bool(pending),
                        finished_at=time.time(),
                    )
                    self.db.execute("UPDATE workflow SET body=?", (dumps(data),))
                self.export()
        else:
            raise WorkflowError("terminal workflow")
        return self.status()

    def publication_model(self):
        records = self.snapshot()
        unresolved = [r["value"] for r in records.values() if r["kind"] == "unresolved"]
        unresolved += [
            {
                "description": f["description"],
                "object_names": [r["target"].get("name", "") for r in f["objects"]],
            }
            for f in self.findings().values()
            if f["status"] != "accepted" or f.get("resolution", {}).get("status") == "unresolved"
        ]
        return {
            "meta": {
                "schema_version": "8.0.0",
                "protocol": self.data["protocol"],
                "complete": not unresolved,
                "documents": [
                    {
                        "doc_id": name,
                        "rfc": doc["meta"]["rfc"],
                        "input_sha256": doc["meta"]["input_sha256"],
                        "document_sha256": document_digest(doc),
                        "path": f"snapshots/{name}.json",
                    }
                    for name, doc in self.documents.items()
                ],
            },
            **{
                kind: [r["value"] for r in records.values() if r["kind"] == kind]
                for kind in COLLECTIONS
            },
            "unresolved": unresolved,
        }

    def export(self):
        if self.data["phase"] != "published":
            raise WorkflowError("publication requires completed independent review")
        self.check_sources()
        model = self.publication_model()
        errors = validate_protocol(model, self.documents)
        if errors:
            raise WorkflowError("; ".join(errors))
        reports = {
            "batches.json": self.batches,
            "review.json": self.findings(),
            "assignments.json": self.store.assignments(),
            "report.json": {
                "rounds": self.data["round"],
                "complete": model["meta"]["complete"],
                "review_limit_reached": self.data["review_limit_reached"],
            },
            "protocol.json": model,
        }
        for name, body in reports.items():
            path = self.root / name
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            tmp.replace(path)


class Worker:
    """One tool invocation; no worker-specific mutable state is shared by threads."""

    def __init__(self, workflow, identity):
        self.w, self.s, self.who = workflow, workflow.store, identity
        self.a = self.s.owned(identity, claimed=False)
        if self.a["draft"]:
            draft, self.records = self.s.draft(self.a["draft"])
            self.revision = draft["revision"]
        else:
            self.records, self.revision = (
                self.w.snapshot(self.a["base_revision"]),
                self.a["base_revision"],
            )
        self.batches = {b["batch_id"]: b for b in workflow.batches}
        self.citations = source_citations(workflow.batches)

    def status(self):
        a = self.s.assignment(self.a["name"])
        public = {k: v for k, v in a.items() if k != "decisions"}
        public["checks"] = [
            {
                "name": c["name"],
                "batches": c.get("batches", []),
                "record_count": len(c.get("records", [])),
                "type": c.get("type", "cross_range"),
            }
            for c in a["checks"]
        ]
        completed_checks = {
            c["name"]
            for c in a["checks"]
            if self.w.db.execute(
                "SELECT 1 FROM conclusions WHERE name=? AND revision=?",
                (f"{a['name']}/{c['name']}", self.revision),
            ).fetchone()
        }
        return {
            "assignment": public,
            "revision": self.revision,
            "pending_batches": [b for b in a["batches"] if b not in self.s.completed(a["name"])],
            "pending_checks": [c["name"] for c in a["checks"] if c["name"] not in completed_checks],
            "allowed_tools": sorted(self.w.allowed(self.who)),
            "stop_turn": a["status"] == "submitted",
        }

    def claim_assignment(self, name, attempt):
        self.s.claim(self.who, name, attempt)
        return self.status()

    def version(self, revision, cursor=0):
        if cursor and revision is None:
            raise WorkflowError("continued pages require their revision")
        if revision is not None and revision != self.revision:
            raise WorkflowError(f"revision changed; current revision={self.revision}")

    def page(self, rows, cursor=0, max_tokens=None, revision=None):
        self.version(revision, cursor)
        return records_page(rows, cursor, max_tokens, self.revision)

    def list_batches(self, cursor=0, max_tokens=None, revision=None, assigned_only=False):
        done = self.s.completed(self.a["name"])
        rows = [
            {
                "batch_id": b["batch_id"],
                "doc_id": b["doc_id"],
                "section_path": b["section_path"],
                "assigned": b["batch_id"] in self.a["batches"],
                "complete": b["batch_id"] in done,
            }
            for b in self.w.batches
            if not assigned_only or b["batch_id"] in self.a["batches"]
        ]
        return self.page(rows, cursor, max_tokens, revision)

    def read_batches(self, batch_ids, max_tokens=None):
        if len(batch_ids) != len(set(batch_ids)) or not set(batch_ids) <= set(self.batches):
            raise WorkflowError("batch_ids must be unique known batches")
        ordered = [b for b in self.batches if b in batch_ids]
        citations = {(e["batch_id"], e["block_id"]): name for name, e in self.citations.items()}
        prepared = [
            {
                "batch": {
                    **self.batches[b],
                    "segments": [
                        {**s, "citation": citations[(b, s["block_id"])]}
                        for s in self.batches[b]["segments"]
                    ],
                },
                "receipt": f"{self.a['name']}/{self.a['attempt']}/{b}",
            }
            for b in ordered
        ]
        budget = resolve_budget(max_tokens)

        def render(count):
            return seal_response(
                {
                    "batches": prepared[:count],
                    "remaining": ordered[count:],
                    "delivered_ids": ordered[:count],
                    "max_tokens": budget,
                    "format": "records",
                    "revision": self.revision,
                }
            )

        count = 0
        while count < len(prepared) and render(count + 1)["tokens"] <= budget:
            count += 1
        if not count:
            raise WorkflowError(f"first batch requires at least {render(1)['tokens']} tokens")
        result = render(count)
        self.s.deliver(self.who, ordered[:count])
        return result

    def search_source(self, query, batch_ids=None, cursor=0, max_tokens=None, revision=None):
        rows = [
            {"batch_id": b["batch_id"], **segment}
            for b in self.w.batches
            if batch_ids is None or b["batch_id"] in batch_ids
            for segment in b["segments"]
            if query.casefold() in segment["text"].casefold()
        ]
        return self.page(rows, cursor, max_tokens, revision)

    def evidence(self, entries):
        if not entries:
            raise WorkflowError("source evidence is required")
        errors = []
        for e in entries:
            if e.get("batch_id") not in self.batches or e.get("block_id") not in {
                s["block_id"] for s in self.batches[e["batch_id"]]["segments"]
            }:
                errors.append(f"invalid source pair {e!r}")
            else:
                try:
                    self.s.receipt(
                        self.who,
                        e["batch_id"],
                        f"{self.a['name']}/{self.a['attempt']}/{e['batch_id']}",
                    )
                except ValueError as error:
                    errors.append(str(error))
        if errors:
            raise WorkflowError("; ".join(errors))

    def expand(self, entries):
        result = expand_evidence(entries, self.citations)
        self.evidence(result)
        return result

    def resolve_objects(self, objects):
        return [select(self.records, r["kind"], r["target"]) for r in objects]

    def remember(self, family, rows):
        encoded = [(dumps(name), dumps(body)) for name, body in rows]
        with self.s.transaction():
            self.s.owned(self.who)
            self.w.db.executemany(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (self.a["name"], self.a["attempt"], self.revision, family, n, b)
                    for n, b in encoded
                ),
            )

    def delivered(self, family):
        return {
            json.loads(n)
            for (n,) in self.w.db.execute(
                "SELECT name FROM deliveries WHERE assignment=? AND attempt=? "
                "AND revision=? AND family=?",
                (self.a["name"], self.a["attempt"], self.revision, family),
            )
        }

    def query_model(
        self,
        view="current",
        kind=None,
        filters=None,
        batch_ids=None,
        scope=None,
        check_name=None,
        cursor=0,
        max_tokens=None,
        revision=None,
    ):
        self.version(revision, cursor)
        if view == "conflicts":
            if self.a["phase"] != "merge":
                raise WorkflowError("conflicts view requires a merge assignment")
            return self.page(
                merge.conflicts(self.records, self.a["checks"]), cursor, max_tokens, revision
            )
        records = self.records
        if view == "sealed":
            records = {}
            for a in self.s.assignments():
                if (
                    a["draft"]
                    and a["status"] == "submitted"
                    and (scope is None or scope == a["worker"])
                ):
                    for key, r in self.s.draft(a["draft"])[1].items():
                        records[dumps([a["name"], key])] = {**r, "scope": a["worker"]}
        selected = source_context(records, batch_ids) if batch_ids is not None else set(records)
        check = None
        if check_name is not None:
            check = next((c for c in self.a["checks"] if c["name"] == check_name), None)
            if self.a["phase"] != "review" or check is None or view != "current":
                raise WorkflowError("check_name requires an assigned frozen-model review check")
            selected &= set(check["records"])
        rows = [
            {**r, "_key": key}
            for key, r in records.items()
            if key in selected
            and (not kind or r["kind"] == kind)
            and (not filters or all(r["value"].get(k) == v for k, v in filters.items()))
            and (scope is None or view == "sealed" or r.get("scope") == scope)
        ]
        if view == "index":
            rows = [{"kind": r["kind"], "target": target(r), "_key": r["_key"]} for r in rows]
        elif check:
            for row in rows:
                row["owned"] = row["_key"] in check["owned"]
        result = self.page(rows, cursor, max_tokens, revision)
        keys = [r.pop("_key") for r in result["items"]]
        if view == "current":
            self.remember("model", [(k, {}) for k in keys])
            if batch_ids is None and check_name is None and scope is None:
                query_scope = {"kind": kind, "filters": filters or {}}
                page = {"scope": query_scope, "cursor": cursor, "next": result["next_cursor"]}
                self.remember("query-page", [(dumps(page), page)])
                pages = {
                    p["cursor"]: p["next"]
                    for name in self.delivered("query-page")
                    if (p := json.loads(name))["scope"] == query_scope
                }
                next_cursor, seen = 0, set()
                while next_cursor in pages and next_cursor not in seen:
                    seen.add(next_cursor)
                    next_cursor = pages[next_cursor]
                    if next_cursor is None:
                        queries = [
                            {"kind": k, "filters": filters or {}}
                            for k in ([kind] if kind else (*COLLECTIONS, "unresolved"))
                        ]
                        self.remember("query", [(dumps(q), q) for q in queries])
                        break
        return seal_response(result)

    def apply_changes(self, expected_revision, operation_id, changes):
        args = {
            "expected_revision": expected_revision,
            "operation_id": operation_id,
            "changes": changes,
        }
        cached = self.s.previous_operation(self.who, operation_id, args)
        if cached is not None:
            return cached
        self.version(expected_revision)
        normalized = copy.deepcopy(changes)
        errors = []
        for i, change in enumerate(normalized):
            if "evidence" in change:
                try:
                    change["evidence"] = self.expand(change["evidence"])
                except ValueError as error:
                    errors.append(f"changes[{i}].evidence: {error}")
        if errors:
            raise WorkflowError("; ".join(errors) + f"; revision={self.revision}")
        checks, decisions = None, None
        if self.a["phase"] == "merge":
            records, checks, decisions = merge.apply(
                self.records, normalized, self.a["checks"], evidence_check=self.evidence
            )
            edits = delta(self.records, records)
            ledger = [merge.ref(after or before) for before, after in edits.values()]
            blockers = merge.conflicts(records, checks)
        else:
            if any("scope" in change for change in normalized):
                raise WorkflowError("ordinary edits cannot select another draft scope")
            edits, ledger, blockers = plan_changes(
                self.records, normalized, evidence_check=self.evidence
            )
        result = {
            "revision": self.revision + 1,
            "records": ledger,
            "draft_reference_errors": blockers,
        }
        return self.s.commit_records(
            self.who,
            revision=self.revision,
            operation_id=operation_id,
            arguments=args,
            changes=edits,
            result=result,
            checks=checks,
            decisions=decisions,
        )

    def complete_batch(self, revision, reports):
        self.version(revision)
        source_reports, conclusions = [], []
        for item in reports:
            report = copy.deepcopy(item["report"])
            objects = self.resolve_objects(report["objects"])
            name = item.get("batch_id") or item["check_name"]
            if "batch_id" in item:
                if name not in self.a["batches"]:
                    raise WorkflowError("cannot complete another worker's batch")
                self.s.receipt(self.who, name, item["receipt"])
                required = (
                    source_context(self.records, [name]) if self.a["phase"] == "review" else set()
                )
                required_sources = [name]
                source_reports.append({"batch": name, "receipt": item["receipt"], "report": report})
            else:
                check = next((c for c in self.a["checks"] if c["name"] == name), None)
                if self.a["phase"] != "review" or check is None:
                    raise WorkflowError("unknown assigned cross-range check")
                required = set(check["records"])
                required_sources = check["batches"]
                if not set(check["owned"]) <= set(objects):
                    raise WorkflowError(
                        "cross-range report must account for every owned model record"
                    )
                for batch in check["batches"]:
                    self.s.receipt(self.who, batch, f"{self.a['name']}/{self.a['attempt']}/{batch}")
            if self.a["phase"] == "review":
                required |= closure(self.records, objects)
                if not required <= self.delivered("model"):
                    raise WorkflowError(
                        "read the complete associated model context before reporting"
                    )
                if not report.get("requirements"):
                    raise WorkflowError("review reports require actual source/model comparisons")
                findings = self.w.findings()
                for requirement in report["requirements"]:
                    self.resolve_objects(requirement["objects"])
                    if requirement["result"] == "finding" and not requirement["findings"]:
                        raise WorkflowError("a discrepancy requires submitted findings")
                    for finding in requirement["findings"]:
                        if (
                            finding not in findings
                            or findings[finding]["reviewer"] != self.who.worker
                        ):
                            raise WorkflowError("unknown independently submitted finding")
                        for query in findings[finding].get("queries", []):
                            if query not in report.setdefault("queries", []):
                                report["queries"].append(query)
                for query in report.get("queries", []):
                    if dumps(query) not in self.delivered("query"):
                        raise WorkflowError(
                            "read the complete model query scope before asserting absence"
                        )
                conclusions.append(
                    (
                        f"{self.a['name']}/{name}",
                        {
                            "assignment": self.a["name"],
                            "attempt": self.a["attempt"],
                            "batch": item.get("batch_id"),
                            "check": item.get("check_name"),
                            "records": sorted(set(objects) | required),
                            "source_batches": required_sources,
                            "queries": report.get("queries", []),
                            "report": report,
                        },
                    )
                )
            else:
                for obligation in report.get("obligations", []):
                    obligation["evidence"] = self.expand(obligation["evidence"])
        self.s.complete(self.who, source_reports, revision, conclusions=conclusions)
        return self.status()

    def get_changes(self, from_revision, cursor=0, max_tokens=None, revision=None):
        self.version(revision, cursor)
        if self.a["draft"]:
            rows = [
                {"revision": v, **json.loads(body)}
                for v, body in self.w.db.execute(
                    "SELECT revision, body FROM history "
                    "WHERE draft=? AND revision>? ORDER BY revision",
                    (self.a["draft"], from_revision),
                )
            ]
        else:
            before = self.w.snapshot(from_revision)
            rows = [
                {"target": object_ref(a or b), "before": b, "after": a}
                for b, a in delta(before, self.records).values()
            ]
        return self.page(rows, cursor, max_tokens, revision)

    def list_findings(self, cursor=0, max_tokens=None, revision=None):
        rows = [
            {"name": name, **f}
            for name, f in self.w.findings().items()
            if name in self.a["findings"] or f["reviewer"] == self.who.worker
        ]
        return self.page(rows, cursor, max_tokens, revision)

    def submit_findings(self, revision, findings):
        self.version(revision)
        values = []
        for finding in findings:
            body = copy.deepcopy(finding)
            body["evidence"] = self.expand(body["evidence"])
            body["queries"] = []
            queries = [json.loads(q) for q in self.delivered("query")]
            for obj in body["objects"]:
                try:
                    select(self.records, obj["kind"], obj["target"])
                except ValueError:
                    covering = [
                        q
                        for q in queries
                        if q["kind"] == obj["kind"]
                        and all(
                            k in obj["target"] and obj["target"][k] == v
                            for k, v in q["filters"].items()
                        )
                    ]
                    if not covering:
                        raise WorkflowError(
                            "an absence or ambiguity finding requires "
                            "its complete model query scope"
                        ) from None
                    body["queries"].extend(q for q in covering if q not in body["queries"])
            name = f"{self.a['name']}/{body.pop('name')}"
            body.update(reviewer=self.who.worker, revision=revision, status="open", history=[])
            if not any(e["batch_id"] in self.a["batches"] for e in body["evidence"]) and body.get(
                "check_name"
            ) not in {c["name"] for c in self.a["checks"]}:
                raise WorkflowError("finding must belong to your source or cross-range checks")
            values.append((name, body))
        with self.s.transaction():
            self.s.owned(self.who)
            for name, body in values:
                old = self.w.db.execute(
                    "SELECT body FROM findings WHERE name=?", (name,)
                ).fetchone()
                if old and old[0] != dumps(body):
                    raise WorkflowError("finding name already used for different content")
                self.w.db.execute(
                    "INSERT OR IGNORE INTO findings VALUES (?, ?)", (name, dumps(body))
                )
        return {"names": [name for name, _ in values], "revision": revision}

    def submit_resolutions(self, revision, resolutions):
        self.version(revision)
        findings = self.w.findings()
        for resolution in resolutions:
            name = resolution["name"]
            if name not in self.a["findings"] or findings[name]["status"] not in {
                "open",
                "submitted",
            }:
                raise WorkflowError("resolution is not assigned to this parser")
            value = copy.deepcopy(resolution)
            value["evidence"] = self.expand(value["evidence"])
            self.resolve_objects(value["objects"])
            if findings[name]["status"] == "submitted" and findings[name]["resolution"] != value:
                raise WorkflowError("resolution already submitted with different content")
            findings[name].update(
                status="submitted",
                resolution=value,
                fix_assignment=self.a["name"],
                fix_revision=revision,
            )
        with self.s.transaction():
            self.s.owned(self.who)
            for resolution in resolutions:
                name = resolution["name"]
                self.w.db.execute(
                    "UPDATE findings SET body=? WHERE name=?", (dumps(findings[name]), name)
                )
        return {"names": [r["name"] for r in resolutions]}

    def review_resolutions(self, revision, reviews):
        self.version(revision)
        findings = self.w.findings()
        for review in reviews:
            name = review["name"]
            if name not in self.a["findings"]:
                raise WorkflowError("resolution is not assigned to this reviewer")
            f = findings[name]
            if f["status"] != "submitted":
                raise WorkflowError("resolution already reviewed")
            value = {
                **review,
                "evidence": self.expand(review["evidence"]),
                "revision": revision,
                "reviewer": self.who.worker,
                "assignment": self.a["name"],
            }
            required = source_context(self.records, [e["batch_id"] for e in f["evidence"]])
            if not required <= self.delivered("model"):
                raise WorkflowError("read the changed model context before accepting a resolution")
            f["history"].append(value)
            f["status"] = "accepted" if review["accepted"] else "open"
        with self.s.transaction():
            self.s.owned(self.who)
            for review in reviews:
                self.w.db.execute(
                    "UPDATE findings SET body=? WHERE name=?",
                    (dumps(findings[review["name"]]), review["name"]),
                )
        return {"names": [r["name"] for r in reviews]}

    def advance_phase(self):
        blockers = []
        if self.a["phase"] == "merge":
            blockers.extend(merge.conflicts(self.records, self.a["checks"]))
        if self.a["phase"] == "review":
            blockers.extend(review_errors(self.s, self.a, self.records))
            for name in self.a["findings"]:
                if self.w.findings()[name]["status"] == "submitted":
                    blockers.append(f"resolution {name} awaits independent acceptance")
        if self.a["phase"] == "fix":
            for name in self.a["findings"]:
                f = self.w.findings()[name]
                if f["status"] != "submitted" or f["fix_revision"] != self.revision:
                    blockers.append(f"resolution {name} must match the final draft revision")
        if blockers:
            raise WorkflowError("gate rejected: " + dumps(blockers))
        self.s.submit(self.who)
        return {**self.status(), "stop_turn": True}
