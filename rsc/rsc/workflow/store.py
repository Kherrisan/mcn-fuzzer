"""Small versioned store for immutable submissions and server-owned scheduling."""

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

VERSION = 3
SCHEMA = """
CREATE TABLE workflow (singleton INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE documents (name TEXT PRIMARY KEY, ordinal INTEGER, body TEXT NOT NULL);
CREATE TABLE batches (name TEXT PRIMARY KEY, ordinal INTEGER, body TEXT NOT NULL);
CREATE TABLE assignments (name TEXT PRIMARY KEY, worker TEXT, role TEXT, phase TEXT,
 round INTEGER, attempt INTEGER, status TEXT, body TEXT NOT NULL);
CREATE TABLE attempts (assignment TEXT, attempt INTEGER, status TEXT, body TEXT,
 PRIMARY KEY(assignment,attempt));
CREATE TABLE credentials (token TEXT PRIMARY KEY, worker TEXT, role TEXT, assignment TEXT,
 attempt INTEGER);
CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, body TEXT NOT NULL);
CREATE TABLE submissions (assignment TEXT, attempt INTEGER, unit TEXT, arguments TEXT,
 result TEXT, PRIMARY KEY(assignment,attempt,unit));
CREATE TABLE snapshots (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE groups (name TEXT PRIMARY KEY, body TEXT NOT NULL, result TEXT);
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
        self.db = sqlite3.connect(
            (self.root / "workflow.sqlite").as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version != VERSION:
            self.db.close()
            raise StoreError(f"workflow version {version}: use its archived source to resume")
        self.lock = threading.RLock()

    @classmethod
    def create(cls, root, settings):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "workflow.sqlite"
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version={VERSION}")
            db.execute("INSERT INTO workflow VALUES (1,?)", (dumps(settings),))
            db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?)",
                (secrets.token_urlsafe(32), "coordinator", "coordinator", None, None),
            )
        store = cls(root)
        store.write_credentials()
        return store

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
        return json.loads(self.db.execute("SELECT body FROM workflow").fetchone()[0])

    def settings_update(self, **changes):
        data = {**self.settings(), **changes}
        self.db.execute("UPDATE workflow SET body=?", (dumps(data),))
        return data

    def authenticate(self, token):
        row = self.db.execute("SELECT * FROM credentials WHERE token=?", (token,)).fetchone()
        if row is None:
            raise StoreError("invalid or revoked credential")
        return Identity(**{k: row[k] for k in ("worker", "role", "assignment", "attempt")})

    def write_credentials(self):
        for row in self.db.execute("SELECT * FROM credentials ORDER BY rowid"):
            path = self.root / f"{row['worker']}.token"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(row["token"])
            temporary.chmod(0o600)
            temporary.replace(path)

    def assignment(self, name):
        row = self.db.execute("SELECT * FROM assignments WHERE name=?", (name,)).fetchone()
        if row is None:
            raise StoreError("unknown assignment")
        return {**dict(row), **json.loads(row["body"])}

    def assignments(self):
        return [
            self.assignment(r[0])
            for r in self.db.execute("SELECT name FROM assignments ORDER BY rowid")
        ]

    def owned(self, identity):
        a = self.assignment(identity.assignment)
        if (a["worker"], a["role"], a["attempt"]) != (
            identity.worker,
            identity.role,
            identity.attempt,
        ):
            raise StoreError("stale assignment or attempt")
        if a["status"] == "failed":
            raise StoreError("assignment failed")
        return a

    def assign(self, worker, role, phase, round_, **body):
        serial = self.db.execute("SELECT count(*) FROM assignments").fetchone()[0] + 1
        name = f"{phase}-{round_}/{worker}-{serial}"
        self.db.execute(
            "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)",
            (name, worker, role, phase, round_, 1, "pending", dumps(body)),
        )
        self.db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?)",
            (name, 1, "pending", dumps({"created_at": time.time()})),
        )
        self.db.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?)",
            (secrets.token_urlsafe(32), worker, role, name, 1),
        )
        return name

    def mark(self, a, status, **extra):
        self.db.execute("UPDATE assignments SET status=? WHERE name=?", (status, a["name"]))
        row = self.db.execute(
            "SELECT body FROM attempts WHERE assignment=? AND attempt=?", (a["name"], a["attempt"])
        ).fetchone()
        body = json.loads(row[0])
        body.setdefault(f"{status}_at", time.time())
        body.update(extra)
        self.db.execute(
            "UPDATE attempts SET status=?,body=? WHERE assignment=? AND attempt=?",
            (status, dumps(body), a["name"], a["attempt"]),
        )

    def sources(self):
        docs = {
            r[0]: json.loads(r[1])
            for r in self.db.execute("SELECT name,body FROM documents ORDER BY ordinal")
        }
        batches = [
            json.loads(r[0]) for r in self.db.execute("SELECT body FROM batches ORDER BY ordinal")
        ]
        return docs, batches

    def items(self, owner=None):
        rows = self.db.execute(
            "SELECT id,body FROM items" + (" WHERE owner=?" if owner else ""),
            (owner,) if owner else (),
        )
        return {f"i{r[0]}": json.loads(r[1]) for r in rows}

    def snapshot(self, name):
        row = self.db.execute("SELECT body FROM snapshots WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_snapshot(self, name, body):
        self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)", (name, dumps(body)))

    def event(self, kind, **body):
        self.db.execute(
            "INSERT INTO events(body) VALUES (?)",
            (dumps({"time": time.time(), "kind": kind, **body}),),
        )
