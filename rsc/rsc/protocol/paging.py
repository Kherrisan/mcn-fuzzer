"""Bounded, deterministic pagination tied to a particular query and visible view."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy


def paginate(rows: list[dict], top_k: int, cursor: str | None, scope: object) -> dict:
    """Return a page; reject cursors from another query or changed visible content."""

    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    signature = hashlib.sha256(
        json.dumps(scope, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    offset = 0
    if cursor is not None:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            offset = decoded["offset"]
            if (
                decoded["scope"] != signature
                or type(offset) is not int
                or not 0 <= offset <= len(rows)
            ):
                raise ValueError("cursor scope or offset does not match")
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("invalid or stale pagination cursor") from exc
    end = min(len(rows), offset + top_k)
    next_cursor = None
    if end < len(rows):
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"offset": end, "scope": signature}, separators=(",", ":")).encode()
        ).decode()
    return {
        "status": "ok" if rows[offset:end] else "empty",
        "items": deepcopy(rows[offset:end]),
        "next_cursor": next_cursor,
        "omitted": len(rows) - end,
    }
