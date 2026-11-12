"""Proposal v2 construction, validation, and atomic publication."""

import json
import tempfile
from pathlib import Path

from coverage_analysis.mutations import MutationError, validate_mutations
from coverage_analysis.protocol import ProtocolError

ORIGINS = {"coverage", "autonomous"}
TRACE_FIELDS = {
    "name", "step", "action", "condition", "message",
    "channel", "sender", "receiver", "party", "reason", "constraints",
    "mutable_fields",
}


class ProposalError(ValueError):
    pass


def _as_list(value, label):
    if not isinstance(value, list):
        raise ProposalError(f"{label} must be an array")
    return value


def _instances(trace):
    names = []
    for item in trace:
        if not isinstance(item, dict):
            raise ProposalError("every trace item must be an object")
        name = str(item.get("name") or "")
        step = str(item.get("step") or "")
        if not name or not step:
            raise ProposalError("every trace item needs name and step")
        if any(char in name for char in ":[]"):
            raise ProposalError(f"instance name {name} must not contain :[]")
        if name in names:
            raise ProposalError(f"duplicate instance name {name}")
        names.append(name)
    return names


def _known_names(graph):
    model = graph.base.get("model") or {}
    parties = {party.get("name") for party in model.get("parties") or [] if party.get("name")}
    parties.update(name for name in graph.catalog.get("parties") or [] if name)
    channels = {channel.get("name") for channel in model.get("channels") or [] if channel.get("name")}
    return parties, channels, {channel.get("name"): channel for channel in model.get("channels") or []}


def _check_maps(graph, parties, channels):
    if not isinstance(parties, dict) or not isinstance(channels, dict):
        raise ProposalError("parties and channels must be objects")
    known_parties, known_channels, _channels = _known_names(graph)
    for instance, model_party in parties.items():
        if not isinstance(model_party, str) or model_party not in known_parties:
            raise ProposalError(f"party instance {instance} does not name a model party")
    for instance, channel in channels.items():
        if not isinstance(channel, dict):
            raise ProposalError(f"channel instance {instance} must be an object")
        if channel.get("channel") not in known_channels:
            raise ProposalError(f"channel instance {instance} does not name a model channel")
        for endpoint in channel.get("endpoints") or []:
            if endpoint not in parties:
                raise ProposalError(f"channel {instance} endpoint {endpoint} is not a party instance")


def _carried(graph, model_step):
    binding = graph.catalog.get("binding") or {}
    if not binding:
        return
    _parties, _channels, channels = _known_names(graph)
    names = [
        model_step.get("sender_name"),
        model_step.get("receiver_name"),
        model_step.get("party_name"),
    ]
    channel = channels.get(model_step.get("channel_name")) or {}
    names.extend(channel.get("endpoint_party_names") or [])
    names.append(channel.get("initiator_party_name"))
    names.append(channel.get("acceptor_party_name"))
    for name in names:
        if name and binding.get(name) not in {"sut", "generator"}:
            raise ProposalError(f"step {model_step.get('name')} cannot be carried by this campaign")


def _check_binding(graph, parties, channels, item):
    if "handler_ids" in item:
        step_name = graph.resolve(item["step"])
        action = graph.step(step_name).get("action") or "send"
        if action != "send":
            raise ProposalError(f"network action {item.get('name')} cannot carry handler ids")
        raise ProposalError("trace item has unsupported fields: handler_ids")
    extra = set(item) - TRACE_FIELDS
    if extra:
        raise ProposalError(f"trace item has unsupported fields: {', '.join(sorted(extra))}")
    step_name = graph.resolve(item["step"])
    model = graph.step(step_name)
    _carried(graph, model)
    channel_instance = str(item.get("channel") or "")
    channel = (channels or {}).get(channel_instance)
    if not isinstance(channel, dict):
        raise ProposalError(f"unknown channel instance {channel_instance}")
    if channel.get("channel") != model.get("channel_name"):
        raise ProposalError(f"instance {item['name']} channel does not match the model step")
    endpoints = set(channel.get("endpoints") or [])
    action = model.get("action") or "send"

    def party_model(instance):
        if instance not in endpoints:
            raise ProposalError(f"party instance {instance} is not an endpoint of {channel_instance}")
        model_party = (parties or {}).get(instance)
        if not model_party:
            raise ProposalError(f"unknown party instance {instance}")
        return model_party

    if action == "send":
        sender = party_model(item.get("sender"))
        receiver = party_model(item.get("receiver"))
        if sender != model.get("sender_name") or receiver != model.get("receiver_name"):
            raise ProposalError(f"instance {item['name']} endpoints do not match the model step")
        if item.get("party"):
            raise ProposalError(f"send instance {item['name']} cannot name a party")
    else:
        actor = party_model(item.get("party"))
        if actor != model.get("party_name"):
            raise ProposalError(f"instance {item['name']} party does not match the model step")
        if item.get("sender") or item.get("receiver"):
            raise ProposalError(f"network instance {item['name']} cannot name a sender or receiver")
    if item.get("action") not in (None, "", model.get("action") or "send"):
        raise ProposalError(f"instance {item['name']} action does not match the model step")
    if "condition" in item and item.get("condition") not in (None, model.get("condition") or ""):
        raise ProposalError(f"instance {item['name']} condition does not match the model step")
    if "message" in item and item.get("message") != model.get("message_name"):
        raise ProposalError(f"instance {item['name']} message does not match the model step")
    return step_name


