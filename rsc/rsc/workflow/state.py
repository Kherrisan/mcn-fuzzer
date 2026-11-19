"""File checkpoints, deterministic gates and independent semantic review."""

import time
from pathlib import Path

from rsc.protocol.contract import COLLECTIONS, record_identity
from rsc.protocol.validate import validate_protocol

from . import partition
from .evidence import validate_evidence
from .metrics import result_metrics
from .model import validate_record
from .paging import records_page, resolve_budget, seal_response
from .patch import apply_patch
from .review_scope import build_scope
from .sources import prepare, token_count, validate_batches
from .store import Store, StoreError, atomic_json, read_json
from .tools import (
    CHECKPOINT,
    CONTRACTS,
    PARSE_CHECKPOINT,
    PHASE_TOOLS,
    REVIEW,
    SubmissionError,
    validate_arguments,
    validate_shape,
)

WorkflowError = StoreError
INTERNAL = {
    "workflow_status",
    "tick",
    "record_send",
    "restart_worker",
    "continue_worker",
    "record_nondelivery",
}
PARSE_SETTINGS = (
    "parse_workers",
    "parse_max_workers",
    "parse_context_tokens",
    "parse_fixed_tokens",
)


def track_findings(tracked, findings, round_):
    pending = [entry["finding"] for entry in tracked.values() if entry["status"] == "open"]
    for finding in findings:
        if finding not in pending:
            tracked[f"F{len(tracked) + 1}"] = {
                "finding": finding,
                "status": "open",
                "opened_round": round_,
            }
            pending.append(finding)
    return pending


