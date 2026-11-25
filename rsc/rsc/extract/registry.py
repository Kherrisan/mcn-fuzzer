"""Batch workspaces, evidence capture and registry checks."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from rsc.protocol.models import (
    Channel,
    Party,
    ProtocolMessage,
    State,
    Step,
    StepDependency,
    StrictModel,
)
from rsc.protocol.references import duplicate_issues, reference_issues
from rsc.protocol.validate import validate_protocol
from rsc.workflow.store import dumps

from .validation import COVERAGE_STAGES, RegistryIndex

ORDER = ("parties", "channels", "messages", "states", "steps", "step_dependencies")
# The first three categories are short and cross-reference each other, so one pass
# writes them together. States and dependencies describe one interaction with its
# step, so they are not scanned separately either: the steps pass writes that unit
# in one sitting.
SCAN_KINDS = ("basics", "steps")
SCAN_WRITES = {
    "basics": ("parties", "channels", "messages"),
    "steps": ("steps", "states", "step_dependencies"),
}
# Older checkpoints keep their own layout so each task stays resumable.
LEGACY_SCAN_KINDS = ("parties", "channels", "messages", "steps")
Kind = Literal["parties", "channels", "messages", "states", "steps", "step_dependencies"]
ADAPTERS = {
    kind: TypeAdapter(model)
    for kind, model in zip(
        ORDER, (Party, Channel, ProtocolMessage, State, Step, StepDependency), strict=True
    )
}


class SourceBlock(StrictModel):
    document_id: str
    block_id: str
    start: int | None = None
    end: int | None = None


class Evidence(SourceBlock):
    field: str = Field(default="", description="JSON Pointer relative to the entity value.")
    note: str = Field(default="", description="Explain how the source supports this field.")


class Addition(StrictModel):
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    aliases: list[str] = Field(default_factory=list)
    mentions: list[SourceBlock] = Field(default_factory=list)


class PartyAddition(Addition):
    kind: Literal["parties"]
    value: Party


class ChannelAddition(Addition):
    kind: Literal["channels"]
    value: Channel


class MessageAddition(Addition):
    kind: Literal["messages"]
    value: ProtocolMessage


class StateAddition(Addition):
    kind: Literal["states"]
    value: State


class StepAddition(Addition):
    kind: Literal["steps"]
    value: Step = Field(
        description=(
            "One wire transmission or network action. For processing, list concrete inputs in "
            "reads and outputs in writes, and state the trigger, behavior and failure in rule. "
            "Use on_receive on the corresponding send for receiver-side work."
        )
    )


class DependencyAddition(Addition):
    kind: Literal["step_dependencies"]
    value: StepDependency


EntityAddition = Annotated[
    PartyAddition
    | ChannelAddition
    | MessageAddition
    | StateAddition
    | StepAddition
    | DependencyAddition,
    Field(discriminator="kind"),
]


class FieldAddition(StrictModel):
    name: Annotated[str, Field(min_length=1)] = Field(description="Exact name of the added field.")
    before: str = Field(
        default="",
        description="Exact name of the field it goes before; empty appends to the end.",
    )


class FieldRename(StrictModel):
    name: Annotated[str, Field(min_length=1)] = Field(description="Exact existing field name.")
    new_name: Annotated[str, Field(min_length=1)] = Field(
        description="Name that replaces it in place.",
    )


class AssociatedFieldsDelta(StrictModel):
    """Name-based edit of one message's associated_fields, applied by the write path."""

    remove: list[str] = Field(default_factory=list, description="Exact names to drop.")
    replace: list[FieldRename] = Field(
        default_factory=list, description="In-place renames, keeping the current position."
    )
    add: list[FieldAddition] = Field(
        default_factory=list, description="New names, in submission order."
    )


class EntityUpdate(StrictModel):
    id: str = Field(description="Existing entity ID returned by registry tools.")
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    patch: dict[str, Any] = Field(
        default_factory=dict,
        description="Object mapping changed top-level fields to their full replacement values.",
    )
    associated_fields_delta: AssociatedFieldsDelta | None = Field(
        default=None,
        description=(
            "Optional name-based edit of a message's associated_fields; mutually exclusive "
            "with patch.associated_fields."
        ),
    )
    aliases: list[str] | None = None
    withdraw_reason: str = ""
    mentions: list[SourceBlock] = Field(default_factory=list)


