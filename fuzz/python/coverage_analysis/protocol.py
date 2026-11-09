"""Protocol model, stable catalog, and the mutable dependency graph.

The graph is a NetworkX directed graph whose nodes are steps and whose edges
are dependencies. Analysis writes apply only to a transaction copy; Rust
commits it after validating the proposal.
"""

import copy
import json
from pathlib import Path

import networkx as nx


class ProtocolError(ValueError):
    pass


def _step_view(step):
    return {
        "name": step.get("name", ""),
        "action": step.get("action") or "send",
        "message": step.get("message_name") or None,
        "channel": step.get("channel_name") or "",
        "sender": step.get("sender_name") or None,
        "receiver": step.get("receiver_name") or None,
        "party": step.get("party_name") or None,
        "condition": step.get("condition") or "",
    }


class ProtocolGraph:
    def __init__(self, base, catalog):
        self.base = base
        self.catalog = catalog
        names = [step.get("name") for step in catalog["steps"]]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ProtocolError("catalog step names must be unique")
        self.steps = {step["name"]: step for step in catalog["steps"]}
        self.graph = nx.DiGraph()
        for step in catalog["steps"]:
            self.graph.add_node(step["name"], annotations=[], constraints=[])
        for dependency in catalog.get("dependencies", []):
            prior = dependency.get("prior_step")
            dependent = dependency.get("dependent_step")
            if prior in self.steps and dependent in self.steps:
                self.graph.add_edge(prior, dependent,
                                    condition=dependency.get("condition", ""),
                                    field_relations=list(dependency.get("field_relations") or []),
                                    origin="base")
        self.revision = 0
        self.committed_at = None

    def copy(self):
        clone = copy.copy(self)
        clone.graph = self.graph.copy()
        return clone

    def resolve(self, step):
        if step in self.steps:
            return step
        raise ProtocolError(f"unknown step {step}")

    def step(self, step):
        return self.steps[self.resolve(step)]

    def view(self, step):
        node = self.graph.nodes[self.resolve(step)]
        return {**_step_view(self.steps[self.resolve(step)]),
                "annotations": list(node.get("annotations") or []),
                "constraints": list(node.get("constraints") or [])}

    def dependencies(self, step):
        step = self.resolve(step)
        prior = [{"step": source, **self.step(source), **self.graph.edges[source, step]}
                 for source in self.graph.predecessors(step)]
        after = [{"step": target, **self.step(target), **self.graph.edges[step, target]}
                 for target in self.graph.successors(step)]
        return {"before": prior, "after": after}

    def annotate(self, step, annotations=None, constraints=None):
        step = self.resolve(step)
        node = self.graph.nodes[step]
        if annotations is not None:
            node["annotations"] = [str(item) for item in annotations]
        if constraints is not None:
            node["constraints"] = [str(item) for item in constraints]
        return self.view(step)

    def upsert_dependency(self, prior, dependent, condition="", field_relations=None):
        prior, dependent = self.resolve(prior), self.resolve(dependent)
        if prior == dependent:
            raise ProtocolError("a step cannot depend on itself")
        self.graph.add_edge(prior, dependent, condition=str(condition or ""),
                            field_relations=list(field_relations or []), origin="analysis")
        return {"prior": prior, "dependent": dependent}

    def remove_dependency(self, prior, dependent):
        prior, dependent = self.resolve(prior), self.resolve(dependent)
        if not self.graph.has_edge(prior, dependent):
            raise ProtocolError("dependency does not exist")
        self.graph.remove_edge(prior, dependent)
        return {"prior": prior, "dependent": dependent}

    def induced_dependencies(self, step_ids):
        chosen = {self.resolve(step) for step in step_ids}
        rows = []
        for source, target, attrs in self.graph.edges(data=True):
            if source in chosen and target in chosen:
                rows.append({
                    "prior": source,
                    "dependent": target,
                    "condition": attrs.get("condition", ""),
                    "field_relations": list(attrs.get("field_relations") or []),
                })
        return rows

    def checkpoint(self, request_id=None):
        nodes = []
        for step, attrs in sorted(self.graph.nodes(data=True)):
            annotations = list(attrs.get("annotations") or [])
            constraints = list(attrs.get("constraints") or [])
            if annotations or constraints:
                nodes.append({"step": step, "annotations": annotations,
                              "constraints": constraints})
        edges = []
        for source, target, attrs in self.graph.edges(data=True):
            if attrs.get("origin") == "base":
                continue
            edges.append({"prior": source, "dependent": target,
                          "condition": attrs.get("condition", ""),
                          "field_relations": list(attrs.get("field_relations") or [])})
        return {
            "version": 2,
            "revision": self.revision,
            "request_id": request_id,
            "nodes": nodes,
            "edges": edges,
        }

    def apply_checkpoint(self, checkpoint):
        if checkpoint.get("version") != 2:
            raise ProtocolError("graph checkpoint must have version 2")
        for node in checkpoint.get("nodes") or []:
            step = self.resolve(node["step"])
            self.graph.nodes[step]["annotations"] = list(node.get("annotations") or [])
            self.graph.nodes[step]["constraints"] = list(node.get("constraints") or [])
        for edge in checkpoint.get("edges") or []:
            prior, dependent = self.resolve(edge["prior"]), self.resolve(edge["dependent"])
            self.graph.add_edge(prior, dependent,
                                condition=edge.get("condition", ""),
                                field_relations=list(edge.get("field_relations") or []),
                                origin="analysis")
        self.revision = int(checkpoint.get("revision") or self.revision)


