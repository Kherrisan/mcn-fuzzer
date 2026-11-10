"""Versioned workflow storage, assignment ownership and fenced worker attempts.

Only the coordinator creates assignments or replaces a confirmed dead attempt.
The service owns this connection; model inference and terminal control happen
outside its short transactions. Protocol validation lives in the workflow layer.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

VERSION = 2

SCHEMA = """
CREATE TABLE workflow (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL);
CREATE TABLE documents (name TEXT PRIMARY KEY, ordinal INTEGER UNIQUE, body TEXT NOT NULL);
CREATE TABLE batches (name TEXT PRIMARY KEY, ordinal INTEGER UNIQUE, body TEXT NOT NULL);
CREATE TABLE workers (name TEXT PRIMARY KEY, role TEXT NOT NULL);
CREATE TABLE credentials (
    worker TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
    assignment TEXT, attempt INTEGER
);
CREATE TABLE assignments (
    name TEXT PRIMARY KEY, worker TEXT NOT NULL REFERENCES workers(name),
    role TEXT NOT NULL, phase TEXT NOT NULL, round INTEGER NOT NULL,
    attempt INTEGER NOT NULL, status TEXT NOT NULL,
    base_revision INTEGER NOT NULL, draft TEXT, body TEXT NOT NULL
);
CREATE TABLE attempts (
    assignment TEXT NOT NULL REFERENCES assignments(name), attempt INTEGER NOT NULL,
    status TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(assignment, attempt)
);
CREATE TABLE drafts (
    name TEXT PRIMARY KEY, revision INTEGER NOT NULL, base_revision INTEGER NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE records (
    draft TEXT NOT NULL REFERENCES drafts(name), key TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(draft, key)
);
CREATE TABLE operations (
    assignment TEXT NOT NULL, attempt INTEGER NOT NULL, name TEXT NOT NULL,
    arguments TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(assignment, attempt, name)
);
CREATE TABLE receipts (
    assignment TEXT NOT NULL, attempt INTEGER NOT NULL, batch TEXT NOT NULL,
    name TEXT NOT NULL, PRIMARY KEY(assignment, attempt, batch)
);
CREATE TABLE reports (
    assignment TEXT NOT NULL, attempt INTEGER NOT NULL, batch TEXT NOT NULL,
    phase TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(assignment, attempt, batch)
);
CREATE TABLE history (
    draft TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(draft, revision)
);
CREATE TABLE snapshots (revision INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE findings (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE conclusions (name TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE deliveries (
    assignment TEXT NOT NULL, attempt INTEGER NOT NULL, revision INTEGER NOT NULL,
    family TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(assignment, attempt, revision, family, name)
);
CREATE TABLE events (seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
"""


class StoreError(ValueError):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Identity:
    worker: str
    role: str
    assignment: str | None = None
    attempt: int | None = None


class Store:
    def __init__(self, root, *, readonly=False):
        self.root = Path(root).resolve()
        path = self.root / "workflow.sqlite"
        # Opening a missing or historical database must never initialize/alter it.
        self.db = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version != VERSION:
            self.db.close()
            raise StoreError(
                f"workflow storage version {version} cannot execute here; "
                "audit historical runs or resume them with their archived source package"
            )
        self.db.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.db.execute("PRAGMA synchronous=FULL")
        self.lock = threading.RLock()

    @classmethod
    def create(cls, root, settings):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "workflow.sqlite"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version={VERSION}")
            db.execute("INSERT INTO workflow VALUES (1, ?)", (dumps(settings),))
            db.execute(
                "INSERT INTO credentials VALUES (?, ?, NULL, NULL)",
                (
                    "coordinator",
                    secrets.token_urlsafe(32),
                ),
            )
        db.close()
        result = cls(root)
        result.write_credentials()
        return result

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()

    def settings(self):
        with self.lock:
            return json.loads(self.db.execute("SELECT body FROM workflow").fetchone()[0])

    def authenticate(self, token):
        if not isinstance(token, str):
            raise StoreError("invalid credential")
        with self.lock:
            row = self.db.execute(
                "SELECT c.*, w.role FROM credentials c LEFT JOIN workers w ON w.name=c.worker "
                "WHERE token=?",
                (token,),
            ).fetchone()
        if row is None:
            raise StoreError("invalid or revoked credential")
        return Identity(
            row["worker"], row["role"] or "coordinator", row["assignment"], row["attempt"]
        )

    def write_credentials(self):
        """Recover token files after commit/restart; secrets remain in the private DB.

        An old bridge keeps its old token, so rewriting the file never authorizes
        an already-running process to write a replacement attempt.
        """
        with self.lock:
            rows = self.db.execute("SELECT worker, token FROM credentials").fetchall()
            for worker, token in rows:
                path = self.root / f"{worker}.token"
                temporary = path.with_suffix(".token.tmp")
                fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as out:
                    out.write(token)
                temporary.replace(path)

    @staticmethod
    def require_coordinator(identity):
        if identity != Identity("coordinator", "coordinator"):
            raise StoreError("coordinator credential required")

    def install_sources(self, identity, documents, batches):
        self.require_coordinator(identity)
        # Source parsing and serialization can be large: do them before BEGIN.
        docs = [(name, i, dumps(body)) for i, (name, body) in enumerate(documents.items())]
        rows = [(b["batch_id"], i, dumps(b)) for i, b in enumerate(batches)]
        if not rows:
            raise StoreError("no source batches")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM batches LIMIT 1").fetchone():
                raise StoreError("sources are immutable once prepared")
            self.db.executemany("INSERT INTO documents VALUES (?, ?, ?)", docs)
            self.db.executemany("INSERT INTO batches VALUES (?, ?, ?)", rows)

    def sources(self):
        with self.lock:
            docs = dict(self.db.execute("SELECT name, body FROM documents ORDER BY ordinal"))
            batches = self.db.execute("SELECT body FROM batches ORDER BY ordinal").fetchall()
        return {k: json.loads(v) for k, v in docs.items()}, [json.loads(b[0]) for b in batches]

    def assignment(self, name):
        with self.lock:
            row = self.db.execute("SELECT * FROM assignments WHERE name=?", (name,)).fetchone()
        if row is None:
            raise StoreError(f"unknown assignment {name!r}")
        row = dict(row)
        body = json.loads(row.pop("body"))
        return {**row, **body}

    def assignments(self):
        with self.lock:
            names = [r[0] for r in self.db.execute("SELECT name FROM assignments ORDER BY rowid")]
            return [self.assignment(name) for name in names]

    def activate(self, identity, assignments, *, phase, round_, base_revision):
        """Persist a whole phase's fixed assignments before dispatching any worker."""
        self.require_coordinator(identity)
        role = "reviewer" if phase == "review" else "parser"
        if phase not in {"parse", "merge", "review", "fix"} or not assignments:
            raise StoreError("invalid phase or empty assignments")
        rows = []
        for a in assignments:
            if not a["worker"].startswith(role + "-") or not a["worker"][len(role) + 1 :].isdigit():
                raise StoreError("worker name must be its role followed by a positive ordinal")
            if int(a["worker"][len(role) + 1 :]) < 1:
                raise StoreError("worker ordinal must be positive")
            if len(a["batches"]) != len(set(a["batches"])):
                raise StoreError("duplicate batch in assignment")
            body = {
                "batches": a["batches"],
                "checks": a.get("checks", []),
                "findings": a.get("findings", []),
            }
            rows.append((a, dumps(body)))
        if len({a["worker"] for a, _ in rows}) != len(rows):
            raise StoreError("a worker may have only one active assignment")
        seed_records = {
            a["name"]: [
                (a.get("draft"), key, dumps(record)) for key, record in a.get("records", {}).items()
            ]
            for a, _ in rows
        }
        seeds = {
            a["name"]: dumps(
                {"initial_records": a.get("records", {}), "checks": a.get("checks", [])}
            )
            for a, _ in rows
        }
        carried = {
            a["name"]: [
                (name, body, dumps(body), dumps(body["report"]))
                for name, body in a.get("carried", [])
            ]
            for a, _ in rows
        }
        if phase != "review" and any(carried.values()):
            raise StoreError("only independent review conclusions can be carried")
        with self.transaction():
            active = self.db.execute(
                "SELECT name FROM assignments WHERE status != 'submitted'"
            ).fetchall()
            if active:
                raise StoreError("previous assignments have not been submitted")
            known = {r[0] for r in self.db.execute("SELECT name FROM batches")}
            if any(not set(a["batches"]) <= known for a, _ in rows):
                raise StoreError("unknown assigned batch")
            if phase in {"parse", "review"}:
                owned = [b for a, _ in rows for b in a["batches"]]
                if len(set(owned)) != len(owned):
                    raise StoreError("a batch may have only one primary owner")
            for a, body in rows:
                self.db.execute("INSERT OR IGNORE INTO workers VALUES (?, ?)", (a["worker"], role))
                draft = a.get("draft")
                if phase != "review" and not draft:
                    raise StoreError("a parser assignment requires a draft")
                if phase == "review" and draft:
                    raise StoreError("reviewer cannot own a writable draft")
                if draft:
                    self.db.execute(
                        "INSERT INTO drafts VALUES (?, 0, ?, 0)", (draft, base_revision)
                    )
                    self.db.executemany(
                        "INSERT INTO records VALUES (?, ?, ?)",
                        seed_records[a["name"]],
                    )
                    self.db.execute(
                        "INSERT INTO history VALUES (?, 0, ?)", (draft, seeds[a["name"]])
                    )
                self.db.execute(
                    "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        a["name"],
                        a["worker"],
                        role,
                        phase,
                        round_,
                        "pending",
                        base_revision,
                        draft,
                        body,
                    ),
                )
                self.db.execute(
                    "INSERT INTO attempts VALUES (?, 1, 'pending', ?)",
                    (
                        a["name"],
                        dumps({"created_at": time.time()}),
                    ),
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO credentials VALUES (?, ?, ?, 1)",
                    (
                        a["worker"],
                        secrets.token_urlsafe(32),
                        a["name"],
                    ),
                )
                for name, conclusion, encoded, report in carried[a["name"]]:
                    if conclusion["assignment"] != a["name"] or conclusion["attempt"] != 0:
                        raise StoreError("carried conclusion has the wrong assignment")
                    self.db.execute(
                        "INSERT INTO conclusions VALUES (?, ?, ?)", (name, base_revision, encoded)
                    )
                    if conclusion["batch"]:
                        self.db.execute(
                            "INSERT INTO reports VALUES (?, 0, ?, 'review', ?, ?)",
                            (a["name"], conclusion["batch"], base_revision, report),
                        )
                expected = {*a["batches"], *(c["name"] for c in a.get("checks", []))}
                complete = {c[1]["batch"] or c[1]["check"] for c in carried[a["name"]]}
                if phase == "review" and expected == complete and not a.get("findings"):
                    self._status({"name": a["name"], "attempt": 1}, "submitted")
            settings = self.settings()
            settings.update(phase=phase, round=round_, revision=base_revision)
            self.db.execute("UPDATE workflow SET body=?", (dumps(settings),))
        self.write_credentials()

    def owned(self, identity, *, claimed=True, submitted=False):
        a = self.assignment(identity.assignment)
        if (a["worker"], a["role"], a["attempt"]) != (
            identity.worker,
            identity.role,
            identity.attempt,
        ):
            raise StoreError("assignment owner or attempt changed")
        if a["status"] == "failed" or (a["status"] == "submitted" and not submitted):
            raise StoreError(f"assignment is {a['status']}")
        if claimed and a["status"] != "claimed":
            raise StoreError("claim the assignment first")
        return a

    def claim(self, identity, name, attempt):
        with self.transaction():
            a = self.owned(identity, claimed=False)
            if (name, attempt) != (a["name"], a["attempt"]):
                raise StoreError("stale assignment name or attempt")
            self._status(a, "claimed")
        return self.assignment(name)

    def _status(self, a, status):
        self.db.execute("UPDATE assignments SET status=? WHERE name=?", (status, a["name"]))
        self.db.execute(
            "UPDATE attempts SET status=? WHERE assignment=? AND attempt=?",
            (
                status,
                a["name"],
                a["attempt"],
            ),
        )
        row = self.db.execute(
            "SELECT body FROM attempts WHERE assignment=? AND attempt=?", (a["name"], a["attempt"])
        ).fetchone()
        body = json.loads(row[0])
        body.setdefault(f"{status}_at", time.time())
        self.db.execute(
            "UPDATE attempts SET body=? WHERE assignment=? AND attempt=?",
            (dumps(body), a["name"], a["attempt"]),
        )

    def record_send(self, identity, name, attempt):
        """Save intent before hc send. A lost reply leaves a recoverable intent."""
        self.require_coordinator(identity)
        with self.transaction():
            a = self.assignment(name)
            if a["attempt"] != attempt or a["status"] not in {"pending", "sent"}:
                raise StoreError("assignment is not awaiting dispatch")
            self._status(a, "sent")
            row = self.db.execute(
                "SELECT body FROM attempts WHERE assignment=? AND attempt=?", (name, attempt)
            ).fetchone()
            body = json.loads(row[0])
            body.setdefault("send_intent_at", time.time())
            self.db.execute(
                "UPDATE attempts SET body=? WHERE assignment=? AND attempt=?",
                (dumps(body), name, attempt),
            )

    def replace_attempt(self, identity, name, *, attempt, terminal):
        """Fence a confirmed terminal process, retaining its draft and finished batches.

        Observation timeouts never qualify. The supervisor supplies authoritative
        hc terminal evidence; retaining it makes every retry auditable.
        """
        self.require_coordinator(identity)
        if not isinstance(terminal, dict) or terminal.get("state") not in {"exited", "missing"}:
            raise StoreError("replacement requires a confirmed exited or missing terminal")
        if not terminal.get("name"):
            raise StoreError("terminal evidence requires its name")
        with self.transaction():
            a = self.assignment(name)
            if a["attempt"] != attempt or a["status"] in {"failed", "submitted"}:
                raise StoreError("stale or terminal assignment")
            self._status(a, "failed")
            row = self.db.execute(
                "SELECT body FROM attempts WHERE assignment=? AND attempt=?", (name, attempt)
            ).fetchone()
            body = {**json.loads(row[0]), "terminal": terminal, "finished_at": time.time()}
            self.db.execute(
                "UPDATE attempts SET body=? WHERE assignment=? AND attempt=?",
                (
                    dumps(body),
                    name,
                    attempt,
                ),
            )
            self.db.execute("DELETE FROM credentials WHERE worker=?", (a["worker"],))
            if attempt < 3:
                self.db.execute(
                    "UPDATE assignments SET attempt=?, status='pending' WHERE name=?",
                    (
                        attempt + 1,
                        name,
                    ),
                )
                self.db.execute(
                    "INSERT INTO attempts VALUES (?, ?, 'pending', ?)",
                    (
                        name,
                        attempt + 1,
                        dumps({"created_at": time.time()}),
                    ),
                )
                self.db.execute(
                    "INSERT INTO credentials VALUES (?, ?, ?, ?)",
                    (
                        a["worker"],
                        secrets.token_urlsafe(32),
                        name,
                        attempt + 1,
                    ),
                )
        self.write_credentials()
        return self.assignment(name)

    def draft(self, name):
        with self.lock:
            row = self.db.execute("SELECT * FROM drafts WHERE name=?", (name,)).fetchone()
            if row is None:
                raise StoreError("unknown draft")
            records = self.db.execute(
                "SELECT key, body FROM records WHERE draft=?", (name,)
            ).fetchall()
        return dict(row), {r[0]: json.loads(r[1]) for r in records}

    def previous_operation(self, identity, name, arguments):
        """Check ownership before retry lookup, including retries after reassignment."""
        with self.lock:
            self.owned(identity)
            row = self.db.execute(
                "SELECT arguments, result FROM operations "
                "WHERE assignment=? AND attempt=? AND name=?",
                (identity.assignment, identity.attempt, name),
            ).fetchone()
        if row:
            if row[0] != dumps(arguments):
                raise StoreError("operation_id reused with different arguments")
            return json.loads(row[1])
        return None

    def commit_records(
        self,
        identity,
        *,
        revision,
        operation_id,
        arguments,
        changes,
        result,
        checks=None,
        decisions=None,
    ):
        """Commit prevalidated before/after images using an independent draft revision.

        The workflow validates schema, evidence and phase permissions before this
        call. Rechecking the attempt and revision under the write lock closes the
        gap between validation and commit. Only changed records are written.
        """
        encoded = [
            (
                key,
                dumps(before) if before is not None else None,
                dumps(after) if after is not None else None,
            )
            for key, (before, after) in changes.items()
        ]
        argument_json, result_json = dumps(arguments), dumps(result)
        history = dumps(
            {
                "assignment": identity.assignment,
                "attempt": identity.attempt,
                "changes": [{"key": k, "before": b, "after": a} for k, (b, a) in changes.items()],
                "checks": checks,
                "decisions": decisions,
            }
        )
        with self.transaction():
            cached = self.previous_operation(identity, operation_id, arguments)
            if cached is not None:
                return cached
            a = self.owned(identity)
            if identity.role != "parser" or not a["draft"]:
                raise StoreError("only a parser can write its draft")
            draft = self.db.execute("SELECT * FROM drafts WHERE name=?", (a["draft"],)).fetchone()
            if draft["sealed"] or draft["revision"] != revision:
                raise StoreError(f"draft is sealed or revision changed: {draft['revision']}")
            for key, before, after in encoded:
                old = self.db.execute(
                    "SELECT body FROM records WHERE draft=? AND key=?",
                    (
                        a["draft"],
                        key,
                    ),
                ).fetchone()
                if (old[0] if old else None) != before:
                    raise StoreError(f"record before-image changed: {key}")
                if after is None:
                    self.db.execute(
                        "DELETE FROM records WHERE draft=? AND key=?", (a["draft"], key)
                    )
                else:
                    self.db.execute(
                        "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                        (
                            a["draft"],
                            key,
                            after,
                        ),
                    )
            self.db.execute("UPDATE drafts SET revision=revision+1 WHERE name=?", (a["draft"],))
            self.db.execute(
                "INSERT INTO history VALUES (?, ?, ?)", (a["draft"], revision + 1, history)
            )
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                (
                    identity.assignment,
                    identity.attempt,
                    operation_id,
                    argument_json,
                    result_json,
                ),
            )
            if checks is not None:
                row = self.db.execute(
                    "SELECT body FROM assignments WHERE name=?", (identity.assignment,)
                ).fetchone()
                body = json.loads(row[0])
                body["checks"] = checks
                body.setdefault("decisions", []).extend(decisions or [])
                self.db.execute(
                    "UPDATE assignments SET body=? WHERE name=?", (dumps(body), identity.assignment)
                )
        return result

    def deliver(self, identity, batches):
        """Record complete deliveries only, after the bounded response has been built."""
        with self.transaction():
            self.owned(identity)
            known = {r[0] for r in self.db.execute("SELECT name FROM batches")}
            if not set(batches) <= known:
                raise StoreError("unknown delivered batch")
            receipts = {}
            for batch in batches:
                row = self.db.execute(
                    "SELECT name FROM receipts WHERE assignment=? AND attempt=? AND batch=?",
                    (identity.assignment, identity.attempt, batch),
                ).fetchone()
                # Names encode their scope, not a digest of the source. Their presence
                # in the ledger proves delivery; guessing a name cannot create a row.
                name = row[0] if row else f"{identity.assignment}/{identity.attempt}/{batch}"
                self.db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?)",
                    (
                        identity.assignment,
                        identity.attempt,
                        batch,
                        name,
                    ),
                )
                receipts[batch] = name
        return receipts

    def receipt(self, identity, batch, name):
        row = self.db.execute(
            "SELECT name FROM receipts WHERE assignment=? AND attempt=? AND batch=?",
            (
                identity.assignment,
                identity.attempt,
                batch,
            ),
        ).fetchone()
        if row is None or row[0] != name:
            raise StoreError("missing complete source delivery for this worker and attempt")

    def completed(self, name):
        """Successful reports survive retries; receipts never transfer to a new attempt."""
        with self.lock:
            return {
                r[0]
                for r in self.db.execute("SELECT batch FROM reports WHERE assignment=?", (name,))
            }

    def complete(self, identity, reports, revision, *, conclusions=()):
        encoded = [(report, dumps(report["report"])) for report in reports]
        encoded_conclusions = [(name, dumps(body)) for name, body in conclusions]
        with self.transaction():
            a = self.owned(identity)
            if a["phase"] not in {"parse", "review"}:
                raise StoreError("batch reports are only valid for parse and review")
            current = (
                self.db.execute(
                    "SELECT revision FROM drafts WHERE name=?", (a["draft"],)
                ).fetchone()[0]
                if a["draft"]
                else a["base_revision"]
            )
            if current != revision:
                raise StoreError("report revision changed")
            done = self.completed(a["name"])
            remaining = [b for b in a["batches"] if b not in done]
            for report, body in encoded:
                batch = report["batch"]
                if batch not in a["batches"]:
                    raise StoreError("cannot complete another worker's batch")
                self.receipt(identity, batch, report["receipt"])
                old = self.db.execute(
                    "SELECT body, revision FROM reports WHERE assignment=? "
                    "AND attempt=? AND batch=?",
                    (
                        a["name"],
                        a["attempt"],
                        batch,
                    ),
                ).fetchone()
                if old:
                    if (old[0], old[1]) != (body, revision):
                        raise StoreError("batch report already submitted with different content")
                    continue
                if batch in done:
                    raise StoreError("batch already completed in a previous attempt")
                if not remaining or batch != remaining.pop(0):
                    raise StoreError("complete batches in assigned source order")
                self.db.execute(
                    "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        a["name"],
                        a["attempt"],
                        batch,
                        a["phase"],
                        revision,
                        body,
                    ),
                )

            for name, body in encoded_conclusions:
                old = self.db.execute(
                    "SELECT body FROM conclusions WHERE name=?", (name,)
                ).fetchone()
                if old and old[0] != body:
                    raise StoreError("conclusion already submitted with different content")
                self.db.execute(
                    "INSERT OR IGNORE INTO conclusions VALUES (?, ?, ?)", (name, revision, body)
                )

    def submit(self, identity):
        """Seal an assignment after the workflow's semantic/structural gate succeeds."""
        with self.transaction():
            a = self.owned(identity, claimed=False, submitted=True)
            if a["status"] == "submitted":
                return a
            if a["status"] != "claimed":
                raise StoreError("claim the assignment first")
            if a["phase"] in {"parse", "review"} and set(a["batches"]) != self.completed(a["name"]):
                raise StoreError("assigned batches remain incomplete")
            if a["draft"]:
                self.db.execute("UPDATE drafts SET sealed=1 WHERE name=?", (a["draft"],))
            self._status(a, "submitted")
        return self.assignment(a["name"])