class Unresolved(StrictModel):
    description: Annotated[str, Field(min_length=1)]
    entity_ids: list[str]
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    missing_information: str = ""
    check_refs: list[str] = Field(
        default_factory=list,
        description="Exact check_ref strings from validate_entities, only for listed checks.",
    )


@dataclass(frozen=True)
class Assignment:
    id: str
    phase: Literal["scan", "category", "review", "gap", "global"]
    kind: str | None = None
    # Set when one gap assignment covers a whole document instead of one batch.
    document_id: str | None = None


def scan_writes(assignment):
    """Entity kinds one scan assignment may write."""
    if assignment.kind is None:
        return ()
    return SCAN_WRITES.get(assignment.kind, (assignment.kind,))


def identity(row):
    value = row["value"]
    return value.get("name") or f"{value['prior_step']} → {value['dependent_step']}"


def summary(row):
    result = {
        "id": row["id"],
        "kind": row["kind"],
        "name": identity(row),
        "aliases": row["aliases"],
    }
    if row["kind"] == "states":
        result["party_name"] = row["value"]["party_name"]
    return result


def normalized(kind, value):
    return ADAPTERS[kind].dump_python(
        ADAPTERS[kind].validate_python(value),
        mode="json",
        by_alias=True,
    )


def delta_fields(fields, delta):
    """Rebuild one message's full ordered field list from a name-based delta.

    Removals disappear, renames stay in their original position, and additions land in
    submission order before their still-existing anchor (or at the end without one). The
    caller then feeds the result through the normal full-list write path.
    """
    if not (delta.remove or delta.replace or delta.add):
        raise ValueError("associated_fields_delta needs at least one add, replace or remove")
    names = [field["name"] for field in fields]
    if len(set(names)) != len(names):
        raise ValueError("the existing associated_fields names are not unique; submit the list")
    removed = list(delta.remove)
    if len(set(removed)) != len(removed):
        raise ValueError("each remove target may appear only once")
    for name in removed:
        if name not in names:
            raise ValueError(f"remove target {name!r} is not in associated_fields")
    renamed = {}
    targets = []
    for item in delta.replace:
        if item.name not in names:
            raise ValueError(f"replace target {item.name!r} is not in associated_fields")
        if item.name in removed:
            raise ValueError(f"{item.name!r} cannot be both removed and replaced")
        if item.new_name in renamed.values() or item.new_name in targets:
            raise ValueError(f"replace target {item.new_name!r} is used more than once")
        renamed[item.name] = item.new_name
        targets.append(item.new_name)
    added = [item.name for item in delta.add]
    if len(set(added)) != len(added):
        raise ValueError("each added name may appear only once")
    result = []
    for field in fields:
        name = field["name"]
        if name in removed:
            continue
        result.append({"name": renamed.get(name, name)})
    for item in delta.add:
        if not item.before:
            result.append({"name": item.name})
            continue
        index = next(
            (position for position, field in enumerate(result) if field["name"] == item.before),
            None,
        )
        if index is None:
            raise ValueError(f"add anchor {item.before!r} is not in the resulting fields")
        result.insert(index, {"name": item.name})
    final = [field["name"] for field in result]
    duplicates = sorted({name for name in final if final.count(name) > 1})
    if duplicates:
        raise ValueError(f"names already exist or repeat after this delta: {duplicates}")
    return result


def moved_field_evidence(evidence, before, after):
    """Index-based evidence paths that would land on another field after a delta."""

    moved = []
    for entry in evidence:
        parts = entry["field"].strip("/").split("/")
        if parts[0] != "associated_fields" or len(parts) < 2 or not parts[1].isdecimal():
            continue
        index = int(parts[1])
        old = before[index]["name"] if index < len(before) else None
        new = after[index]["name"] if index < len(after) else None
        if old != new:
            moved.append(entry["field"])
    return moved


def migrate_registry(data):
    """Normalize saved unresolved records and remove legacy entity versions."""
    records = data["entities"]
    migrated = False
    for issue in data["unresolved"].values():
        for key in ("deferred_kind", "disposition"):
            if key in issue:
                issue.pop(key)
                migrated = True
        if (versions := issue.pop("check_revisions", None)) is not None:
            if versions:
                issue["check_entity_ids"] = sorted(versions)
                if any(
                    records.get(key, {}).get("revision") != rev
                    for key, rev in versions.items()
                ):
                    issue["invalidated"] = True
            migrated = True

    archived = [
        row["entity"]
        for bucket in ("redirects", "withdrawn")
        for row in data[bucket].values()
        if "entity" in row
    ]
    for row in [*records.values(), *archived]:
        if "revision" in row:
            row.pop("revision")
            migrated = True
    return migrated


