"""Result submission and automatic scheduling over a single task-local store."""

import json
import time
from copy import deepcopy
from pathlib import Path

from rsc.protocol.context import document_digest
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import validate_protocol

from . import merge
from .evidence import validate_evidence
from .metrics import result_metrics
from .model import validate_record
from .paging import records_page, resolve_budget, seal_response
from .partition import partition_batches
from .sources import prepare, validate_batches
from .store import Store, StoreError, dumps
from .tools import PHASE_TOOLS, validate_arguments

WorkflowError = StoreError
INTERNAL = {"workflow_status", "tick", "record_send", "replace_attempt"}


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
        parse_workers=3,
        merge_workers=3,
        max_feedback_rounds=2,
    ):
        if not 128 <= target_tokens <= max_tokens:
            raise WorkflowError("invalid batch token limits")
        if any(type(n) is not int or n < 1 for n in (parse_workers, merge_workers)):
            raise WorkflowError("worker counts must be positive integers")
        if type(max_feedback_rounds) is not int or max_feedback_rounds < 1:
            raise WorkflowError("max_feedback_rounds must be a positive integer")
        Store.create(
            root,
            {
                "name": Path(root).resolve().name,
                "protocol": protocol,
                "sources": [str(Path(p).resolve()) for p in sources],
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
                "parse_workers": parse_workers,
                "merge_workers": merge_workers,
                "max_feedback_rounds": max_feedback_rounds,
                "phase": "preprocess",
                "round": 0,
                "revision": 0,
                "started_at": time.time(),
            },
        ).close()
        return cls(root)

    def authenticate(self, token):
        with self.lock:
            return self.store.authenticate(token)

    def allowed(self, identity):
        if identity.role == "coordinator":
            return INTERNAL
        a = self.store.owned(identity)
        return PHASE_TOOLS[a["phase"]] if a["status"] != "submitted" else set()

    def catalog(self, identity):
        if identity.role == "coordinator":
            return set()
        phases = ("parse", "cluster") if identity.role == "parser" else ("merge",)
        return set().union(*(PHASE_TOOLS[p] for p in phases))

    def record_catalog(self, identity, names, catalog):
        self.store.event("catalog", worker=identity.worker, catalog=catalog, size=len(names))

    def status(self):
        return {
            **self.data,
            "assignments": self.store.assignments(),
            "source_catalog": [
                {k: b[k] for k in ("batch_id", "doc_id", "section_path")} for b in self.batches
            ],
        }

    def call(self, identity, name, arguments=None, *, origin="agent"):
        arguments = arguments or {}
        started = time.monotonic()
        with self.lock:
            context = {
                "phase": self.data["phase"],
                "round": self.data["round"],
                "started_at": time.time(),
            }
            try:
                if identity.role == "coordinator":
                    if name not in INTERNAL:
                        raise WorkflowError("forbidden coordinator operation")
                    result = self.internal(name, arguments)
                else:
                    a = self.store.owned(identity)
                    context.update(
                        phase=a["phase"],
                        round=a["round"],
                        assignment=a["name"],
                        attempt=a["attempt"],
                    )
                    if name not in PHASE_TOOLS[a["phase"]]:
                        raise WorkflowError(f"tool {name} is forbidden")
                    validate_arguments(name, arguments)
                    if name.startswith("submit_"):
                        result = self.submit(a, name, arguments)
                    else:
                        if a["status"] == "submitted":
                            raise WorkflowError("assignment already submitted")
                        self.received(a)
                        result = self.read(a, name, arguments)
                self.store.event(
                    "call",
                    origin=origin,
                    tool=name,
                    worker=identity.worker,
                    **context,
                    status="ok",
                    delivery=self.delivery(name, result),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                return result
            except (ValueError, KeyError, TypeError) as error:
                self.store.event(
                    "call",
                    origin=origin,
                    tool=name,
                    worker=identity.worker,
                    **context,
                    status="rejected",
                    error=str(error),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                raise WorkflowError(str(error)) from error
            finally:
                if self.data["phase"] in {"published", "failed"}:
                    self.export_metrics()

    @staticmethod
    def delivery(name, result):
        if name not in {"read_source_batches", "search_source", "query_model"}:
            return {}
        rows = result.get("batches", result.get("items", []))
        return {
            "tokens": result.get("tokens", 0),
            "items": [
                r.get("id") or r.get("block_id") or r.get("batch_id") or r.get("name") for r in rows
            ],
        }

    def received(self, a):
        if a["status"] == "pending":
            raise WorkflowError("assignment has not been dispatched")
        if a["status"] == "sent":
            with self.store.transaction():
                self.store.mark(a, "running")

    def internal(self, name, args):
        if name == "workflow_status":
            return self.status()
        if name == "tick":
            self.advance()
            return self.status()
        a = self.store.assignment(args["name"])
        if args["attempt"] != a["attempt"]:
            raise WorkflowError("stale attempt")
        with self.store.transaction():
            if name == "record_send":
                if a["status"] != "pending":
                    raise WorkflowError("assignment is not awaiting dispatch")
                self.store.mark(a, "sent", send_intent_at=time.time())
            else:
                terminal = args["terminal"]
                if terminal.get("state") not in {"exited", "missing"} or not terminal.get("name"):
                    raise WorkflowError("replacement requires confirmed termination")
                if a["status"] in {"submitted", "failed"}:
                    raise WorkflowError("terminal assignment cannot be replaced")
                self.store.mark(a, "failed", terminal=terminal)
                self.db.execute(
                    "DELETE FROM credentials WHERE assignment=? AND attempt=?",
                    (a["name"], a["attempt"]),
                )
                if a["attempt"] < 3:
                    import secrets

                    attempt = a["attempt"] + 1
                    self.db.execute(
                        "UPDATE assignments SET attempt=?,status='pending' WHERE name=?",
                        (attempt, a["name"]),
                    )
                    self.db.execute(
                        "INSERT INTO attempts VALUES (?,?,?,?)",
                        (a["name"], attempt, "pending", dumps({"created_at": time.time()})),
                    )
                    self.db.execute(
                        "INSERT INTO credentials VALUES (?,?,?,?,?)",
                        (secrets.token_urlsafe(32), a["worker"], a["role"], a["name"], attempt),
                    )
        self.store.write_credentials()
        return self.store.assignment(a["name"])

    def done_batches(self, a):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT unit FROM submissions WHERE assignment=?", (a["name"],)
            )
        }

    def progress(self, a, **extra):
        a = self.store.assignment(a["name"])
        pending = [b for b in a.get("batches", []) if b not in self.done_batches(a)]
        return {
            "assignment": a["name"],
            "pending_batches": pending,
            "stop_turn": a["status"] == "submitted",
            "next_group": a.get("group_name") if a["status"] != "submitted" else None,
            "group": self.groups().get(a.get("group_name")) if a["status"] != "submitted" else None,
            **extra,
        }

    def submit(self, a, tool, args):
        unit = args.get("batch_id", args.get("group_name", "clustering"))
        old = self.db.execute(
            "SELECT arguments,result FROM submissions WHERE assignment=? AND attempt=? AND unit=?",
            (a["name"], a["attempt"], unit),
        ).fetchone()
        if old:
            if old[0] != dumps(args):
                raise WorkflowError("different content for completed submission")
            self.advance()
            return json.loads(old[1])
        if a["status"] == "submitted":
            raise WorkflowError("assignment already submitted")
        self.received(a)
        with self.store.transaction():
            if tool == "submit_parse_result":
                pending = [b for b in a["batches"] if b not in self.done_batches(a)]
                if not pending or args["batch_id"] != pending[0]:
                    raise WorkflowError(f"expected next batch {pending[:1]}")
                result = self.parse_items(a, args["items"])
                finished = len(pending) == 1
            elif tool == "submit_clustering_result":
                self.set_groups(args["groups"])
                result, finished = {}, True
            else:
                if args["group_name"] != a["group_name"]:
                    raise WorkflowError("group belongs to another assignment")
                if "regroup_reason" not in args:
                    g = self.groups()[a["group_name"]]
                    original = self.store.snapshot("parse")
                    kind = original[g["members"][0]]["kind"]
                    merge.validate_proposals(original, self.groups(), a["group_name"], args)
                    validate_record(kind, args["value"])
                    self.validate_evidence(args["evidence"])
                    for d in args.get("dependencies", []):
                        validate_record("step_dependencies", d["value"])
                        self.validate_evidence(d["evidence"])
                    for p in args.get("text_updates", []):
                        self.validate_evidence(p["evidence"])
                self.db.execute(
                    "UPDATE groups SET result=? WHERE name=?", (dumps(args), a["group_name"])
                )
                # Reuse this worker's session and credential for its next group.
                reserved = {
                    x.get("group_name")
                    for x in self.store.assignments()
                    if x["phase"] == "merge"
                    and x["round"] == a["round"]
                    and x["status"] != "submitted"
                }
                waiting = [
                    n for n, g in self.groups().items() if g["result"] is None and n not in reserved
                ]
                finished = not waiting
                if waiting:
                    body = json.loads(a["body"])
                    body["group_name"] = waiting[0]
                    self.db.execute(
                        "UPDATE assignments SET body=? WHERE name=?", (dumps(body), a["name"])
                    )
                result = {}
            if finished:
                self.store.mark(a, "submitted")
            # Save the immutable acknowledgment before any next assignment is created.
            self.db.execute(
                "INSERT INTO submissions VALUES (?,?,?,?,?)",
                (a["name"], a["attempt"], unit, dumps(args), "{}"),
            )
            result = self.progress(a, **result)
            self.db.execute(
                "UPDATE submissions SET result=? WHERE assignment=? AND attempt=? AND unit=?",
                (dumps(result), a["name"], a["attempt"], unit),
            )
        self.advance()
        return result

    def validate_evidence(self, entries):
        validate_evidence(entries, {b["batch_id"]: b for b in self.batches})

    def parse_items(self, a, items):
        records = self.store.items(a["worker"])
        updated, ids = set(), []
        for item in items:
            validate_record(item["kind"], item["value"])
            self.validate_evidence(item["evidence"])
            key = item.get("id")
            if key:
                if key not in records or records[key]["kind"] != item["kind"] or key in updated:
                    raise WorkflowError("item update requires a unique owned ID of the same kind")
                row = {**records[key], **deepcopy(item)}
                row["evidence"] = merge.evidence([records[key], item])
            else:
                cursor = self.db.execute(
                    "INSERT INTO items(owner,body) VALUES (?,?)", (a["worker"], "{}")
                )
                key = f"i{cursor.lastrowid}"
                row = {**deepcopy(item), "id": key, "owner": a["worker"]}
            records[key] = row
            updated.add(key)
            ids.append({"id": key, "kind": row["kind"], "name": row["value"].get("name")})
        for key, row in records.items():
            merge.bind(row, records)
            self.db.execute("UPDATE items SET body=? WHERE id=?", (dumps(row), merge.number(key)))
        return {"items": ids}

    def groups(self):
        return {
            r[0]: {**json.loads(r[1]), "result": json.loads(r[2]) if r[2] else None}
            for r in self.db.execute("SELECT name,body,result FROM groups ORDER BY rowid")
        }

    def set_groups(self, groups):
        members = merge.grouped(self.store.snapshot("parse"), groups)
        existing = self.groups()
        by_members = {tuple(g["members"]): (n, g) for n, g in existing.items()}
        next_number = max([int(n[1:]) for n in existing] or [0])
        self.db.execute("DELETE FROM groups")
        for group in members:
            if tuple(group) in by_members:
                name, previous = by_members[tuple(group)]
                result = previous["result"]
                if result and "regroup_reason" in result:
                    result = None
            else:
                next_number += 1
                name, result = f"g{next_number}", None
            self.db.execute(
                "INSERT INTO groups VALUES (?,?,?)",
                (name, dumps({"members": group}), dumps(result) if result else None),
            )

    def phase(self, name, **extra):
        data = self.data
        self.store.event(
            "phase", phase=name, previous=data["phase"], round=extra.get("round", data["round"])
        )
        self.store.settings_update(phase=name, revision=data["revision"] + 1, **extra)

    def advance(self):
        """Called after commits and on restart; no LLM or HC operation holds a write lock."""
        data = self.data
        if data["phase"] == "preprocess":
            docs, batches = prepare(
                self.root, data["sources"], data["target_tokens"], data["max_tokens"]
            )
            partitions = partition_batches(batches, data["parse_workers"])
            with self.store.transaction():
                for i, (name, doc) in enumerate(docs.items()):
                    self.db.execute("INSERT INTO documents VALUES (?,?,?)", (name, i, dumps(doc)))
                for i, batch in enumerate(batches):
                    self.db.execute(
                        "INSERT INTO batches VALUES (?,?,?)", (batch["batch_id"], i, dumps(batch))
                    )
                for i, ids in enumerate(partitions, 1):
                    self.store.assign(f"parser-{i}", "parser", "parse", 0, batches=ids)
                self.phase("parse")
            self.documents, self.batches = docs, batches
        elif data["phase"] == "parse":
            if any(a["status"] != "submitted" for a in self.store.assignments()):
                return
            records = self.store.items()
            for row in records.values():
                merge.bind(row, records)
            with self.store.transaction():
                self.store.save_snapshot("parse", records)
                self.phase("cluster")
                self.store.assign("parser-1", "parser", "cluster", 0)
        elif data["phase"] == "cluster":
            current = [
                a
                for a in self.store.assignments()
                if a["phase"] == "cluster" and a["round"] == data["round"]
            ]
            if not current or any(a["status"] != "submitted" for a in current):
                return
            with self.store.transaction():
                self.phase("merge")
            self.advance()
        elif data["phase"] == "merge":
            groups = self.groups()
            active = [
                a
                for a in self.store.assignments()
                if a["phase"] == "merge"
                and a["round"] == data["round"]
                and a["status"] != "submitted"
            ]
            assigned = {a["group_name"] for a in active}
            pending = [n for n, g in groups.items() if g["result"] is None and n not in assigned]
            busy = {a["worker"] for a in active}
            with self.store.transaction():
                for i in range(1, data["merge_workers"] + 1):
                    worker = f"merge-{i}"
                    if worker not in busy and pending:
                        self.store.assign(
                            worker,
                            "merge",
                            "merge",
                            data["round"],
                            group_name=pending.pop(0),
                            errors=data.get("errors", []),
                        )
            if active or any(g["result"] is None for g in groups.values()):
                self.store.write_credentials()
                return
            regroup = [n for n, g in groups.items() if "regroup_reason" in g["result"]]
            if regroup:
                self.retry(
                    [
                        {
                            "type": "clustering",
                            "groups": regroup,
                            "description": "reconsider groups: "
                            + dumps({n: groups[n]["result"]["regroup_reason"] for n in regroup}),
                        }
                    ],
                    cluster=True,
                )
            else:
                with self.store.transaction():
                    self.phase("review")
                self.advance()
        elif data["phase"] == "review":
            groups = self.groups()
            try:
                candidate, table, errors = merge.integrate(self.store.snapshot("parse"), groups)
            except ValueError as error:
                # Malformed cross-group proposals need an explicit global decision, never a guess.
                candidate, table = {}, {}
                errors = [{"type": "integration", "description": str(error), "groups": []}]
            with self.store.transaction():
                self.store.save_snapshot("candidate", candidate)
                self.store.save_snapshot("revision_table", table)
                self.store.save_snapshot("checks", errors)
            if errors:
                self.retry(errors, cluster=any(e["type"] == "same_name" for e in errors))
            else:
                model = self.publication_model(candidate)
                errors = validate_protocol(model, self.documents)
                with self.store.transaction():
                    if errors:
                        self.phase("failed", errors=errors, finished_at=time.time())
                    else:
                        self.store.save_snapshot("published", candidate)
                        self.phase("published", errors=[], finished_at=time.time())
                if not errors:
                    self.export()
        elif data["phase"] == "published":
            self.export()
        self.store.write_credentials()

    def retry(self, errors, *, cluster=False):
        data = self.data
        affected = {g for e in errors for g in e.get("groups", [])}
        with self.store.transaction():
            if data["round"] >= data.get("max_feedback_rounds", 2) or not affected:
                self.phase("failed", errors=errors, finished_at=time.time())
                return
            round_ = data["round"] + 1
            for name in affected:
                self.db.execute("UPDATE groups SET result=NULL WHERE name=?", (name,))
            self.phase("cluster" if cluster else "merge", round=round_, errors=errors)
            if cluster:
                self.store.assign("parser-1", "parser", "cluster", round_, errors=errors)
        if not cluster:
            self.advance()

    def read(self, a, name, args):
        revision = self.data["revision"]
        if a["phase"] == "parse":
            revision = self.db.execute(
                "SELECT count(*) FROM submissions WHERE assignment=?", (a["name"],)
            ).fetchone()[0]
        if args.get("cursor", 0) and args.get("revision") != revision:
            raise WorkflowError(f"continued page requires revision={revision}")
        if args.get("revision", revision) != revision:
            raise WorkflowError(f"revision changed: {revision}")
        if name == "read_source_batches":
            wanted = args["batch_ids"]
            known = {b["batch_id"] for b in self.batches}
            if len(wanted) != len(set(wanted)) or not set(wanted) <= known:
                raise WorkflowError("batch_ids must be unique known batches")
            rows = [b for b in self.batches if b["batch_id"] in wanted]
            budget = resolve_budget(args.get("max_tokens"))

            def render(count):
                return seal_response(
                    {
                        "batches": rows[:count],
                        "remaining": [b["batch_id"] for b in rows[count:]],
                        "revision": revision,
                        "max_tokens": budget,
                    }
                )

            count = 0
            while count < len(rows) and render(count + 1)["tokens"] <= budget:
                count += 1
            if not count:
                raise WorkflowError(f"first batch requires {render(1)['tokens']} tokens")
            return render(count)
        if name == "search_source":
            rows = [
                {"batch_id": b["batch_id"], "doc_id": b["doc_id"], **s}
                for b in self.batches
                if "batch_ids" not in args or b["batch_id"] in args["batch_ids"]
                for s in b["segments"]
                if args["query"].casefold() in s["text"].casefold()
            ]
        else:
            view = args.get("view", "items")
            if view == "groups":
                rows = [
                    {"name": n, "members": g["members"]}
                    for n, g in self.groups().items()
                    if "name" not in args or n == args["name"]
                ]
            else:
                records = (
                    self.store.items(a["worker"])
                    if a["phase"] == "parse"
                    else self.store.snapshot("parse")
                )
                if view == "candidate":
                    records = self.store.snapshot("candidate")
                rows = [
                    r
                    for k, r in records.items()
                    if ("ids" not in args or k in args["ids"])
                    and ("kind" not in args or r["kind"] == args["kind"])
                    and ("name" not in args or r["value"].get("name") == args["name"])
                    and (
                        "references" not in args
                        or any(
                            set(args["references"]).intersection(
                                [link.get("id"), *link.get("candidates", [])]
                            )
                            for link in r.get("refs", {}).values()
                        )
                    )
                ]
                if view == "index":
                    rows = [
                        {"id": r["id"], "kind": r["kind"], "name": r["value"].get("name")}
                        for r in rows
                        if "name" in r["value"]
                    ]
        return records_page(rows, args.get("cursor", 0), args.get("max_tokens"), revision)

    def publication_model(self, records=None):
        records = self.store.snapshot("published") if records is None else records
        return {
            "meta": {
                "schema_version": "8.0.0",
                "protocol": self.data["protocol"],
                "complete": True,
                "documents": [
                    {
                        "doc_id": n,
                        "rfc": d["meta"]["rfc"],
                        "input_sha256": d["meta"]["input_sha256"],
                        "document_sha256": document_digest(d),
                        "path": f"snapshots/{n}.json",
                    }
                    for n, d in self.documents.items()
                ],
            },
            **{
                kind: [r["value"] for r in records.values() if r["kind"] == kind]
                for kind in COLLECTIONS
            },
            "unresolved": [],
        }

    def export(self):
        if self.data["phase"] != "published":
            raise WorkflowError("not published")
        validate_batches(self.documents, self.batches, self.data["max_tokens"])
        model = self.publication_model()
        errors = validate_protocol(model, self.documents)
        if errors:
            raise WorkflowError("; ".join(errors))
        snapshots = self.root / "snapshots"
        snapshots.mkdir(exist_ok=True)
        files = {
            "protocol.json": model,
            "revision-table.json": self.store.snapshot("revision_table"),
            "checks.json": self.store.snapshot("checks"),
            "assignments.json": self.store.assignments(),
            "report.json": {**self.data, "semantic_review": False},
        }
        files.update({f"snapshots/{n}.json": d for n, d in self.documents.items()})
        for name, body in files.items():
            path = self.root / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            temporary.replace(path)

    def export_metrics(self):
        events = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM events ORDER BY seq")]
        path = self.root / "phases.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result_metrics(self.store, events), indent=2))
        temporary.replace(path)
