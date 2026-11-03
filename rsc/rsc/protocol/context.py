"""Batch preflight and structural queries limited to the selected RFC set."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from rsc.artifacts import read_json
from rsc.index import RSCIndex, build_structural_index
from rsc.schemas import validate_instance, validator


def document_digest(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def load_batch(root: Path, schemas_dir: Path) -> dict[str, dict]:
    batch = read_json(root / "batch.json")
    errors = validate_instance(validator("batch", schemas_dir), batch)
    if errors:
        raise ValueError("invalid batch: " + "; ".join(errors[:5]))
    if not batch["documents"]:
        raise ValueError("batch contains no documents")
    documents: dict[str, dict] = {}
    check = validator("document", schemas_dir)
    for entry in batch["documents"]:
        if entry["status"] not in {"ok", "unchanged"} or not entry.get("document"):
            raise ValueError(f"incomplete batch: {entry['doc_id']} has status {entry['status']}")
        path = (root / entry["document"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"document path escapes batch: {path}")
        document = read_json(path)
        errors = validate_instance(check, document)
        if errors:
            raise ValueError(f"{entry['doc_id']}: " + "; ".join(errors[:5]))
        meta = document["meta"]
        if meta["source_id"] != entry["doc_id"] or meta["input_sha256"] != entry["input_sha256"]:
            raise ValueError(f"batch identity/checksum mismatch: {entry['doc_id']}")
        blocks = document["blocks"]
        if not blocks or len({b["block_id"] for b in blocks}) != len(blocks):
            raise ValueError(f"empty document or duplicate block IDs: {entry['doc_id']}")
        previous = documents.get(entry["doc_id"])
        if previous and previous != document:
            raise ValueError(f"duplicate document identity: {entry['doc_id']}")
        documents[entry["doc_id"]] = document
    return dict(sorted(documents.items()))


class Corpus:
    def __init__(self, documents: dict[str, dict]):
        self.documents = documents
        self.indexes = {key: build_structural_index(doc) for key, doc in documents.items()}
        self.blocks = {
            (doc_id, block["block_id"]): block
            for doc_id, document in documents.items()
            for block in document["blocks"]
        }
        self.queries = {key: RSCIndex(index) for key, index in self.indexes.items()}
        self.block_index = {
            (doc_id, block["block_id"]): block
            for doc_id, index in self.indexes.items()
            for block in index["blocks"]
        }
        self.sections_by_id = {
            (doc_id, section["section_id"]): section
            for doc_id, index in self.indexes.items()
            for section in index["sections"]
        }
        self.section_blocks = defaultdict(list)
        for (doc_id, block_id), entry in self.block_index.items():
            self.section_blocks[doc_id, entry["section_id"]].append(block_id)
        self.search_terms = {}
        for doc_id, index in self.indexes.items():
            titles = {tuple(s["path"]): s["title"] for s in index["sections"]}
            for block in documents[doc_id]["blocks"]:
                text = titles.get(tuple(block["section_path"]), "") + " " + block["text"]
                self.search_terms[doc_id, block["block_id"]] = set(
                    re.findall(r"[\w-]{3,}", text.casefold())
                )

    def get_block_index(self, doc_id: str, block_id: str) -> dict:
        """Return the document-local index entry; unknown identities raise KeyError."""

        return self.queries[doc_id].get_block_index(block_id)

    def get_parent_section(self, doc_id: str, block_id: str) -> dict | None:
        """Return the direct parent section, or None for a preamble block."""

        return self.queries[doc_id].get_parent_section(block_id)

    def get_neighbor_blocks(
        self,
        doc_id: str,
        block_id: str,
        before: int = 1,
        after: int = 1,
        scope: str = "document",
    ) -> dict[str, list[dict]]:
        """Return source blocks in order without crossing document boundaries."""

        neighbors = self.queries[doc_id].get_neighbor_blocks(block_id, before, after, scope)
        return {
            direction: [self.block(doc_id, self.blocks[doc_id, b["block_id"]]) for b in rows]
            for direction, rows in neighbors.items()
        }

    def block(self, doc_id: str, block: dict) -> dict:
        return {"doc_id": doc_id, **block}

    def search(self, query: str, limit: int = 24, doc_ids=None) -> list[dict]:
        """Retrieve matching section titles and block text."""
        terms = set(re.findall(r"[\w-]{3,}", query.casefold()))
        scores = []
        for (doc_id, block_id), block in self.blocks.items():
            if doc_ids is not None and doc_id not in doc_ids:
                continue
            score = len(terms & self.search_terms[doc_id, block_id])
            if score:
                scores.append((-score, doc_id, block["order"], block_id))
        return [
            self.block(doc_id, self.blocks[doc_id, block_id])
            for _, doc_id, _, block_id in sorted(scores)[:limit]
        ]

    def reference_context(self, doc_id: str, blocks: list[dict]) -> tuple[list[dict], list[dict]]:
        ids = {b["block_id"] for b in blocks}
        related = {}
        missing = []
        for reference in self.indexes[doc_id]["references"]:
            if reference["from_block"] not in ids:
                continue
            target_rfc = reference.get("target_rfc")
            cross_section = None
            if reference["ref_type"] == "section":
                text = self.blocks[doc_id, reference["from_block"]]["text"]
                explicit = re.search(
                    re.escape(reference["raw"]) + r"\s+(?:of|in)\s+\[?RFC\s*(\d+)",
                    text,
                    re.IGNORECASE,
                )
                if explicit:
                    target_rfc = "RFC " + explicit.group(1)
                    section = re.search(r"\d+(?:\.\d+)*", reference["raw"])
                    if section:
                        cross_section = tuple(section.group().split("."))
            target_docs = [doc_id]
            if target_rfc:

                def normalize(s):
                    return re.sub(r"\W", "", str(s)).casefold()

                target_docs = [
                    key
                    for key, doc in self.documents.items()
                    if normalize(doc["meta"]["rfc"]) == normalize(target_rfc)
                ]
            if not target_docs or (
                reference["ref_type"] == "section"
                and not reference["resolved"]
                and cross_section is None
            ):
                missing.append({"doc_id": doc_id, **reference})
                continue
            for target in target_docs:
                target_section = reference.get("target_section_id")
                sections = self.indexes[target]["sections"]
                paths = {tuple(s["path"]) for s in sections if s["section_id"] == target_section}
                if cross_section is not None:
                    paths = {
                        tuple(s["path"]) for s in sections if tuple(s["path"]) == cross_section
                    }
                    if not paths:
                        missing.append({"doc_id": doc_id, **reference})
                        continue
                # RFC-only references use lexical retrieval, never an external lookup.
                candidates = (
                    self.documents[target]["blocks"]
                    if paths
                    else self.search(
                        " ".join(b["text"] for b in blocks), limit=16, doc_ids={target}
                    )
                )
                for block in candidates:
                    if paths and tuple(block["section_path"]) not in paths:
                        continue
                    if not paths and block["doc_id"] != target:
                        continue
                    key = (target, block["block_id"])
                    if key not in {(doc_id, value) for value in ids}:
                        related[key] = self.block(target, block)
        return list(related.values()), missing
