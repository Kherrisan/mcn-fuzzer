"""Replay actual submissions and deterministic integration without executing agents."""

import json
from copy import deepcopy

from rsc.protocol.validate import validate_protocol_file

from . import merge
from .model import validate_record
from .sources import validate_batches
from .state import Workflow, WorkflowError


def audit_run(root):
    w = Workflow(root, readonly=True)

    def require(ok, message):
        if not ok:
            raise WorkflowError("audit failed: " + message)

    try:
        require(w.data["phase"] == "published", "not published")
        validate_batches(w.documents, w.batches, w.data["max_tokens"])
        reconstructed, covered = {}, set()
        submissions = []
        for row in w.db.execute("SELECT * FROM submissions ORDER BY rowid"):
            a = w.store.assignment(row["assignment"])
            attempt = w.db.execute(
                "SELECT body FROM attempts WHERE assignment=? AND attempt=?",
                (row["assignment"], row["attempt"]),
            ).fetchone()
            require(attempt is not None, "submission has no attempt")
            require("send_intent_at" in json.loads(attempt[0]), "submission preceded dispatch")
            args, result = json.loads(row["arguments"]), json.loads(row["result"])
            submissions.append(args)
            if a["phase"] != "parse":
                continue
            require(
                args["batch_id"] in a["batches"] and args["batch_id"] not in covered,
                "batch ownership or duplicate completion",
            )
            covered.add(args["batch_id"])
            require(len(args["items"]) == len(result["items"]), "item ID acknowledgment mismatch")
            for item, answer in zip(args["items"], result["items"], strict=True):
                key = answer["id"]
                if "id" in item:
                    require(
                        key == item["id"]
                        and key in reconstructed
                        and reconstructed[key]["owner"] == a["worker"],
                        "invalid item update",
                    )
                    record = {**reconstructed[key], **deepcopy(item)}
                    record["evidence"] = merge.evidence([reconstructed[key], item])
                else:
                    require(key not in reconstructed, "reused item ID")
                    record = {**deepcopy(item), "id": key, "owner": a["worker"]}
                w.validate_evidence(record["evidence"])
                validate_record(record["kind"], record["value"])
                reconstructed[key] = record
            local = {k: r for k, r in reconstructed.items() if r["owner"] == a["worker"]}
            for r in local.values():
                merge.bind(r, local)
        require(covered == {b["batch_id"] for b in w.batches}, "incomplete source coverage")
        for row in reconstructed.values():
            merge.bind(row, reconstructed)
        require(reconstructed == w.store.snapshot("parse"), "frozen parse differs from submissions")
        groups = w.groups()
        merge.grouped(reconstructed, [g["members"] for g in groups.values()])
        partitions = [s["groups"] for s in submissions if "groups" in s]
        require(
            bool(partitions)
            and {tuple(sorted(g["members"])) for g in groups.values()}
            == {tuple(sorted(m)) for m in partitions[-1]},
            "groups differ from clustering",
        )
        for name, g in groups.items():
            require(
                g["result"].get("group_name") == name and g["result"] in submissions,
                "group result has no submission",
            )
        candidate, table, errors = merge.integrate(reconstructed, groups)
        require(not errors, "deterministic checks failed")
        require(candidate == w.store.snapshot("published"), "published candidate differs")
        require(table == w.store.snapshot("revision_table"), "revision table differs")
        require(
            json.loads((w.root / "revision-table.json").read_text()) == table,
            "exported revision table differs",
        )
        require(
            json.loads((w.root / "protocol.json").read_text()) == w.publication_model(),
            "export differs",
        )
        require(
            not validate_protocol_file(w.root / "protocol.json"),
            "invalid protocol or source snapshot",
        )
        return {
            "verified": True,
            "complete": True,
            "semantic_review": False,
            "source_batches": len(covered),
            "items": len(candidate),
            "groups": len(groups),
        }
    finally:
        w.db.close()