def rewrite_references(before, after, redirects):
    """Rewrite only resolved references that the edit did not explicitly replace."""
    bindings = {}
    reference_issues(before, resolved=bindings)

    def existing(value, path):
        for part in path:
            value = value[part]
        return value

    changed = set()
    for (owner, path), target in bindings.items():
        canonical = redirects.get(target, target)
        if owner not in after or canonical not in after:
            continue
        old_name = identity(before[target])
        new_name = identity(after[canonical])
        if old_name == new_name:
            continue
        old_value = existing(before[owner]["value"], path)
        try:
            parent = existing(after[owner]["value"], path[:-1])
            if parent[path[-1]] != old_value:
                continue
        except (KeyError, IndexError, TypeError):
            continue
        parent[path[-1]] = f"state.{new_name}" if before[target]["kind"] == "states" else new_name
        changed.add(owner)

    # Qualified field names in dependency edges follow their referenced message.
    for owner, row in before.items():
        if row["kind"] != "step_dependencies" or owner not in after:
            continue
        for step_key, field_key in (
            ("prior_step", "prior_field"),
            ("dependent_step", "dependent_field"),
        ):
            step = bindings.get((owner, (step_key,)))
            message = bindings.get((step, ("message_name",)))
            canonical = redirects.get(message, message)
            if canonical not in after:
                continue
            old_prefix = f"{identity(before[message])}."
            new_prefix = f"{identity(after[canonical])}."
            if old_prefix == new_prefix:
                continue
            for index, relation in enumerate(row["value"]["field_relations"]):
                target = after[owner]["value"]["field_relations"]
                field = relation[field_key]
                if (
                    index < len(target)
                    and field.startswith(old_prefix)
                    and target[index][field_key] == field
                ):
                    target[index][field_key] = new_prefix + field[len(old_prefix) :]
                    changed.add(owner)
    return changed


