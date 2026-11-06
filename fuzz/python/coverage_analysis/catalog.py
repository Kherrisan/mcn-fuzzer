"""Stable identifiers shared by the MCP service, the agent tools, and Rust.

The encodings mirror ``fuzz/src/protocol/id.rs``: length-prefixed fields for
step digests, a canonical JSON encoding for protocol and proposal content.
Every public identifier is the first eight lower-case hex characters.
"""

import hashlib
import json

ID_LEN = 8

STEP_FIELDS = (
    "name",
    "message_name",
    "channel_name",
    "sender_name",
    "receiver_name",
    "condition",
)


def _push(out, text):
    raw = text.encode()
    out += len(raw).to_bytes(8, "big")
    out += raw


def _sha8(data):
    return hashlib.sha256(data).hexdigest()[:ID_LEN]


def canonical(value, out):
    """Append the canonical encoding used by ``protocol_id``.

    Object keys are sorted, array order is preserved, and every scalar string
    is length-prefixed so concatenated values cannot collide.
    """
    if value is None:
        out.append(ord("n"))
    elif value is True:
        out.append(ord("t"))
    elif value is False:
        out.append(ord("f"))
    elif isinstance(value, (int, float)):
        _push(out, json.dumps(value, separators=(",", ":")))
    elif isinstance(value, str):
        _push(out, value)
    elif isinstance(value, list):
        out.append(ord("["))
        out += len(value).to_bytes(8, "big")
        for item in value:
            canonical(item, out)
    elif isinstance(value, dict):
        out.append(ord("{"))
        out += len(value).to_bytes(8, "big")
        for key in sorted(value):
            _push(out, key)
            canonical(value[key], out)
    else:
        raise TypeError(f"cannot canonicalize {type(value).__name__}")


def step_digest(step):
    """Full SHA-256 digest of one step definition."""
    out = bytearray()
    _push(out, "mcn.step.v1")
    for field in STEP_FIELDS:
        _push(out, str(step.get(field, "")))
    return hashlib.sha256(out).hexdigest()


def step_id(step):
    return step_digest(step)[:ID_LEN]


def trace_id(steps):
    """Ordered sequence identity; repeats matter."""
    out = bytearray()
    _push(out, "mcn.trace.v1")
    out += len(steps).to_bytes(8, "big")
    for step in steps:
        _push(out, step_digest(step))
    return hashlib.sha256(out).hexdigest()[:ID_LEN]


def protocol_id(model):
    out = bytearray()
    _push(out, "mcn.protocol.v1")
    canonical(model, out)
    return hashlib.sha256(out).hexdigest()[:ID_LEN]


def proposal_id(proposal):
    """Content identity of a proposal, excluding the ``proposal_id`` field."""
    body = {key: value for key, value in proposal.items() if key != "proposal_id"}
    out = bytearray()
    _push(out, "mcn.proposal.v1")
    canonical(body, out)
    return hashlib.sha256(out).hexdigest()[:ID_LEN]


def handler_id(symbol, file=None):
    """Symbol plus source file, so same-named static functions stay distinct."""
    out = bytearray()
    _push(out, "mcn.handler.v1")
    _push(out, symbol or "")
    _push(out, file or "")
    return hashlib.sha256(out).hexdigest()[:ID_LEN]


def variant_id(kind, *parts):
    out = bytearray()
    _push(out, kind)
    for part in parts:
        _push(out, str(part))
    return hashlib.sha256(out).hexdigest()[:ID_LEN]