class Workflow:
    def __init__(self, root, *, readonly=False):
        self.store = Store(root, readonly=readonly)
        self.root, self.lock = self.store.root, self.store.lock
        self.documents, self.batches = self.store.sources()

    @property
    def data(self):
        return self.store.settings()

    @classmethod
    def create(
        cls,
        root,
        sources,
        protocol,
        *,
        target_tokens=8000,
        max_tokens=12000,
        parse_workers="auto",
        parse_max_workers=4,
        parse_context_tokens=258400,
        parse_fixed_tokens=45000,
        merge_context_tokens=258400,
        merge_fixed_tokens=45000,
        max_feedback_rounds=5,
    ):
        if not 128 <= target_tokens <= max_tokens:
            raise WorkflowError("invalid batch token limits")
        partition.source_capacity(parse_context_tokens, parse_fixed_tokens)
        partition.source_capacity(merge_context_tokens, merge_fixed_tokens)
        if parse_workers != "auto" and (type(parse_workers) is not int or parse_workers < 1):
            raise WorkflowError("parse_workers must be auto or a positive integer")
        if any(type(n) is not int or n < 1 for n in (parse_max_workers, max_feedback_rounds)):
            raise WorkflowError("concurrency and feedback limit must be positive integers")
        Store.create(
            root,
            {
                "name": Path(root).resolve().name,
                "protocol": protocol,
                "sources": [str(Path(p).resolve()) for p in sources],
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
                "parse_workers": parse_workers,
                "parse_max_workers": parse_max_workers,
                "parse_context_tokens": parse_context_tokens,
                "parse_fixed_tokens": parse_fixed_tokens,
                "merge_context_tokens": merge_context_tokens,
                "merge_fixed_tokens": merge_fixed_tokens,
                "max_feedback_rounds": max_feedback_rounds,
                "phase": "preprocess",
                "round": 0,
                "started_at": time.time(),
            },
        ).close()
        return cls(root)

    def authenticate(self, token):
        with self.lock:
            return self.store.authenticate(token)

    def allowed(self, identity):
        if identity.role == "coordinator":
            return INTERNAL
        a = self.store.owned(identity)
        return PHASE_TOOLS[a["phase"]] if a["status"] != "submitted" else set()

    def catalog(self, identity):
        if identity.role == "coordinator":
            return set()
        self.store.owned(identity)
        return set(CONTRACTS)

    def record_catalog(self, identity, names, catalog):
        self.store.event("catalog", worker=identity.worker, catalog=catalog, size=len(names))

    def status(self):
        return {
            **self.data,
            "assignments": self.store.assignments(),
            "source_catalog": [
                {k: b[k] for k in ("batch_id", "doc_id", "section_path")} for b in self.batches
            ],
            "source_files": [str(p) for p in sorted((self.root / "sources/text").glob("*.txt"))],
        }

    def call(self, identity, name, arguments=None, *, origin="agent"):
        arguments = arguments or {}
        started = time.monotonic()
        with self.lock:
            context = {"phase": self.data["phase"], "round": self.data["round"]}
            result, error = None, None
            try:
                if self.store.readonly:
                    raise WorkflowError("read-only workflow")
                if identity.role == "coordinator":
                    if (
                        identity.token != self.store._state["coordinator_token"]
                        or name not in INTERNAL
                    ):
                        raise WorkflowError("forbidden coordinator operation")
                    result = self.internal(name, arguments)
                else:
                    a = self.store.owned(identity)
                    context.update(phase=a["phase"], round=a["round"], assignment=a["name"])
                    if name not in PHASE_TOOLS[a["phase"]]:
                        raise WorkflowError(f"tool {name} is forbidden for {a['phase']}")
                    validate_arguments(name, arguments)
                    if name == "validate_model":
                        result = self.prevalidate(a, arguments["path"])
                        if not result["valid"]:
                            error = result["errors"][0]["message"]
                    elif name.startswith("submit_"):
                        result = self.submit(a, arguments["path"])
                    else:
                        if a["status"] == "submitted":
                            raise WorkflowError("assignment already submitted")
                        self.received(a)
                        result = self.read(a, name, arguments)
                return result
            except (ValueError, OSError, KeyError, TypeError) as exc:
                error = str(exc)
                raise WorkflowError(error) from exc
            finally:
                if not self.store.readonly:
                    self.store.event(
                        "call",
                        origin=origin,
                        tool=name,
                        worker=identity.worker,
                        **context,
                        status="rejected" if error else "ok",
                        error=error,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        delivery={"tokens": (result or {}).get("tokens", 0)},
                    )
                    self.export_metrics()

    def internal(self, name, args):
        if name == "workflow_status":
            return self.status()
        if name == "tick":
            self.advance()
            return self.status()
        a = self.store.assignment(args["name"])
        if a["status"] == "submitted":
            return self.status()
        if name == "record_send":
            if a["status"] == "pending":
                self.store.mark(a, "sent")
        elif name == "record_nondelivery":
            if a["status"] == "sent":
                self.store.mark(a, "pending")
        elif name == "restart_worker":
            terminal = args["terminal"]
            if terminal.get("state") not in {"exited", "missing"}:
                raise WorkflowError("replacement requires confirmed terminal death")
            # Reconcile accepted files before revoking any successful session.
            self.advance()
            a = self.store.assignment(a["name"])
            if a["status"] != "submitted":
                self.store.restart(a, terminal)
                self.store.write_credentials()
        elif name == "continue_worker":
            # An ended turn is not failure of the process. Continue in the same session.
            continuations = a.get("continuations", [])
            if len(continuations) >= 2:
                self.fail(["worker ended without completing its result after two continuations"])
            else:
                self.store.mark(a, "pending", continuations=[*continuations, time.time()])
        return self.status()

    def received(self, a):
        current = self.store.assignment(a["name"])
        if current["status"] in {"pending", "sent"}:
            self.store.mark(current, "running")

    def validate_evidence(self, entries):
        validate_evidence(entries, {b["batch_id"]: b for b in self.batches})

    def validate_checkpoint(self, payload, *, parser=None):
        validate_shape(PARSE_CHECKPOINT if parser else CHECKPOINT, payload)
        named, errors = set(), []
        for index, item in enumerate(payload["items"]):
            value = item["value"]
            label = value.get("name") or item["kind"]
            for field, code, check, args in (
                ("value", "record", validate_record, (item["kind"], value)),
                ("evidence", "evidence", self.validate_evidence, (item["evidence"],)),
            ):
                try:
                    check(*args)
                except ValueError as exc:
                    errors.append(
                        {
                            "path": f"items[{index}].{field}",
                            "code": code,
                            "message": str(exc),
                            "objects": [label],
                        }
                    )
            if not isinstance(value.get("name"), str) or (
                item["kind"] == "states" and not isinstance(value.get("party_name"), str)
            ):
                continue
            key = record_identity(item["kind"], value)
            if key in named:
                errors.append(
                    {
                        "path": f"items[{index}].value.name",
                        "code": "duplicate",
                        "message": f"duplicate kind/name in checkpoint: {key}",
                        "objects": [label],
                    }
                )
            elif key is not None:
                named.add(key)
        for index, issue in enumerate(payload["unresolved"]):
            try:
                validate_record("unresolved", issue)
            except ValueError as exc:
                errors.append(
                    {"path": f"unresolved[{index}]", "code": "record", "message": str(exc)}
                )
        if errors:
            raise SubmissionError(errors)
        if parser:
            complete = payload.get("complete", True)
            completed = payload.get("completed_batches", parser["batches"] if complete else None)
            if not isinstance(completed, list) or len(set(completed)) != len(completed):
                raise WorkflowError("progress checkpoint needs unique completed_batches")
            if not set(completed) <= set(parser["batches"]):
                raise WorkflowError("completed_batches contains another parser's source ranges")
            if complete and set(completed) != set(parser["batches"]):
                raise WorkflowError("final parser checkpoint must cover all assigned batches")
            payload = {**payload, "complete": complete, "completed_batches": completed}
        return payload

    def validate_review(self, payload, a):
        validate_shape(REVIEW, payload)
        covered = payload["reviewed_batches"]
        if (
            len(set(covered)) != len(covered)
            or not set(covered) <= set(a["batches"])
            or (payload.get("complete", True) and set(covered) != set(a["batches"]))
        ):
            raise WorkflowError(
                "reviewed_batches must cover every assigned source range exactly once"
            )
        known = {b["batch_id"] for b in self.batches}
        for finding in payload["findings"]:
            if (
                len(set(finding["batch_ids"])) != len(finding["batch_ids"])
                or not set(finding["batch_ids"]) <= known
            ):
                raise WorkflowError("finding refers to unknown or duplicate source batches")
        assigned = a.get("finding_refs", {})
        resolved = set()
        for resolution in payload.get("resolutions", []):
            ref = resolution["finding"]
            if ref not in assigned or ref in resolved:
                raise WorkflowError(f"unknown or duplicate finding reference: {ref}")
            if not set(assigned[ref]["batch_ids"]) <= set(covered):
                raise WorkflowError(f"finding {ref} requires review of its source batches")
            resolved.add(ref)
        missing = sorted(assigned.keys() - resolved)
        if payload.get("complete", True) and missing:
            raise WorkflowError("missing finding resolutions: " + ", ".join(missing))
        return payload

    def work_path(self, a, path):
        path = Path(path)
        path = (path if path.is_absolute() else self.root / path).resolve()
        directory = (self.root / "work" / a["worker"]).resolve()
        if not path.is_relative_to(directory):
            raise WorkflowError(f"write the submission JSON under {directory}")
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise WorkflowError("submission must be a JSON file under 64 MiB")
        return path

    def progress(self, a, payload=None):
        progress_key = "reviewed_batches" if a["phase"] == "review" else "completed_batches"
        done = (payload or {}).get(progress_key, [])
        finished = a["phase"] not in {"parse", "review"} or (payload or {}).get("complete", True)
        return {
            "accepted": True,
            "path": a["output_path"],
            "stop_turn": finished,
            "pending_batches": [b for b in a.get("batches", []) if b not in done]
            if not finished
            else [],
        }

    def prevalidate(self, a, path):
        try:
            self.prepare_submission(a, path)
        except SubmissionError as exc:
            return {"valid": False, "errors": exc.errors}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {
                "valid": False,
                "errors": [{"path": "$", "code": "submission", "message": str(exc)}],
            }
        return {"valid": True, "errors": []}

    def prepare_submission(self, a, path):
        payload = read_json(self.work_path(a, path))
        if a["phase"] == "review":
            payload = self.validate_review(payload, a)
        else:
            if a["phase"] == "fix":
                base = a["candidate_path"]
                payload = apply_patch(self.checkpoint(base), payload, base)
            payload = self.validate_checkpoint(payload, parser=a if a["phase"] == "parse" else None)
            whole_model = a["phase"] in {"merge", "fix"} or (
                a["phase"] == "parse"
                and payload["complete"]
                and sum(x["phase"] == "parse" for x in self.store.assignments()) == 1
            )
            if whole_model:
                candidate = {k: payload[k] for k in ("items", "unresolved")}
                diagnostics = []
                errors = validate_protocol(
                    self.publication_model(candidate), self.documents, diagnostics=diagnostics
                )
                if errors:
                    details = []
                    for issue in diagnostics:
                        owner, *related = issue["records"]
                        kind, index = owner.split(":")
                        path = ".".join(map(str, issue["path"]))
                        details.append(
                            {
                                "path": f"{kind}[{index}].{path}",
                                "code": issue["type"],
                                "message": issue["description"],
                                "objects": [owner, *related],
                            }
                        )
                    reported = {d["message"] for d in details}
                    details.extend(
                        {"path": "$", "code": "model", "message": err}
                        for err in errors
                        if err not in reported
                    )
                    raise SubmissionError(
                        details,
                        "candidate validation failed; correct this work file and resubmit "
                        "in the same turn",
                    )
        official = self.root / a["output_path"]
        if official.exists() and a.get("accepted_file"):
            previous = read_json(official)
            if previous == payload:
                return payload, True
            if a["phase"] not in {"parse", "review"} or previous.get("complete", True):
                raise WorkflowError("different content for completed submission")
            progress_key = "reviewed_batches" if a["phase"] == "review" else "completed_batches"
            if not set(previous[progress_key]) <= set(payload[progress_key]):
                raise WorkflowError("progress cannot discard completed source batches")
            old_evidence = {
                (e["batch_id"], e["block_id"])
                for item in previous.get("items", [])
                for e in item["evidence"]
            }
            new_evidence = {
                (e["batch_id"], e["block_id"])
                for item in payload.get("items", [])
                for e in item["evidence"]
            }
            if not old_evidence <= new_evidence:
                raise WorkflowError("progress checkpoint must preserve accumulated source evidence")
        if a["status"] == "submitted":
            raise WorkflowError("assignment already submitted")
        return payload, False

    def submit(self, a, path):
        payload, accepted = self.prepare_submission(a, path)
        if accepted:
            self.advance()
            return self.progress(a, payload)
        official = self.root / a["output_path"]
        self.received(a)
        # Install complete output before recording acceptance or advancing scheduling.
        # A crash before acceptance requires a harmless resubmission of the work file.
        atomic_json(official, payload, readonly=True)
        self.store.update_assignment(a["name"], accepted_file=True)
        self.advance()
        return self.progress(a, payload)

    def phase(self, name, **extra):
        data = self.data
        if name != data["phase"] or extra.get("round", data["round"]) != data["round"]:
            self.store.event(
                "phase", phase=name, previous=data["phase"], round=extra.get("round", data["round"])
            )
        self.store.settings_update(phase=name, **extra)

    def fail(self, errors):
        self.phase("failed", errors=errors, finished_at=time.time())
        # A prior partial export must never look like publication of a failed run.
        (self.root / "protocol.json").unlink(missing_ok=True)

    def assign(self, worker, role, phase, round_, output_path, **body):
        (self.root / "work" / worker).mkdir(parents=True, exist_ok=True)
        return self.store.assign(
            worker,
            role,
            phase,
            round_,
            output_path=output_path,
            work_directory=str(self.root / "work" / worker),
            **body,
        )

    def advance(self):
        """Reconcile accepted files before each transition, including after service restart."""
        with self.lock:
            for _ in range(32):
                data = self.data
                phase, round_ = data["phase"], data["round"]
                if phase == "preprocess":
                    self.preprocess()
                    continue
                if phase in {"published", "failed"}:
                    if phase == "published":
                        self.export()
                    break
                current = [
                    a
                    for a in self.store.assignments()
                    if a["phase"] == phase and a["round"] == round_
                ]
                if not current:
                    raise WorkflowError(f"missing {phase} assignments")
                for a in current:
                    path = self.root / a["output_path"]
                    if path.exists() and a.get("accepted_file") and a["status"] != "submitted":
                        payload = read_json(path)
                        if phase == "review":
                            self.validate_review(payload, a)
                        else:
                            payload = self.validate_checkpoint(
                                payload, parser=a if phase == "parse" else None
                            )
                        if phase not in {"parse", "review"} or payload.get("complete", True):
                            self.store.mark(a, "submitted")
                        else:
                            progress_key = (
                                "reviewed_batches" if phase == "review" else "completed_batches"
                            )
                            self.store.update_assignment(
                                a["name"], **{progress_key: payload[progress_key]}
                            )
                current = [self.store.assignment(a["name"]) for a in current]
                if any(a["status"] == "failed" for a in current):
                    self.fail(["worker exhausted its recovery limit"])
                    break
                if any(a["status"] != "submitted" for a in current):
                    break
                if phase == "parse":
                    if len(current) == 1:
                        self.store.settings_update(
                            fix_owner=current[0]["worker"], fix_role="parser"
                        )
                        self.check_candidate(self.checkpoint(current[0]["output_path"]), round_)
                    else:
                        paths = [a["output_path"] for a in current]
                        size = sum(token_count(read_json(self.root / p)) for p in paths)
                        required = data["merge_fixed_tokens"] + size + max(size, 40000) + 20000
                        budget = data["merge_context_tokens"] * 3 // 4
                        atomic_json(
                            self.root / "merge-budget.json",
                            {
                                "input_tokens": size,
                                "estimated_context_tokens": required,
                                "available_context_tokens": budget,
                                "fits": required <= budget,
                            },
                        )
                        if required > budget:
                            self.fail(
                                [
                                    "whole-input merge exceeds context budget: "
                                    f"{required} > {budget}; "
                                    "parser files preserved"
                                ]
                            )
                            break
                        with self.store.transaction():
                            self.store.settings_update(fix_owner="merge-1", fix_role="merge")
                            self.assign(
                                "merge-1",
                                "merge",
                                "merge",
                                0,
                                "merge.json",
                                inputs=paths,
                                batches=[b["batch_id"] for b in self.batches],
                            )
                            self.phase("merge")
                elif phase in {"merge", "fix"}:
                    self.check_candidate(self.checkpoint(current[0]["output_path"]), round_)
                elif phase == "review":
                    self.finish_review(current[0], data)
                if (phase, round_) == (self.data["phase"], self.data["round"]):
                    break
            self.store.write_credentials()

    def finish_review(self, assignment, data):
        review = read_json(self.root / assignment["output_path"])
        round_ = assignment["round"]
        retained = [
            f
            for f in data.get("accepted_ambiguities", [])
            if not set(f["batch_ids"]) <= set(assignment["batches"])
        ]
        classifications = [
            *retained,
            *(f for f in review["findings"] if f["category"] != "correctable"),
        ]
        tracked = data.get("tracked_findings", {})
        for resolution in review.get("resolutions", []):
            entry = tracked[resolution["finding"]]
            entry.update(status=resolution["status"], reviewed_round=round_)
            if "note" in resolution:
                entry["note"] = resolution["note"]
            else:
                entry.pop("note", None)

        candidate = self.checkpoint(assignment["candidate_path"])
        allowed = {f["description"] for f in classifications}
        new_errors = [f for f in review["findings"] if f["category"] == "correctable"]
        new_errors.extend(
            {
                "category": "correctable",
                "description": "Unresolved issue needs reviewer classification: "
                + issue["description"],
                "batch_ids": assignment["batches"],
                "global": True,
            }
            for issue in candidate["unresolved"]
            if issue["description"] not in allowed
        )
        pending = track_findings(tracked, new_errors, round_)

        with self.store.transaction():
            self.store.settings_update(
                accepted_ambiguities=classifications, tracked_findings=tracked
            )
            if pending:
                self.request_fix([*classifications, *pending])
            else:
                self.publish_review(assignment, candidate, classifications)

    def publish_review(self, assignment, candidate, classifications):
        # Findings retain source evidence in the immutable review file.
        unresolved = [*candidate["unresolved"]]
        for finding in classifications:
            citations = [
                f"{batch['doc_id']}/{part['block_id']}"
                for batch in self.batches
                if batch["batch_id"] in finding["batch_ids"]
                for part in batch["segments"]
            ]
            issue = {
                "description": finding["description"]
                + " [source: "
                + ", ".join(dict.fromkeys(citations))
                + "]",
                "object_names": [],
            }
            unresolved = [x for x in unresolved if x["description"] != finding["description"]]
            if issue not in unresolved:
                unresolved.append(issue)
        self.store.settings_update(
            unresolved=unresolved,
            semantic_review=True,
            reviewed_candidate=assignment["candidate_path"],
        )
        self.phase("published", findings=[], errors=[], finished_at=time.time())

    def preprocess(self):
        data = self.data
        if not self.documents:
            docs, batches = prepare(
                self.root,
                data["sources"],
                data["target_tokens"],
                data["max_tokens"],
                capacity_tokens=partition.source_capacity(
                    data["parse_context_tokens"], data["parse_fixed_tokens"]
                ),
            )
            atomic_json(
                self.root / "sources.json", {"documents": docs, "batches": batches}, readonly=True
            )
            self.documents, self.batches = docs, batches
        plan = partition.plan_parsers(self.batches, **{k: data[k] for k in PARSE_SETTINGS})
        atomic_json(self.root / "parse-plan.json", plan, readonly=True)
        with self.store.transaction():
            for part in plan["partitions"]:
                self.assign(
                    part["name"],
                    "parser",
                    "parse",
                    0,
                    f"parsers/{part['name']}.json",
                    batches=part["batch_ids"],
                    source_tokens=part["source_tokens"],
                )
            self.phase("parse", parallel_agents=plan["parallel_agents"])

    def checkpoint(self, path):
        value = read_json(self.root / path)
        return {key: value[key] for key in ("items", "unresolved")}

    def candidate_errors(self, candidate):
        self.validate_checkpoint(candidate)
        return validate_protocol(self.publication_model(candidate), self.documents)

    def check_candidate(self, candidate, round_):
        atomic_json(self.root / "candidate.json", candidate, readonly=True)
        errors = self.candidate_errors(candidate)
        atomic_json(self.root / "checks.json", errors)
        if errors:
            self.request_fix(
                [
                    {
                        "category": "correctable",
                        "description": str(error),
                        "batch_ids": [b["batch_id"] for b in self.batches],
                        "global": True,
                    }
                    for error in errors
                ]
            )
            return
        candidate_path = f"reviews/round-{round_}/candidate.json"
        atomic_json(self.root / candidate_path, candidate, readonly=True)
        before_path = f"reviews/round-{round_}/input.json" if round_ else None
        before = (
            self.checkpoint(before_path)
            if before_path and (self.root / before_path).exists()
            else None
        )
        data = self.data
        tracked = data.get("tracked_findings", {})
        previous_findings = list(data.get("findings", []))
        if "tracked_findings" not in data and previous_findings:
            # A run resumed after a legacy Fix can start tracking at its next review.
            track_findings(
                tracked,
                (f for f in previous_findings if f["category"] == "correctable"),
                round_ - 1,
            )
        finding_refs = {
            ref: entry["finding"] for ref, entry in tracked.items() if entry["status"] == "open"
        }
        for finding in finding_refs.values():
            if finding not in previous_findings:
                previous_findings.append(finding)
        scope = build_scope(
            before,
            candidate,
            [b["batch_id"] for b in self.batches],
            previous_findings,
            before_path=before_path,
            after_path=candidate_path,
        )
        scope_path = f"reviews/round-{round_}/review_scope.json"
        atomic_json(self.root / scope_path, scope, readonly=True)
        with self.store.transaction():
            self.store.settings_update(tracked_findings=tracked)
            self.assign(
                "reviewer-1",
                "reviewer",
                "review",
                round_,
                f"reviews/round-{round_}/review.json",
                batches=scope["batches"],
                candidate_path=candidate_path,
                review_scope_path=scope_path,
                previous_findings=previous_findings,
                finding_refs=finding_refs,
            )
            self.phase("review", errors=[])

    def request_fix(self, findings):
        data = self.data
        if data["round"] >= data["max_feedback_rounds"]:
            self.fail(findings)
            return
        round_ = data["round"] + 1
        # Fix reads the preceding complete candidate even after accepted result recovery.
        input_path = f"reviews/round-{round_}/input.json"
        atomic_json(self.root / input_path, self.checkpoint("candidate.json"), readonly=True)
        with self.store.transaction():
            self.assign(
                data["fix_owner"],
                data["fix_role"],
                "fix",
                round_,
                f"reviews/round-{round_}/fix.json",
                candidate_path=input_path,
                findings=findings,
                batches=[b["batch_id"] for b in self.batches],
            )
            self.phase("fix", round=round_, findings=findings, errors=[])

    def read(self, a, name, args):
        if name == "read_source_batches":
            wanted = args["batch_ids"]
            if len(set(wanted)) != len(wanted) or not set(wanted) <= {
                b["batch_id"] for b in self.batches
            }:
                raise WorkflowError("batch_ids must be unique known batches")
            rows = [b for b in self.batches if b["batch_id"] in wanted]
            budget = resolve_budget(args.get("max_tokens"))

            def render(count):
                return seal_response(
                    {
                        "batches": rows[:count],
                        "remaining": [b["batch_id"] for b in rows[count:]],
                        "max_tokens": budget,
                    }
                )

            count = 0
            while count < len(rows) and render(count + 1)["tokens"] <= budget:
                count += 1
            if not count:
                raise WorkflowError(f"first batch requires {render(1)['tokens']} tokens")
            return render(count)
        paths = a.get("inputs", [])
        if a.get("candidate_path"):
            paths = [a["candidate_path"]]
        if a["phase"] == "parse":
            paths = [a["output_path"]] if (self.root / a["output_path"]).exists() else []
        rows = [
            {**row, "input_file": path} for path in paths for row in self.checkpoint(path)["items"]
        ]

        def references(value):
            if isinstance(value, dict):
                return any(references(v) for v in value.values())
            if isinstance(value, list):
                return any(references(v) for v in value)
            return isinstance(value, str) and value in args.get("references", [])

        rows = [
            r
            for r in rows
            if ("kind" not in args or r["kind"] == args["kind"])
            and ("name" not in args or r["value"].get("name") == args["name"])
            and ("references" not in args or references(r["value"]))
        ]
        if args.get("view") == "index":
            rows = [
                {
                    "kind": r["kind"],
                    "name": r["value"]["name"],
                    "input_file": r["input_file"],
                    **({"party_name": r["value"]["party_name"]} if r["kind"] == "states" else {}),
                }
                for r in rows
                if "name" in r["value"]
            ]
        return records_page(
            rows,
            args.get("cursor", 0),
            args.get("max_tokens"),
            metadata={
                "input_files": paths,
                "unresolved": [
                    {**issue, "input_file": path}
                    for path in paths
                    for issue in self.checkpoint(path)["unresolved"]
                ],
            },
        )

    def publication_model(self, candidate=None):
        if candidate is None:
            candidate = self.checkpoint(self.data.get("reviewed_candidate", "candidate.json"))
        unresolved = self.data.get("unresolved", candidate["unresolved"])
        collections = {kind: [] for kind in COLLECTIONS}
        for item in candidate["items"]:
            kind, value = item["kind"], item["value"]
            if kind == "steps" and "processing" in value:
                value = dict(value)
                processing = {hook: ops for hook, ops in value.pop("processing").items() if ops}
                if processing:
                    value["processing"] = processing
            collections[kind].append(value)
        if not collections["states"]:
            del collections["states"]
        return {
            "meta": {
                "protocol": self.data["protocol"],
                "complete": not unresolved,
                "documents": [
                    {"doc_id": n, "rfc": d["meta"]["rfc"], "path": f"snapshots/{n}.json"}
                    for n, d in self.documents.items()
                ],
            },
            **collections,
            "unresolved": unresolved,
        }

    def export(self):
        if self.data["phase"] != "published":
            raise WorkflowError("not published")
        validate_batches(self.documents, self.batches, self.data["max_tokens"])
        candidate = self.checkpoint(self.data["reviewed_candidate"])
        self.validate_checkpoint(candidate)
        model = self.publication_model(candidate)
        errors = validate_protocol(model, self.documents)
        if errors:
            raise WorkflowError("publication validation failed: " + "; ".join(errors))
        for name, document in self.documents.items():
            atomic_json(self.root / f"snapshots/{name}.json", document, readonly=True)
        atomic_json(self.root / "report.json", self.data)
        atomic_json(self.root / "protocol.json", model, readonly=True)

    def export_metrics(self):
        atomic_json(self.root / "phases.json", result_metrics(self.store, self.store.events()))