def _mutable_fields(values):
    if not isinstance(values, list):
        raise ProposalError("mutable_fields must be an array")
    out = []
    seen = set()
    for item in values:
        if not isinstance(item, dict):
            raise ProposalError("mutable_fields entries must be objects")
        field = str(item.get("field") or "").strip()
        if not field or field in seen:
            raise ProposalError(f"mutable field {field!r} must be non-empty and unique")
        seen.add(field)
        out.append({
            "field": field,
            "kind": str(item.get("kind") or "string"),
            "default": item.get("default"),
            "source": str(item.get("source") or "proposal"),
        })
    return out


def _dependencies(values, names):
    if not isinstance(values, list):
        raise ProposalError("dependencies must be an array")
    out = []
    known = set(names)
    positions = {name: index for index, name in enumerate(names)}
    bindings = set()
    for item in values:
        if not isinstance(item, dict):
            raise ProposalError("dependency entries must be objects")
        prior = str(item.get("prior") or "")
        dependent = str(item.get("dependent") or "")
        if prior not in known or dependent not in known:
            raise ProposalError(f"dependency {prior} -> {dependent} references an unknown instance")
        if prior == dependent:
            raise ProposalError("dependency cannot point an instance at itself")
        if positions[prior] >= positions[dependent]:
            raise ProposalError(f"dependency {prior} -> {dependent} is not in trace order")
        kind = str(item.get("kind") or "order")
        if kind not in {"order", "data", "resource", "state"}:
            raise ProposalError(f"unknown dependency kind {kind}")
        if "field_relations" in item:
            raise ProposalError("dependency uses fields as an array of state key strings")
        fields = item.get("fields", [])
        if not isinstance(fields, list) or any(not isinstance(field, str) or not field.strip() for field in fields):
            raise ProposalError("dependency fields must be non-empty state key strings")
        if kind == "data" and not fields:
            raise ProposalError("data dependency requires at least one state key")
        for field in fields:
            binding = (dependent, field)
            if binding in bindings:
                raise ProposalError(f"step {dependent} state key {field} is bound more than once")
            bindings.add(binding)
        out.append({
            "prior": prior,
            "dependent": dependent,
            "condition": str(item.get("condition") or ""),
            "kind": kind,
            "fields": fields,
        })
    return out


