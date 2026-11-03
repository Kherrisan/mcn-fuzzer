"""Small hand-annotated protocol: request/reply correlation and sequencing."""

from __future__ import annotations

from copy import deepcopy

from rsc.ingest import parse_txt
from rsc.protocol.contract import BASE

TEXT = """Network Working Group
Request for Comments: 9999

1. Interaction

The requester, also called user, connects to the responder over TCP.

A request carries CSeq and Session; the response repeats both values.

Requests increase CSeq by one except retransmissions, which reuse CSeq.

Session is reused by later requests in the same session.

The responder listens before the requester opens the control connection.
"""


def document(text=TEXT, doc_id="rfc9999"):
    import hashlib

    return parse_txt(
        text,
        source_path=doc_id + ".txt",
        input_sha256=hashlib.sha256(text.encode()).hexdigest(),
        doc_id=doc_id,
    ).document


def base(doc=None):
    del doc
    messages = []
    for name in ("request", "response"):
        messages.append(
            {
                "name": name,
                "associated_fields": [
                    {"name": "CSeq"},
                    {"name": "Session"},
                ],
            }
        )
    return {
        "parties": [
            {"name": "requester", "description": "request originator"},
            {"name": "responder", "description": "request handler"},
        ],
        "messages": messages,
        "channels": [
            {
                "name": "control",
                "description": "control connection",
                "transport": "TCP",
                "connection": "connection_oriented",
                "endpoint_party_names": ["requester", "responder"],
                "initiator_party_name": "requester",
                "acceptor_party_name": "responder",
                "endpoint_sources": [],
            }
        ],
        "unresolved": [],
    }


def interactions(inventory, doc=None):
    del doc, inventory
    steps = [
        {
            "id": "send-" + name,
            "name": "send " + name,
            "message_name": name,
            "channel_name": "control",
            "sender_name": sender,
            "receiver_name": receiver,
            "condition": "",
        }
        for name, sender, receiver in [
            ("request", "requester", "responder"),
            ("response", "responder", "requester"),
        ]
    ]

    def relation(prior, dependent):
        return {"prior_field": prior, "dependent_field": dependent}

    def edge(prior, dependent, condition, relations):
        return {
            "prior_step": "send " + prior,
            "dependent_step": "send " + dependent,
            "condition": condition,
            "field_relations": relations,
        }

    return {
        "steps": steps,
        "step_dependencies": [
            edge(
                "request",
                "response",
                "reply to request",
                [relation("CSeq", "CSeq"), relation("Session", "Session")],
            ),
            edge(
                "request",
                "request",
                "new request, excluding retransmissions",
                [relation("CSeq", "CSeq")],
            ),
            edge("request", "request", "retransmission", [relation("CSeq", "CSeq")]),
            edge("request", "request", "same session", [relation("Session", "Session")]),
        ],
        "unresolved": [],
    }


def object_response(data):
    """Copy name-only object definitions into an Agent response."""

    result = {
        key: deepcopy(data.get(key, [])) for key in BASE
    }
    result["unresolved"] = [
        {"description": item["description"], "object_names": list(item.get("object_names") or [])}
        for item in data.get("unresolved", [])
    ]
    return result


def interaction_response(data):
    """Copy steps and dependencies. Step names stay; step IDs are not model output."""

    result = {
        "steps": [],
        "step_dependencies": deepcopy(data.get("step_dependencies", [])),
    }
    for row in data.get("steps", []):
        copied = deepcopy(row)
        copied.pop("id", None)
        result["steps"].append(copied)
    result["unresolved"] = [
        {"description": item["description"], "object_names": list(item.get("object_names") or [])}
        for item in data.get("unresolved", [])
    ]
    return result
