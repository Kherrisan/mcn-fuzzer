"""Multi-round sequential Issue completion with immediate private-state commits."""

from __future__ import annotations

from copy import deepcopy

from rsc.artifacts import write_json, write_jsonl

from . import agent_prompts
from .agent_runtime import RunBudget
from .contract import BASE, COLLECTIONS
from .extraction import ExtractionRuntime, summarize_runs
from .keywords import KeywordIndex
from .merge import issue, unique
from .models import InteractionRepairResponse, ObjectRepairResponse
from .options import RepairOptions
from .proposals import (
    CandidateState,
    ProposalError,
    ProposalValidator,
    commit_proposal,
    current_issues,
)
from .registry import Registry
from .retrieval import RetrievalContext
from .store import ExtractionStore
from .tools import AgentDeps
from .validate import object_errors, prune_invalid

EMPTY_ISSUE = {
    "objects": "No base objects extracted",
    "interactions": "No complete message interaction extracted",
}


def _step_names(state: CandidateState) -> set[str]:
    return {row.get("name", "") for row in state.model["steps"] if row.get("name")}


def collect_current(state: CandidateState, runtime: ExtractionRuntime, phase: str) -> list[dict]:
    """Worklist for this phase. Phase lives on the tracker, not inside the Issue."""

    state.issues = [
        row
        for row in current_issues(state.model, state.issues, runtime.corpus.documents)
        if row["description"] not in EMPTY_ISSUE.values()
    ]
    kinds = BASE if phase == "objects" else ("steps",)
    if not any(state.model[kind] for kind in kinds):
        state.issues.append(issue(EMPTY_ISSUE[phase], []))
    state.issues = unique(state.issues)
    state.history.refresh(state.issues, 0, reset_pending=False)
    for item in state.issues:
        state.history.entries[state.history.key(item)].setdefault("phase", phase)
    if phase == "objects":
        return [
            row
            for row in state.issues
            if state.history.entries[state.history.key(row)].get("phase") == "objects"
        ]
    names = _step_names(state)
    return [
        row
        for row in state.issues
        if state.history.entries[state.history.key(row)].get("phase") == "interactions"
        or names.intersection(row.get("object_names") or [])
    ]


def _focus_identities(registry: Registry, names: list[str]) -> set[str]:
    found = set()
    for name in names:
        found.update(registry.owners_for_ref(name))
    neighbors = set()
    for identity in found:
        neighbors.update(registry.reverse.get(identity, ()))
    return found | neighbors


def repair_context(
    runtime: ExtractionRuntime,
    state: CandidateState,
    current: dict,
    phase: str,
    keywords: KeywordIndex,
    store: ExtractionStore,
    round_number: int,
    feedback: str | None,
) -> tuple[AgentDeps, dict, set[str]]:
    """Supply only the current Issue; read definitions and sources on demand."""

    registry = state.registry(runtime.corpus.documents)
    allowed = BASE if phase == "objects" else COLLECTIONS
    issue_names = list(current.get("object_names") or [])
    focused = {
        identity
        for identity in (
            _focus_identities(registry, issue_names) if issue_names else registry.rows
        )
        if registry.kinds.get(identity) in allowed
    }
    context = RetrievalContext(
        runtime.corpus,
        phase=phase,
        agent_options=runtime.agent_options,
        store=store,
        lookup=registry.root,
    )
    deps = AgentDeps(
        runtime.protocol,
        "repair",
        phase,
        runtime.corpus,
        registry,
        keywords,
        context,
        runtime.events,
        options=runtime.agent_options,
    )
    editable_names = sorted(
        {
            registry.rows[identity].get("name")
            for identity in focused
            if issue_names and registry.rows[identity].get("name")
        }
    )
    payload = deps.payload(
        issue={
            "description": current["description"],
            "object_names": list(current.get("object_names") or []),
        },
        editable_object_names=editable_names,
        previous_feedback=[{"description": current["description"], "reason": feedback[:1000]}]
        if feedback
        else [],
    )
    # Scope is expressed internally by snapshot identities, not public names:
    # several channel variants can legitimately share the same name.
    return deps, payload, focused


def _same_issue(left: dict, right: dict) -> bool:
    return left.get("description") == right.get("description") and list(
        left.get("object_names") or []
    ) == list(right.get("object_names") or [])


def closed_by_changes(
    current: dict,
    before: dict,
    after: dict,
    returned: list[dict],
    documents: dict,
) -> bool:
    """Close an Issue only after the model changes and the same Issue is not returned."""

    if before == after:
        return False
    if any(_same_issue(current, row) for row in returned):
        return False
    text = current["description"]
    for messages in object_errors(after, documents).values():
        if any(text == message or text in message for message in messages):
            return False
    return True


