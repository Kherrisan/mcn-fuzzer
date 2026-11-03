"""Phase-scoped keyword annotations with private writers and immutable barriers."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from .context import Corpus, document_digest
from .models import Keyword
from .paging import paginate
from .planning import FocusBlock


def normalize_keyword(text: str) -> str:
    """Normalize case and whitespace while retaining protocol punctuation."""

    return " ".join(text.split()).casefold()


class KeywordIndex:
    def __init__(
        self, protocol: str, corpus: Corpus, *, list_id: str | None = None, phase: str = "objects"
    ) -> None:
        self.protocol, self.corpus, self.list_id, self.phase = protocol, corpus, list_id, phase
        self._records: dict[tuple[str, str, str], dict] = {}
        self._owners: dict[tuple[str, str, str], str] = {}
        self._postings: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        self._frozen = False

    def commit(
        self,
        focus: FocusBlock,
        response: dict | None = None,
        *,
        error: str | None = None,
        phase: str | None = None,
    ) -> dict:
        """Commit one block response in the active phase, including empty results."""

        phase = phase or self.phase
        if phase not in {"objects", "interactions"}:
            raise ValueError("unknown keyword phase")
        if self._frozen:
            raise ValueError("keyword index is frozen")
        if self.list_id is not None and focus.list_id != self.list_id:
            raise ValueError("cannot index another extraction list")
        if focus.key not in self.corpus.blocks:
            raise KeyError(focus.key)
        if (response is None) == (error is None):
            raise ValueError("provide exactly one keyword response or failure")
        terms = {}
        if response is not None:
            for value in response.get("keywords", []):
                term = Keyword.model_validate(value)
                name = normalize_keyword(term.text)
                if not name:
                    raise ValueError("keyword text must not be blank")
                terms.setdefault(name, {"key": name, "text": " ".join(term.text.split())})
        record = {
            **focus.ref(),
            "phase": phase,
            "sequence": focus.sequence,
            "status": "failed" if error is not None else "ok" if terms else "empty",
            "keywords": [terms[name] for name in sorted(terms)],
            "error": error,
        }
        key = (phase, *focus.key)
        previous = self._records.get(key)
        if previous is not None and previous != record:
            raise ValueError("keyword block record already committed")
        self._records[key] = deepcopy(record)
        self._owners[key] = focus.list_id
        for name in terms:
            self._postings[name].add(key)
        return deepcopy(record)

    def record(self, doc_id: str, block_id: str, phase: str | None = None) -> dict | None:
        return deepcopy(self._records.get((phase or self.phase, doc_id, block_id)))

    def fork(self, *, list_id: str, phase: str = "interactions") -> KeywordIndex:
        """Seed a private mutable view from the previous phase's public barrier."""

        result = KeywordIndex(self.protocol, self.corpus, list_id=list_id, phase=phase)
        result._records, result._owners = deepcopy(self._records), deepcopy(self._owners)
        result._postings = deepcopy(self._postings)
        return result

    def search(
        self,
        keywords: list[str],
        *,
        before_sequence: int | None = None,
        list_id: str | None = None,
        top_k: int = 8,
        cursor: str | None = None,
    ) -> dict:
        """Rank exact keyword hits; prior-phase public entries bypass private filters."""

        keys = sorted({normalize_keyword(value) for value in keywords if normalize_keyword(value)})
        hits = defaultdict(set)
        sequences = {}
        for name in keys:
            for key in self._postings.get(name, set()):
                phase, doc_id, block_id = key
                record = self._records[key]
                if phase == self.phase:
                    if before_sequence is not None and record["sequence"] >= before_sequence:
                        continue
                    if list_id is not None and self._owners[key] != list_id:
                        continue
                block_key = (doc_id, block_id)
                hits[block_key].add(name)
                sequences[block_key] = record["sequence"]
        rows = [
            {
                "doc_id": doc_id,
                "block_id": block_id,
                "sequence": sequences[doc_id, block_id],
                "matched_keywords": sorted(matched),
            }
            for (doc_id, block_id), matched in hits.items()
        ]
        rows.sort(
            key=lambda row: (
                -len(row["matched_keywords"]),
                row["sequence"],
                row["doc_id"],
                row["block_id"],
            )
        )
        return paginate(rows, top_k, cursor, [keys, before_sequence, list_id, rows])

    def snapshot(self) -> dict:
        sequence = {
            (row["doc_id"], row["block_id"]): row["sequence"] for row in self._records.values()
        }
        return {
            "meta": {
                "protocol": self.protocol,
                "normalization_version": "phase-casefold-v2",
                "document_sha256_by_doc_id": {
                    key: document_digest(doc) for key, doc in sorted(self.corpus.documents.items())
                },
            },
            "records": deepcopy(
                sorted(
                    self._records.values(),
                    key=lambda row: (row["phase"], row["sequence"], row["doc_id"], row["block_id"]),
                )
            ),
            "postings": [
                {
                    "key": key,
                    "blocks": [
                        {"doc_id": doc_id, "block_id": block_id}
                        for doc_id, block_id in sorted(
                            {(doc_id, block_id) for _, doc_id, block_id in blocks},
                            key=lambda pair: (sequence[pair], *pair),
                        )
                    ],
                }
                for key, blocks in sorted(self._postings.items())
            ],
        }

    @classmethod
    def merge(cls, indexes: list[KeywordIndex]) -> KeywordIndex:
        """Freeze a barrier, allowing identical inherited public entries across lists."""

        if not indexes:
            raise ValueError("cannot merge an empty set of keyword indexes")
        first = indexes[0]
        result = cls(first.protocol, first.corpus, phase=first.phase)
        identity = first.snapshot()["meta"]
        for index in indexes:
            if index.snapshot()["meta"] != identity:
                raise ValueError("keyword indexes belong to different compilations")
            for key, record in index._records.items():
                if key in result._records and (
                    result._records[key] != record or result._owners[key] != index._owners[key]
                ):
                    raise ValueError("source block belongs to multiple keyword lists")
                result._records[key] = deepcopy(record)
                result._owners[key] = index._owners[key]
            for key, blocks in index._postings.items():
                result._postings[key].update(blocks)
        result._frozen = True
        return result
