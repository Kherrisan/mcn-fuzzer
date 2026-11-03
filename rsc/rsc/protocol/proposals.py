"""Validate and atomically commit extraction or Issue-repair proposals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .contract import BASE, COLLECTIONS, INTERACTIONS
from .history import GapHistory
from .ids import AllocationKey, GlobalIdAllocator, IdConsistencyError
from .merge import canonical, issue, semantic, unique
from .registry import NESTED, Registry, normalize_name
from .validate import object_errors, prune_invalid

NESTED_LISTS = set(NESTED) | {"endpoint_sources"}
ADDITIVE_LISTS = {"endpoint_party_names"}


class ProposalError(ValueError):
    """A candidate failed validation and must not change visible state."""


def empty_model() -> dict:
    return {kind: [] for kind in COLLECTIONS}


def apply_id_map(value: dict, mapping: dict[str, str]) -> dict:
    """Rewrite Python step IDs only. Name strings are not replaced."""

    result = deepcopy(value)
    for row in result.get("steps", []):
        if row.get("id") in mapping:
            row["id"] = mapping[row["id"]]
    return result


def merge_compatible(left: dict, right: dict) -> dict:
    """Combine exactly compatible definitions; preserve independent nested provenance."""

    merged = deepcopy(left)
    for name in NESTED_LISTS:
        if name not in left:
            continue
        available = {canonical(semantic(row)): row for row in right.get(name, [])}
        merged[name] = [
            merge_compatible(row, available.get(canonical(semantic(row)), row))
            for row in left[name]
        ]
    return merged


def normalize_duplicates(data: dict) -> tuple[dict, dict[str, str]]:
    """Unify exact same-response duplicates; semantic ambiguity is left for link."""

    result = deepcopy(data)
    for kind in COLLECTIONS:
        groups = {}
        output = []
        for row in result.get(kind, []):
            signature = canonical(semantic(row))
            if signature in groups:
                continue
            groups[signature] = row
            output.append(row)
        result[kind] = output
    return result, {}


def additive_errors(old: Any, new: Any, path: str = "") -> list[str]:
    """Check append-only extraction updates, preserving exact existing fact wording."""

    errors = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, before in old.items():
            after = new.get(key)
            current = f"{path}.{key}"
            if key in NESTED_LISTS and isinstance(before, list) and isinstance(after, list):
                by_id = {item.get("id", canonical(semantic(item))): item for item in after}
                for item in before:
                    identity = item.get("id", canonical(semantic(item)))
                    if identity not in by_id:
                        errors.append(
                            f"{current}: cannot delete or replace existing extraction entries"
                        )
                    else:
                        errors.extend(additive_errors(item, by_id[identity], current))
            elif key in ADDITIVE_LISTS and isinstance(before, list) and isinstance(after, list):
                if not {canonical(v) for v in before} <= {canonical(v) for v in after}:
                    errors.append(f"{current}: cannot remove existing entries")
            elif before != after:
                errors.append(f"{current}: scalar or identity changes require Issue completion")
    elif old != new:
        errors.append(f"{path}: incompatible replacement")
    return errors


def reference_kind_errors(value: Any, registry: Registry) -> list[str]:
    """Name references are not checked for existence or owner."""

    del value, registry
    return []


def current_issues(model: dict, persistent: list[dict], documents: dict) -> list[dict]:
    """Recompute structural gaps without discarding unresolved semantic reports."""

    items = [
        {"description": row["description"], "object_names": list(row.get("object_names", []))}
        for row in persistent
        if isinstance(row, dict) and row.get("description")
    ]
    for identity, errors in object_errors(model, documents).items():
        for error in errors:
            names = (
                []
                if identity.startswith(
                    (
                        "parties[",
                        "messages[",
                        "channels[",
                        "steps[",
                        "step_dependencies[",
                    )
                )
                else [identity]
            )
            items.append(issue(error, names))
    return unique(items)


@dataclass
class CandidateState:
    """A single writer owns this state; other lists receive separate copies."""

    model: dict = field(default_factory=empty_model)
    issues: list[dict] = field(default_factory=list)
    revision: int = 0
    redirects: dict[str, str] = field(default_factory=dict)
    origins: dict[str, dict] = field(default_factory=dict)
    history: GapHistory = field(default_factory=GapHistory)

    def registry(self, documents: dict) -> Registry:
        # Recursively invalid references cannot be advertised as reusable definitions.
        valid, _ = prune_invalid(self.model, documents)
        valid_sigs = {canonical(semantic(row)) for kind in COLLECTIONS for row in valid[kind]}
        invalid = set()
        for kind in COLLECTIONS:
            for index, row in enumerate(self.model.get(kind, [])):
                if canonical(semantic(row)) not in valid_sigs:
                    if kind == "steps" and row.get("id"):
                        invalid.add(row["id"])
                    else:
                        invalid.add(f"{kind}:{index}:{row.get('name', '')}")
        return Registry(
            self.model,
            redirects=self.redirects,
            invalid_ids=invalid,
            revision=self.revision,
        )

    def snapshot(self) -> dict:
        return deepcopy(self.model)


def _existing_named_id(registry: Registry, kind: str, name: str) -> str | None:
    """Resolve one exact existing root name without guessing across duplicates."""

    matches = {
        identity
        for identity in registry.name_index.get(normalize_name(name), set())
        if registry.kinds.get(identity) == kind
    }
    return next(iter(matches)) if len(matches) == 1 else None


def named_matches(registry: Registry, kind: str, name: str) -> list[str]:
    """Own-name matches of one kind. Field names and other kinds are not guesses."""

    key = normalize_name(name or "")
    return sorted(
        identity
        for identity in registry.name_index.get(key, ())
        if registry.kinds.get(identity) == kind
        and normalize_name(registry.rows[identity].get("name", "")) == key
    )


def dependency_key(row: dict) -> tuple[str, str, str]:
    return (
        normalize_name(row.get("prior_step", "")),
        normalize_name(row.get("dependent_step", "")),
        normalize_name(row.get("condition", "")),
    )


def materialize_response(data: dict, registry: Registry) -> dict:
    """Keep model-authored names. Reuse a unique step ID, otherwise assign a local one."""

    result = deepcopy(data)
    used = set()
    for index, row in enumerate(result.get("steps", [])):
        matches = [
            identity
            for identity in named_matches(registry, "steps", row.get("name", ""))
            if identity not in used
        ]
        if len(matches) == 1:
            row["id"] = matches[0]
            used.add(matches[0])
        elif not str(row.get("id") or "").startswith("local:"):
            row["id"] = f"local:steps:{index + 1}"
    cleaned = []
    for problem in result.get("unresolved", []):
        if not isinstance(problem, dict):
            continue
        cleaned.append(
            {
                "description": problem.get("description") or "unspecified issue",
                "object_names": list(problem.get("object_names") or []),
            }
        )
    result["unresolved"] = cleaned
    return result


class ProposalValidator:
    """Pure validation against one visible revision, before any ID is reserved."""

    def __init__(
        self,
        state: CandidateState,
        deps,
        *,
        repair: bool = False,
        editable_ids: set[str] | None = None,
    ) -> None:
        self.state, self.deps, self.repair = state, deps, repair
        self.editable_ids = set(editable_ids or ())
        self.allowed = (
            BASE if deps.phase == "objects" else COLLECTIONS if repair else INTERACTIONS
        )
        self.registry = deps.registry
        self.editable_definitions = (
            {
                identity
                for value in self.editable_ids
                for identity in (
                    {value} if value in self.registry.rows else self.registry.owners_for_ref(value)
                )
            }
            if editable_ids is not None
            else set(self.registry.rows)
        )
        self._positions = {}
        for kind in COLLECTIONS:
            for index, current in enumerate(state.model.get(kind, [])):
                identity = (
                    current.get("id")
                    if kind == "steps" and current.get("id")
                    else f"{kind}:{index}:{current.get('name', '')}"
                )
                self._positions[identity] = index
        self.before = object_errors(state.model, deps.corpus.documents)
        self.candidates = []
        self.repair_selections = []

    def _token(self, kind: str, row: dict) -> str | None:
        if kind == "steps":
            return row.get("id") or row.get("name")
        if kind == "step_dependencies":
            prior, dependent = row.get("prior_step"), row.get("dependent_step")
            if prior and dependent:
                return f"{prior} -> {dependent}"
            return None
        return row.get("name")

    def _locate(self, kind: str, rows: list[dict], row: dict) -> int | None:
        if kind == "steps":
            identity = row.get("id")
            matches = [index for index, current in enumerate(rows) if current.get("id") == identity]
            return matches[0] if len(matches) == 1 else None
        if kind == "step_dependencies":
            key = dependency_key(row)
            matches = [
                index for index, current in enumerate(rows) if dependency_key(current) == key
            ]
            return matches[0] if len(matches) == 1 else None
        name = normalize_name(row.get("name", ""))
        matches = [
            index
            for index, current in enumerate(rows)
            if normalize_name(current.get("name", "")) == name
        ]
        return matches[0] if len(matches) == 1 else None

    def _label(self, kind: str, row: dict) -> str:
        return self._token(kind, row) or kind

    def _repair_index(self, kind: str, row: dict) -> tuple[int | None, str | None, str | None]:
        """Select one scoped, fully read snapshot row without adding a same-name repair."""

        if kind == "step_dependencies":
            matches = [
                identity
                for identity, current in self.registry.rows.items()
                if self.registry.kinds[identity] == kind
                and dependency_key(current)[:2] == dependency_key(row)[:2]
            ]
        else:
            matches = named_matches(self.registry, kind, row.get("name", ""))
        if not matches:
            return None, None, None
        editable = [identity for identity in matches if identity in self.editable_definitions]
        label = self._label(kind, row)
        if not editable:
            return (
                None,
                None,
                f"{label}: the existing definition is outside this Issue's edit scope",
            )
        delivered = getattr(self.deps, "delivered_definition_ids", None)
        if delivered is None:
            # Compatibility for callers with an older dependency object is safe
            # only when a name identifies exactly one complete definition.
            delivered = {
                identity
                for identity in editable
                if len(matches) == 1
                and self._token(kind, self.registry.rows[identity])
                in self.deps.delivered_object_ids
            }
        read = [identity for identity in editable if identity in delivered]
        if not read:
            return None, None, f"{label}: read the complete definition before editing it"
        selected = read
        basis = "issue_scope_and_full_definition"
        if len(selected) > 1:
            exact = [
                identity
                for identity in selected
                if semantic(self.registry.rows[identity]) == semantic(row)
            ]
            if len(exact) == 1:
                selected = exact
                basis = "exact_definition"
            else:
                fields = {
                    "channels": (
                        "connection",
                        "transport",
                        "endpoint_party_names",
                        "initiator_party_name",
                        "acceptor_party_name",
                    ),
                    "steps": ("message_name", "channel_name", "sender_name", "receiver_name"),
                }.get(kind, ())

                def known(value):
                    if value is None or value == "" or value == []:
                        return None
                    if isinstance(value, list):
                        return frozenset(normalize_name(item) for item in value)
                    return normalize_name(value) if isinstance(value, str) else value

                compatible = [
                    identity
                    for identity in selected
                    if all(
                        known(self.registry.rows[identity].get(key)) is None
                        or known(row.get(key)) is None
                        or known(self.registry.rows[identity][key]) == known(row[key])
                        for key in fields
                    )
                ]
                if len(compatible) == 1:
                    selected = compatible
                    basis = "unchanged_communication_identity"
        if len(selected) != 1:
            return (
                None,
                None,
                f"{label}: multiple editable definitions remain; read and distinguish the "
                "intended definition, or leave the Issue unresolved instead of adding a copy",
            )
        identity = selected[0]
        self.repair_selections.append(
            {"kind": kind, "name": row.get("name"), "target": identity, "basis": basis}
        )
        return self._positions[identity], identity, None

    def prepare(self, output: BaseModel | dict) -> tuple[dict, dict, dict]:
        raw = (
            output.model_dump(mode="json", by_alias=True)
            if isinstance(output, BaseModel)
            else deepcopy(output)
        )
        data = materialize_response(raw, self.registry)
        self.repair_selections = []
        errors = []
        step_ids = [row.get("id") for row in data.get("steps", []) if row.get("id")]
        if len(step_ids) != len(set(step_ids)):
            errors.append("step IDs must be unique within one response")
        if errors:
            return (
                data,
                self.state.snapshot(),
                {"status": "invalid", "hard_errors": errors, "missing": []},
            )
        data, duplicate_map = normalize_duplicates(data)
        data = apply_id_map(data, {key: value for key, value in self.registry.redirects.items()})
        candidate = self.state.snapshot()
        repaired = set()
        for kind in COLLECTIONS:
            if kind not in self.allowed and data.get(kind):
                errors.append(f"{kind}: definitions are not editable in this stage")
            rows = list(candidate.get(kind, []))
            for row in data.get(kind, []):
                label = self._label(kind, row)
                for name in NESTED:
                    signatures = [canonical(semantic(item)) for item in row.get(name, [])]
                    if len(set(signatures)) != len(signatures):
                        errors.append(f"{label}: duplicate nested facts need explicit linking")
                if self.repair:
                    index, target, error = self._repair_index(kind, row)
                    if error:
                        errors.append(error)
                        continue
                    if target is not None:
                        if target in repaired:
                            errors.append(
                                f"{label}: return one replacement per existing definition"
                            )
                            continue
                        repaired.add(target)
                        if kind == "steps":
                            row["id"] = self.registry.rows[target]["id"]
                else:
                    index = self._locate(kind, rows, row)
                if kind == "steps" and str(row.get("id", "")).startswith("local:"):
                    if any(
                        self.registry.kinds.get(known_id) == "steps"
                        and semantic(known) == semantic(row)
                        for known_id, known in self.registry.rows.items()
                    ):
                        errors.append(f"{label}: exact existing step must reuse its name")
                    rows.append(row)
                    continue
                if index is None:
                    if all(semantic(current) != semantic(row) for current in rows):
                        rows.append(row)
                    continue
                previous = rows[index]
                token = self._token(kind, previous)
                if token and token not in self.deps.delivered_object_ids:
                    errors.append(f"{label}: read the complete definition before editing it")
                if self.repair:
                    # A scoped, fully read channel replacement may correct or
                    # remove duplicate and unsupported endpoint parameter sources.
                    for nested in NESTED:
                        # A scoped, fully read message replacement may correct field
                        # ownership or remove fields outside the interaction model.
                        if kind == "messages" and nested == "associated_fields":
                            continue
                        old_nested = {
                            canonical(semantic(item)) for item in previous.get(nested, [])
                        }
                        new_nested = {canonical(semantic(item)) for item in row.get(nested, [])}
                        if not old_nested <= new_nested:
                            errors.append(f"{label}: repair cannot silently delete nested facts")
                else:
                    errors.extend(additive_errors(previous, row, label))
                rows[index] = row
            candidate[kind] = rows
        missing = []
        for identity, messages in object_errors(candidate, self.deps.corpus.documents).items():
            for message in messages:
                if message in self.before.get(identity, []):
                    continue
                errors.append(f"{identity}: {message}")
        return (
            data,
            candidate,
            {
                "status": "invalid" if errors else "ok",
                "hard_errors": sorted(set(errors)),
                "missing": missing,
                "duplicate_id_map": duplicate_map,
                "repair_targets": deepcopy(self.repair_selections),
            },
        )

    def check(self, output: BaseModel | dict) -> dict:
        # Only complete typed proposals can later be recovered after a failed run.
        data, _, feedback = self.prepare(output)
        if isinstance(output, BaseModel) and (not self.candidates or self.candidates[-1] != data):
            self.candidates.append(deepcopy(data))
        return feedback

    def recover(self) -> dict | None:
        """Keep each independently valid object. Name misses do not reject the rest."""

        for original in reversed(self.candidates):
            selected = {kind: [] for kind in COLLECTIONS}
            selected["unresolved"] = []
            rejected = []
            for kind in COLLECTIONS:
                for row in original.get(kind, []):
                    proposed = deepcopy(selected)
                    proposed[kind].append(deepcopy(row))
                    feedback = self.prepare(proposed)[2]
                    if feedback["status"] == "ok":
                        selected = proposed
                    else:
                        rejected.append(
                            {
                                "object_names": [row["name"]] if row.get("name") else [],
                                "reasons": feedback["hard_errors"],
                            }
                        )
            for problem in original.get("unresolved", []):
                proposed = deepcopy(selected)
                proposed["unresolved"].append(deepcopy(problem))
                feedback = self.prepare(proposed)[2]
                if feedback["status"] == "ok":
                    selected = proposed
                else:
                    rejected.append(
                        {
                            "description": problem.get("description"),
                            "object_names": list(problem.get("object_names") or []),
                            "reasons": feedback["hard_errors"],
                        }
                    )
            if any(selected.get(kind) for kind in COLLECTIONS) or selected["unresolved"]:
                return {"data": selected, "rejected": rejected}
        return None

    def output_errors(self, output: BaseModel | dict) -> list[str]:
        result = self.check(output)
        return result["hard_errors"]


async def commit_proposal(
    validator: ProposalValidator,
    output: BaseModel | dict,
    allocator: GlobalIdAllocator,
    *,
    origin: str,
    list_id: str | None,
    sequence: int | None = None,
) -> dict:
    """Reserve new step IDs in one batch, then publish one private state revision."""

    data, candidate, feedback = validator.prepare(output)
    if feedback["status"] != "ok":
        raise ProposalError("; ".join(feedback["hard_errors"]))
    state, deps = validator.state, validator.deps
    start_revision = state.revision
    keys = {}
    for row in data.get("steps", []):
        identity = row.get("id")
        if isinstance(identity, str) and identity.startswith("local:"):
            keys[identity] = AllocationKey(
                allocator.protocol, "steps", deps.stage, list_id, origin, identity
            )
    allocations = await allocator.reserve_many(keys.values())
    mapping = {identity: allocations[key] for identity, key in keys.items()}
    data = apply_id_map(data, mapping)
    if state.revision != start_revision:
        raise IdConsistencyError("candidate revision changed during an atomic proposal commit")
    # Publish precisely the validated snapshot placement. Repeating name lookup
    # here would append an ambiguous repair or replace another same-name row.
    candidate = apply_id_map(candidate, mapping)
    problems = object_errors(candidate, deps.corpus.documents)
    for identity, messages in problems.items():
        for message in messages:
            if message not in validator.before.get(identity, []):
                raise ProposalError(f"allocated candidate is invalid: {identity}: {message}")
    accepted_issues = [
        issue(row["description"], row.get("object_names", []))
        for row in data.get("unresolved", [])
        if isinstance(row, dict) and row.get("description")
    ]
    origins = deepcopy(state.origins)
    for kind in COLLECTIONS:
        for index, row in enumerate(data.get(kind, [])):
            key = row.get("id") or f"{kind}:{row.get('name', '')}:{index}"
            origins.setdefault(key, {"sequence": sequence, "origin": origin, "stage": deps.stage})
    state.model = candidate
    state.issues = current_issues(candidate, state.issues + accepted_issues, deps.corpus.documents)
    state.origins = origins
    state.revision += 1
    return {
        "items": {kind: deepcopy(data.get(kind, [])) for kind in COLLECTIONS},
        "reused_names": [],
        "unresolved": accepted_issues,
        "id_map": mapping,
        "model_revision": state.revision,
        "repair_targets": deepcopy(validator.repair_selections),
    }
