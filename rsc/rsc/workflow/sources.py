"""Immutable RFC snapshots and bounded chapter batches."""

from __future__ import annotations

import json
from itertools import groupby
from pathlib import Path

import tiktoken

from rsc.ingest import IngestOptions, ingest_path


def render(batch: dict) -> str:
    # Match the text delivered by the MCP bridge, including JSON whitespace.
    return json.dumps(batch, ensure_ascii=False)


def token_count(batch: dict) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(render(batch), disallowed_special=()))


def build_batches(documents: dict, target: int = 8000, maximum: int = 12000) -> list[dict]:
    if not 128 <= target <= maximum:
        raise ValueError("batch tokens must satisfy 128 <= target <= maximum")
    batches = []

    for doc_id, document in documents.items():

        def make_batch(segments, doc_id=doc_id):
            path = list(segments[0]["section_path"])
            for segment in segments[1:]:
                while path and segment["section_path"][: len(path)] != path:
                    path.pop()
            return {
                "batch_id": "batch-00000",
                "doc_id": doc_id,
                "section_path": path,
                "segments": segments,
            }

        def fits(segments):
            return token_count(make_batch(segments)) <= maximum

        def pack(units):
            """Combine adjacent whole units without splitting their chapter boundaries."""
            current = []
            for unit in units:
                if current and not fits(current + unit):
                    yield current
                    current = []
                current += unit
                if token_count(make_batch(current)) >= target:
                    yield current
                    current = []
            if current:
                yield current

        def split_block(segment):
            text = segment["text"]
            offset = 0
            while offset < len(text):
                lo, hi = offset, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    part = {**segment, "start": offset, "end": mid, "text": text[offset:mid]}
                    if fits([part]):
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == offset:
                    raise ValueError("batch metadata alone exceeds token maximum")
                yield [{**segment, "start": offset, "end": lo, "text": text[offset:lo]}]
                offset = lo

        def split_section(segments, depth):
            if fits(segments):
                yield segments
                return
            if any(len(s["section_path"]) > depth for s in segments):
                units = (
                    unit
                    for _, group in groupby(
                        segments, key=lambda s: tuple(s["section_path"][: depth + 1])
                    )
                    for unit in split_section(list(group), depth + 1)
                )
            else:
                units = (
                    unit
                    for segment in segments
                    for unit in ([[segment]] if fits([segment]) else split_block(segment))
                )
            yield from pack(units)

        blocks = sorted(document["blocks"], key=lambda b: b["order"])
        segments = [
            {
                "block_id": block["block_id"],
                "start": 0,
                "end": len(block["text"]),
                "text": block["text"],
                "section_path": block["section_path"],
            }
            for block in blocks
            if block["text"].strip()
        ]
        for _, chapter in groupby(segments, key=lambda s: tuple(s["section_path"][:1])):
            for unit in split_section(list(chapter), 1):
                batch = make_batch(unit)
                batch["batch_id"] = f"batch-{len(batches) + 1:05d}"
                batches.append(batch)
    if not batches:
        raise ValueError("no source text")
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


def prepare(root: Path, paths: list[str], target: int, maximum: int,
            *, capacity_tokens: int | None = None) -> tuple[dict, list]:
    if capacity_tokens is not None:
        maximum = min(maximum, capacity_tokens)
        target = min(target, maximum)
        if maximum < 128:
            raise ValueError("context capacity is too small for source batch metadata")
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
    text_root = root / "sources" / "text"
    text_root.mkdir(parents=True, exist_ok=True)
    for index, (doc_id, document) in enumerate(documents.items(), 1):
        # Stable locators are embedded in searchable text; no digest is needed.
        lines = [f"Document: {doc_id}"]
        for block in sorted(document["blocks"], key=lambda b: b["order"]):
            lines.extend([f"\n[{doc_id}/{block['block_id']}] {' / '.join(block['section_path'])}",
                          block["text"]])
        path = text_root / f"source-{index:03d}.txt"
        temporary = path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.chmod(0o444)
        temporary.replace(path)
    return documents, build_batches(documents, target, maximum)
