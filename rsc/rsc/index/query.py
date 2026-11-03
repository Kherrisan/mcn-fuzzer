"""Read-only document-local structural index queries."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(slots=True)
class RSCIndex:
    data: dict[str, Any]
    _blocks: dict[str, dict] = field(init=False, repr=False)
    _sections: dict[str, dict] = field(init=False, repr=False)
    _section_blocks: dict[str | None, list[str]] = field(init=False, repr=False)
    _ordered: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.data = deepcopy(self.data)
        self._blocks = {b["block_id"]: b for b in self.data["blocks"]}
        self._sections = {s["section_id"]: s for s in self.data["sections"]}
        self._ordered = [
            b["block_id"] for b in sorted(self._blocks.values(), key=lambda b: b["order"])
        ]
        self._section_blocks = defaultdict(list)
        for block_id in self._ordered:
            self._section_blocks[self._blocks[block_id]["section_id"]].append(block_id)

    @classmethod
    def load(cls, path: str | Path) -> RSCIndex:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get_block_index(self, block_id: str) -> dict:
        """Return a block index entry, raising KeyError for an unknown block."""

        return deepcopy(self._blocks[block_id])

    def get_parent_section(self, block_id: str) -> dict | None:
        """Return the block's direct section; preamble blocks have no section."""

        section_id = self._blocks[block_id]["section_id"]
        return deepcopy(self._sections[section_id]) if section_id is not None else None

    def get_section_ancestors(self, section_id: str) -> list[dict]:
        """Return ancestors from outermost to innermost, excluding the input section."""

        result = []
        seen = {section_id}
        current = self._sections[section_id]
        while current.get("parent_section_id") is not None:
            parent = current["parent_section_id"]
            if parent in seen:
                raise ValueError("cyclic section ancestry")
            seen.add(parent)
            current = self._sections[parent]
            result.append(deepcopy(current))
        return list(reversed(result))

    def get_neighbor_blocks(
        self,
        block_id: str,
        before: int = 1,
        after: int = 1,
        scope: Literal["document", "section"] = "document",
    ) -> dict[str, list[dict]]:
        """Return ordered neighboring index entries, excluding the focus block."""

        if scope not in {"document", "section"}:
            raise ValueError("scope must be document or section")
        if any(type(value) is not int or value < 0 for value in (before, after)):
            raise ValueError("neighbor counts must be nonnegative integers")
        block = self._blocks[block_id]
        ordered = (
            self._ordered if scope == "document" else self._section_blocks[block["section_id"]]
        )
        position = ordered.index(block_id)
        return {
            "before": [
                deepcopy(self._blocks[key]) for key in ordered[max(0, position - before) : position]
            ],
            "after": [
                deepcopy(self._blocks[key]) for key in ordered[position + 1 : position + 1 + after]
            ],
        }

    def references_from(self, block_id: str) -> list[dict[str, Any]]:
        """Return references originating in a known block."""

        self._blocks[block_id]
        return deepcopy(
            [
                reference
                for reference in self.data["references"]
                if reference["from_block"] == block_id
            ]
        )

    def section_by_path(self, path: list[str]) -> dict[str, Any] | None:
        matches = [section for section in self._sections.values() if section["path"] == path]
        if len(matches) > 1:
            raise ValueError("section path is ambiguous; use a block or section ID")
        return deepcopy(matches[0]) if matches else None
