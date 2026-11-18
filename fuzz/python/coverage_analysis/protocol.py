"""Protocol model and the mutable dependency graph.

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
        self.checkpoint_request_id = None
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

    def message_index(self):
        """Map message name to the steps that carry it, plus model fields."""
        model = self.base.get("model") or {}
        fields = {}
        for message in model.get("messages") or []:
            name = message.get("name")
            if name:
                fields[name] = [field.get("name") for field in message.get("associated_fields") or []]
        index = {name: [] for name in fields}
        for step in self.steps.values():
            message = step.get("message_name")
            if not message:
                continue
            index.setdefault(message, []).append({
                "step": step["name"],
                "action": step.get("action") or "send",
                "channel": step.get("channel_name") or "",
                "sender": step.get("sender_name"),
                "receiver": step.get("receiver_name"),
                "party": step.get("party_name"),
            })
        return {"fields": fields, "steps": index}

    def direction(self, row, roles):
        """Classify one carrying step from the campaign's party roles."""
        if row["action"] != "send":
            return "network"
        sender, receiver = row.get("sender"), row.get("receiver")
        if roles.get(receiver) == "sut" and roles.get(sender) != "sut":
            return "sut_inbound"
        if roles.get(sender) == "sut" and roles.get(receiver) != "sut":
            return "sut_outbound"
        if roles.get(sender) == "generator" or roles.get(receiver) == "generator":
            return "generator_local"
        return "unbound"

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
        removed = [
            {"prior": edge["prior_step"], "dependent": edge["dependent_step"]}
            for edge in self.catalog.get("dependencies", [])
            if edge["prior_step"] in self.steps and edge["dependent_step"] in self.steps
            and not self.graph.has_edge(edge["prior_step"], edge["dependent_step"])
        ]
        return {
            "revision": self.revision,
            "request_id": request_id,
            "nodes": nodes,
            "edges": edges,
            **({"removed_edges": removed} if removed else {}),
        }

    def apply_checkpoint(self, checkpoint):
        for edge in checkpoint.get("removed_edges") or []:
            prior, dependent = self.resolve(edge["prior"]), self.resolve(edge["dependent"])
            if self.graph.has_edge(prior, dependent):
                self.graph.remove_edge(prior, dependent)
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
        self.checkpoint_request_id = checkpoint.get("request_id")


def _validate_protocol_shape(model):
    collections = {"parties", "messages", "channels", "steps", "step_dependencies", "unresolved"}
    optional = {"states"}
    if not isinstance(model, dict):
        raise ProtocolError("protocol must be an object")
    keys = set(model)
    if not collections | {"meta"} <= keys or not keys <= collections | {"meta"} | optional:
        raise ProtocolError("protocol must contain meta and the current named object collections")
    if "states" in model:
        if not isinstance(model["states"], list) or any(
                not isinstance(row, dict) for row in model["states"]):
            raise ProtocolError("protocol states must be an array of objects")
    meta = model["meta"]
    if not isinstance(meta, dict) or set(meta) != {"protocol", "complete", "documents"}:
        raise ProtocolError("protocol meta must contain only protocol, complete, documents")
    if not isinstance(meta["protocol"], str) or not meta["protocol"]:
        raise ProtocolError("protocol meta.protocol must be a nonempty string")
    if type(meta["complete"]) is not bool or not isinstance(meta["documents"], list):
        raise ProtocolError("protocol meta requires a boolean complete and a documents array")
    for source in meta["documents"]:
        if (not isinstance(source, dict) or set(source) != {"doc_id", "rfc", "path"}
                or any(not isinstance(value, str) or not value for value in source.values())):
            raise ProtocolError("protocol documents require nonempty doc_id, rfc, path only")
    for kind in collections:
        if not isinstance(model[kind], list) or any(not isinstance(row, dict) for row in model[kind]):
            raise ProtocolError(f"protocol {kind} must be an array of objects")
        for row in model[kind]:
            if kind in {"parties", "messages", "channels", "steps"}:
                if not isinstance(row.get("name"), str) or not row["name"]:
                    raise ProtocolError(f"protocol {kind} require nonempty names")


def load_protocol(protocol_path, checkpoint_path=None, binding=None):
    path = Path(protocol_path)
    try:
        model = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolError(f"cannot read protocol model {path}: {error}") from error
    _validate_protocol_shape(model)
    parties = model["parties"]
    by_name = {party["name"]: party for party in parties}
    if len(by_name) != len(parties):
        raise ProtocolError("party names must be unique")
    for party in parties:
        aliases = party.get("alias", [])
        if not isinstance(aliases, list) or any(not isinstance(name, str) or not name for name in aliases):
            raise ProtocolError(f"party {party['name']} alias must be an array of nonempty party names")
        for name in aliases:
            if name not in by_name or name == party["name"]:
                raise ProtocolError(f"party {party['name']} has invalid alias {name!r}")
    aliases = {name: set(party.get("alias", [])) for name, party in by_name.items()}
    for name, values in list(aliases.items()):
        for other in list(values):
            aliases[other].add(name)
    for name, party in by_name.items():
        party["alias"] = sorted(aliases[name])
    meta = model["meta"]
    base = {"protocol_name": meta.get("protocol", ""), "model": model}
    catalog = build_catalog(model)
    if binding:
        catalog["binding"] = dict(binding)
    graph = ProtocolGraph(base, catalog)
    if checkpoint_path:
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ProtocolError(f"cannot read graph checkpoint: {error}") from error
        graph.apply_checkpoint(checkpoint)
    return graph


def build_catalog(model, protocol_id=None):
    """Build the name/dependency index used by the graph and proposal checks."""
    del protocol_id
    _validate_protocol_shape(model)
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
            "processing": raw.get("processing"),
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
        "protocol_name": (model.get("meta") or {}).get("protocol", ""),
        "binding": {},
        "parties": [party.get("name") for party in model.get("parties") or []],
        "party_aliases": {party["name"]: list(party.get("alias") or [])
                          for party in model.get("parties") or []},
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
    """Every mapping must name steps that exist and carry that exact message."""
    problems = []
    for row in handlers.rows:
        normalized = []
        for step in row.step_names:
            if step not in graph.steps:
                problems.append(f"handler {row.message} references unknown step {step}")
                continue
            expected = graph.steps[step].get("message_name")
            if expected != row.message:
                problems.append(
                    f"handler {row.message} references step {step} of message {expected}"
                )
                continue
            normalized.append(step)
        row.step_names = normalized
    return problems
