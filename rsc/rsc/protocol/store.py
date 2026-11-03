"""Immutable per-block extraction records, independent of prompt construction."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from .contract import COLLECTIONS
from .planning import FocusBlock


class ExtractionStore:
    """Immutable block commits; consumers receive newly rendered snapshots."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], dict] = {}

    def commit(self, record: dict) -> None:
        stage = record["stage"]
        if stage not in {"objects", "interactions"}:
            raise ValueError("unknown extraction record stage")
        if record["status"] not in {"ok", "empty", "partial", "failed"}:
            raise ValueError("unknown extraction record status")
        key = stage, record["doc_id"], record["block_id"]
        if key in self._records and self._records[key] != record:
            raise ValueError("block extraction already committed")
        self._records[key] = deepcopy(record)

    def snapshot(self) -> list[dict]:
        return deepcopy(
            sorted(
                self._records.values(),
                key=lambda row: (
                    row["sequence"],
                    row["stage"],
                    row["doc_id"],
                    row["block_id"],
                ),
            )
        )

    def fork(self) -> ExtractionStore:
        result = ExtractionStore()
        result._records = deepcopy(self._records)
        return result

    def for_block(
        self,
        doc_id: str,
        block_id: str,
        lookup: Callable[[str], dict | None] | None = None,
    ) -> dict | None:
        """Render immutable barrier history for link and Issue completion."""

        result = {}
        for stage in ("objects", "interactions"):
            record = self._records.get((stage, doc_id, block_id))
            if record is None:
                continue
            rendered = deepcopy(record)
            if lookup is not None:
                for kind in COLLECTIONS:
                    rows = []
                    for row in rendered["items"].get(kind, []):
                        token = row.get("name") or (row.get("id") if kind == "steps" else None)
                        current = lookup(token) if token else None
                        shown = deepcopy(current if current is not None else row)
                        if kind != "steps":
                            shown.pop("id", None)
                        rows.append(shown)
                    rendered["items"][kind] = rows
                labels = list(rendered.get("reused_names") or [])
                rendered.pop("reused_object_ids", None)
                names = sorted(
                    {
                        row.get("name")
                        for identity in labels
                        if identity and (row := lookup(identity)) is not None and row.get("name")
                    }
                )
                if names:
                    rendered["reused_names"] = names
                else:
                    rendered.pop("reused_names", None)
            result[stage] = rendered
        return result or None

    def visible(
        self,
        doc_id: str,
        block_id: str,
        focus: FocusBlock,
        stage: str,
        lookup: Callable[[str], dict | None] | None = None,
    ) -> dict | None:
        result = {}
        for record_stage in ("objects",) if stage == "objects" else ("objects", "interactions"):
            record = self._records.get((record_stage, doc_id, block_id))
            if record is None:
                continue
            # The object barrier is public during interactions. Same-stage commits
            # remain private to the list and must precede this focus.
            public_objects = stage == "interactions" and record_stage == "objects"
            if not public_objects:
                if record["sequence"] >= focus.sequence:
                    continue
                if record_stage == stage and record["list_id"] != focus.list_id:
                    continue
            rendered = deepcopy(record)
            if lookup is not None:
                # Refresh emitted definitions. Non-step rows do not carry a definition ID.
                for kind in COLLECTIONS:
                    rows = []
                    for row in record["items"].get(kind, []):
                        token = row.get("name") or (row.get("id") if kind == "steps" else None)
                        current = lookup(token) if token else None
                        if current is not None:
                            shown = deepcopy(current)
                        elif kind != "steps" or not row.get("id"):
                            shown = deepcopy(row)
                        else:
                            continue
                        if kind != "steps":
                            shown.pop("id", None)
                        rows.append(shown)
                    rendered["items"][kind] = rows
                labels = list(record.get("reused_names") or [])
                rendered.pop("reused_object_ids", None)
                names = sorted(
                    {
                        row.get("name")
                        for identity in labels
                        if identity and (row := lookup(identity)) is not None and row.get("name")
                    }
                )
                if names:
                    rendered["reused_names"] = names
                else:
                    rendered.pop("reused_names", None)
            result[record_stage] = rendered
        return result or None
