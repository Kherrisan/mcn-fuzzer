"""Scope review, concept partitions, and narrowly scoped reference updates."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace

from pydantic import ValidationError

from rsc.artifacts import write_json, write_jsonl

from . import agent_prompts
from .agent_runtime import RunBudget, encode
from .contract import BASE, COLLECTIONS
from .extraction import ExtractionRuntime, summarize_runs
from .keywords import KeywordIndex
from .merge import issue, sort_rows, stable_id, unique
from .models import (
    Channel,
    LinkMergeResponse,
    LinkPartitionResponse,
    Party,
    ProtocolMessage,
    Step,
    StepDependency,
)
from .options import LinkOptions
from .proposals import CandidateState, current_issues
from .registry import Registry, normalize_name
from .retrieval import RetrievalContext
from .store import ExtractionStore
from .tools import AgentDeps

ROW_TYPES = {
    "parties": Party,
    "messages": ProtocolMessage,
    "channels": Channel,
    "steps": Step,
    "step_dependencies": StepDependency,
}


def member_name(kind: str, row: dict) -> str:
    """Label one supplied object for a partition. This label is not stored on the object."""

    if kind == "step_dependencies":
        return f"{row.get('prior_step', '')} -> {row.get('dependent_step', '')}"
    return row.get("name") or kind


def public_row(row: dict) -> dict:
    return {key: deepcopy(value) for key, value in row.items() if key != "id"}


def partition_errors(output, expected_names: list[str]) -> list[str]:
    actual = [name for group in output.groups for name in group.member_names]
    actual.extend(entry.name for entry in output.excluded)
    if Counter(actual) != Counter(expected_names):
        return [
            "Groups and exclusions must cover each supplied name exactly once, "
            "repeating a shared name once per supplied object"
        ]
    return []


def assign_names(
    slots: list[int], names: dict[int, str], groups: list[list[str]]
) -> list[list[int]]:
    """Consume each supplied occurrence in order. Equal names are not treated as one object."""

    buckets: dict[str, list[int]] = defaultdict(list)
    for slot in slots:
        buckets[names[slot]].append(slot)
    assigned = []
    for group in groups:
        chosen = []
        for name in group:
            chosen.append(buckets[name].pop(0))
        assigned.append(chosen)
    return assigned


def assign_partition(output, entries: list[dict]) -> tuple[list[list[int]], list[int], list[str]]:
    """Resolve internal occurrence indexes before any unambiguous name fallback."""

    errors = partition_errors(output, [entry["name"] for entry in entries])
    supplied = {entry["slot"]: entry for entry in entries}
    destinations = [
        (list(group.member_names), list(group.member_indexes)) for group in output.groups
    ] + [([entry.name], [] if entry.index is None else [entry.index]) for entry in output.excluded]
    labels = defaultdict(set)
    for number, (names, _indexes) in enumerate(destinations):
        for name in names:
            labels[name].add(number)
    ambiguous = {
        name
        for name, locations in labels.items()
        if len(locations) > 1
        and len(
            {encode(entry["summary"]["definition"]) for entry in entries if entry["name"] == name}
        )
        > 1
    }
    reserved = set()
    resolved = []
    for names, indexes in destinations:
        if not indexes:
            if any(name in ambiguous for name in names):
                errors.append(
                    "Distinct same-name occurrences assigned to different destinations require "
                    "member_indexes or exclusion index matching the supplied summaries"
                )
            resolved.append(None)
            continue
        resolved.append(indexes)
        if len(names) != len(indexes):
            errors.append("member_indexes must have the same length and order as member_names")
            continue
        for name, index in zip(names, indexes, strict=True):
            if index not in supplied or supplied[index]["name"] != name:
                errors.append(
                    f"Index {index} does not identify a supplied occurrence named {name!r}"
                )
            if index in reserved:
                errors.append(f"Supplied occurrence index {index} is repeated")
            reserved.add(index)
    if errors:
        return [], [], errors
    remaining = defaultdict(list)
    for entry in entries:
        if entry["slot"] not in reserved:
            remaining[entry["name"]].append(entry["slot"])
    for number, (names, _indexes) in enumerate(destinations):
        if resolved[number] is not None:
            continue
        indexes = []
        for name in names:
            if not remaining[name]:
                return [], [], [f"No unassigned supplied occurrence remains for {name!r}"]
            indexes.append(remaining[name].pop(0))
        resolved[number] = indexes
    count = len(output.groups)
    return resolved[:count], [slots[0] for slots in resolved[count:]], []


def canonical_party_name(name, names: dict[str, str]):
    """Resolve only proven, unambiguous party names; leave unknown values unchanged."""

    if not isinstance(name, str):
        return name
    return names.get(normalize_name(name), name)


def rewrite_party_references(model: dict, names: dict[str, str]) -> dict:
    """Update party reference fields without changing names or descriptive prose."""

    result = deepcopy(model)
    for row in result.get("channels", []):
        row["endpoint_party_names"] = list(
            dict.fromkeys(
                canonical_party_name(name, names) for name in row.get("endpoint_party_names", [])
            )
        )
        for field in ("initiator_party_name", "acceptor_party_name"):
            row[field] = canonical_party_name(row.get(field), names)
        for source in row.get("endpoint_sources", []):
            source["party_name"] = canonical_party_name(source.get("party_name"), names)
    for row in result.get("steps", []):
        for field in ("sender_name", "receiver_name"):
            row[field] = canonical_party_name(row.get(field), names)
    return result


def merged_row(
    kind: str,
    members: list[dict],
    output: LinkMergeResponse,
) -> tuple[dict | None, list[str]]:
    """Validate merged structure; source-grounded semantic corrections belong to the Agent."""

    if output.status != "merged" or output.merged_object is None:
        return None, []
    row = output.merged_object.model_dump(mode="json", by_alias=True)
    if kind == "steps":
        row["id"] = members[0]["id"]
        row["name"] = row.get("name") or members[0]["name"]
    try:
        ROW_TYPES[kind].model_validate(row)
    except ValidationError as exc:
        return None, [f"Merged object does not match {kind}: {exc}"]
    # Preserve model-authored role corrections instead of applying an older party
    # map or treating extracted endpoint, direction and transport values as truth.
    if kind == "channels" and row["connection"] == "connectionless" and (
        row["initiator_party_name"] is not None or row["acceptor_party_name"] is not None
    ):
        return None, ["connectionless channels must have null connection roles"]
    return row, []


class Linker:
    def __init__(
        self,
        runtime: ExtractionRuntime,
        state: CandidateState,
        phase: str,
        keywords: KeywordIndex,
        store: ExtractionStore,
        options: LinkOptions,
        workers: int,
    ):
        self.runtime, self.state, self.phase = runtime, state, phase
        self.keywords, self.store, self.options, self.workers = keywords, store, options, workers
        self.partition_requests = 0
        self.records, self.partitions, self.decisions, self.summaries = [], [], [], []
        self.mapping = {}
        self.exclusions = []
        self.excluded_slots = defaultdict(set)
        self.party_names = {}
        self.party_name_updates = []
        self.coverage_complete = True
        self.final_groups = {}
        self.pending_scopes = []
        self.root = runtime.out / f"{phase}-link" if runtime.out is not None else None
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            write_json(self.root / "input-model.json", state.model)
        self.focuses = runtime.plan.focus_map()
        self._registry = None
        self._registry_revision = None
        self.input_counts = {kind: len(state.model[kind]) for kind in COLLECTIONS}
        runtime.coordinator.update_model(state.model, state.issues, state.revision)

    def registry(self) -> Registry:
        if self._registry_revision != self.state.revision:
            self._registry = self.state.registry(self.runtime.corpus.documents)
            self._registry_revision = self.state.revision
        return self._registry

    def problem(self, description: str, names=()):
        row = issue(description, names)
        self.state.issues = unique(self.state.issues + [row])

    def deps(self, state: CandidateState, stage: str, material=()) -> AgentDeps:
        registry = state.registry(self.runtime.corpus.documents)
        context = RetrievalContext(
            self.runtime.corpus,
            phase=self.phase,
            agent_options=self.runtime.agent_options,
            store=self.store,
            lookup=registry.root,
        )
        return AgentDeps(
            self.runtime.protocol,
            stage,
            self.phase,
            self.runtime.corpus,
            registry,
            self.keywords,
            context,
            self.runtime.events,
            options=self.runtime.agent_options,
        )

    def summary(self, kind: str, row: dict) -> dict:
        data = {
            "name": member_name(kind, row),
            "description": row.get("description"),
            "definition": public_row(row) if kind != "steps" else deepcopy(row),
        }
        if kind == "messages":
            data["associated_fields"] = [
                {"name": field["name"]} for field in row.get("associated_fields", [])
            ]
        if kind == "channels":
            data["endpoint_sources"] = deepcopy(row.get("endpoint_sources", []))
        if kind == "step_dependencies":
            data["condition"] = row.get("condition", "")
            data["field_relations"] = deepcopy(row.get("field_relations", []))
        return data

    async def partition_call(self, kind: str, entries: list[dict], mode: str):
        names = [entry["name"] for entry in entries]
        if self.partition_requests >= self.options.max_partition_calls:
            return None, "partition_call_limit"
        deps = self.deps(self.state, "link_partition")
        summaries = [{**entry["summary"], "index": entry["slot"]} for entry in entries]

        def check(output):
            _groups, _excluded, errors = assign_partition(output, entries)
            if output.excluded and kind not in BASE:
                errors.append("Only out-of-scope parties, messages or channels may be excluded")
            return errors

        remaining = self.options.max_partition_calls - self.partition_requests
        budget = RunBudget(
            replace(
                self.runtime.agent_options,
                max_model_requests=min(self.runtime.agent_options.max_model_requests, remaining),
            )
        )
        payload = deps.payload(
            kind=kind,
            summaries=summaries,
            model_revision=self.state.revision,
            prior_decisions=[],
        )
        limit = (
            self.runtime.limits.get(f"{self.phase}_link_partition")
            or self.runtime.limits["link_partition"]
        )
        run_id = f"link-partition:{self.phase}:{kind}:{len(self.partitions)}"
        record = await self.runtime.run(
            "link_partition",
            self.phase,
            LinkPartitionResponse,
            payload,
            run_id=run_id,
            deps=deps,
            output_check=check,
            budget=budget,
            instructions=agent_prompts.LINK_PARTITION,
            limits_override=limit,
            session=self.runtime.coordinator,
            new_input_max_bytes=self.options.partition_max_input_bytes,
        )
        self.partition_requests += max(1, record["request_count"])
        self.records.append(record)
        self.runtime.events.append("link.jsonl", record)
        self.partitions.append(
            {
                "kind": kind,
                "mode": mode,
                "member_names": list(names),
                "input_revision": self.state.revision,
                "record": record,
            }
        )
        if record["status"] != "ok":
            return None, record["end_reason"]
        data = record["data"]
        for problem in data["unresolved"]:
            self.problem(problem["description"], problem.get("object_names", []))
        groups, excluded_indexes, errors = assign_partition(
            LinkPartitionResponse.model_validate(data), entries
        )
        if errors:
            return None, "invalid_partition"
        excluded = data.get("excluded", [])
        for entry, slot in zip(excluded, excluded_indexes, strict=True):
            if slot in self.excluded_slots[kind]:
                continue
            self.excluded_slots[kind].add(slot)
            self.exclusions.append(
                {
                    "kind": kind,
                    "slot": slot,
                    "name": entry["name"],
                    "reason": entry["reason"],
                    "run_id": record["run_id"],
                    "input_revision": self.state.revision,
                    "definition": next(
                        deepcopy(item["summary"]["definition"])
                        for item in entries
                        if item["slot"] == slot
                    ),
                }
            )
        return groups, None

    async def partition(self, kind: str, entries: list[dict]) -> list[list[int]]:
        if not entries:
            return []
        by_slot = {entry["slot"]: entry for entry in entries}
        pages, groups = [], []
        queue = [entries]
        while queue:
            page = queue.pop(0)
            if len(page) == 1 and kind not in BASE:
                found, error = [[page[0]["slot"]]], None
            elif len(encode([entry["summary"] for entry in page]).encode()) > (
                self.options.partition_max_input_bytes
            ):
                found, error = None, "request_budget"
            else:
                found, error = await self.partition_call(kind, page, "within_page")
            if (
                found is None
                and error in {"request_budget", "context_length", "output_truncated"}
                and len(page) > 1
            ):
                midpoint = len(page) // 2
                queue[0:0] = [page[:midpoint], page[midpoint:]]
                continue
            if found is None:
                self.coverage_complete = False
                names = [entry["name"] for entry in page]
                self.pending_scopes.append(
                    {"kind": kind, "mode": "within_page", "member_names": names, "reason": error}
                )
                self.problem(f"Link partition unresolved: {error}", names)
                found = [[entry["slot"]] for entry in page]
            page_number = len(pages)
            pages.append(page)
            groups.extend({"ids": list(group), "pages": {page_number}} for group in found)
        reviewed = set()
        while True:
            pair = None
            for i, left in enumerate(groups):
                for j in range(i + 1, len(groups)):
                    right = groups[j]
                    if left["pages"] == right["pages"] and len(left["pages"]) == 1:
                        continue
                    identities = tuple(sorted(left["ids"] + right["ids"]))
                    if identities not in reviewed:
                        pair = i, j, identities
                        break
                if pair:
                    break
            if pair is None:
                break
            i, j, identities = pair
            if self.partition_requests >= self.options.max_partition_calls:
                self.coverage_complete = False
                names = [by_slot[slot]["name"] for group in groups for slot in group["ids"]]
                self.pending_scopes.append(
                    {
                        "kind": kind,
                        "mode": "cross_page",
                        "member_names": names,
                        "reason": "partition_call_limit",
                    }
                )
                self.problem(
                    "Cross-page concept comparison reached its call limit",
                    names,
                )
                break
            found, error = await self.partition_call(
                kind, [by_slot[slot] for slot in identities], "cross_page"
            )
            reviewed.add(identities)
            if found is None:
                self.coverage_complete = False
                names = [by_slot[slot]["name"] for slot in identities]
                self.pending_scopes.append(
                    {
                        "kind": kind,
                        "mode": "cross_page",
                        "member_names": names,
                        "reason": error,
                    }
                )
                self.problem(f"Cross-page concept comparison unresolved: {error}", names)
                continue
            provenance = groups[i]["pages"] | groups[j]["pages"]
            replacement = [{"ids": list(group), "pages": provenance} for group in found]
            groups = [
                group for index, group in enumerate(groups) if index not in {i, j}
            ] + replacement
            groups.sort(key=lambda group: group["ids"])
            for x, left in enumerate(replacement):
                for right in replacement[x + 1 :]:
                    reviewed.add(tuple(sorted(left["ids"] + right["ids"])))
        partition = [group["ids"] for group in sorted(groups, key=lambda group: group["ids"])]
        self.final_groups[kind] = partition
        return partition

    def publish(self, candidate: dict):
        self.state.model = {kind: sort_rows(kind, candidate[kind]) for kind in COLLECTIONS}
        self.state.issues = current_issues(
            self.state.model, self.state.issues, self.runtime.corpus.documents
        )
        self.state.revision += 1
        self._registry_revision = None
        self.runtime.coordinator.update_model(
            self.state.model, self.state.issues, self.state.revision
        )
        self.save()

    async def merge_group(self, kind: str, slots: list[int], frozen: list[dict]) -> dict:
        members = [deepcopy(frozen[slot]) for slot in slots]
        if kind == "steps":
            members.sort(key=lambda row: row.get("id") or "")
        deps = self.deps(self.state, "link_merge", members)
        names = [member_name(kind, row) for row in members]

        def check(output):
            _row, errors = merged_row(kind, members, output)
            return errors

        payload = deps.payload(
            kind=kind,
            member_names=names,
            members=[public_row(row) if kind != "steps" else deepcopy(row) for row in members],
        )
        limit = (
            self.runtime.limits.get(f"{self.phase}_link_merge") or self.runtime.limits["link_merge"]
        )
        records = []
        budget = RunBudget(self.runtime.agent_options)
        record = None
        for attempt in (1, 2):
            record = await self.runtime.run(
                "link_merge",
                self.phase,
                LinkMergeResponse,
                payload,
                run_id=f"link-merge:{self.phase}:{kind}:{stable_id('group', names)}:{attempt}",
                deps=deps,
                output_check=check,
                instructions=agent_prompts.LINK_MERGE,
                limits_override=limit,
                budget=budget,
                session=self.runtime.coordinator,
                new_input_max_bytes=self.options.merge_max_input_bytes,
            )
            record["attempt"] = attempt
            records.append(record)
            if (
                record["status"] == "ok"
                or attempt == 2
                or record["end_reason"]
                not in {"request_budget", "context_length", "output_truncated"}
            ):
                break
            if budget.model_requests >= self.runtime.agent_options.max_model_requests:
                break
            try:
                budget.remaining_seconds()
            except RuntimeError:
                break
            payload = deps.payload(
                kind=kind,
                member_names=names,
                members=[public_row(row) if kind != "steps" else deepcopy(row) for row in members],
                previous_feedback=record["error"],
            )
        return {"slots": slots, "members": members, "record": record, "records": records}

    async def link_kind(self, kind: str):
        frozen = [deepcopy(row) for row in self.state.model[kind]]
        entries = [
            {"slot": index, "name": member_name(kind, row), "summary": self.summary(kind, row)}
            for index, row in enumerate(frozen)
        ]
        self.summaries.extend({"kind": kind, **entry["summary"]} for entry in entries)
        groups = await self.partition(kind, entries)
        # A coordinator conversation has one writer and observes each committed group.
        merge_groups = [group for group in groups if len(group) > 1]
        removed = set(self.excluded_slots[kind])
        party_outcomes = {
            slot: None if slot in removed else row.get("name") for slot, row in enumerate(frozen)
        }
        if removed:
            candidate = deepcopy(self.state.model)
            candidate[kind] = [row for slot, row in enumerate(frozen) if slot not in removed]
            self.publish(candidate)
            by_run = defaultdict(list)
            for exclusion in self.exclusions:
                if exclusion["kind"] != kind:
                    continue
                by_run[exclusion["run_id"]].append(
                    {key: exclusion[key] for key in ("kind", "name", "reason")}
                )
                self.decisions.append(
                    {
                        "kind": kind,
                        "member_names": [exclusion["name"]],
                        "run_id": exclusion["run_id"],
                        "status": "excluded",
                        "reasons": [exclusion["reason"]],
                    }
                )
            for run_id, excluded in by_run.items():
                self.runtime.coordinator.record_commit(run_id, {}, changes={"excluded": excluded})
        replacements = []
        for index, group in enumerate(merge_groups):
            self.runtime.coordinator.update_model(
                self.state.model,
                self.state.issues,
                self.state.revision,
                pending_groups=[
                    {
                        "kind": kind,
                        "member_names": [member_name(kind, frozen[slot]) for slot in pending],
                    }
                    for pending in merge_groups[index:]
                ],
            )
            proposal = await self.merge_group(kind, group, frozen)
            slots, record, members = proposal["slots"], proposal["record"], proposal["members"]
            names = [member_name(kind, row) for row in members]
            self.records.extend(proposal["records"])
            for attempt in proposal["records"]:
                self.runtime.events.append("link.jsonl", attempt)
            decision = {
                "kind": kind,
                "member_names": names,
                "run_id": record["run_id"],
                "status": "deferred",
                "reasons": [],
            }
            if record["status"] == "ok" and record["data"]["status"] == "merged":
                output = LinkMergeResponse.model_validate(record["data"])
                row, errors = merged_row(kind, members, output)
                if row is not None and not errors:
                    removed.update(slots)
                    replacements.append(row)
                    decision["status"] = "merged"
                    if kind == "parties":
                        for slot in slots:
                            party_outcomes[slot] = row["name"]
                    if kind == "steps":
                        kept = row["id"]
                        for member in members:
                            if member.get("id") and member["id"] != kept:
                                self.mapping[member["id"]] = kept
                    for problem in output.unresolved:
                        self.problem(problem.description, problem.object_names)
                else:
                    decision["reasons"] = errors
                    self.problem("Link merge did not match the object schema", names)
            else:
                reason = record.get("error") or "merge deferred"
                decision["reasons"] = [reason]
                unresolved = (record.get("data") or {}).get("unresolved") or []
                if unresolved:
                    for problem in unresolved:
                        self.problem(
                            problem.get("description", reason), problem.get("object_names", names)
                        )
                else:
                    self.problem(str(reason), names)
            self.decisions.append(decision)
            if decision["status"] == "merged":
                candidate = deepcopy(self.state.model)
                candidate[kind] = [
                    row for slot, row in enumerate(frozen) if slot not in removed
                ] + replacements
                self.publish(candidate)
                self.runtime.coordinator.record_commit(
                    record["run_id"],
                    {kind: [row]},
                    (record.get("data") or {}).get("unresolved", []),
                )
            else:
                self.runtime.coordinator.record_commit(record["run_id"], {}, accepted=False)
                self.save()
        if kind == "parties":
            targets = defaultdict(set)
            for slot, before in enumerate(frozen):
                targets[normalize_name(before["name"])].add(party_outcomes[slot])
            target_counts = Counter(
                normalize_name(row["name"]) for row in self.state.model["parties"]
            )
            self.party_names = {
                before: next(iter(after))
                for before, after in targets.items()
                if len(after) == 1
                and None not in after
                and target_counts[normalize_name(next(iter(after)))] == 1
            }
            for before, after in targets.items():
                ambiguous_target = any(
                    name is not None and target_counts[normalize_name(name)] > 1 for name in after
                )
                if len(after) > 1 or ambiguous_target:
                    self.problem(
                        "Party name has multiple scope or identity outcomes; "
                        "references were left unchanged",
                        [before],
                    )
            self.party_name_updates = unique(
                [
                    {"from": row["name"], "to": self.party_names[normalize_name(row["name"])]}
                    for row in frozen
                    if normalize_name(row["name"]) in self.party_names
                    and row["name"] != self.party_names[normalize_name(row["name"])]
                ]
            )
            candidate = rewrite_party_references(self.state.model, self.party_names)
            if candidate != self.state.model:
                self.publish(candidate)
                self.runtime.coordinator.record_commit(
                    f"link-party-references:{self.phase}:{self.state.revision}",
                    {"channels": candidate["channels"], "steps": candidate["steps"]},
                    changes={"party_name_updates": self.party_name_updates},
                )
        self.runtime.coordinator.update_model(
            self.state.model, self.state.issues, self.state.revision, pending_groups=[]
        )
        self.save()

    def save(self):
        if self.root is None:
            return
        write_jsonl(self.root / "summaries.jsonl", self.summaries)
        write_jsonl(self.root / "partitions.jsonl", self.partitions)
        write_jsonl(self.root / "merge-decisions.jsonl", self.decisions)
        write_jsonl(self.root / "exclusions.jsonl", self.exclusions)
        write_json(self.root / "id-map.json", self.mapping)
        write_json(self.root / "party-name-map.json", self.party_names)
        write_json(self.root / "model.json", self.state.model)
        write_json(self.root / "unresolved.json", self.state.issues)

    def finish(self) -> tuple[CandidateState, dict]:
        report = {
            "phase": self.phase,
            "before_counts": self.input_counts,
            "after_counts": {kind: len(self.state.model[kind]) for kind in COLLECTIONS},
            "groups": sum(len(groups) for groups in self.final_groups.values()),
            "singletons": sum(
                len(group) == 1 for groups in self.final_groups.values() for group in groups
            ),
            "partition_requests": self.partition_requests,
            "partition_calls": sum(
                row["calls"] for row in self.records if row["stage"] == "link_partition"
            ),
            "partition_coverage": {
                "complete": self.coverage_complete,
                "checked_scopes": [
                    {"kind": row["kind"], "mode": row["mode"], "member_names": row["member_names"]}
                    for row in self.partitions
                    if row["record"]["status"] == "ok"
                ],
                "pending_scopes": self.pending_scopes,
            },
            "remaining_issues": len(self.state.issues),
            "merged_groups": sum(row["status"] == "merged" for row in self.decisions),
            "deferred_groups": sum(row["status"] == "deferred" for row in self.decisions),
            "excluded_objects": len(self.exclusions),
            "excluded_counts": dict(Counter(row["kind"] for row in self.exclusions)),
            "party_name_updates": self.party_name_updates,
            "unresolved": len(self.state.issues),
            **summarize_runs(self.records),
        }
        self.runtime.coordinator.update_model(
            self.state.model, self.state.issues, self.state.revision
        )
        self.save()
        if self.root is not None:
            write_json(self.root / "report.json", report)
        return self.state, report


async def link_objects(
    runtime: ExtractionRuntime,
    state: CandidateState,
    *,
    keywords: KeywordIndex,
    store: ExtractionStore,
    options: LinkOptions,
    workers: int,
) -> tuple[CandidateState, dict]:
    linker = Linker(runtime, state, "objects", keywords, store, options, workers)
    for kind in BASE:
        await linker.link_kind(kind)
    return linker.finish()


async def link_interactions(
    runtime: ExtractionRuntime,
    state: CandidateState,
    *,
    keywords: KeywordIndex,
    store: ExtractionStore,
    options: LinkOptions,
    workers: int,
) -> tuple[CandidateState, dict]:
    linker = Linker(runtime, state, "interactions", keywords, store, options, workers)
    await linker.link_kind("steps")
    await linker.link_kind("step_dependencies")
    return linker.finish()
