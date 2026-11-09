"""Inbound message to handler mapping produced during coverage initialization.

A unique symbol is the handler name. A repeated symbol is
``relative/path.c::symbol``. Digest ids are not assigned.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


class HandlerError(ValueError):
    pass


def handler_name(symbol, location, colliding):
    file = location.rsplit(":", 1)[0] if location and ":" in location else (location or "")
    if not colliding:
        return symbol
    return f"{file}::{symbol}"


@dataclass
class Handler:
    id: str
    symbol: str
    location: str
    evidence: str
    uncertain: bool

    def view(self):
        return {"id": self.id, "symbol": self.symbol, "location": self.location,
                "evidence": self.evidence, "uncertain": self.uncertain}


@dataclass
class HandlerRow:
    message: str
    handlers: list
    step_names: list
    reason: str | None

    def view(self):
        return {"message": self.message,
                "handlers": [handler.view() for handler in self.handlers],
                "step_names": list(self.step_names),
                "reason": self.reason}


@dataclass
class Handlers:
    rows: list
    by_message: dict = field(default_factory=dict)
    by_id: dict = field(default_factory=dict)

    def reconcile(self):
        self.by_message = {}
        self.by_id = {}
        for row in self.rows:
            self.by_message.setdefault(row.message, []).extend(row.handlers)
            for handler in row.handlers:
                self.by_id[handler.id] = handler

    def for_message(self, message):
        return self.by_message.get(message, [])

    def ids_for_steps(self, step_names, graph):
        """Handler names associated with steps through their message mapping."""
        result = {}
        for step_name in step_names:
            message = graph.view(step_name)["message"]
            result[step_name] = [handler.id for handler in self.for_message(message)]
        return result


def load_handlers(path):
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise HandlerError(f"cannot read handlers {path}: {error}") from error
    if document.get("version") != 2:
        raise HandlerError("handlers must have version 2")
    pending = []
    for entry in document.get("mappings") or []:
        if "step_ids" in entry:
            raise HandlerError("handler mapping uses removed step_ids; use step_names")
        message = entry.get("message")
        if not message:
            raise HandlerError("handler mapping has no message")
        raw_handlers = []
        for raw in entry.get("handlers") or []:
            symbol = raw.get("symbol")
            if not symbol:
                raise HandlerError(f"handler for {message} has no symbol")
            raw_handlers.append(raw)
        pending.append((entry, raw_handlers))
    counts = Counter(raw.get("symbol") for _entry, handlers in pending for raw in handlers)
    seen = set()
    rows = []
    for entry, raw_handlers in pending:
        handlers = []
        for raw in raw_handlers:
            symbol = str(raw.get("symbol"))
            location = str(raw.get("location") or "")
            ident = handler_name(symbol, location, counts[symbol] > 1)
            if ident in seen:
                raise HandlerError(f"duplicate handler name {ident}")
            seen.add(ident)
            handlers.append(Handler(ident, symbol, location, str(raw.get("evidence") or ""),
                                    bool(raw.get("uncertain"))))
        rows.append(HandlerRow(entry.get("message"), handlers,
                               list(entry.get("step_names") or []),
                               entry.get("reason")))
    loaded = Handlers(rows)
    loaded.reconcile()
    return loaded