async def complete_phase(
    runtime: ExtractionRuntime,
    state: CandidateState,
    *,
    phase: str,
    keywords: KeywordIndex,
    store: ExtractionStore,
    options: RepairOptions,
) -> tuple[CandidateState, dict, list[dict]]:
    """Traverse a frozen tracking-ID worklist, reading the newest state before each Issue."""

    rounds_limit = (
        options.objects_max_rounds if phase == "objects" else options.interactions_max_rounds
    )
    output_type = ObjectRepairResponse if phase == "objects" else InteractionRepairResponse
    instructions = (
        agent_prompts.OBJECTS_REPAIR if phase == "objects" else agent_prompts.INTERACTIONS_REPAIR
    )
    history = state.history
    records, rounds = [], []
    root = runtime.out / f"{phase}-repair" if runtime.out is not None else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    runtime.coordinator.update_model(state.model, state.issues, state.revision)
    active = collect_current(state, runtime, phase)
    history.refresh(state.issues, 0, reset_pending=False)
    stop_reason = (
        "disabled" if not options.enabled else "no_missing" if not active else "round_limit"
    )
    if options.enabled:
        for number in range(1, rounds_limit + 1):
            active = collect_current(state, runtime, phase)
            if not active:
                stop_reason = "no_missing"
                break
            history.refresh(state.issues, number, reset_pending=False)
            for current in active:
                history.entries[history.key(current)]["status"] = "pending"
            worklist = list(dict.fromkeys(history.key(row) for row in history.ordered(active)))
            starting = set(worklist)
            round_root = root / f"round-{number:03d}" if root is not None else None
            if round_root is not None:
                round_root.mkdir()
                write_json(round_root / "input-model.json", state.model)
                write_json(round_root / "input-issues.json", active)
                write_json(round_root / "worklist.json", worklist)
                for name in ("requests.jsonl", "responses.jsonl", "decisions.jsonl"):
                    write_jsonl(round_root / name, [])
            requests, responses, decisions = [], [], []
            for cursor, tracking_id in enumerate(worklist):
                active = collect_current(state, runtime, phase)
                history.refresh(state.issues, number, reset_pending=False)
                current = next((row for row in active if history.key(row) == tracking_id), None)
                if current is None:
                    decisions.append(
                        {
                            "phase": phase,
                            "round": number,
                            "tracking_id": tracking_id,
                            "issue_cursor": cursor,
                            "status": "skipped_resolved",
                        }
                    )
                    if round_root is not None:
                        write_jsonl(round_root / "decisions.jsonl", decisions)
                    continue
                feedback = history.entries[tracking_id].get("last_feedback")
                budget = RunBudget(runtime.agent_options)
                normal_limit = runtime.limits.get(f"{phase}_repair") or runtime.limits["repair"]
                accepted = False
                last_error = None
                for attempt in (1, 2):
                    deps, payload, editable = repair_context(
                        runtime,
                        state,
                        current,
                        phase,
                        keywords,
                        store,
                        number,
                        feedback,
                    )
                    validator = ProposalValidator(state, deps, repair=True, editable_ids=editable)

                    def check(output, validator=validator):
                        return validator.output_errors(output)

                    limits = normal_limit
                    run_id = f"repair:{phase}:{number}:{tracking_id}:{attempt}"
                    requests.append(
                        {
                            "run_id": run_id,
                            "tracking_id": tracking_id,
                            "description": current["description"],
                            "object_names": list(current.get("object_names") or []),
                            "attempt": attempt,
                            "model_revision": state.revision,
                            "payload": deepcopy(payload),
                        }
                    )
                    if round_root is not None:
                        write_jsonl(round_root / "requests.jsonl", requests)
                    record = await runtime.run(
                        "repair",
                        phase,
                        output_type,
                        payload,
                        run_id=run_id,
                        deps=deps,
                        output_check=check,
                        instructions=instructions,
                        budget=budget,
                        limits_override=limits,
                        session=runtime.coordinator,
                    )
                    record.update(
                        round=number,
                        tracking_id=tracking_id,
                        description=current["description"],
                        object_names=list(current.get("object_names") or []),
                        attempt=attempt,
                    )
                    records.append(record)
                    responses.append(record)
                    last_error = record["error"]
                    recovery = validator.recover() if record["status"] != "ok" else None
                    data = (
                        record["data"]
                        if record["status"] == "ok"
                        else recovery["data"]
                        if recovery
                        else None
                    )
                    if recovery is not None:
                        record["recovery"] = recovery
                    if data is not None:
                        validator.prepare(data)
                    record["repair_targets"] = deepcopy(validator.repair_selections)
                    runtime.events.append("repair.jsonl", record)
                    if round_root is not None:
                        write_jsonl(round_root / "responses.jsonl", responses)
                    if data is not None:
                        before = state.snapshot()
                        try:
                            committed = await commit_proposal(
                                validator,
                                data,
                                runtime.allocator,
                                origin=f"repair:{phase}:{number}:{tracking_id}",
                                list_id=None,
                            )
                        except ProposalError as exc:
                            last_error = str(exc)
                            runtime.coordinator.record_commit(record["run_id"], {}, accepted=False)
                        else:
                            accepted = before != state.model
                            returned = list(data.get("unresolved") or [])
                            if not (recovery and recovery.get("rejected")) and closed_by_changes(
                                current,
                                before,
                                state.model,
                                returned,
                                runtime.corpus.documents,
                            ):
                                signature = state.history.signature(current)
                                state.issues = [
                                    row
                                    for row in state.issues
                                    if state.history.signature(row) != signature
                                ]
                            if recovery is not None:
                                state.issues.append(
                                    issue("Agent stopped after a partial valid proposal", [])
                                )
                            collect_current(state, runtime, phase)
                            runtime.coordinator.update_model(
                                state.model, state.issues, state.revision
                            )
                            runtime.coordinator.record_commit(
                                record["run_id"],
                                committed["items"],
                                committed["unresolved"],
                            )
                            last_error = None
                        break
                    if attempt == 2 or record["end_reason"] not in {
                        "output_truncated",
                        "context_length",
                        "context_length_exceeded",
                    }:
                        break
                    if budget.model_requests >= runtime.agent_options.max_model_requests:
                        break
                    try:
                        budget.remaining_seconds()
                    except RuntimeError:
                        break
                    feedback = record["error"]
                active = collect_current(state, runtime, phase)
                runtime.coordinator.update_model(state.model, state.issues, state.revision)
                history.refresh(state.issues, number, reset_pending=False)
                remaining = next((row for row in active if history.key(row) == tracking_id), None)
                reasons = [last_error] if last_error else []
                if remaining:
                    message = last_error or "Issue remains unresolved after this proposal"
                    history.defer([remaining], message, attempted=True)
                    reasons = [message]
                else:
                    history.entries[tracking_id]["attempts"] += 1
                decisions.append(
                    {
                        "phase": phase,
                        "round": number,
                        "tracking_id": tracking_id,
                        "description": current["description"],
                        "object_names": list(current.get("object_names") or []),
                        "issue_cursor": cursor,
                        "status": "accepted"
                        if accepted and remaining is None
                        else "candidate"
                        if accepted
                        else "deferred",
                        "reasons": reasons,
                        "model_revision": state.revision,
                    }
                )
                if round_root is not None:
                    write_jsonl(round_root / "decisions.jsonl", decisions)
                    write_json(round_root / "model.json", state.model)
                    write_json(round_root / "unresolved.json", state.issues)
            verified, _ = prune_invalid(state.model, runtime.corpus.documents)
            active = collect_current(state, runtime, phase)
            history.refresh(state.issues, number, reset_pending=False)
            ending = {history.key(row) for row in active}
            summary = {
                "round": number,
                "starting_missing": len(starting),
                "ending_missing": len(ending),
                "resolved": len(starting - ending),
                "new": len(ending - starting),
                "deferred": len(starting & ending),
                "visited_issues": len(decisions),
                "called_issues": len({row["tracking_id"] for row in responses}),
                "skipped_resolved": sum(row["status"] == "skipped_resolved" for row in decisions),
                "retries": sum(row["attempt"] > 1 for row in responses),
                "round_issue_worklist": worklist,
                "active_issues": len(active),
                **summarize_runs(responses),
            }
            rounds.append(summary)
            if round_root is not None:
                for filename, value in (
                    ("model.json", state.model),
                    ("verified.json", verified),
                    ("unresolved.json", state.issues),
                    ("gap-history.json", history.snapshot()),
                    ("report.json", summary),
                ):
                    write_json(round_root / filename, value)
                write_jsonl(round_root / "requests.jsonl", requests)
                write_jsonl(round_root / "responses.jsonl", responses)
                write_jsonl(round_root / "decisions.jsonl", decisions)
            if not active:
                stop_reason = "no_missing"
                break
    runtime.coordinator.update_model(state.model, state.issues, state.revision)
    inventory, _ = prune_invalid(state.model, runtime.corpus.documents)
    report = {
        "enabled": options.enabled,
        "rounds": len(rounds),
        "round_limit": rounds_limit,
        "round_reports": rounds,
        "stop_reason": stop_reason,
        "active_issues": len(active),
        "remaining_missing": len(active),
        "unresolved": len(state.issues),
        **summarize_runs(records),
    }
    if root is not None:
        write_json(root / "model.json", state.model)
        write_json(root / "unresolved.json", state.issues)
        write_json(root / "report.json", report)
        if phase == "objects":
            write_json(root / "inventory.json", inventory)
    return state, report, records


async def complete_objects(
    runtime: ExtractionRuntime,
    state: CandidateState,
    *,
    keywords: KeywordIndex,
    store: ExtractionStore,
    options: RepairOptions,
) -> tuple[CandidateState, dict, list[dict]]:
    """Complete base-object Issues before freezing the interaction inventory."""

    return await complete_phase(
        runtime, state, phase="objects", keywords=keywords, store=store, options=options
    )


async def complete_interactions(
    runtime: ExtractionRuntime,
    state: CandidateState,
    *,
    keywords: KeywordIndex,
    store: ExtractionStore,
    options: RepairOptions,
) -> tuple[CandidateState, dict, list[dict]]:
    """Complete interaction Issues after relationship linking."""

    return await complete_phase(
        runtime, state, phase="interactions", keywords=keywords, store=store, options=options
    )
