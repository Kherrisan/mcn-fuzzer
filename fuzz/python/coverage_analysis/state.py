"""In-memory datasets, analysis requests, and atomic graph transactions."""

import hashlib
import json
import shutil
import subprocess
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from coverage_analysis import callgraph as callgraph_mod
from coverage_analysis import cfg_dot, coverage, handlers as handlers_mod, proposal as proposal_mod
from coverage_analysis import protocol as protocol_mod

DATASETS = ("coverage", "cfg", "callgraph", "protocol", "handlers")
HISTORY = 3


class StateError(ValueError):
    def __init__(self, code, message, missing=None):
        super().__init__(message)
        self.code = code
        self.missing = missing or []


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


@dataclass
class Loaded:
    key: str
    version: int
    digest: str
    identity: dict
    value: object


@dataclass
class Request:
    request_id: str
    session: str
    build: str
    snapshot: int
    output_dir: Path
    snapshot_dir: Path
    model: Path
    catalog: Path
    handlers_path: Path
    checkpoint_path: Path | None = None
    source_dir: Path | None = None
    llvm_cov: str | None = None
    bindings: dict = field(default_factory=dict)
    txn: object = None
    proposal: dict | None = None
    proposal_digest: str | None = None
    committed: bool = False
    aborted: bool = False

    def identity(self):
        return {"session": self.session, "request_id": self.request_id,
                "build": self.build, "snapshot": f"{self.snapshot:06}"}


