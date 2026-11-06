"""Inbound message to handler mapping produced during coverage initialization."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from coverage_analysis import catalog as ids


class HandlerError(ValueError):
    pass


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
    step_ids: list
    step_names: list
    reason: str | None

    def view(self):
        return {"message": self.message,
                "handlers": [handler.view() for handler in self.handlers],
                "step_ids": list(self.step_ids),
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

    def ids_for_steps(self, step_ids, graph):
        """Handler IDs associated with steps through their message mapping."""
        result = {}
        for step_id in step_ids:
            message = graph.view(step_id)["message"]
            result[step_id] = [handler.id for handler in self.for_message(message)]
        return result


def _handler_id(symbol, location):
    file = location.rsplit(":", 1)[0] if location and ":" in location else location
    return ids.handler_id(symbol, file or None)


def load_handlers(path):
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise HandlerError(f"cannot read handlers {path}: {error}") from error
    if document.get("version") != 1:
        raise HandlerError("handlers must have version 1")
    rows = []
    for entry in document.get("mappings") or []:
        message = entry.get("message")
        if not message:
            raise HandlerError("handler mapping has no message")
        handlers = []
        for raw in entry.get("handlers") or []:
            symbol = raw.get("symbol")
            if not symbol:
                raise HandlerError(f"handler for {message} has no symbol")
            location = str(raw.get("location") or "")
            handlers.append(Handler(_handler_id(symbol, location), str(symbol),
                                    location, str(raw.get("evidence") or ""),
                                    bool(raw.get("uncertain"))))
        rows.append(HandlerRow(message, handlers,
                               list(entry.get("step_ids") or []),
                               list(entry.get("step_names") or []),
                               entry.get("reason")))
    loaded = Handlers(rows)
    loaded.reconcile()
    return loaded