def load_protocol(protocol_path, catalog_path=None, checkpoint_path=None):
    path = Path(protocol_path)
    try:
        model = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f"cannot read protocol model {path}: {error}") from error
    meta = model.get("meta") or {}
    actual = meta.get("schema_version")
    if actual != "8.0.0":
        raise ProtocolError(f"protocol schema_version is {actual}, expected 8.0.0")
    base = {"protocol_name": meta.get("protocol", ""), "model": model}
    if catalog_path:
        try:
            catalog = json.loads(Path(catalog_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ProtocolError(f"cannot read catalog {catalog_path}: {error}") from error
        if catalog.get("version") != 3:
            raise ProtocolError(f"catalog version is {catalog.get('version')}, expected 3")
        if not catalog.get("steps"):
            raise ProtocolError("catalog has no steps")
        model_steps = {step.get("name"): step for step in model.get("steps") or []}
        for step in catalog["steps"]:
            source = model_steps.get(step.get("name"))
            if source is None:
                raise ProtocolError(f"catalog step {step.get('name')!r} is not in the protocol")
            if (source.get("action") or "send") != (step.get("action") or "send"):
                raise ProtocolError(f"catalog step {step.get('name')!r} action does not match")
            if (source.get("action") or "send") != "send" and step.get("message_name"):
                raise ProtocolError("network action catalog step cannot name a message")
    else:
        catalog = build_catalog(model)
    graph = ProtocolGraph(base, catalog)
    if checkpoint_path:
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ProtocolError(f"cannot read graph checkpoint: {error}") from error
        graph.apply_checkpoint(checkpoint)
    return graph


def build_catalog(model, protocol_id=None):
    """Fallback catalog when Rust did not write one (offline tests only)."""
    del protocol_id
    steps = []
    for raw in model.get("steps") or []:
        action = raw.get("action") or "send"
        steps.append({
            "name": raw.get("name") or "",
            "action": action,
            "message_name": raw.get("message_name") if action == "send" else None,
            "party_name": raw.get("party_name") if action != "send" else None,
            "channel_name": raw.get("channel_name") or "",
            "sender_name": raw.get("sender_name") if action == "send" else None,
            "receiver_name": raw.get("receiver_name") if action == "send" else None,
            "condition": raw.get("condition") or "",
        })
    dependencies = []
    for raw in model.get("step_dependencies") or []:
        dependencies.append({
            "prior_step": raw.get("prior_step") or "",
            "dependent_step": raw.get("dependent_step") or "",
            "condition": raw.get("condition") or "",
            "field_relations": raw.get("field_relations") or [],
        })
    return {
        "version": 3,
        "protocol_name": (model.get("meta") or {}).get("protocol", ""),
        "binding": {},
        "parties": [party.get("name") for party in model.get("parties") or []],
        "steps": steps,
        "dependencies": dependencies,
        "documents": (model.get("meta") or {}).get("documents") or [],
    }


def context_for(graph, trace_ids, handlers):
    """Minimal definitions needed by the synthesis agent for one proposal."""
    steps = [graph.view(step) for step in trace_ids]
    messages = sorted({step["message"] for step in steps if step.get("message")})
    channels = sorted({step["channel"] for step in steps if step.get("channel")})
    parties = sorted({
        name
        for step in steps
        for name in (step.get("sender"), step.get("receiver"), step.get("party"))
        if name
    })
    message_handlers = handlers.by_message if handlers else {}
    handler_rows = []
    for message in messages:
        for handler in message_handlers.get(message, []):
            handler_rows.append(handler.view())
    documents = []
    for document in graph.catalog.get("documents") or []:
        documents.append({key: document.get(key) for key in ("doc_id", "rfc", "path")})
    return {
        "steps": steps,
        "messages": messages,
        "channels": channels,
        "parties": parties,
        "handlers": handler_rows,
        "dependencies": graph.induced_dependencies(trace_ids),
        "documents": documents,
    }


def validate_handlers(handlers, graph):
    """Every handler mapping must reference a step name that exists in the catalog."""
    known = set(graph.steps)
    problems = []
    for row in handlers.rows:
        normalized = []
        for step in row.step_names:
            if step in known:
                normalized.append(step)
            else:
                problems.append(f"handler {row.message} references unknown step {step}")
        row.step_names = normalized
    return problems
