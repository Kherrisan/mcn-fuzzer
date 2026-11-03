"""On-demand, bounded source reads; session history owns delivered context."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from .context import Corpus
from .options import AgentOptions
from .planning import FocusBlock
from .projection import artifact_views
from .store import ExtractionStore

SOURCE_CHARS = 2400
ROLES = {"focus", "previous", "next", "parent", "lead_in", "reference", "keyword", "retrieved"}


def source_range(source: dict, start: int | None, end: int | None, full: bool) -> tuple[int, int]:
    """Validate half-open Unicode coordinates against the complete normalized block."""

    left = 0 if full or start is None else start
    right = (
        len(source["text"])
        if full
        else (min(len(source["text"]), left + SOURCE_CHARS) if end is None else end)
    )
    if (
        type(left) is not int
        or type(right) is not int
        or not 0 <= left <= right <= len(source["text"])
    ):
        raise ValueError("source range must use full-block Unicode coordinates")
    return left, right


class RetrievalContext:
    """Read source and visible artifacts without preloading or cumulative source quotas."""

    def __init__(
        self,
        corpus: Corpus,
        *,
        store: ExtractionStore | None = None,
        lookup: Callable[[str], dict | None] | None = None,
        focus: FocusBlock | None = None,
        stage: str | None = None,
        phase: str | None = None,
        agent_options: AgentOptions | None = None,
    ) -> None:
        self.corpus = corpus
        self.store, self.lookup = store, lookup
        self.focus = focus
        self.stage = stage or phase
        if self.stage not in {None, "objects", "interactions"}:
            raise ValueError("retrieval phase must be objects or interactions")
        self.agent_options = agent_options or AgentOptions()
        self.revision = 0
        self.omitted: list[dict] = []
        self._reads: list[dict] = []

    def add(
        self,
        doc_id: str,
        block_id: str,
        *,
        roles: list[str] | None = None,
        matched_keywords: list[str] | None = None,
        text_start: int | None = None,
        text_end: int | None = None,
        full: bool = False,
        required: bool = False,
    ) -> dict:
        """Return at most 2,400 source characters plus explicitly located continuation."""

        del required
        selected_roles = list(dict.fromkeys(roles or ["retrieved"]))
        if not set(selected_roles) <= ROLES:
            raise ValueError("unsupported source role")
        source = self.corpus.blocks[doc_id, block_id]
        start, requested_end = source_range(source, text_start, text_end, full)
        end = min(requested_end, start + SOURCE_CHARS)
        block = {
            "doc_id": doc_id,
            "block_id": block_id,
            "block_type": source["block_type"],
            "text": source["text"][start:end],
        }
        if start != 0 or end != len(source["text"]):
            block.update(
                text_start=start,
                text_end=end,
                text_length=len(source["text"]),
                next_start=end if end < len(source["text"]) else None,
            )
        artifact = None
        if self.store is not None:
            if self.focus is not None and self.stage is not None:
                artifact = self.store.visible(doc_id, block_id, self.focus, self.stage, self.lookup)
            else:
                artifact = self.store.for_block(doc_id, block_id, self.lookup)
        pair = {
            "block": block,
            "compile_result": artifact,
            "roles": selected_roles,
            "matched_keywords": sorted(set(matched_keywords or [])),
        }
        rendered = artifact_views([pair])[0]
        self._reads.append(
            {
                "doc_id": doc_id,
                "block_id": block_id,
                "text_start": start,
                "text_end": end,
                "roles": selected_roles,
            }
        )
        self.revision += 1
        return {"status": "ok", "artifact": rendered, "omitted": []}

    def snapshot(self) -> dict:
        """Describe explicit reads for diagnostics, without re-injecting their bodies."""

        return {
            "phase": self.stage,
            "focus_block": self.focus.ref() if self.focus is not None else None,
            "revision": self.revision,
            "reads": deepcopy(self._reads),
        }