def build_proposal(graph, origin, session, build, snapshot,
                   goal, trace, handlers, parties, channels, mutations=None,
                   dependencies=None):
    if origin not in ORIGINS:
        raise ProposalError(f"origin must be one of {sorted(ORIGINS)}")
    trace = _as_list(trace, "trace")
    if not trace:
        raise ProposalError("trace must not be empty")
    goal = goal or {}
    goal_handlers = [str(item) for item in _as_list(goal.get("handler_ids") or [], "goal.handler_ids")]
    if origin == "coverage" and not goal_handlers:
        raise ProposalError("coverage proposals need goal.handler_ids")
    if not str(goal.get("reason") or "").strip():
        raise ProposalError("goal.reason is required")
    _instances(trace)
    _check_maps(graph, parties or {}, channels or {})
    normalized = []
    for item in trace:
        step_name = _check_binding(graph, parties, channels, item)
        model = graph.step(step_name)
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ProposalError(f"trace step {item['name']} needs a reason")
        if model.get("action") != "send" and goal_handlers:
            pass
        normalized.append({
            "name": item["name"],
            "step": step_name,
            "action": model.get("action") or "send",
            "condition": model.get("condition") or "",
            "message": model.get("message_name"),
            "channel": item.get("channel"),
            "sender": item.get("sender"),
            "receiver": item.get("receiver"),
            "party": item.get("party"),
            "reason": reason,
            "constraints": [str(value) for value in _as_list(item.get("constraints") or [], "constraints")],
            "mutable_fields": _mutable_fields(item.get("mutable_fields") or []),
        })
    try:
        validate_mutations(normalized, mutations or [], graph.steps)
    except MutationError as error:
        raise ProposalError(str(error)) from error
    known_handlers = set(handlers.by_id) if handlers else set()
    for handler in goal_handlers:
        if handlers is not None and handler not in known_handlers:
            raise ProposalError(f"goal references unknown handler {handler}")
    if dependencies is None:
        first = {}
        for item in normalized:
            first.setdefault(item["step"], item["name"])
        dependencies = []
        for dep in graph.induced_dependencies([item["step"] for item in normalized]):
            prior = first.get(dep["prior"])
            dependent = first.get(dep["dependent"])
            if not prior or not dependent:
                continue
            relations = dep.get("field_relations") or []
            dependencies.append({
                "prior": prior,
                "dependent": dependent,
                "condition": dep.get("condition") or "",
                "kind": "data" if relations else "order",
                "fields": list(dict.fromkeys(str(rel.get("prior_field") or "") for rel in relations)),
            })
    dependencies = _dependencies(dependencies or [], _instances(trace))
    return {
        "origin": origin,
        "session": session,
        "build": build,
        "snapshot": snapshot,
        "goal": {"handler_ids": goal_handlers, "reason": str(goal["reason"])},
        "parties": parties or {},
        "channels": channels or {},
        "trace": normalized,
        "mutations": list(mutations or []),
        "dependencies": dependencies,
    }


def validate_proposal(proposal, graph, handlers, request=None):
    if not isinstance(proposal, dict):
        raise ProposalError("proposal must be an object")
    allowed = {"origin", "session", "build", "snapshot", "goal", "parties",
               "channels", "trace", "mutations", "dependencies"}
    unknown = sorted(set(proposal) - allowed)
    if unknown:
        raise ProposalError(f"unknown proposal fields: {', '.join(unknown)}")
    if proposal.get("origin") not in ORIGINS:
        raise ProposalError("proposal origin is invalid")
    for field in ("session", "build", "snapshot"):
        if not str(proposal.get(field) or "").strip() and proposal.get(field) != "":
            # build and snapshot may be empty for autonomous drafts before a campaign request.
            if field in {"build", "snapshot"} and proposal.get("origin") == "autonomous":
                continue
            if not str(proposal.get(field) or "").strip():
                raise ProposalError(f"proposal.{field} is required")
    if request is not None:
        for field in ("session", "build", "snapshot"):
            if field in {"build", "snapshot"} and request.get(field) in (None, "") and proposal.get("origin") == "autonomous":
                continue
            if str(proposal.get(field)) != str(request.get(field)):
                raise ProposalError(f"proposal.{field} does not match the registered request")
    goal = proposal.get("goal") or {}
    if not str(goal.get("reason") or "").strip():
        raise ProposalError("goal.reason is required")
    if proposal["origin"] == "coverage" and not goal.get("handler_ids"):
        raise ProposalError("coverage proposals need goal.handler_ids")
    trace = _as_list(proposal.get("trace") or [], "trace")
    if not trace:
        raise ProposalError("proposal trace must not be empty")
    _instances(trace)
    _check_maps(graph, proposal.get("parties") or {}, proposal.get("channels") or {})
    for item in trace:
        _check_binding(graph, proposal.get("parties") or {}, proposal.get("channels") or {}, item)
        if not str(item.get("reason") or "").strip():
            raise ProposalError(f"trace step {item.get('name')} needs a reason")
    try:
        validate_mutations(trace, proposal.get("mutations") or [], graph.steps)
    except MutationError as error:
        raise ProposalError(str(error)) from error
    names = _instances(trace)
    for item in trace:
        _mutable_fields(item.get("mutable_fields") or [])
    _dependencies(proposal.get("dependencies") or [], names)
    known_handlers = set(handlers.by_id) if handlers else set()
    for handler in goal.get("handler_ids") or []:
        if handlers is not None and handler not in known_handlers:
            raise ProposalError(f"goal references unknown handler {handler}")
    return proposal


def freeze(path, proposal):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(proposal, indent=2, sort_keys=False) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False) as handle:
        handle.write(body)
        temporary = Path(handle.name)
    temporary.replace(target)
    return target


def read_proposal(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProposalError(f"cannot read proposal {path}: {error}") from error
