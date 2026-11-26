"""Frozen source snapshots, the global block table and the scan and section indexes."""

from hashlib import sha256
from pathlib import Path

from rsc.index.references import extract_references
from rsc.index.sections import build_section_index
from rsc.ingest import IngestOptions, ingest_path
from rsc.ingest.detect import decode_bytes
from rsc.workflow.sources import build_batches, token_count, validate_batches
from rsc.workflow.store import atomic_json, read_json


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
            frozen = (root / source["snapshot"]).read_bytes()
            if sha256(frozen).hexdigest() != source["sha256"]:
                raise ValueError(f"source snapshot changed: {doc_id}")
            self.originals[doc_id] = decode_bytes(frozen).text
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
        # One integer per block for the whole run: documents in document_id order,
        # and inside a document the block order. The same rule rebuilds the table for
        # an old checkpoint, so a saved block_id maps to the number it always had.
        self.block_ids = [
            (doc_id, block["block_id"])
            for doc_id in sorted(self.documents)
            for block in sorted(self.documents[doc_id]["blocks"], key=lambda item: item["order"])
        ]
        self.block_numbers = {key: number for number, key in enumerate(self.block_ids)}
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

    def block_key(self, document_id, block_id):
        """Canonical (document_id, block_id) key; short block numbers are accepted."""
        key = (document_id, block_id)
        if key in self.blocks:
            return key
        if not isinstance(block_id, str):
            return None
        prefix, separator, ordinal = block_id.partition("-")
        if prefix == "blk" and separator and ordinal.isascii() and ordinal.isdecimal():
            key = (document_id, f"blk-{ordinal.lstrip('0').zfill(6)}")
            if key in self.blocks:
                return key
        return None

    def block_number(self, document_id, block_id):
        """Global integer ID of one block, or None when it is not part of this run."""
        key = self.block_key(document_id, block_id)
        return None if key is None else self.block_numbers.get(key)

    def block_locator(self, number):
        """Locator of one globally numbered block; an out-of-range ID is an error."""
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"unknown source block {number}")
        if not 0 <= number < len(self.block_ids):
            raise ValueError(f"unknown source block {number}")
        return self.evidence(*self.block_ids[number])

    def block_table(self):
        """The checkpoint's `blocks` array: one (document_id, block_id) pair per block."""
        return [[document_id, block_id] for document_id, block_id in self.block_ids]

    def use_block_table(self, entries):
        """Adopt a checkpoint's numbering; a table with the same blocks keeps its order."""
        table = [(entry[0], entry[1]) for entry in entries]
        if table == self.block_ids:
            return
        if sorted(table) == sorted(self.block_ids):
            self.block_ids = table
            self.block_numbers = {key: number for number, key in enumerate(table)}

    def evidence(self, document_id, block_id, start=None, end=None):
        key = self.block_key(document_id, block_id)
        block = self.blocks.get(key) if key is not None else None
        if block is None:
            raise ValueError(f"unknown source block {document_id}/{block_id}")
        block_id = key[1]
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
                {
                    "id": self.block_numbers[(batch["doc_id"], part["block_id"])],
                    "block": part["block_id"],
                    "section": part["section_path"],
                    "text": part["text"],
                }
                for part in batch["segments"]
            ],
        }
