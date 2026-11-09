"""Immutable RFC snapshots and bounded chapter batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import tiktoken

from rsc.ingest import IngestOptions, ingest_path


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def render(batch: dict) -> str:
    # Match the text delivered by the MCP bridge, including JSON whitespace.
    return json.dumps(batch, ensure_ascii=False)


def token_count(batch: dict) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(render(batch), disallowed_special=()))


def build_batches(documents: dict, target: int = 8000, maximum: int = 12000) -> list[dict]:
    if not 128 <= target <= maximum:
        raise ValueError("batch tokens must satisfy 128 <= target <= maximum")
    batches: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current and current["segments"]:
            current["batch_id"] = f"batch-{len(batches) + 1:05d}"
            batches.append(current)
        current = None

    for doc_id, document in documents.items():
        for block in sorted(document["blocks"], key=lambda b: b["order"]):
            text = block["text"]
            if not text.strip():
                continue
            section = block["section_path"][:1]
            offset = 0
            while offset < len(text):
                if current is None:
                    current = {
                        "batch_id": "batch-00000",
                        "doc_id": doc_id,
                        "section_path": section,
                        "segments": [],
                    }
                if current["doc_id"] != doc_id or current["section_path"] != section:
                    flush()
                    continue
                segment = {
                    "block_id": block["block_id"],
                    "start": offset,
                    "end": len(text),
                    "text": text[offset:],
                    "section_path": block["section_path"],
                }
                candidate = {**current, "segments": current["segments"] + [segment]}
                if token_count(candidate) > maximum:
                    if current["segments"]:
                        flush()
                        continue
                    # Split at Unicode character boundaries, never decoded token boundaries.
                    lo, hi = offset, len(text)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        part = {**segment, "end": mid, "text": text[offset:mid]}
                        if token_count({**current, "segments": [part]}) <= maximum:
                            lo = mid
                        else:
                            hi = mid - 1
                    if lo == offset:
                        raise ValueError("batch metadata alone exceeds token maximum")
                    segment = {**segment, "end": lo, "text": text[offset:lo]}
                current["segments"].append(segment)
                offset = segment["end"]
                if token_count(current) >= target:
                    flush()
    flush()
    validate_batches(documents, batches, maximum)
    return batches


def validate_batches(documents: dict, batches: list, maximum: int) -> None:
    ranges: dict[tuple, list] = {}
    for batch in batches:
        if token_count(batch) > maximum:
            raise ValueError(f"oversized batch {batch['batch_id']}")
        document = documents[batch["doc_id"]]
        blocks = {b["block_id"]: b for b in document["blocks"]}
        for part in batch["segments"]:
            text = blocks[part["block_id"]]["text"]
            if not 0 <= part["start"] < part["end"] <= len(text):
                raise ValueError("invalid source range")
            if text[part["start"] : part["end"]] != part["text"]:
                raise ValueError("source text mismatch")
            ranges.setdefault((batch["doc_id"], part["block_id"]), []).append(
                (part["start"], part["end"])
            )
    for doc_id, document in documents.items():
        for block in document["blocks"]:
            if not block["text"].strip():
                continue
            cursor = 0
            for start, end in sorted(ranges.get((doc_id, block["block_id"]), [])):
                if start != cursor:
                    raise ValueError("source coverage gap or overlapping ranges")
                cursor = end
            if cursor != len(block["text"]):
                raise ValueError("source coverage incomplete")


def prepare(root: Path, paths: list[str], target: int, maximum: int) -> tuple[dict, list]:
    documents = {}
    for index, path in enumerate(paths):
        result = ingest_path(path, IngestOptions(out_dir=root / "sources" / str(index)))
        for item in result.documents:
            if item.status not in {"ok", "unchanged"} or not item.document_path:
                raise ValueError(f"RFC parse failed: {item.error or item.status}")
            doc = json.loads(item.document_path.read_text())
            if item.doc_id in documents:
                raise ValueError(f"duplicate RFC identity: {item.doc_id}")
            documents[item.doc_id] = doc
    if not documents:
        raise ValueError("no RFC documents")
    return documents, build_batches(documents, target, maximum)
