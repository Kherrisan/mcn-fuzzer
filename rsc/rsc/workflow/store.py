"""Atomic workflow files and a small coordinator-owned scheduling journal."""

import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path


class StoreError(ValueError):
    pass


def dumps(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def read_json(path):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise StoreError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise StoreError(f"invalid JSON number: {value}")

    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=invalid)


def atomic_json(path, value, *, readonly=False, exclusive=False):
    """Flush the complete file before installing it; never expose a partial document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444 if readonly else 0o600)
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Identity:
    worker: str
    role: str
    assignment: str | None = None
    token: str = ""


class Store:
    def __init__(self, root, *, readonly=False):
        self.root = Path(root).resolve()
        self.readonly = readonly
        self.lock = threading.RLock()
        if (self.root / "workflow.sqlite").exists():
            raise StoreError("old SQLite runs require their archived implementation")
        self._state = read_json(self.root / "state.json")
        self._depth = 0

    @classmethod
    def create(cls, root, settings):
        root = Path(root).resolve()
        if (root / "workflow.sqlite").exists():
            raise StoreError("old SQLite runs require their archived implementation")
        atomic_json(
            root / "state.json",
            {
                **settings,
                "assignments": [],
                "credentials": {},
                "coordinator_token": secrets.token_urlsafe(32),
            },
            exclusive=True,
        )
        store = cls(root)
        store.write_credentials()
        return store

    def save(self):
        if self.readonly:
            raise StoreError("read-only workflow")
        if not self._depth:
            atomic_json(self.root / "state.json", self._state)

    @contextmanager
    def transaction(self):
        with self.lock:
            before = deepcopy(self._state)
            self._depth += 1
            try:
                yield
            except BaseException:
                self._state = before
                raise
            finally:
                self._depth -= 1
            self.save()

    def close(self):
        pass

    def settings(self):
        return deepcopy(
            {
                k: v
                for k, v in self._state.items()
                if k not in {"assignments", "credentials", "coordinator_token"}
            }
        )

    def settings_update(self, **changes):
        self._state.update(deepcopy(changes))
        self.save()
        return self.settings()

    def authenticate(self, token):
        if token == self._state["coordinator_token"]:
            return Identity("coordinator", "coordinator", token=token)
        for name, credential in self._state["credentials"].items():
            if token == credential:
                a = self.assignment(name)
                return Identity(a["worker"], a["role"], name, token)
        raise StoreError("invalid or revoked credential")

    def write_credentials(self):
        if self.readonly:
            return
        values = {"coordinator": self._state["coordinator_token"]}
        for name, credential in self._state["credentials"].items():
            values[self.assignment(name)["worker"]] = credential
        for worker, value in values.items():
            path = self.root / f"{worker}.token"
            descriptor, temporary = tempfile.mkstemp(prefix=".token-", dir=self.root)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def assignment(self, name):
        for a in self._state["assignments"]:
            if a["name"] == name:
                return deepcopy(a)
        raise StoreError("unknown assignment")

    def assignments(self):
        return deepcopy(self._state["assignments"])

    def owned(self, identity):
        if self._state["credentials"].get(identity.assignment) != identity.token:
            raise StoreError("stale or revoked session credential")
        a = self.assignment(identity.assignment)
        if (a["worker"], a["role"]) != (identity.worker, identity.role):
            raise StoreError("stale assignment")
        if a["status"] == "failed":
            raise StoreError("assignment failed")
        return a

    def assign(self, worker, role, phase, round_, **body):
        name = f"{phase}-{round_}/{worker}"
        existing = [a for a in self.assignments() if a["name"] == name]
        if existing:
            return existing[0]
        for a in self.assignments():
            if a["worker"] == worker:
                self._state["credentials"].pop(a["name"], None)
        a = {
            "name": name,
            "worker": worker,
            "role": role,
            "phase": phase,
            "round": round_,
            "status": "pending",
            "created_at": time.time(),
            "read_batches": [],
            "failures": [],
            **deepcopy(body),
        }
        self._state["assignments"].append(a)
        self._state["credentials"][name] = secrets.token_urlsafe(32)
        self.save()
        return deepcopy(a)

    def update_assignment(self, name, **changes):
        for a in self._state["assignments"]:
            if a["name"] == name:
                a.update(deepcopy(changes))
                self.save()
                return deepcopy(a)
        raise StoreError("unknown assignment")

    def mark(self, a, status, **extra):
        return self.update_assignment(
            a["name"], status=status, **{f"{status}_at": time.time()}, **extra
        )

    def restart(self, a, terminal):
        failures = [*a["failures"], {"time": time.time(), "terminal": terminal}]
        self._state["credentials"].pop(a["name"], None)
        if len(failures) > 2:
            return self.mark(a, "failed", failures=failures)
        self._state["credentials"][a["name"]] = secrets.token_urlsafe(32)
        return self.mark(a, "pending", failures=failures, read_batches=[], model_read=False)

    def sources(self):
        path = self.root / "sources.json"
        if not path.exists():
            return {}, []
        data = read_json(path)
        return data["documents"], data["batches"]

    def event(self, kind, **body):
        if self.readonly:
            raise StoreError("read-only workflow")
        with (self.root / "events.jsonl").open("a") as stream:
            stream.write(dumps({"time": time.time(), "kind": kind, **body}) + "\n")
            stream.flush()

    def events(self):
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        result = []
        for line in path.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                continue  # A torn final diagnostic event never owns workflow progress.
        return result
