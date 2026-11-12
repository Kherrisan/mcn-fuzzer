"""Check the accepted file chain and independent review without rerunning agents."""

from rsc.protocol.validate import validate_protocol_file

from .sources import validate_batches
from .state import Workflow, WorkflowError
from .store import read_json


def audit_run(root):
    w = Workflow(root, readonly=True)

    def require(value, message):
        if not value:
            raise WorkflowError("audit failed: " + message)

    try:
        require(w.data["phase"] == "published", "not published")
        validate_batches(w.documents, w.batches, w.data["max_tokens"])
        for name, document in w.documents.items():
            require(
                read_json(w.root / f"snapshots/{name}.json") == document,
                "exported source snapshot differs",
            )
        plan = read_json(w.root / "parse-plan.json")
        assigned = [b for p in plan["partitions"] for b in p["batch_ids"]]
        require(
            len(set(assigned)) == len(assigned)
            and set(assigned) == {b["batch_id"] for b in w.batches},
            "parser plan does not cover source exactly once",
        )
        parsers, reviews = [], []
        for a in w.store.assignments():
            require(a["status"] == "submitted", "unfinished assignment")
            require(a.get("accepted_file"), "file was not accepted through the service")
            payload = read_json(w.root / a["output_path"])
            if a["phase"] == "review":
                w.validate_review(payload, a)
                require(
                    a["worker"] != w.data["fix_owner"] and a["role"] == "reviewer",
                    "review is not independent",
                )
                require(
                    not w.candidate_errors(w.checkpoint(a["candidate_path"])),
                    "reviewed invalid candidate",
                )
                reviews.append((a, payload))
            else:
                w.validate_checkpoint(payload, parser=a if a["phase"] == "parse" else None)
                if a["phase"] == "parse":
                    require(payload["complete"], "parser checkpoint incomplete")
                    parsers.append(a)
                if a["phase"] == "fix":
                    require(a["worker"] == w.data["fix_owner"], "incorrect Fix owner")
        require(len(parsers) == len(plan["partitions"]), "parser count differs from plan")
        require(bool(reviews), "missing independent review")
        merges = [a for a in w.store.assignments() if a["phase"] == "merge"]
        require(len(merges) == (0 if len(parsers) == 1 else 1), "incorrect merge path")
        if merges:
            require(
                merges[0]["inputs"] == [a["output_path"] for a in parsers], "merge omitted an input"
            )
        final, review = reviews[-1]
        retained = [
            f
            for f in w.data.get("accepted_ambiguities", [])
            if not set(f["batch_ids"]) <= set(final["batches"])
        ]
        require(
            all(f["category"] != "correctable" for f in retained), "invalid retained classification"
        )
        require(
            final["candidate_path"] == w.data["reviewed_candidate"],
            "publication lacks final review",
        )
        require(
            not any(f["category"] == "correctable" for f in review["findings"]),
            "correctable findings remain",
        )
        require(
            w.checkpoint("candidate.json") == w.checkpoint(final["candidate_path"]),
            "frozen candidate differs",
        )
        producer = next(
            a for a in reversed(w.store.assignments()) if a["phase"] in {"parse", "merge", "fix"}
        )
        require(
            w.checkpoint(producer["output_path"]) == w.checkpoint(final["candidate_path"]),
            "candidate differs from accepted result",
        )
        require(
            read_json(w.root / "protocol.json") == w.publication_model(),
            "published protocol differs",
        )
        require(
            not validate_protocol_file(w.root / "protocol.json"),
            "invalid protocol or source snapshots",
        )
        return {
            "verified": True,
            "complete": w.publication_model()["meta"]["complete"],
            "semantic_review": True,
            "source_batches": len(assigned),
            "parsers": len(parsers),
            "reviews": len(reviews),
            "items": len(w.checkpoint("candidate.json")["items"]),
        }
    finally:
        w.store.close()