class State:
    def __init__(self, state_dir, source_root=None):
        self.lock = threading.RLock()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.source_root = Path(source_root).resolve() if source_root else None
        self.versions = {key: OrderedDict() for key in DATASETS}
        self.latest = {}
        self.counter = 0
        self.requests = {}
        self.committed = None
        self.committed_checkpoint = None
        self.committed_proposal = None
        self.committed_request_id = None
        self.graph_revision = 0
        self.alias_diagnostics = []
        # One analysis session loads one build and one callgraph, and both
        # carry every symbol of that build. The join therefore stays valid for
        # the whole session; snapshots differ only in counters.
        self.alias_mapping = None

    def log(self, kind, body):
        path = self.state_dir / "service.jsonl"
        event = {"time_ms": int(time.time() * 1000), "kind": kind, **body}
        with self.lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    # -- loaders ---------------------------------------------------------
    def _store(self, key, value, digest, identity):
        self.counter += 1
        loaded = Loaded(key, self.counter, digest, identity, value)
        self.versions[key][loaded.version] = loaded
        while len(self.versions[key]) > HISTORY:
            self.versions[key].popitem(last=False)
        self.latest[key] = loaded
        return loaded

    def dataset(self, key, request_id=None):
        if key not in self.versions:
            raise StateError("internal", f"unknown dataset {key}")
        if request_id:
            request = self.requests.get(request_id)
            if request and key in request.bindings:
                loaded = self.versions[key].get(request.bindings[key])
                if loaded:
                    return loaded
        loaded = self.latest.get(key)
        if loaded is None:
            raise StateError("not_loaded", f"{key} is not loaded",
                             missing=[f"load_{key}"])
        return loaded

    def _bind(self, request_id, key, loaded):
        if request_id:
            request = self.requests.get(request_id)
            if request:
                request.bindings[key] = loaded.version

    def _coverage_for_path(self, coverage_json):
        wanted = str(coverage_json)
        for loaded in reversed(self.versions["coverage"].values()):
            if loaded.identity.get("coverage_path") == wanted:
                return loaded
        return None

    def _loaded_identity(self, key):
        loaded = self.latest.get(key)
        return loaded.identity if loaded else {}

    def _alias_join(self, coverage_data):
        """Join one coverage model to the loaded callgraph.

        Returns True when the mapping already existed. One analysis session
        loads a single build and callgraph, so the first model computes the
        join and later snapshots reuse it; each model still pays for re-keying
        its own function table.
        """
        graph = self.latest.get("callgraph")
        if graph is None:
            return False
        reused = self.alias_mapping is not None
        if self.alias_mapping is None:
            self.alias_mapping, self.alias_diagnostics = coverage.compute_aliases(
                coverage_data.functions, graph.value.successors.keys())
        if coverage_data.aliases is not self.alias_mapping:
            coverage.apply_aliases(coverage_data, self.alias_mapping)
        return reused

    def load_coverage_json(self, request_id, snapshot_dir):
        started = time.perf_counter()

        def mark():
            nonlocal started
            now = time.perf_counter()
            elapsed = round((now - started) * 1000, 1)
            started = now
            return elapsed

        snapshot = Path(snapshot_dir).resolve()
        coverage_path = snapshot / "coverage.json"
        cached = self._coverage_for_path(coverage_path)
        lookup_ms = mark()
        if cached is not None:
            self._bind(request_id, "coverage", cached)
            alias_ms = 0.0
            aliases_reused = False
            if self.latest.get("callgraph"):
                aliases_reused = self._alias_join(cached.value)
                alias_ms = mark()
            self.log("load_coverage_json", {
                "request_id": request_id,
                "coverage_path": str(coverage_path),
                "reused": True,
                "version": cached.version,
                "lookup_ms": lookup_ms,
                "build_aliases_ms": alias_ms,
                "aliases_reused": aliases_reused,
                "total_ms": round(lookup_ms + alias_ms, 1),
            })
            return {"version": cached.version, **cached.identity, "reused": True}
        metadata_path = snapshot / "metadata.json"
        if not metadata_path.is_file():
            raise StateError("incompatible_data", f"missing {metadata_path}")
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("incompatible_data", f"cannot read snapshot metadata: {error}")
        metadata_ms = mark()
        temporaries = []
        if coverage_path.is_file():
            try:
                digest = sha256_file(coverage_path)
                raw = coverage_path.read_bytes()
            except OSError as error:
                raise StateError("incompatible_data", f"cannot read {coverage_path}: {error}")
        else:
            digest, raw, temporaries = self._export_from_profdata(
                snapshot, metadata, request_id)
        sha_ms = mark()
        read_ms = mark()
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StateError("incompatible_data", f"cannot parse snapshot coverage: {error}")
        parse_ms = mark()
        try:
            data = coverage.load_coverage_document(document, self.source_root)
        except coverage.CoverageError as error:
            raise StateError("incompatible_data", str(error))
        model_ms = mark()
        data.digest = digest
        request = self.requests.get(request_id)
        build = request.build if request else snapshot.parent.name
        identity = {"build": build, "snapshot": snapshot.name,
                    "binary": metadata.get("binary"),
                    "coverage_path": str(coverage_path),
                    }
        loaded = self._store("coverage", data, digest, identity)
        store_ms = mark()
        alias_ms = 0.0
        aliases_reused = False
        if self.latest.get("callgraph"):
            aliases_reused = self._alias_join(data)
            alias_ms = mark()
        self._bind(request_id, "coverage", loaded)
        # The snapshot JSON is a transfer format: once its counters are in
        # memory the file is dead weight (hundreds of MB per snapshot).
        json_removed = self._discard_coverage_json(coverage_path)
        for path in temporaries:
            path.unlink(missing_ok=True)
        stages = {
            "lookup_ms": lookup_ms,
            "metadata_ms": metadata_ms,
            "sha256_ms": sha_ms,
            "read_file_ms": read_ms,
            "json_parse_ms": parse_ms,
            "build_model_ms": model_ms,
            "store_ms": store_ms,
            "build_aliases_ms": alias_ms,
        }
        self.log("load_coverage_json", {
            "request_id": request_id,
            "coverage_path": str(coverage_path),
            "reused": False,
            "version": loaded.version,
            "bytes": len(raw),
            "functions": len(data.functions),
            "aliases_reused": aliases_reused,
            "json_removed": json_removed,
            **stages,
            "total_ms": round(sum(stages.values()), 1),
        })
        return {"version": loaded.version, **identity, "coverage": coverage_path.name}

    @staticmethod
    def _discard_coverage_json(path):
        """Delete the snapshot JSON after its model has been built."""
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _export_from_profdata(self, snapshot, metadata, request_id):
        """Recreate the detailed JSON from the compressed profile.

        The fuzzer only stores the merged profile; decompression and the
        llvm-cov export happen here, and both temporaries are removed once
        the model is built.
        """
        request = self.requests.get(request_id)
        llvm_cov = (request.llvm_cov if request else None) or shutil.which("llvm-cov-18") \
            or shutil.which("llvm-cov")
        if not llvm_cov:
            raise StateError("incompatible_data", "llvm-cov executable is not configured")
        binary = metadata.get("binary")
        if not binary or not Path(binary).is_file():
            raise StateError("incompatible_data", f"snapshot binary is missing: {binary}")
        profile = snapshot / "coverage.profdata.zlib"
        if not profile.is_file():
            raise StateError("incompatible_data", f"missing {profile}")
        try:
            data = zlib.decompress(profile.read_bytes())
        except zlib.error as error:
            raise StateError("incompatible_data", f"cannot decompress {profile}: {error}")
        profdata = snapshot / ".profdata.tmp"
        output = snapshot / ".coverage.tmp.json"
        profdata.write_bytes(data)
        try:
            with output.open("wb") as handle:
                result = subprocess.run(
                    [llvm_cov, "export", binary, f"-instr-profile={profdata}"],
                    stdout=handle, stderr=subprocess.PIPE, text=False)
        except OSError as error:
            profdata.unlink(missing_ok=True)
            raise StateError("incompatible_data", f"cannot run {llvm_cov}: {error}")
        if result.returncode != 0:
            profdata.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise StateError("incompatible_data", f"{llvm_cov} export failed: {detail}")
        try:
            raw = output.read_bytes()
            digest = sha256_bytes(raw)
        except OSError as error:
            profdata.unlink(missing_ok=True)
            raise StateError("incompatible_data", f"cannot read exported json: {error}")
        return digest, raw, [output, profdata]

    def load_cfg(self, cfg_dir):
        root = Path(cfg_dir).resolve()
        if not root.is_dir():
            raise StateError("incompatible_data", f"CFG directory {root} does not exist")
        value = cfg_dot.load_cfg_dir(root)
        identity = {"cfg_dir": str(root), "functions": len(value["functions"])}
        digest = sha256_bytes(json.dumps(
            {name: cfg for name, cfg in value["functions"].items()},
            sort_keys=True, separators=(",", ":")).encode())
        loaded = self._store("cfg", value, digest, identity)
        return {"version": loaded.version, **identity}

    def load_callgraph(self, callgraph_dot):
        path = Path(callgraph_dot).resolve()
        if not path.is_file():
            raise StateError("incompatible_data", f"callgraph {path} does not exist")
        value = callgraph_mod.load_callgraph(path)
        value.digest = sha256_file(path)
        identity = {"callgraph": str(path), "functions": value.node_count}
        loaded = self._store("callgraph", value, value.digest, identity)
        if self.latest.get("coverage"):
            self._alias_join(self.latest["coverage"].value)
        return {"version": loaded.version, **identity,
                "aliases": len(self.latest["coverage"].value.aliases)
                if self.latest.get("coverage") else 0}

    def load_protocol(self, protocol_path, catalog_path, checkpoint_path=None):
        graph = protocol_mod.load_protocol(protocol_path, catalog_path, checkpoint_path)
        identity = {"protocol_name": graph.base["protocol_name"],
                    "steps": len(graph.steps), "revision": graph.revision}
        digest = sha256_file(protocol_path)
        loaded = self._store("protocol", graph, digest, identity)
        self.committed = graph
        self.committed_checkpoint = graph.checkpoint(None)
        self.graph_revision = graph.revision
        return {"version": loaded.version, **identity}

    def load_handlers(self, handlers_path):
        value = handlers_mod.load_handlers(handlers_path)
        identity = {"handlers": str(Path(handlers_path).resolve()),
                    "messages": len(value.rows),
                    "handlers": len(value.by_id)}
        digest = sha256_file(handlers_path)
        loaded = self._store("handlers", value, digest, identity)
        if self.committed:
            problems = protocol_mod.validate_handlers(value, self.committed)
            if problems:
                self.versions["handlers"].pop(loaded.version)
                self.latest.pop("handlers", None)
                raise StateError("incompatible_data", "; ".join(problems[:5]))
        return {"version": loaded.version, **identity}

    # -- requests and transactions --------------------------------------
    def register_request(self, payload):
        for field in ("request_id", "session", "build", "snapshot",
                      "output_dir", "snapshot_dir", "model", "catalog",
                      "handlers"):
            if payload.get(field) in (None, ""):
                raise StateError("bad_request", f"request registration needs {field}")
        request = Request(
            request_id=str(payload["request_id"]),
            session=str(payload["session"]),
            build=str(payload["build"]),
            snapshot=int(payload["snapshot"]),
            output_dir=Path(payload["output_dir"]),
            snapshot_dir=Path(payload["snapshot_dir"]),
            model=Path(payload["model"]),
            catalog=Path(payload["catalog"]),
            handlers_path=Path(payload["handlers"]),
            checkpoint_path=Path(payload["checkpoint"]) if payload.get("checkpoint") else None,
            source_dir=Path(payload["source_dir"]) if payload.get("source_dir") else None,
            llvm_cov=str(payload["llvm_cov"]) if payload.get("llvm_cov") else None,
        )
        self.requests[request.request_id] = request
        return request.identity()

    def request(self, request_id):
        request = self.requests.get(request_id)
        if request is None:
            raise StateError("unknown_request", f"request {request_id} is not registered")
        return request

    def txn_graph(self, request_id):
        request = self.request(request_id)
        if request.aborted:
            raise StateError("aborted", "request was aborted")
        if request.txn is None:
            raise StateError("no_transaction", "begin_analysis has not been called")
        return request.txn

    def begin_analysis(self, request_id):
        request = self.request(request_id)
        if request.aborted:
            raise StateError("aborted", "request was aborted")
        base = self.committed
        if base is None:
            loaded = self.dataset("protocol", request_id)
            base = loaded.value
        request.txn = base.copy()
        request.txn.revision = self.graph_revision
        return {"request_id": request_id, "graph_revision": request.txn.revision,
                "steps": len(request.txn.steps)}

    def submit_proposal(self, request_id, payload):
        request = self.request(request_id)
        graph = self.txn_graph(request_id)
        handlers = self.dataset("handlers", request_id).value
        proposal = proposal_mod.build_proposal(
            graph,
            origin=str(payload.get("origin") or "coverage"),
            session=request.session,
            build=request.build,
            snapshot=f"{request.snapshot:06}",
            goal=payload.get("goal") or {},
            trace=payload.get("trace") or [],
            handlers=handlers,
            parties=payload.get("parties") or {},
            channels=payload.get("channels") or {},
            mutations=payload.get("mutations") or [],
            dependencies=payload.get("dependencies"),
        )
        proposal_mod.validate_proposal(proposal, graph, handlers, request.identity())
        path = proposal_mod.freeze(request.output_dir / "proposal.json", proposal)
        request.proposal = proposal
        request.proposal_digest = sha256_file(path)
        checkpoint = graph.checkpoint(request.request_id)
        checkpoint_path = request.output_dir / "graph-checkpoint.json"
        proposal_mod.freeze(checkpoint_path, checkpoint)
        return {"request_id": request_id,
                "graph_revision": graph.revision,
                "proposal_sha256": request.proposal_digest,
                "path": str(path)}

    def commit_analysis(self, request_id, proposal_sha256):
        request = self.request(request_id)
        graph = self.txn_graph(request_id)
        if request.proposal is None:
            raise StateError("no_proposal", "no proposal was submitted for this request")
        path = request.output_dir / "proposal.json"
        if not path.is_file() or sha256_file(path) != proposal_sha256:
            raise StateError("mismatch", "frozen proposal bytes do not match")
        if request.committed:
            return {"request_id": request_id,
                    "graph_revision": self.graph_revision, "committed": True}
        graph.revision = self.graph_revision + 1
        self.graph_revision = graph.revision
        self.committed = graph
        self.committed_checkpoint = graph.checkpoint(request_id)
        self.committed_proposal = request.proposal
        self.committed_request_id = request_id
        request.committed = True
        self._persist_commit()
        return {"request_id": request_id,
                "graph_revision": self.graph_revision, "committed": True}

    def _persist_commit(self):
        directory = self.state_dir / "committed"
        directory.mkdir(parents=True, exist_ok=True)
        proposal_mod.freeze(directory / "proposal.json", self.committed_proposal)
        proposal_mod.freeze(directory / "graph.json", self.committed_checkpoint)

    def abort_analysis(self, request_id):
        request = self.request(request_id)
        request.aborted = True
        request.txn = None
        request.proposal = None
        return {"request_id": request_id, "aborted": True}

    def loaded_state(self):
        state = {}
        for key in DATASETS:
            loaded = self.latest.get(key)
            state[key] = ({"loaded": True, "version": loaded.version,
                           "digest": loaded.digest, **loaded.identity}
                          if loaded else {"loaded": False})
        state["graph_revision"] = self.graph_revision
        state["committed_proposal"] = self.committed_request_id
        return state
