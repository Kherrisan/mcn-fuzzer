"""Protocol model, stable catalog, and the mutable dependency graph.

The graph is a NetworkX directed graph whose nodes are steps and whose edges
are dependencies. Analysis writes apply only to a transaction copy; Rust
commits it after validating the proposal.
"""

import copy
import json
from pathlib import Path

import networkx as nx

from coverage_analysis import catalog as ids


class ProtocolError(ValueError):
    pass


def _step_view(step):
    return {
        "step_id": step["step_id"],
        "source_id": step.get("source_id"),
        "name": step.get("name", ""),
        "message": step.get("message_name", ""),
        "channel": step.get("channel_name", ""),
        "sender": step.get("sender_name", ""),
        "receiver": step.get("receiver_name", ""),
        "condition": step.get("condition", ""),
    }


class ProtocolGraph:
    def __init__(self, base, catalog):
        self.base = base
        self.catalog = catalog
        self.steps = {step["step_id"]: step for step in catalog["steps"]}
        self.by_source = {step["source_id"]: step["step_id"]
                          for step in catalog["steps"] if step.get("source_id")}
        self.by_name = {step.get("name"): step["step_id"]
                        for step in catalog["steps"] if step.get("name")}
        self.graph = nx.DiGraph()
        for step in catalog["steps"]:
            self.graph.add_node(step["step_id"], annotations=[], constraints=[])
        for dependency in catalog.get("dependencies", []):
            prior = (dependency.get("prior_step_id")
                     or self.by_source.get(dependency.get("prior_step"))
                     or self.by_name.get(dependency.get("prior_step")))
            dependent = (dependency.get("dependent_step_id")
                         or self.by_source.get(dependency.get("dependent_step"))
                         or self.by_name.get(dependency.get("dependent_step")))
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
        if step in self.by_source:
            return self.by_source[step]
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
        prior = [{"step_id": source, **self.step(source), **self.graph.edges[source, step]}
                 for source in self.graph.predecessors(step)]
        after = [{"step_id": target, **self.step(target), **self.graph.edges[step, target]}
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

    def checkpoint(self, proposal_id=None):
        nodes = []
        for step, attrs in sorted(self.graph.nodes(data=True)):
            annotations = list(attrs.get("annotations") or [])
            constraints = list(attrs.get("constraints") or [])
            if annotations or constraints:
                nodes.append({"step_id": step, "annotations": annotations,
                              "constraints": constraints})
        edges = []
        for source, target, attrs in self.graph.edges(data=True):
            if attrs.get("origin") == "base":
                continue
            edges.append({"prior": source, "dependent": target,
                          "condition": attrs.get("condition", ""),
                          "field_relations": list(attrs.get("field_relations") or [])})
        return {
            "version": 1,
            "protocol_id": self.base["protocol_id"],
            "revision": self.revision,
            "proposal_id": proposal_id,
            "nodes": nodes,
            "edges": edges,
        }

    def apply_checkpoint(self, checkpoint):
        if checkpoint.get("version") != 1:
            raise ProtocolError("graph checkpoint must have version 1")
        if checkpoint.get("protocol_id") != self.base["protocol_id"]:
            raise ProtocolError("graph checkpoint belongs to another protocol")
        for node in checkpoint.get("nodes") or []:
            step = self.resolve(node["step_id"])
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
    if meta.get("schema_version") != "7.0.0":
        raise ProtocolError("protocol schema_version must be 7.0.0")
    base = {"protocol_id": ids.protocol_id(model), "protocol_name": meta.get("protocol", ""),
            "model": model}
    if catalog_path:
        try:
            catalog = json.loads(Path(catalog_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ProtocolError(f"cannot read catalog {catalog_path}: {error}") from error
        if catalog.get("version") != 1:
            raise ProtocolError("catalog must have version 1")
        if catalog.get("protocol_id") != base["protocol_id"]:
            raise ProtocolError("catalog protocol_id does not match the protocol model")
        if not catalog.get("steps"):
            raise ProtocolError("catalog has no steps")
        seen = set()
        for step in catalog["steps"]:
            if step["step_id"] in seen:
                raise ProtocolError(f"duplicate step id {step['step_id']}")
            seen.add(step["step_id"])
    else:
        catalog = build_catalog(model, base["protocol_id"])
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
    protocol_id = protocol_id or ids.protocol_id(model)
    steps = []
    for raw in model.get("steps") or []:
        step = {
            "name": raw.get("name", ""),
            "message_name": raw.get("message_name", ""),
            "channel_name": raw.get("channel_name", ""),
            "sender_name": raw.get("sender_name", ""),
            "receiver_name": raw.get("receiver_name", ""),
            "condition": raw.get("condition", ""),
        }
        steps.append({**step, "step_id": ids.step_id(step), "source_id": raw.get("id")})
    by_name = {raw.get("name"): ids.step_id(step) for raw, step in
               zip(model.get("steps") or [], steps)}
    dependencies = []
    for raw in model.get("step_dependencies") or []:
        dependencies.append({
            "prior_step_id": by_name.get(raw.get("prior_step"), ""),
            "dependent_step_id": by_name.get(raw.get("dependent_step"), ""),
            "condition": raw.get("condition", ""),
            "field_relations": raw.get("field_relations") or [],
        })
    return {
        "version": 1,
        "protocol_id": protocol_id,
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
    parties = sorted({name for step in steps for name in (step["sender"], step["receiver"]) if name})
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
    """Every handler mapping must reference a step that exists in the catalog."""
    known = set(graph.steps)
    source = graph.by_source
    problems = []
    for row in handlers.rows:
        normalized = []
        for step in row.step_ids:
            if step in known:
                normalized.append(step)
            elif step in source:
                normalized.append(source[step])
            else:
                problems.append(f"handler {row.message} references unknown step {step}")
        row.step_ids = normalized
    return problems
