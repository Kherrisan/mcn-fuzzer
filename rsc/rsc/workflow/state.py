"""Transactional workflow state; gates are enforced on every tool call."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from rsc.protocol.context import document_digest
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import validate_protocol

from .metrics import phase_metrics
from .model import reference_errors, validate_record
from .paging import (
    PageError,
    compact_page,
    minimum_budget,
    records_page,
    resolve_budget,
    seal_response,
)
from .sources import digest, prepare, token_count, validate_batches

READ = {
    "workflow_status",
    "claim_assignment",
    "list_batches",
    "read_batch",
    "read_batches",
    "search_source",
    "query_model",
    "list_findings",
    "get_changes",
    "check_gate",
    "advance_phase",
}
TOOLS = {
    "preprocess": READ | {"prepare_sources"},
    "parse": READ | {"apply_changes", "complete_batch"},
    "repair": READ | {"apply_changes", "submit_resolutions"},
    "review": READ | {"submit_findings", "complete_review_batch", "review_resolutions"},
    "published": {"workflow_status", "list_batches", "query_model", "list_findings", "get_changes"},
}


class WorkflowError(ValueError):
    """A rejected tool request; the transaction is rolled back."""


class Workflow:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "workflow.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS task (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, body TEXT)")
        self.db.commit()
        self.data = self._load()

    @classmethod
    def create(
        cls, root, sources, protocol, *, target_tokens=8000, max_tokens=12000, max_review_rounds=5
    ):
        if not isinstance(protocol, str) or not protocol.strip():
            raise WorkflowError("protocol must be a nonempty name and version")
        if type(max_review_rounds) is not int or max_review_rounds < 1:
            raise WorkflowError("max_review_rounds must be a positive integer")
        if not 128 <= target_tokens <= max_tokens:
            raise WorkflowError("batch limits must satisfy 128 <= target <= maximum")
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        database = root / "workflow.sqlite"
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        obj = cls(root)
        tokens = {role: secrets.token_urlsafe(32) for role in ("parser", "reviewer")}
        for role, token in tokens.items():
            p = root / f"{role}.token"
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(token)
        obj.data = {
            "task_id": secrets.token_hex(4),
            "phase": "preprocess",
            "revision": 0,
            "round": 0,
            "max_review_rounds": max_review_rounds,
            "protocol": protocol,
            "sources": [str(Path(p).resolve()) for p in sources],
            "target_tokens": target_tokens,
            "max_tokens": max_tokens,
            "credentials": {
                role: hashlib.sha256(token.encode()).hexdigest() for role, token in tokens.items()
            },
            "documents": {},
            "batches": [],
            "source_hash": None,
            "records": {},
            "history": [],
            "receipts": {},
            "processed": {},
            "reviews": {},
            "findings": {},
            "operations": {},
            "diff_reads": {},
            "model_reads": {},
            "review_base": 0,
        }
        obj._assign()
        with obj.db:
            obj._save()
        return obj

    def _load(self):
        row = self.db.execute("SELECT body FROM task WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def _save(self):
        self.db.execute(
            "INSERT OR REPLACE INTO task VALUES (1, ?)",
            (json.dumps(self.data, ensure_ascii=False),),
        )

    def _assign(self):
        self.data["assignment"] = {
            "dispatch_id": secrets.token_hex(12),
            "role": self.actor,
            "phase": self.data["phase"],
            "round": self.data["round"],
            "revision": self.data["revision"],
            "claimed": False,
            "created_at": time.time(),
        }

    @property
    def actor(self):
        return "reviewer" if self.data["phase"] == "review" else "parser"

    def authenticate(self, token):
        if not isinstance(token, str):
            raise WorkflowError("invalid role credential")
        value = hashlib.sha256(token.encode()).hexdigest()
        for role, hashed in self.data["credentials"].items():
            if secrets.compare_digest(value, hashed):
                return role
        raise WorkflowError("invalid role credential")

    def allowed(self, role):
        if role != self.actor and self.data["phase"] != "published":
            return {"workflow_status"}
        return TOOLS[self.data["phase"]]

    def catalog(self, role):
        """Stable role schemas for clients that cache catalogs for the process lifetime."""
        phases = ("preprocess", "parse", "repair") if role == "parser" else ("review",)
        return set().union(*(TOOLS[phase] for phase in phases))

    def _audit(self, before, role, name, started, status, error=None):
        event = {
            "time": time.time(),
            "task_id": before["task_id"],
            "role": role,
            "tool": name,
            "status": status,
            "phase": before["phase"],
            "next_phase": self.data["phase"],
            "round": before["round"],
            "revision": self.data["revision"],
            "dispatch_id": before["assignment"]["dispatch_id"],
            "elapsed_ms": (time.monotonic() - started) * 1000,
        }
        if error is not None:
            event["error"] = str(error)
        delivery = before.get("_last_delivery")
        if delivery:
            event["delivery"] = delivery
        self.db.execute("INSERT INTO events(body) VALUES (?)", (json.dumps(event),))

    def call(self, role, name, arguments=None):
        from .tools import validate_arguments

        arguments = {} if arguments is None else arguments
        started = time.monotonic()
        with self.lock:
            self.data = self._load()
            before = copy.deepcopy(self.data)
            try:
                with self.db:
                    if name not in self.allowed(role):
                        raise WorkflowError(
                            f"tool {name} is forbidden for {role} in {self.data['phase']}"
                        )
                    validate_arguments(name, arguments)
                    if name not in {"workflow_status", "claim_assignment"} and (
                        self.data["phase"] != "published" and not self.data["assignment"]["claimed"]
                    ):
                        raise WorkflowError("claim the assignment first")
                    result = getattr(self, name)(**arguments)
                    if name in {"workflow_status", "claim_assignment", "advance_phase"}:
                        result["allowed_tools"] = sorted(self.allowed(role))
                    if isinstance(result, dict) and "delivered_ids" in result:
                        before["_last_delivery"] = {
                            "format": result.get("format"),
                            "max_tokens": result.get("max_tokens"),
                            "tokens": result.get("tokens"),
                            "delivered_ids": result.get("delivered_ids"),
                            "revision": result.get("revision"),
                            "sha256": digest(result),
                        }
                        self.data.setdefault("response_log", []).append(
                            {"tool": name, "dispatch_id": before["assignment"]["dispatch_id"], **before["_last_delivery"]}
                        )
                    self._save()
                    self._audit(before, role, name, started, "ok")
            except (ValueError, KeyError, TypeError) as error:
                self.data = before
                with self.db:
                    self._audit(before, role, name, started, "rejected", error)
                raise WorkflowError(str(error)) from error
            if self.data["phase"] == "published":
                self.export()
            return result

    def workflow_status(self):
        return {
            k: copy.deepcopy(self.data[k])
            for k in (
                "task_id",
                "phase",
                "revision",
                "round",
                "max_review_rounds",
                "assignment",
                "review_base",
            )
        } | {
            "allowed_tools": sorted(TOOLS[self.data["phase"]]),
            "gate": self.check_gate(),
            "pending_batches": self.pending_batches(),
        }

    def claim_assignment(self, dispatch_id):
        if dispatch_id != self.data["assignment"]["dispatch_id"]:
            raise WorkflowError("stale dispatch ID")
        self.data["assignment"]["claimed"] = True
        return self.workflow_status()

    def prepare_sources(self):
        if self.data["documents"]:
            raise WorkflowError("sources already prepared; start a new task to change inputs")
        docs, batches = prepare(
            self.root, self.data["sources"], self.data["target_tokens"], self.data["max_tokens"]
        )
        snapshots = self.root / "snapshots"
        snapshots.mkdir(exist_ok=True)
        for doc_id, doc in docs.items():
            (snapshots / f"{doc_id}.json").write_text(json.dumps(doc, ensure_ascii=False))
        self.data.update(documents=docs, batches=batches, source_hash=digest(docs))
        return {"documents": len(docs), "batches": len(batches)}

    def _batch(self, batch_id):
        for batch in self.data["batches"]:
            if batch["batch_id"] == batch_id:
                return batch
        raise WorkflowError("unknown batch")

    def _key(self, batch_id):
        return self.data["assignment"]["dispatch_id"] + ":" + batch_id

    def pending_batches(self):
        phase = self.data["phase"]
        if phase == "parse":
            return [
                b["batch_id"]
                for b in self.data["batches"]
                if b["batch_id"] not in self.data["processed"]
            ]
        if phase == "review":
            required = self.data.get("review_batches", [])
            return [bid for bid in required if self._key(bid) not in self.data["reviews"]]
        return []

    def _page_revision(self, revision, cursor):
        if cursor and revision is None:
            raise WorkflowError("continued pages require the revision from the previous page")
        if revision is not None and (
            type(revision) is not int or revision != self.data["revision"]
        ):
            raise WorkflowError("model revision changed; read the model again")

    def _budget_page(self, rows, cursor, max_tokens, format="records"):
        try:
            if format == "compact":
                return compact_page(rows, cursor, max_tokens, self.data["revision"])
            if format != "records":
                raise WorkflowError("format must be compact or records")
            return records_page(rows, cursor, max_tokens, self.data["revision"])
        except PageError as error:
            raise WorkflowError(str(error)) from error

    @staticmethod
    def _page(rows, cursor=0):
        try:
            return records_page(rows, cursor, None, 0)
        except PageError as error:
            raise WorkflowError(str(error)) from error

    def list_batches(self, filter=None, cursor=0, max_tokens=None, revision=None):
        self._page_revision(revision, cursor)
        rows = [
            {
                "batch_id": b["batch_id"],
                "doc_id": b["doc_id"],
                "section_path": b["section_path"],
                "pending": b["batch_id"] in self.pending_batches(),
            }
            for b in self.data["batches"]
        ]
        if filter:
            rows = [r for r in rows if all(r.get(k) == v for k, v in filter.items())]
        return self._budget_page(rows, cursor, max_tokens)

    def read_batch(self, batch_id):
        result = self.read_batches([batch_id])
        if not result["batches"]:
            raise WorkflowError(f"batch {batch_id} was not delivered")
        return result["batches"][0]

    def read_batches(self, batch_ids, max_tokens=None):
        if not isinstance(batch_ids, list) or not batch_ids:
            raise WorkflowError("batch_ids must list at least one batch")
        order = [batch["batch_id"] for batch in self.data["batches"]]
        positions = []
        for batch_id in batch_ids:
            if batch_id not in order:
                raise WorkflowError("unknown batch")
            positions.append(order.index(batch_id))
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise WorkflowError("batches must be requested in assignment order without gaps")
        try:
            budget = resolve_budget(max_tokens)
        except PageError as error:
            raise WorkflowError(str(error)) from error
        prepared = []
        for batch_id in batch_ids:
            batch = self._batch(batch_id)
            prepared.append(
                {
                    "batch": batch,
                    "receipt_id": digest(
                        {"dispatch": self.data["assignment"]["dispatch_id"], "batch": batch}
                    ),
                }
            )

        def render(count, candidate):
            delivered = prepared[:count]
            remaining = [item["batch"]["batch_id"] for item in prepared[count:]]
            return seal_response(
                {
                    "batches": delivered,
                    "delivered": [item["batch"]["batch_id"] for item in delivered],
                    "remaining": remaining,
                    "delivered_ids": [item["batch"]["batch_id"] for item in delivered],
                    "format": "records",
                    "max_tokens": candidate,
                    "revision": self.data["revision"],
                }
            )

        chosen = 0
        for count in range(1, len(prepared) + 1):
            payload = render(count, budget)
            if payload["tokens"] > budget:
                if chosen == 0:
                    needed = minimum_budget(lambda candidate: render(1, candidate))
                    raise WorkflowError(f"first batch requires {needed} tokens")
                break
            chosen = count
        result = render(chosen, budget)
        for item in prepared[:chosen]:
            self.data["receipts"][self._key(item["batch"]["batch_id"])] = item["receipt_id"]
        return result

    def search_source(self, query, filters=None, cursor=0, max_tokens=None, revision=None):
        self._page_revision(revision, cursor)
        rows = []
        for b in self.data["batches"]:
            if filters and any(b.get(k) != v for k, v in filters.items()):
                continue
            for segment in b["segments"]:
                if query.casefold() in segment["text"].casefold():
                    rows.append({"batch_id": b["batch_id"], "doc_id": b["doc_id"], **segment})
        return self._budget_page(rows, cursor, max_tokens)

    def query_model(
        self,
        kind=None,
        filters=None,
        record_ids=None,
        detail=True,
        cursor=0,
        format="compact",
        max_tokens=None,
        revision=None,
    ):
        self._page_revision(revision, cursor)
        rows = []
        for rid, record in self.data["records"].items():
            if kind and record["kind"] != kind or record_ids is not None and rid not in record_ids:
                continue
            if filters and any(record["value"].get(k) != v for k, v in filters.items()):
                continue
            rows.append(
                {"record_id": rid, **record}
                if detail
                else {"record_id": rid, "kind": record["kind"], "name": record["value"].get("name")}
            )
        result = self._budget_page(rows, cursor, max_tokens, format)
        if (
            self.data["phase"] == "review"
            and detail
            and not kind
            and not filters
            and record_ids is None
        ):
            key = self.data["assignment"]["dispatch_id"]
            reads = self.data.setdefault("model_reads", {}).setdefault(key, [])
            for rid in result["delivered_ids"] or ["__empty__"]:
                if rid not in reads:
                    reads.append(rid)
        return result

    def _evidence(self, evidence):
        if not isinstance(evidence, list) or not evidence:
            raise WorkflowError("source evidence is required")
        for item in evidence:
            batch = self._batch(item["batch_id"])
            if not any(s["block_id"] == item["block_id"] for s in batch["segments"]):
                raise WorkflowError("evidence does not belong to its source batch")
            if self._key(item["batch_id"]) not in self.data["receipts"]:
                raise WorkflowError("read evidence source before using it")

    def apply_changes(self, expected_revision, operation_id, changes):
        signature = digest({"revision": expected_revision, "changes": changes})
        if operation_id in self.data["operations"]:
            old = self.data["operations"][operation_id]
            if old["signature"] != signature:
                raise WorkflowError("operation ID reused with different changes")
            return old["result"]
        self._revision(expected_revision)
        if not changes:
            raise WorkflowError("empty changes")
        updates = []
        for change in changes:
            op = change["op"]
            rid = change.get("record_id")
            before = copy.deepcopy(self.data["records"].get(rid))
            if op == "add":
                rid = secrets.token_hex(8)
                kind = change["kind"]
            elif op in {"update", "delete"} and before:
                kind = before["kind"]
            else:
                raise WorkflowError("invalid operation or missing record")
            if kind not in (*COLLECTIONS, "unresolved"):
                raise WorkflowError("unknown record kind")
            if op == "delete":
                if not change.get("reason"):
                    raise WorkflowError("deletion requires a reason")
                del self.data["records"][rid]
                for report in self.data["processed"].values():
                    if rid in report.get("record_ids", []):
                        report.setdefault("removed_records", {})[rid] = change["reason"]
            else:
                value = copy.deepcopy(change["value"])
                if kind == "steps" and before and value.get("name") != before["value"].get("name"):
                    raise WorkflowError("step name cannot change during update")
                validate_record(kind, value)
                self._evidence(change["evidence"])
                if token_count({"value": value, "evidence": change["evidence"]}) > 8000:
                    raise WorkflowError(
                        "record exceeds 8000 tokens; keep definitions concise and cite sources"
                    )
                self.data["records"][rid] = {
                    "kind": kind,
                    "value": value,
                    "evidence": change["evidence"],
                }
            updates.append(
                {
                    "record_id": rid,
                    "before": before,
                    "after": copy.deepcopy(self.data["records"].get(rid)),
                }
            )
        self.data["revision"] += 1
        self.data["history"].append({"revision": self.data["revision"], "changes": updates})
        result = {
            "revision": self.data["revision"],
            "record_ids": [u["record_id"] for u in updates],
        }
        self.data["operations"][operation_id] = {"signature": signature, "result": result}
        return result

    def _revision(self, revision):
        if type(revision) is not int or revision != self.data["revision"]:
            raise WorkflowError("stale model revision")

    def _complete(self, batch_id, receipt_id, report, review=False):
        if self.data["receipts"].get(self._key(batch_id)) != receipt_id:
            raise WorkflowError("missing complete batch receipt")
        if not isinstance(report, dict) or not str(report.get("summary", "")).strip():
            raise WorkflowError(
                "report requires a summary, including when no information was found"
            )
        pending = self.pending_batches()
        if batch_id in pending and pending[0] != batch_id:
            raise WorkflowError("complete batches in the assigned order")
        for rid in report.get("record_ids", []):
            if rid not in self.data["records"]:
                raise WorkflowError("report references missing record")
        for fid in report.get("finding_ids", []):
            if fid not in self.data["findings"]:
                raise WorkflowError("report references missing finding")
        key = self._key(batch_id) if review else batch_id
        saved = {**copy.deepcopy(report), "revision": self.data["revision"]}
        if review:
            saved.update(
                round=self.data["round"],
                batch_id=batch_id,
                dispatch_id=self.data["assignment"]["dispatch_id"],
            )
        self.data["reviews" if review else "processed"][key] = saved
        return {"completed": batch_id}

    def complete_batch(self, batch_id, receipt_id, report):
        return self._complete(batch_id, receipt_id, report)

    def complete_review_batch(self, batch_id, receipt_id, report):
        if batch_id not in self.data["review_batches"]:
            raise WorkflowError("batch is not assigned for this review")
        return self._complete(batch_id, receipt_id, report, True)

    def list_findings(self, filters=None, cursor=0, max_tokens=None, revision=None):
        self._page_revision(revision, cursor)
        rows = [{"finding_id": fid, **f} for fid, f in self.data["findings"].items()]
        if filters:
            rows = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
        return self._budget_page(rows, cursor, max_tokens)

    def submit_findings(self, reviewed_revision, findings):
        self._revision(reviewed_revision)
        ids = []
        for finding in findings:
            if not str(finding.get("description", "")).strip():
                raise WorkflowError("finding requires a description")
            self._evidence(finding["evidence"])
            if any(r not in self.data["records"] for r in finding.get("record_ids", [])):
                raise WorkflowError("finding refers to missing record")
            fid = secrets.token_hex(8)
            self.data["findings"][fid] = {
                **copy.deepcopy(finding),
                "status": "open",
                "round": self.data["round"],
                "reviewed_revision": reviewed_revision,
                "history": [],
            }
            ids.append(fid)
        return {"finding_ids": ids}

    def submit_resolutions(self, expected_revision, resolutions):
        self._revision(expected_revision)
        for resolution in resolutions:
            finding = self.data["findings"][resolution["finding_id"]]
            if finding["status"] == "accepted":
                raise WorkflowError("finding already accepted")
            if resolution["status"] not in {"applied", "not_applicable", "unresolved"}:
                raise WorkflowError("invalid resolution status")
            if not str(resolution.get("reason", "")).strip():
                raise WorkflowError("resolution requires a source-based reason")
            self._evidence(resolution["evidence"])
            finding["resolution"] = {**resolution, "revision": expected_revision}
            finding["status"] = "submitted"
            finding["history"].append(
                {
                    **copy.deepcopy(finding["resolution"]),
                    "role": "parser",
                    "round": self.data["round"],
                    "revision": expected_revision,
                }
            )
        return {"submitted": len(resolutions)}

    def review_resolutions(self, reviewed_revision, decisions):
        self._revision(reviewed_revision)
        for decision in decisions:
            finding = self.data["findings"][decision["finding_id"]]
            if finding["status"] != "submitted":
                raise WorkflowError("finding has no pending resolution")
            if type(decision.get("accepted")) is not bool or not decision.get("reason"):
                raise WorkflowError("decision requires accepted boolean and reason")
            finding["status"] = "accepted" if decision["accepted"] else "open"
            finding["history"].append(
                {
                    **decision,
                    "role": "reviewer",
                    "round": self.data["round"],
                    "revision": reviewed_revision,
                }
            )
        return {"reviewed": len(decisions)}

    def _diff_rows(self, from_revision, to_revision):
        return [
            {
                "change_id": f"{h['revision']}:{index}",
                "revision": h["revision"],
                "changes": [change],
            }
            for h in self.data["history"]
            if from_revision < h["revision"] <= to_revision
            for index, change in enumerate(h["changes"])
        ]

    def get_changes(self, from_revision, to_revision, cursor=0, max_tokens=None, revision=None):
        self._revision(to_revision)
        self._page_revision(revision, cursor)
        if not 0 <= from_revision <= to_revision:
            raise WorkflowError("invalid revision range")
        rows = self._diff_rows(from_revision, to_revision)
        result = self._budget_page(rows, cursor, max_tokens)
        if from_revision == self.data["review_base"] and self.data["phase"] == "review":
            key = self.data["assignment"]["dispatch_id"]
            reads = self.data["diff_reads"].setdefault(key, [])
            for change_id in result["delivered_ids"] or ["__empty__"]:
                if change_id not in reads:
                    reads.append(change_id)
        return result

    def _hard_errors(self):
        errors = []
        if not self.data["documents"]:
            return ["sources have not been prepared"]
        if digest(self.data["documents"]) != self.data["source_hash"]:
            errors.append("source snapshot changed")
        for doc_id, doc in self.data["documents"].items():
            try:
                saved = json.loads((self.root / "snapshots" / f"{doc_id}.json").read_text())
                if digest(saved) != digest(doc):
                    errors.append(f"source snapshot file changed: {doc_id}")
            except (OSError, ValueError):
                errors.append(f"source snapshot unavailable: {doc_id}")
        try:
            validate_batches(self.data["documents"], self.data["batches"], self.data["max_tokens"])
        except ValueError as e:
            errors.append(str(e))
        if self.data["phase"] == "preprocess":
            return errors
        missing = [
            b["batch_id"]
            for b in self.data["batches"]
            if b["batch_id"] not in self.data["processed"]
        ]
        if missing:
            errors.append(f"unprocessed batches: {missing}")
        if not self.data["records"]:
            errors.append("model is empty")
        for record in self.data["records"].values():
            try:
                validate_record(record["kind"], record["value"])
                if not record["evidence"]:
                    raise ValueError("record has no evidence")
                for evidence in record["evidence"]:
                    b = self._batch(evidence["batch_id"])
                    if evidence["block_id"] not in [s["block_id"] for s in b["segments"]]:
                        raise ValueError("invalid record evidence")
            except (ValueError, KeyError) as e:
                errors.append(str(e))
        errors.extend(reference_errors(self.data["records"]))
        return errors

    def check_gate(self):
        phase = self.data["phase"]
        errors = self._hard_errors()
        if phase == "review":
            pages = set(self.data["records"]) or {"__empty__"}
            reads = self.data.get("model_reads", {}).get(self.data["assignment"]["dispatch_id"], [])
            if not pages.issubset(reads):
                errors.append("read every complete model page with query_model(detail=true)")
            if self.pending_batches():
                errors.append("review batches remain incomplete")
            if any(f["status"] == "submitted" for f in self.data["findings"].values()):
                errors.append("review all submitted resolutions")
            if self.data["round"] > 1:
                rows = self._diff_rows(self.data["review_base"], self.data["revision"])
                required_pages = {r["change_id"] for r in rows} or {"__empty__"}
                read = self.data["diff_reads"].get(self.data["assignment"]["dispatch_id"], [])
                if not required_pages.issubset(read):
                    errors.append("read the complete revision diff")
        elif phase == "repair":
            if any(f["status"] == "open" for f in self.data["findings"].values()):
                errors.append("submit a resolution for every open finding")
        return {"ready": not errors, "blockers": errors}

    def advance_phase(self):
        gate = self.check_gate()
        if not gate["ready"]:
            raise WorkflowError("gate rejected: " + "; ".join(gate["blockers"]))
        phase = self.data["phase"]
        self.data.setdefault("phase_history", []).append(
            {
                "phase": phase,
                "round": self.data["round"],
                "revision": self.data["revision"],
                "started_at": self.data["assignment"].get("created_at"),
                "finished_at": time.time(),
                "dispatch_id": self.data["assignment"]["dispatch_id"],
            }
        )
        if phase == "preprocess":
            self.data["phase"] = "parse"
        elif phase in {"parse", "repair"}:
            self.data["round"] += 1
            self.data["phase"] = "review"
            if phase == "parse":
                self.data["review_batches"] = [b["batch_id"] for b in self.data["batches"]]
            else:
                affected = set()
                for h in self.data["history"]:
                    if h["revision"] > self.data["review_base"]:
                        for change in h["changes"]:
                            for version in (change["before"], change["after"]):
                                if version:
                                    affected.update(e["batch_id"] for e in version["evidence"])
                for f in self.data["findings"].values():
                    if f["status"] == "submitted":
                        affected.update(e["batch_id"] for e in f["resolution"]["evidence"])
                self.data["review_batches"] = [
                    b["batch_id"] for b in self.data["batches"] if b["batch_id"] in affected
                ]
        elif phase == "review":
            pending = [f for f in self.data["findings"].values() if f["status"] != "accepted"]
            if pending and self.data["round"] < self.data["max_review_rounds"]:
                self.data["phase"] = "repair"
                self.data["review_base"] = self.data["revision"]
            else:
                errors = validate_protocol(self.publication_model(), self.data["documents"])
                if errors:
                    raise WorkflowError("publication validation: " + "; ".join(errors))
                self.data["phase"] = "published"
                self.data["review_limit_reached"] = bool(pending)
        else:
            raise WorkflowError("terminal workflow")
        self._assign()
        return self.workflow_status()

    def publication_model(self):
        """Build the schema-preserving model without publishing or changing state."""
        records = self.data["records"]
        unresolved = [r["value"] for r in records.values() if r["kind"] == "unresolved"]
        for f in self.data["findings"].values():
            if f["status"] != "accepted" or f.get("resolution", {}).get("status") == "unresolved":
                unresolved.append(
                    {
                        "description": f["description"],
                        "object_names": [
                            records[r]["value"].get("name", r)
                            for r in f.get("record_ids", [])
                            if r in records
                        ],
                    }
                )
        sources = []
        for doc_id, doc in self.data["documents"].items():
            path = self.root / "snapshots" / f"{doc_id}.json"
            sources.append(
                {
                    "doc_id": doc_id,
                    "rfc": doc["meta"]["rfc"],
                    "input_sha256": doc["meta"]["input_sha256"],
                    "document_sha256": document_digest(doc),
                    "path": str(path.relative_to(self.root)),
                }
            )
        model = {
            "meta": {
                "schema_version": "8.0.0",
                "protocol": self.data["protocol"],
                "complete": not unresolved,
                "documents": sources,
            },
            **{
                kind: [r["value"] for r in records.values() if r["kind"] == kind]
                for kind in COLLECTIONS
            },
            "unresolved": unresolved,
        }
        return model

    def export(self):
        """Recoverably materialize reports; publish protocol.json last."""
        if self.data["phase"] != "published":
            raise WorkflowError("publication requires a completed workflow gate")
        records = self.data["records"]
        model = self.publication_model()
        errors = self._hard_errors() + validate_protocol(model, self.data["documents"])
        if errors:
            raise WorkflowError("publication validation: " + "; ".join(errors))
        events = [
            json.loads(row[0]) for row in self.db.execute("SELECT body FROM events ORDER BY seq")
        ]
        reports = {
            "sources.json": {rid: r["evidence"] for rid, r in records.items()},
            "batches.json": self.data["batches"],
            "coverage.json": {"parsed": self.data["processed"], "reviewed": self.data["reviews"]},
            "review.json": self.data["findings"],
            "changes.json": self.data["history"],
            "phases.json": phase_metrics(self.data, events),
            "report.json": {
                "rounds": self.data["round"],
                "review_limit_reached": self.data["review_limit_reached"],
                "complete": model["meta"]["complete"],
            },
            "protocol.json": model,
        }
        events_path = self.root / "events.jsonl"
        temporary = events_path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        )
        temporary.replace(events_path)
        for filename, body in reports.items():
            path = self.root / filename
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            tmp.replace(path)