class Workspace:
    def __init__(self, sources, protocol, previous, assignment, history=None):
        self.sources = sources
        self.protocol = protocol
        self.assignment = assignment
        self.data = deepcopy(previous)
        # Sealed records of this run: the steps pass reads the basics record of its own
        # batch from here. Older checkpoints may not carry one; then nothing is asked.
        self.history = tuple(history or ())
        # One steps reminder per workspace, so a retry or a repeat submission cannot
        # turn the reminder into a gate.
        self.reconsidered = False
        self.receipts = {}
        self.mutations = []
        self.initial_issues = set(previous["unresolved"])
        self.completion = None
        self.repair_start_count = 0
        self.gaps = []
        self.reject_count = 0
        # Category Fix scheduling: repair sites that stalled on an unchanged blocked
        # signature, and the group or issue reads this attempt actually made. Both live
        # only in memory; neither is written to the checkpoint.
        self.deferred_groups = {}
        self.read_groups = set()
        self._index = None

    @property
    def records(self):
        return self.data["entities"]

    @property
    def index(self):
        if self._index is None:
            self._index = RegistryIndex(self.records)
        return self._index

    @property
    def repairable_categories(self):
        if self.assignment.phase in ("global", "gap") or self.assignment.kind is None:
            return ORDER
        if self.assignment.phase == "review":
            return ()
        # A scan pass owns every category up to the last one it writes: the merged
        # basics pass owns the first three, the interaction unit owns all six.
        scope = scan_writes(self.assignment)
        return ORDER[: ORDER.index(scope[-1]) + 1]

    @property
    def write_scope(self):
        """Kinds this assignment may actually write."""
        if self.assignment.phase == "scan":
            return scan_writes(self.assignment)
        return self.repairable_categories

    @property
    def ready(self):
        if self.assignment.phase == "scan" and self.assignment.kind is not None:
            return self.repairable_categories[:-1]
        return self.repairable_categories

    def deferred_checks(self):
        return [
            rule
            for rule, (start, end) in COVERAGE_STAGES.items()
            if start in self.ready and end not in self.ready
        ]

    @property
    def created_issues(self):
        return self.data["unresolved"].keys() - self.initial_issues

    def batch_holds(self, batch, cite):
        """Whether one citation points into the text this batch asked the model to read."""
        if cite.get("document_id") != batch["doc_id"]:
            return False
        block = self.sources.blocks.get((batch["doc_id"], cite.get("block_id")))
        if block is None:
            return False
        for part in batch["segments"]:
            if part.get("block_id") != cite.get("block_id"):
                continue
            if part.get("start") == 0 and part.get("end") == len(block.get("text") or ""):
                return True
            # Segment offsets are block-local while evidence offsets are document
            # coordinates, so a fragment only counts when the frozen text locates it
            # once and the cited range reaches into it.
            start, end = cite.get("start"), cite.get("end")
            text = part.get("text") or ""
            originals = self.sources.originals.get(batch["doc_id"]) or ""
            if not isinstance(start, int) or not isinstance(end, int) or not text:
                continue
            at = originals.find(text)
            if at < 0 or originals.count(text) != 1:
                continue
            if start < at + len(text) and at < end:
                return True
        return False

    def steps_recheck(self):
        """Open items this batch's own basics pass raised, for one steps reminder.

        A steps batch that wrote nothing, whose text still carries basics open items no
        existing step refers to, gets one chance to look again before it seals. Returns
        the rows to name, or None when the batch should seal exactly as before.
        """
        if self.assignment.phase != "scan" or self.assignment.kind != "steps":
            return None
        batch = self.sources.batches.get(self.assignment.id)
        if batch is None or self.mutations or self.reconsidered:
            return None
        if any(
            record.get("phase") == "scan"
            and record.get("kind") == "steps"
            and record.get("id") == self.assignment.id
            and record.get("status")
            for record in self.history
        ):
            # A sealed batch is never asked again, even if its own record is re-read.
            return None
        created = {
            (mutation.get("result") or {}).get("id")
            for record in self.history
            if record.get("phase") == "scan"
            and record.get("kind") == "basics"
            and record.get("id") == self.assignment.id
            for mutation in record.get("mutations") or ()
            if mutation.get("operation") == "unresolved"
        }
        created.discard(None)
        if not created:
            return None
        blocks = {(batch["doc_id"], part.get("block_id")) for part in batch["segments"]}
        written = {
            (cite.get("document_id"), cite.get("block_id"))
            for row in self.records.values()
            if row["kind"] == "steps"
            for cite in row.get("evidence") or ()
        }
        if blocks & written:
            return None
        rows = []
        for unresolved in self.open_unresolved():
            if unresolved.get("id") not in created:
                continue
            for cite in unresolved.get("evidence") or ():
                if self.batch_holds(batch, cite):
                    rows.append(
                        {
                            "id": unresolved["id"],
                            "document_id": cite.get("document_id"),
                            "block_id": cite.get("block_id"),
                        }
                    )
                    break
        return rows[:2] or None

    def allowed(self, kind):
        if self.assignment.phase == "review":
            raise ValueError("the reviewer cannot modify entities")
        if self.assignment.phase == "scan" and kind not in scan_writes(self.assignment):
            raise ValueError(
                f"this scan can only write {', '.join(scan_writes(self.assignment))}"
            )
        if self.assignment.phase == "category" and kind not in self.ready:
            raise ValueError(f"category repair cannot write unscanned category {kind}")
        if self.assignment.phase == "gap" and kind not in ORDER:
            raise ValueError(f"this gap fix cannot write {kind}")

    def canonical(self, entity_id):
        seen = set()
        while entity_id in self.data["redirects"]:
            if entity_id in seen:
                raise ValueError(f"cyclic redirect for {entity_id}")
            seen.add(entity_id)
            entity_id = self.data["redirects"][entity_id]["to"]
        if entity_id not in self.records:
            raise ValueError(f"unknown active entity {entity_id!r}")
        return entity_id

    def mutate(self, operation, request, apply, *, touched=(), key_request=None):
        key = dumps([operation, request if key_request is None else key_request])
        if cached := self.receipts.get(key):
            result, updated = cached
            if all(self.records.get(i) == row for i, row in updated.items()) and (
                operation != "unresolved"
                or not self.data["unresolved"][result["id"]].get("invalidated")
            ):
                return result
        if self.completion is not None:
            raise ValueError("assignment is already sealed")
        original = self.data
        self.data = deepcopy(original)
        self._index = None
        try:
            result = apply()
        except Exception:
            self.data = original
            self._index = None
            raise
        changed = {i for i in touched if self.records.get(i) != original["entities"].get(i)}
        if changed:
            for issue in self.data["unresolved"].values():
                if changed.intersection(issue.get("check_entity_ids", ())):
                    issue["invalidated"] = True
        self._index = None
        # An identical patch is a retry while its result is still current.
        updated = {i: self.records.get(i) for i in touched} if operation == "update" else {}
        self.receipts[key] = result, updated
        self.mutations.append({"operation": operation, "request": request, "result": result})
        return result

    def location(self, item):
        return self.sources.evidence(item.document_id, item.block_id, item.start, item.end)

    def evidence(self, evidence, value=None, *, aliases=None):
        result = []
        for item in evidence:
            field = "" if item.field == "/value" else item.field.removeprefix("/value/")
            if field:
                field = "/" + field.lstrip("/")
            if value is not None and field:
                parts = field[1:].split("/")
                current = value
                if parts[0] == "aliases" and aliases is not None:
                    current = {"aliases": aliases}
                for part in parts:
                    part = part.replace("~1", "/").replace("~0", "~")
                    try:
                        if isinstance(current, list) and not part.isdecimal():
                            raise ValueError("invalid array index")
                        current = current[int(part)] if isinstance(current, list) else current[part]
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise ValueError(f"invalid evidence field {item.field!r}") from exc
            result.append(
                {
                    **self.location(item),
                    "field": field,
                    "note": item.note,
                }
            )
        return result

    def add_entities(self, entities: list[EntityAddition]):
        """Add a group of entities atomically, with one or more citations for each."""

        def apply():
            result = []
            for item in entities:
                self.allowed(item.kind)
                value = normalized(item.kind, item.value.model_dump(mode="json", by_alias=True))
                citations = self.evidence(item.evidence, value, aliases=item.aliases)
                self.data["next_entity"] += 1
                entity_id = f"E{self.data['next_entity']}"
                row = {
                    "id": entity_id,
                    "kind": item.kind,
                    "value": value,
                    "aliases": item.aliases,
                    "evidence": citations,
                    "mentions": [self.location(loc) for loc in item.mentions],
                }
                self.records[entity_id] = row
                result.append(summary(row))
            return result

        return self.mutate("add", [entity.model_dump(mode="json") for entity in entities], apply)

    def update_entities(self, entities: list[EntityUpdate]):
        """Update several entities by ID. Each patch replaces named top-level fields.

        Set withdraw_reason during validation repair to retract an unsupported entity and
        record the cited reason. Update or retract its incoming references in the same repair.
        """

        changed = set()
        # The saved request keeps the old shape: a delta is expanded to the full list that
        # the write path actually applied. Receipts key on the model's raw request instead.
        expanded = []

        def apply():
            before = deepcopy(self.records)
            for item in entities:
                entity_id = self.canonical(item.id)
                row = self.records[entity_id]
                self.allowed(row["kind"])
                if entity_id in changed:
                    raise ValueError(f"duplicate update for {entity_id}")
                changed.add(entity_id)
                record = item.model_dump(mode="json", exclude={"associated_fields_delta"})
                if item.withdraw_reason:
                    if (
                        self.assignment.phase == "scan"
                        or item.patch
                        or item.aliases is not None
                        or item.associated_fields_delta is not None
                    ):
                        raise ValueError("withdrawals are standalone validation repairs")
                    if any(link["to"] == entity_id for link in self.data["redirects"].values()):
                        raise ValueError("cannot withdraw a canonical merge target")
                    self.data["withdrawn"][entity_id] = {
                        "entity": self.records.pop(entity_id),
                        "reason": item.withdraw_reason,
                        "evidence": self.evidence(item.evidence),
                    }
                    expanded.append(record)
                    continue
                patch = item.patch
                delta = item.associated_fields_delta
                if delta is not None:
                    if "associated_fields" in patch:
                        raise ValueError(
                            "submit either patch.associated_fields or associated_fields_delta"
                        )
                    if row["kind"] != "messages":
                        raise ValueError("associated_fields_delta only applies to messages")
                    fields = delta_fields(row["value"]["associated_fields"], delta)
                    moved = moved_field_evidence(
                        row["evidence"], row["value"]["associated_fields"], fields
                    )
                    if moved:
                        raise ValueError(
                            "this delta moves fields that existing evidence cites: "
                            + ", ".join(moved)
                        )
                    patch = {**patch, "associated_fields": fields}
                record["patch"] = patch
                expanded.append(record)
                value = normalized(row["kind"], {**row["value"], **patch})
                citations = self.evidence(item.evidence, value, aliases=item.aliases)
                touched = {key for key in patch if value[key] != row["value"].get(key)}
                if len(touched) == 1:
                    (field,) = touched
                    for entry in citations:
                        if not entry["field"]:
                            entry["field"] = f"/{field}"
                covered = {entry["field"].strip("/").split("/")[0] for entry in citations}
                if touched - covered:
                    raise ValueError(f"missing field evidence for {sorted(touched - covered)}")
                if item.aliases is not None:
                    row["aliases"] = item.aliases
                row["value"] = value
                row["evidence"].extend(cite for cite in citations if cite not in row["evidence"])
                locations = row.setdefault("mentions", [])
                for item_location in map(self.location, item.mentions):
                    if item_location not in locations:
                        locations.append(item_location)
            changed.update(rewrite_references(before, self.records, {}))
            return [summary(self.records[key]) for key in self.records if key in changed]

        raw = [entity.model_dump(mode="json") for entity in entities]
        return self.mutate("update", expanded, apply, touched=changed, key_request=raw)

    def merge_entities(
        self,
        keep_id: str,
        ids: list[str],
        reason: str,
        evidence: list[Evidence],
        value: dict[str, Any] | None = None,
    ):
        """Merge duplicates, supplying the final value for conflicting attributes."""

        changed = set()

        def apply():
            keep = self.canonical(keep_id)
            others = list(dict.fromkeys(self.canonical(key) for key in ids))
            if not others or keep in others or not reason.strip() or not evidence:
                raise ValueError("merge requires distinct source IDs, a reason and evidence")
            target = self.records[keep]
            self.allowed(target["kind"])
            before = deepcopy(self.records)
            if any(self.records[key]["kind"] != target["kind"] for key in others):
                raise ValueError("only entities in the same category can be merged")
            if value is None:
                comparable = {k: v for k, v in target["value"].items() if k != "name"}
                for key in others:
                    if comparable != {
                        k: v for k, v in self.records[key]["value"].items() if k != "name"
                    }:
                        raise ValueError("conflicting attributes require the final merged value")
            else:
                target["value"] = normalized(target["kind"], value)
            citations = self.evidence(evidence, target["value"])
            target["evidence"].extend(citations)
            for key in others:
                old = self.records.pop(key)
                aliases = old["aliases"] + (
                    [old["value"]["name"]] if "name" in old["value"] else []
                )
                target["aliases"] = list(dict.fromkeys(target["aliases"] + aliases))
                locations = target.setdefault("mentions", [])
                locations.extend(loc for loc in old.get("mentions", []) if loc not in locations)
                self.data["redirects"][key] = {
                    "to": keep,
                    "reason": reason,
                    "evidence": citations,
                    "entity": old,
                }
            changed.update([keep, *others])
            changed.update(rewrite_references(before, self.records, dict.fromkeys(others, keep)))
            return summary(target)

        request = [keep_id, ids, reason, [item.model_dump() for item in evidence], value]
        return self.mutate("merge", request, apply, touched=changed)

    def record_unresolved(self, issue: Unresolved):
        """Record a supported conflict, unavailable definition or format limitation."""

        def apply():
            for key in issue.entity_ids:
                self.canonical(key)
            check_entity_ids = set()
            if issue.check_refs:
                pending = {row["check_ref"]: row for row in self.coverage_issues()}
                recorded = {
                    ref: item["id"]
                    for item in self.open_unresolved()
                    for ref in item.get("check_refs", [])
                }
                for ref in issue.check_refs:
                    check = pending.get(ref)
                    if check is None:
                        raise ValueError(f"not an open coverage check: {ref}")
                    if ref in recorded:
                        raise ValueError(
                            f"coverage check {ref} already recorded in {recorded[ref]}"
                        )
                    check_entity_ids.update(check["records"])
                if not check_entity_ids <= set(issue.entity_ids):
                    raise ValueError("include every entity listed by the checks")
            self.data["next_issue"] += 1
            key = f"U{self.data['next_issue']}"
            self.data["unresolved"][key] = {
                **issue.model_dump(),
                "id": key,
                "evidence": self.evidence(issue.evidence),
                "assignment": self.assignment.id,
            }
            if check_entity_ids:
                self.data["unresolved"][key]["check_entity_ids"] = sorted(check_entity_ids)
            return {"id": key}

        return self.mutate("unresolved", issue.model_dump(), apply)

    def solve_unresolved(self, unresolved_id: str, reason: str, evidence: list[Evidence]):
        def apply():
            item = self.data["unresolved"].get(unresolved_id)
            if item is None or item.get("resolved"):
                raise ValueError(f"unknown open unresolved item: {unresolved_id!r}")
            refs = set(item.get("check_refs", []))
            remaining = sorted(
                row["check_ref"]
                for row in self.coverage_issues()
                if row["check_ref"] in refs and not row.get("advisory")
            )
            if remaining:
                raise ValueError(f"linked coverage checks remain: {', '.join(remaining)}")
            item["resolved"] = {
                "reason": reason,
                "evidence": self.evidence(evidence),
                "assignment": self.assignment.id,
            }
            return {"id": unresolved_id, "resolved": True}

        request = [unresolved_id, reason, [item.model_dump() for item in evidence]]
        return self.mutate("solve_unresolved", request, apply)

    def describe_issues(self, issues):
        for issue in issues:
            locations = list(issue.get("evidence", []))
            for key in issue["records"]:
                row = self.records.get(key, {})
                for cite in row.get("evidence", [])[:2]:
                    if cite not in locations:
                        locations.append(cite)
            yield {**issue, "evidence": locations[:6]}

    def coverage_issues(self):
        return self.index.coverage(self.ready)

    def advisory_issues(self):
        """Rows the agent should fix but that never block a scan, Fix or publication."""
        return [issue for issue in self.coverage_issues() if issue.get("advisory")]

    def open_unresolved(self):
        active = {row["check_ref"] for row in self.coverage_issues()}
        return [
            unresolved
            for unresolved in self.data["unresolved"].values()
            if not unresolved.get("resolved")
            and not unresolved.get("invalidated")
            and (
                not unresolved.get("check_refs")
                or active.intersection(unresolved["check_refs"])
            )
        ]

    def structural_issues(self):
        issues = list(self.index.issues) + duplicate_issues(self.records)
        if self.assignment.phase != "global":
            return issues
        for key in self.data["redirects"]:
            try:
                self.canonical(key)
            except ValueError as exc:
                issues.append(
                    {
                        "type": "invalid_redirect",
                        "records": [key],
                        "path": [],
                        "description": str(exc),
                    }
                )
        # Coverage rules are listed separately by ``coverage_issues`` with their own
        # check_refs, so the publication check only adds schema and reference errors.
        errors = validate_protocol(
            self.protocol_model(), self.sources.documents, coverage=False
        )
        reported = {item["description"] for item in issues}
        return issues + [
            {"type": "protocol", "records": [], "path": [], "description": error}
            for error in errors
            if error not in reported
        ]

    def issues(self):
        blocking = [
            issue
            for issue in self.structural_issues() + self.coverage_issues()
            if not issue.get("advisory")
        ]
        return list(self.describe_issues(blocking))

    def protocol_model(self):
        collections = {
            kind: [row["value"] for row in self.records.values() if row["kind"] == kind]
            for kind in ORDER
        }
        # The published alias field is a link between two declared parties in the
        # Rust contract. Lexical spellings stay discoverable in the registry.
        party_names = {party["name"] for party in collections["parties"]}
        collections["parties"] = [
            {
                **party,
                "alias": list(
                    dict.fromkeys(
                        alias
                        for alias in party["alias"]
                        if alias in party_names and alias != party["name"]
                    )
                ),
            }
            for party in collections["parties"]
        ]
        issues = []
        for issue in self.open_unresolved():
            names = []
            for entity_id in issue["entity_ids"]:
                if entity_id not in self.data["withdrawn"]:
                    names.append(identity(self.records[self.canonical(entity_id)]))
            issues.append({"description": issue["description"], "object_names": names})
        return {
            "meta": {
                "protocol": self.protocol,
                "complete": not issues,
                "documents": [
                    {key: item[key] for key in ("doc_id", "rfc", "path")}
                    for item in self.sources.manifest["documents"]
                ],
            },
            **collections,
            "unresolved": issues,
        }
