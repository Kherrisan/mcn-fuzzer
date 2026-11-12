"""In-memory datasets, analysis requests, and atomic graph transactions."""

import hashlib
import json
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
    manifest: Path
    model: Path
    catalog: Path
    handlers_path: Path
    checkpoint_path: Path | None = None
    source_dir: Path | None = None
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

    def _manifest(self, path):
        try:
            manifest = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("incompatible_data", f"cannot read manifest {path}: {error}")
        return manifest

    def _bind(self, request_id, key, loaded):
        if request_id:
            request = self.requests.get(request_id)
            if request:
                request.bindings[key] = loaded.version

    def _loaded_identity(self, key):
        loaded = self.latest.get(key)
        return loaded.identity if loaded else {}

    def load_profdata(self, request_id, snapshot_dir, manifest_path):
        snapshot = Path(snapshot_dir).resolve()
        metadata_path = snapshot / "metadata.json"
        coverage_path = snapshot / "coverage.json"
        if not metadata_path.is_file():
            raise StateError("incompatible_data", f"missing {metadata_path}")
        if not coverage_path.is_file():
            raise StateError("incompatible_data", f"missing {coverage_path}")
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("incompatible_data", f"cannot read snapshot metadata: {error}")
        manifest = self._manifest(manifest_path)
        if metadata.get("binary_sha256") and metadata["binary_sha256"] != manifest.get("binary_sha256"):
            raise StateError("incompatible_data",
                             "snapshot binary does not match the build manifest")
        profdata = snapshot / "coverage.profdata.zlib"
        if not profdata.is_file():
            profdata = snapshot / "coverage.profdata"
        if not profdata.is_file():
            raise StateError("incompatible_data", f"no profdata under {snapshot}")
        digest = sha256_file(coverage_path)
        data = coverage.load_coverage(coverage_path, self.source_root)
        data.digest = digest
        build = str(metadata.get("fingerprint") or snapshot.parent.name)
        identity = {"build": build, "snapshot": snapshot.name,
                    "binary": metadata.get("binary"), "binary_sha256": metadata.get("binary_sha256"),
                    }
        loaded = self._store("coverage", data, digest, identity)
        if self.latest.get("callgraph"):
            self.alias_diagnostics = coverage.build_aliases(
                data, self.latest["callgraph"].value.successors.keys())
        self._bind(request_id, "coverage", loaded)
        return {"version": loaded.version, **identity, "profdata": profdata.name}

    def load_cfg(self, cfg_dir, manifest_path):
        root = Path(cfg_dir).resolve()
        if not root.is_dir():
            raise StateError("incompatible_data", f"CFG directory {root} does not exist")
        manifest = self._manifest(manifest_path)
        value = cfg_dot.load_cfg_dir(root)
        identity = {"cfg_dir": str(root), "functions": len(value["functions"]),
                    "graphs_sha256": manifest.get("graphs_sha256")}
        digest = sha256_bytes(json.dumps(
            {name: cfg for name, cfg in value["functions"].items()},
            sort_keys=True, separators=(",", ":")).encode())
        loaded = self._store("cfg", value, digest, identity)
        return {"version": loaded.version, **identity}

    def load_callgraph(self, callgraph_dot, manifest_path):
        path = Path(callgraph_dot).resolve()
        if not path.is_file():
            raise StateError("incompatible_data", f"callgraph {path} does not exist")
        manifest = self._manifest(manifest_path)
        value = callgraph_mod.load_callgraph(path)
        value.digest = sha256_file(path)
        identity = {"callgraph": str(path), "functions": value.node_count,
                    "graphs_sha256": manifest.get("graphs_sha256")}
        loaded = self._store("callgraph", value, value.digest, identity)
        if self.latest.get("coverage"):
            self.alias_diagnostics = coverage.build_aliases(
                self.latest["coverage"].value, value.successors.keys())
        self._verify_graphs(manifest, path)
        return {"version": loaded.version, **identity,
                "aliases": len(self.latest["coverage"].value.aliases)
                if self.latest.get("coverage") else 0}

    def _verify_graphs(self, manifest, callgraph_path):
        if "graphs_sha256" not in manifest:
            return
        cfg_loaded = self.latest.get("cfg")
        if not cfg_loaded:
            return
        root = callgraph_path.parent
        paths = [callgraph_path]
        cfg_root = Path(cfg_loaded.identity["cfg_dir"])
        paths.extend(sorted(cfg_root.glob("*.dot")))
        source_map = cfg_root / "source-map.json"
        if source_map.is_file():
            paths.append(source_map)
        from coverage_analysis.manifest import graphs_digest
        relative = [path.resolve().relative_to(root).as_posix() for path in paths]
        if graphs_digest(root, relative) != manifest["graphs_sha256"]:
            raise StateError("incompatible_data",
                             "callgraph/CFG files do not match manifest graphs_sha256")

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
                      "output_dir", "snapshot_dir", "manifest", "model", "catalog",
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
            manifest=Path(payload["manifest"]),
            model=Path(payload["model"]),
            catalog=Path(payload["catalog"]),
            handlers_path=Path(payload["handlers"]),
            checkpoint_path=Path(payload["checkpoint"]) if payload.get("checkpoint") else None,
            source_dir=Path(payload["source_dir"]) if payload.get("source_dir") else None,
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
