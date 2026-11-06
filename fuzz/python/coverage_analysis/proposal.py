"""Proposal v1 construction, validation, and atomic publication."""

import json
import tempfile
from pathlib import Path

from coverage_analysis import catalog as ids
from coverage_analysis.protocol import ProtocolError, context_for

VERSION = 1
ORIGINS = {"coverage", "autonomous"}


class ProposalError(ValueError):
    pass


def _as_list(value, label):
    if not isinstance(value, list):
        raise ProposalError(f"{label} must be an array")
    return value


def _step_ids(trace):
    ids_out = []
    for item in trace:
        if not isinstance(item, dict) or not item.get("step_id"):
            raise ProposalError("every trace item needs step_id")
        ids_out.append(str(item["step_id"]))
    return ids_out


def _handler_sets(graph, handlers, trace_ids):
    mapping = {}
    for step_id in trace_ids:
        message = graph.view(step_id)["message"]
        mapping[step_id] = [handler.id for handler in handlers.for_message(message)] if handlers else []
    return mapping


def build_proposal(graph, origin, session, request_id, build, snapshot,
                   goal, trace, handlers, graph_revision=None):
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
    trace_ids = [graph.resolve(step_id) for step_id in _step_ids(trace)]
    allowed = _handler_sets(graph, handlers, trace_ids)
    known_handlers = set(handlers.by_id) if handlers else set()
    for handler in goal_handlers:
        if handlers is not None and handler not in known_handlers:
            raise ProposalError(f"goal references unknown handler {handler}")
        if origin == "coverage" and handler not in {item for values in allowed.values() for item in values}:
            raise ProposalError(f"goal handler {handler} is not reachable from the trace")
    normalized = []
    step_records = []
    for item, step_id in zip(trace, trace_ids):
        handler_ids = [str(value) for value in _as_list(item.get("handler_ids") or [], "trace.handler_ids")]
        for handler in handler_ids:
            if handlers is not None and handler not in known_handlers:
                raise ProposalError(f"trace references unknown handler {handler}")
            if handlers is not None and handler not in allowed[step_id]:
                raise ProposalError(
                    f"handler {handler} does not handle step {step_id} ({graph.view(step_id)['message']})")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ProposalError(f"trace step {step_id} needs a reason")
        constraints = [str(value) for value in _as_list(item.get("constraints") or [], "constraints")]
        normalized.append({"step_id": step_id, "handler_ids": handler_ids,
                           "reason": reason, "constraints": constraints})
        step_records.append(graph.step(step_id))
    payload = {
        "version": VERSION,
        "origin": origin,
        "session": session,
        "request_id": request_id,
        "build": build,
        "snapshot": snapshot,
        "protocol_id": graph.base["protocol_id"],
        "graph_revision": int(graph.revision if graph_revision is None else graph_revision),
        "trace_id": ids.trace_id(step_records),
        "goal": {"handler_ids": goal_handlers, "reason": str(goal["reason"])},
        "trace": normalized,
        "context": context_for(graph, trace_ids, handlers),
    }
    payload["proposal_id"] = ids.proposal_id(payload)
    return payload


def validate_proposal(proposal, graph, handlers, request=None):
    if not isinstance(proposal, dict):
        raise ProposalError("proposal must be an object")
    if proposal.get("version") != VERSION:
        raise ProposalError("proposal version must be 1")
    if proposal.get("origin") not in ORIGINS:
        raise ProposalError("proposal origin is invalid")
    for field in ("session", "request_id", "build", "snapshot", "protocol_id", "proposal_id", "trace_id"):
        if not str(proposal.get(field) or "").strip():
            raise ProposalError(f"proposal.{field} is required")
    if proposal["protocol_id"] != graph.base["protocol_id"]:
        raise ProposalError("proposal protocol_id does not match the loaded model")
    if request is not None:
        for field in ("session", "request_id", "build", "snapshot"):
            if str(proposal.get(field)) != str(request.get(field)):
                raise ProposalError(f"proposal.{field} does not match the registered request")
    goal = proposal.get("goal") or {}
    if proposal["origin"] == "coverage" and not goal.get("handler_ids"):
        raise ProposalError("coverage proposals need goal.handler_ids")
    trace = _as_list(proposal.get("trace") or [], "trace")
    if not trace:
        raise ProposalError("proposal trace must not be empty")
    trace_ids = [graph.resolve(str(item.get("step_id"))) for item in trace]
    allowed = _handler_sets(graph, handlers, trace_ids)
    known_handlers = set(handlers.by_id) if handlers else set()
    for handler in goal.get("handler_ids") or []:
        if handlers is not None and handler not in known_handlers:
            raise ProposalError(f"goal references unknown handler {handler}")
    for item, step_id in zip(trace, trace_ids):
        for handler in item.get("handler_ids") or []:
            if handlers is not None and handler not in known_handlers:
                raise ProposalError(f"trace references unknown handler {handler}")
            if handlers is not None and handler not in allowed[step_id]:
                raise ProposalError(f"handler {handler} does not handle step {step_id}")
        if not str(item.get("reason") or "").strip():
            raise ProposalError(f"trace step {step_id} needs a reason")
        if not isinstance(item.get("constraints") or [], list):
            raise ProposalError("trace constraints must be an array")
    expected_trace = ids.trace_id([graph.step(step_id) for step_id in trace_ids])
    if proposal["trace_id"] != expected_trace:
        raise ProposalError("proposal.trace_id does not match the ordered trace")
    expected_id = ids.proposal_id(proposal)
    if proposal["proposal_id"] != expected_id:
        raise ProposalError("proposal.proposal_id does not match the content")
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
