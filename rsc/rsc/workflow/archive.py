"""Read-only view for auditing archived single-worker workflows.

Execution is deliberately absent. Resume an old run using its archived source.
"""

import json
import sqlite3
from pathlib import Path

from rsc.protocol.context import document_digest
from rsc.protocol.contract import COLLECTIONS

from .model import reference_errors, validate_record
from .paging import PageError, records_page
from .sources import digest, validate_batches
from .store import StoreError as WorkflowError


class Archive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = sqlite3.connect((self.root / "workflow.sqlite").as_uri() + "?mode=ro", uri=True)
        row = self.db.execute("SELECT body FROM task WHERE id=1").fetchone()
        self.data = json.loads(row[0]) if row else None

    def _batch(self, batch_id):
        for batch in self.data["batches"]:
            if batch["batch_id"] == batch_id:
                return batch
        raise WorkflowError("unknown batch")

    @staticmethod
    def _page(rows, cursor=0):
        try:
            return records_page(rows, cursor, None, 0)
        except PageError as error:
            raise WorkflowError(str(error)) from error

    def _hard_errors(self):
        errors = []
        if not self.data["documents"]:
            return ["sources have not been prepared"]
        if digest(self.data["documents"]) != self.data["source_hash"]:
            errors.append("source snapshot changed")
        for doc_id, doc in self.data["documents"].items():
            try:
                saved = json.loads((self.root / "snapshots" / f"{doc_id}.json").read_text())
                if digest(saved) != digest(doc):
                    errors.append(f"source snapshot file changed: {doc_id}")
            except (OSError, ValueError):
                errors.append(f"source snapshot unavailable: {doc_id}")
        try:
            validate_batches(self.data["documents"], self.data["batches"], self.data["max_tokens"])
        except ValueError as e:
            errors.append(str(e))
        if self.data["phase"] == "preprocess":
            return errors
        missing = [
            b["batch_id"]
            for b in self.data["batches"]
            if b["batch_id"] not in self.data["processed"]
        ]
        if missing:
            errors.append(f"unprocessed batches: {missing}")
        if not self.data["records"]:
            errors.append("model is empty")
        for record in self.data["records"].values():
            try:
                validate_record(record["kind"], record["value"])
                if not record["evidence"]:
                    raise ValueError("record has no evidence")
                for evidence in record["evidence"]:
                    b = self._batch(evidence["batch_id"])
                    if evidence["block_id"] not in [s["block_id"] for s in b["segments"]]:
                        raise ValueError("invalid record evidence")
            except (ValueError, KeyError) as e:
                errors.append(str(e))
        errors.extend(reference_errors(self.data["records"]))
        return errors

    def publication_model(self):
        """Build the schema-preserving model without publishing or changing state."""
        records = self.data["records"]
        unresolved = [r["value"] for r in records.values() if r["kind"] == "unresolved"]
        for f in self.data["findings"].values():
            if f["status"] != "accepted" or f.get("resolution", {}).get("status") == "unresolved":
                unresolved.append(
                    {
                        "description": f["description"],
                        "object_names": [
                            records[r]["value"].get("name", r)
                            for r in f.get("record_ids", [])
                            if r in records
                        ],
                    }
                )
        sources = []
        for doc_id, doc in self.data["documents"].items():
            path = self.root / "snapshots" / f"{doc_id}.json"
            sources.append(
                {
                    "doc_id": doc_id,
                    "rfc": doc["meta"]["rfc"],
                    "input_sha256": doc["meta"]["input_sha256"],
                    "document_sha256": document_digest(doc),
                    "path": str(path.relative_to(self.root)),
                }
            )
        model = {
            "meta": {
                "schema_version": "8.0.0",
                "protocol": self.data["protocol"],
                "complete": not unresolved,
                "documents": sources,
            },
            **{
                kind: [r["value"] for r in records.values() if r["kind"] == kind]
                for kind in COLLECTIONS
            },
            "unresolved": unresolved,
        }
        return model
