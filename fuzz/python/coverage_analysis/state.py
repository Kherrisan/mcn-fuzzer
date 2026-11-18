"""In-memory datasets, analysis requests, and atomic graph transactions."""

import hashlib
import json
import os
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
    handlers_path: Path
    sut_parties: list = field(default_factory=list)
    generator_parties: list = field(default_factory=list)
    unavailable_parties: list = field(default_factory=list)
    checkpoint_path: Path | None = None
    source_dir: Path | None = None
    llvm_cov: str | None = None
    bindings: dict = field(default_factory=dict)
    txn: object = None
    analysis_target: dict | None = None
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
        self.session_config = {}
        self.session_config_path = None
        self.active_request_id = None
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
    def _store_handlers(self, value, identity):
        """Keep exactly one authoritative handler mapping.

        Initialization may revise the mapping several times and every request
        reads the current one, so versions and digests are not recorded.
        """
        loaded = Loaded("handlers", 1, "", identity, value)
        self.versions["handlers"] = OrderedDict({1: loaded})
        self.latest["handlers"] = loaded
        return loaded

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

    def _check_static_load(self, key):
        if key in self.latest:
            raise StateError("already_loaded", f"{key} is already loaded for this service")

    def load_cfg(self, cfg_dir):
        self._check_static_load("cfg")
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
        self._check_static_load("callgraph")
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

    def load_protocol(self, protocol_path, checkpoint_path=None, binding=None):
        self._check_static_load("protocol")
        graph = protocol_mod.load_protocol(protocol_path, checkpoint_path, binding)
        identity = {"protocol_name": graph.base["protocol_name"],
                    "steps": len(graph.steps), "revision": graph.revision}
        digest = sha256_file(protocol_path)
        loaded = self._store("protocol", graph, digest, identity)
        self.committed = graph
        self.committed_request_id = graph.checkpoint_request_id
        self.committed_checkpoint = graph.checkpoint(self.committed_request_id)
        self.graph_revision = graph.revision
        return {"version": loaded.version, **identity}

    def write_handlers(self, output_dir, session, mappings, load=True):
        """Serialise a handler mapping, re-read it, and load it.

        Agents describe mappings as data instead of writing JSON text, so a
        stray escape or trailing document cannot abort campaign initialization.
        The loaded mapping is authoritative for this service: requests whose
        registered file is byte-identical resolve without a separate
        ``load_handlers`` call.
        """
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        target = output / "handlers.json"
        document = {"session": str(session or ""), "mappings": list(mappings or [])}
        if not document["session"]:
            raise StateError("bad_request", "write_handlers needs a session")
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        os.replace(temporary, target)
        # Re-read through the production loader: it enforces the same shape the
        # campaign would otherwise validate only at startup.
        value = handlers_mod.load_handlers(target)
        graph = self.committed
        if graph is None and self.latest.get("protocol") is not None:
            graph = self.latest["protocol"].value
        problems = protocol_mod.validate_handlers(value, graph) if graph is not None else []
        if problems:
            raise StateError("incompatible_data", "; ".join(problems[:5]))
        identity = {"handlers_path": str(target.resolve()),
                    "messages": len(value.rows),
                    "handlers": len(value.by_id)}
        result = {"handlers_path": identity["handlers_path"],
                  "messages": identity["messages"],
                  "handlers": identity["handlers"],
                  "bytes": target.stat().st_size}
        if load:
            # Initialization may revise the mapping several times; the newest
            # write simply replaces the authoritative mapping.
            self._store_handlers(value, identity)
        return result

    def load_handlers(self, handlers_path):
        value = handlers_mod.load_handlers(handlers_path)
        identity = {"handlers_path": str(Path(handlers_path).resolve()),
                    "messages": len(value.rows),
                    "handlers": len(value.by_id)}
        digest = sha256_file(handlers_path)
        # The protocol graph may exist before any proposal commits; validate
        # against it so a bad step/message pairing fails here instead of
        # aborting the campaign later during initialization.
        graph = self.committed
        if graph is None and self.latest.get("protocol") is not None:
            graph = self.latest["protocol"].value
        if graph is not None:
            problems = protocol_mod.validate_handlers(value, graph)
            if problems:
                raise StateError("incompatible_data", "; ".join(problems[:5]))
        loaded = self._store_handlers(value, identity)
        return {"version": loaded.version, **identity}

    # -- requests and transactions --------------------------------------
    def load_session_config(self, path):
        """Load campaign-wide facts once, when the service starts.

        Session, build, model, handler mapping, party bindings, and the
        llvm-cov tool are identical for every analysis request in a campaign.
        The server reads them from this file instead of receiving copies on
        every register call.
        """
        config_path = Path(path)
        self.session_config_path = config_path
        try:
            document = json.loads(config_path.read_text())
        except FileNotFoundError:
            # The supervisor starts the service before the analysis stage
            # writes this file; load it on the first register instead.
            return {"session_config": str(config_path), "deferred": True}
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("incompatible_data",
                             f"cannot read session config {config_path}: {error}") from error
        if not isinstance(document, dict):
            raise StateError("incompatible_data", "session config must be an object")
        allowed = {"session", "build", "model", "handlers", "llvm_cov",
                   "source_dir", "sut_parties", "generator_parties",
                   "unavailable_parties", "configure_options", "cli_args"}
        unknown = sorted(set(document) - allowed)
        if unknown:
            raise StateError("incompatible_data",
                             f"session config has unknown fields: {', '.join(unknown)}")
        self.session_config = document
        return {"session_config": str(config_path.resolve()),
                "fields": sorted(document)}

    def _ensure_session_config(self):
        if self.session_config or self.session_config_path is None:
            return
        path = self.session_config_path
        try:
            document = json.loads(Path(path).read_text())
        except FileNotFoundError as error:
            raise StateError(
                "incompatible_data",
                f"session config {path} was never written before the first request",
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("incompatible_data",
                             f"cannot read session config {path}: {error}") from error
        if not isinstance(document, dict):
            raise StateError("incompatible_data", "session config must be an object")
        self.session_config = document

    def _session_value(self, payload, field, default=None):
        value = payload.get(field)
        if value in (None, ""):
            value = self.session_config.get(field, default)
        return value

    def register_request(self, payload):
        self._ensure_session_config()
        merged = {
            "request_id": payload.get("request_id"),
            "snapshot": payload.get("snapshot"),
            "output_dir": payload.get("output_dir"),
            "snapshot_dir": payload.get("snapshot_dir"),
            "checkpoint": payload.get("checkpoint"),
        }
        for field in ("session", "build", "model", "handlers", "llvm_cov",
                      "source_dir", "sut_parties", "generator_parties",
                      "unavailable_parties"):
            merged[field] = self._session_value(payload, field)
        for field in ("request_id", "snapshot", "output_dir", "snapshot_dir",
                      "session", "build", "model", "handlers"):
            if merged.get(field) in (None, ""):
                raise StateError("bad_request",
                                 f"request registration needs {field}; "
                                 "provide it here or in the session config")
        payload = merged
        request = Request(
            request_id=str(payload["request_id"]),
            session=str(payload["session"]),
            build=str(payload["build"]),
            snapshot=int(payload["snapshot"]),
            output_dir=Path(payload["output_dir"]),
            snapshot_dir=Path(payload["snapshot_dir"]),
            model=Path(payload["model"]),
            sut_parties=[str(name) for name in payload.get("sut_parties") or []],
            generator_parties=[str(name) for name in payload.get("generator_parties") or []],
            unavailable_parties=[str(name) for name in payload.get("unavailable_parties") or []],
            handlers_path=Path(payload["handlers"]),
            checkpoint_path=Path(payload["checkpoint"]) if payload.get("checkpoint") else None,
            source_dir=Path(payload["source_dir"]) if payload.get("source_dir") else None,
            llvm_cov=str(payload["llvm_cov"]) if payload.get("llvm_cov") else None,
        )
        self.requests[request.request_id] = request
        loaded = self.latest.get("protocol")
        if loaded is not None:
            loaded.value.catalog["binding"] = self._binding(request)
        return request.identity()

    @staticmethod
    def _binding(request):
        binding = {name: "sut" for name in request.sut_parties}
        binding.update({name: "generator" for name in request.generator_parties})
        binding.update({name: "unavailable" for name in request.unavailable_parties})
        return binding

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
        self.active_request_id = request_id
        return {"request_id": request_id, "graph_revision": request.txn.revision,
                "steps": len(request.txn.steps)}

    def _active_request(self):
        if self.active_request_id is None:
            raise StateError("no_active_analysis", "call begin_analysis first")
        return self.request(self.active_request_id)

    def _request_handlers(self, request):
        """The service keeps one handler mapping; every request reads it."""
        loaded = self.latest.get("handlers")
        if loaded is None:
            raise StateError("not_loaded", "the handler mapping is not loaded",
                             missing=["write_handlers"])
        return loaded.value

    @staticmethod
    def _target_status(request):
        target = request.analysis_target
        return {"request_id": request.request_id, "target": target.copy() if target else None}

    def get_analysis_target(self):
        return self._target_status(self._active_request())

    def select_analysis_target(self, handler_id, reason):
        request = self._active_request()
        if request.analysis_target is not None:
            if request.analysis_target == {"handler_id": handler_id, "reason": reason}:
                return self._target_status(request)
            raise StateError("target_already_selected", "use switch_analysis_target to change the target")
        return self._set_analysis_target(request, handler_id, reason)

    def switch_analysis_target(self, handler_id, reason):
        request = self._active_request()
        if request.analysis_target is None:
            raise StateError("analysis_target_required", "call select_analysis_target first")
        if request.analysis_target["handler_id"] == handler_id:
            raise StateError("bad_request", "switch_analysis_target requires a different handler")
        return self._set_analysis_target(request, handler_id, reason)

    def _set_analysis_target(self, request, handler_id, reason):
        if request.proposal is not None:
            raise StateError("proposal_submitted", "the submitted proposal locks the analysis target")
        if not isinstance(reason, str) or not reason.strip():
            raise StateError("bad_request", "an analysis target needs a non-empty reason")
        handlers = self._request_handlers(request)
        if not isinstance(handler_id, str) or handler_id not in handlers.by_id:
            raise StateError("unknown_handler", f"unknown handler {handler_id!r} for this request")
        request.analysis_target = {"handler_id": handler_id, "reason": reason}
        return self._target_status(request)

    def submit_proposal(self, request_id, payload):
        if payload.get("mutations"):
            raise StateError(
                "bad_request",
                "declare mutable_fields on each trace instance instead of mutations",
            )
        request = self.request(request_id)
        graph = self.txn_graph(request_id)
        origin = str(payload.get("origin") or "coverage")
        goal = payload.get("goal") or {}
        if not isinstance(goal, dict):
            raise StateError("bad_request", "goal must be an object")
        if origin == "coverage":
            target = request.analysis_target
            if target is None:
                raise StateError("analysis_target_required", "call select_analysis_target before submitting")
            if goal.get("handler_ids") != [target["handler_id"]]:
                raise StateError("analysis_target_mismatch",
                                 f"goal.handler_ids must be [{target['handler_id']!r}]")
            handlers = self._request_handlers(request)
        else:
            handlers = self.dataset("handlers", request_id).value
        proposal = proposal_mod.build_proposal(
            graph,
            origin=origin,
            session=request.session,
            build=request.build,
            snapshot=f"{request.snapshot:06}",
            goal=goal,
            trace=payload.get("trace") or [],
            handlers=handlers,
            parties=payload.get("parties") or {},
            channels=payload.get("channels") or {},
            dependencies=payload.get("dependencies"),
        )
        proposal_mod.validate_proposal(proposal, graph, handlers, request.identity())
        path = proposal_mod.freeze(request.output_dir / "proposal.json", proposal)
        digest = sha256_file(path)
        checkpoint = graph.checkpoint(request.request_id)
        checkpoint_path = request.output_dir / "graph-checkpoint.json"
        proposal_mod.freeze(checkpoint_path, checkpoint)
        request.proposal = proposal
        request.proposal_digest = digest
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
        if not request.committed:
            graph.revision = self.graph_revision + 1
            self.graph_revision = graph.revision
            self.committed = graph
            self.committed_checkpoint = graph.checkpoint(request_id)
            self.committed_proposal = request.proposal
            self.committed_request_id = request_id
            request.committed = True
            self._persist_commit()
        if self.active_request_id == request_id:
            self.active_request_id = None
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
        if self.active_request_id == request_id:
            self.active_request_id = None
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
