"""Frozen source snapshots and the scan and section indexes."""

from hashlib import sha256
from pathlib import Path

from rsc.index.references import extract_references
from rsc.index.sections import build_section_index
from rsc.ingest import IngestOptions, ingest_path
from rsc.ingest.detect import decode_bytes
from rsc.workflow.sources import build_batches, token_count, validate_batches
from rsc.workflow.store import atomic_json, read_json


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


class Sources:
    def __init__(self, root, manifest):
        self.manifest = manifest
        self.batches = {row["batch_id"]: row for row in manifest["batches"]}
        self.documents = {}
        self.blocks = {}
        self.sections = {}
        self.block_batches = {}
        self.references = {}
        self.originals = {}
        for source in manifest["documents"]:
            doc_id = source["doc_id"]
            document = read_json(root / source["path"])
            self.documents[doc_id] = document
            if file_digest(root / source["snapshot"]) != source["sha256"]:
                raise ValueError(f"source snapshot changed: {doc_id}")
            self.originals[doc_id] = decode_bytes((root / source["snapshot"]).read_bytes()).text
            sections, by_path, _ = build_section_index(document)
            by_id = {s["section_id"]: s["path"] for s in sections}
            self.references[doc_id] = [
                {**link, "target_section_id": by_id.get(link["target_section_id"])}
                for link in extract_references(document, by_path)
            ]
            for block in sorted(document["blocks"], key=lambda block: block["order"]):
                key = (doc_id, block["block_id"])
                self.blocks[key] = block
                path = tuple(block["section_path"])
                for length in range(len(path) + 1):
                    self.sections.setdefault((doc_id, path[:length]), []).append(key)
        for batch_id, batch in self.batches.items():
            for part in batch["segments"]:
                key = (batch["doc_id"], part["block_id"])
                self.block_batches.setdefault(key, []).append(batch_id)
        validate_batches(self.documents, list(self.batches.values()), manifest["batch_maximum"])

    @classmethod
    def create(cls, root, files, target, maximum, extensions):
        documents = {}
        sources = []
        for index, filename in enumerate(files):
            path = Path(filename).resolve()
            raw = path.read_bytes()
            directory = root / "sources" / str(index + 1)
            directory.mkdir(parents=True, exist_ok=True)
            snapshot = directory / path.name
            snapshot.write_bytes(raw)
            result = ingest_path(snapshot, IngestOptions(out_dir=directory / "parsed", force=True))
            (item,) = result.documents
            if item.status != "ok":
                raise ValueError(f"cannot ingest {path}: {item.error}")
            if item.doc_id in documents:
                raise ValueError(f"duplicate RFC identity: {item.doc_id}")
            document = read_json(item.document_path)
            # Ingest's spans are positions into this exact decoding of the frozen bytes.
            decoded = decode_bytes(raw).text
            for block in document["blocks"]:
                span = block["original_span"]
                if not 0 <= span["start"] <= span["end"] <= len(decoded):
                    raise ValueError(f"invalid original source span in {item.doc_id}")
            documents[item.doc_id] = document
            metadata = document["meta"]
            sources.append(
                {
                    "doc_id": item.doc_id,
                    "rfc": metadata["rfc"],
                    "path": str(item.document_path.relative_to(root)),
                    "input": str(path),
                    "snapshot": str(snapshot.relative_to(root)),
                    "sha256": sha256(raw).hexdigest(),
                    "url": f"https://www.rfc-editor.org/info/{item.doc_id}"
                    if item.doc_id.removeprefix("rfc").isdigit()
                    else None,
                    "updates": metadata.get("updates", []),
                    "obsoletes": metadata.get("obsoletes", []),
                }
            )

        batches = []
        for unit in build_batches(documents, target, maximum):
            if batches and batches[-1]["doc_id"] == unit["doc_id"]:
                previous = batches[-1]
                shared = []
                for left, right in zip(
                    previous["section_path"], unit["section_path"], strict=False
                ):
                    if left != right:
                        break
                    shared.append(left)
                combined = {
                    **previous,
                    "section_path": shared,
                    "segments": previous["segments"] + unit["segments"],
                }
                if token_count(previous) < target and token_count(combined) <= maximum:
                    batches[-1] = combined
                    continue
            batches.append({**unit, "batch_id": f"batch-{len(batches) + 1:05d}"})
        manifest = {
            "documents": sources,
            "batches": batches,
            "enabled_extensions": extensions,
            "batch_maximum": maximum,
        }
        parsed = cls(root, manifest)
        atomic_json(root / "source-plan.json", manifest)
        return parsed

    def evidence(self, document_id, block_id, start=None, end=None):
        block = self.blocks.get((document_id, block_id))
        prefix, separator, ordinal = block_id.partition("-")
        if (
            block is None
            and prefix == "blk"
            and separator
            and ordinal.isascii()
            and ordinal.isdecimal()
        ):
            block_id = f"blk-{ordinal.lstrip('0').zfill(6)}"
            block = self.blocks.get((document_id, block_id))
        if block is None:
            raise ValueError(f"unknown source block {document_id}/{block_id}")
        span = block["original_span"]
        if start is None and end is None:
            start, end = span["start"], span["end"]
        elif start is None or end is None or not span["start"] <= start < end <= span["end"]:
            raise ValueError(f"range must be inside {document_id}/{block_id}'s original span")
        return {
            "document_id": document_id,
            "block_id": block_id,
            "section_path": block["section_path"],
            "start": start,
            "end": end,
        }

    def batch_context(self, batch_id):
        batch = self.batches[batch_id]
        return {
            "doc_id": batch["doc_id"],
            "batch_id": batch_id,
            "segments": [
                {"block": part["block_id"], "section": part["section_path"], "text": part["text"]}
                for part in batch["segments"]
            ],
        }
