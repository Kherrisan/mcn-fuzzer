"""Visible protocol definitions, structural search, ownership, and redirects."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy

from .contract import COLLECTIONS
from .paging import paginate

NESTED = {
    "associated_fields": "associated_field",
    "field_relations": "field_relation",
}
REFERENCES = {
    "message_name",
    "channel_name",
    "sender_name",
    "receiver_name",
    "party_name",
    "initiator_party_name",
    "acceptor_party_name",
    "prior_step",
    "dependent_step",
    "prior_field",
    "dependent_field",
    "endpoint_party_names",
    "object_names",
}
FILTERS = {
    "parties": set(),
    "messages": set(),
    "channels": {
        "endpoint_party_names",
        "initiator_party_name",
        "acceptor_party_name",
        "connection",
        "transport",
    },
    "steps": {
        "name",
        "message_name",
        "channel_name",
        "sender_name",
        "receiver_name",
        "condition",
    },
    "step_dependencies": {
        "prior_step",
        "dependent_step",
        "condition",
    },
}


def terms(value: str) -> set[str]:
    """Tokenize literal identifiers and words without regex interpretation of user queries."""

    return set(re.findall(r"[\w-]+", value.casefold()))


def normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def references(value):
    """Yield protocol name references. Other strings stay prose."""

    if isinstance(value, list):
        for item in value:
            yield from references(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in REFERENCES:
                values = item if isinstance(item, list) else [item]
                yield from (entry for entry in values if isinstance(entry, str))
            else:
                yield from references(item)


class Registry:
    """A private candidate view; allocation metadata never makes definitions visible."""

    def __init__(
        self,
        model: dict,
        *,
        redirects: dict[str, str] | None = None,
        invalid_ids: set[str] | None = None,
        revision: int = 0,
    ) -> None:
        self.redirects = dict(redirects or {})
        self.invalid_ids = set(invalid_ids or ())
        self.revision = revision
        self.rows = {}
        self.kinds = {}
        self.owners: dict[str, str] = {}
        self._objects: dict[str, dict] = {}
        for kind in COLLECTIONS:
            for index, raw in enumerate(model.get(kind, [])):
                row = deepcopy(raw)
                if kind == "steps" and isinstance(row.get("id"), str) and row["id"]:
                    identity = row["id"]
                else:
                    identity = f"{kind}:{index}:{row.get('name', '')}"
                self.rows[identity] = row
                self.kinds[identity] = kind
                self.owners[identity] = identity
                self._objects[identity] = row
        self.reverse = {identity: set() for identity in self.rows}
        self.name_index: dict[str, set[str]] = defaultdict(set)
        self.reference_index: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._terms: dict[str, set[str]] = {}
        for identity, row in self.rows.items():
            names = [row.get("name", "")]
            for name in names:
                if name:
                    self.name_index[normalize_name(name)].add(identity)
            text = names + [row.get(key, "") for key in ("condition", "description", "from")]
            text.extend(nested.get("name", "") for field in NESTED for nested in row.get(field, []))
            text.extend(references(row))
            self._terms[identity] = terms(" ".join(text))
            for field in REFERENCES:
                value = row.get(field)
                for ref in value if isinstance(value, list) else [value]:
                    if isinstance(ref, str):
                        self.reference_index[field, ref].add(identity)
        for identity, row in self.rows.items():
            for ref in references(row):
                for owner in self.owners_for_ref(ref):
                    if owner != identity:
                        self.reverse[owner].add(identity)

    def named(self, name: str, *, kind: str | None = None) -> list[str]:
        key = normalize_name(name)
        return sorted(
            identity
            for identity in self.name_index.get(key, ())
            if normalize_name(self.rows[identity].get("name", "")) == key
            and (kind is None or self.kinds.get(identity) == kind)
        )

    def mentioning_field(self, name: str) -> list[str]:
        key = normalize_name(name)
        return sorted(
            identity
            for identity, row in self.rows.items()
            if any(
                normalize_name(field.get("name", "")) == key
                for field in row.get("associated_fields", [])
            )
        )

    def owners_for_ref(self, ref: str) -> set[str]:
        """Candidate owners for a name or step ID. Several matches are all returned."""

        found = set(self.named(ref))
        found.update(self.mentioning_field(ref))
        resolved = self.resolve(ref)
        owner = self.owners.get(resolved)
        if owner:
            found.add(owner)
        return found

    def resolve(self, identity: str) -> str:
        seen = set()
        while identity in self.redirects:
            if identity in seen:
                raise ValueError("cyclic ID redirect")
            seen.add(identity)
            identity = self.redirects[identity]
        return identity

    def root(self, identity: str) -> dict | None:
        resolved = self.resolve(identity)
        owner = self.owners.get(resolved)
        if owner is None:
            matches = self.named(identity)
            if len(matches) == 1:
                owner = matches[0]
            elif len(matches) != 1:
                fields = self.mentioning_field(identity)
                if len(fields) == 1 and not matches:
                    owner = fields[0]
        return deepcopy(self.rows[owner]) if owner else None

    def reusable(self, identity: str) -> bool:
        resolved = self.resolve(identity)
        owner = self.owners.get(resolved)
        return (
            owner is not None and owner not in self.invalid_ids and resolved not in self.invalid_ids
        )

    def closure(self, identifiers) -> set[str]:
        found = set()
        for identity in identifiers:
            found.update(self.owners_for_ref(identity))
        pending = list(sorted(found))
        while pending:
            for ref in references(self.rows[pending.pop()]):
                for parent in self.owners_for_ref(ref):
                    if parent not in found:
                        found.add(parent)
                        pending.append(parent)
        return found

    def _label(self, identity: str) -> str:
        row = self.rows[identity]
        if row.get("name"):
            return row["name"]
        if self.kinds[identity] == "step_dependencies":
            return f"{row.get('prior_step', '')} -> {row.get('dependent_step', '')}"
        return self.kinds[identity]

    def public_row(self, identity: str) -> dict:
        """Copy a definition for tool results. Only steps keep a semantic ID."""

        row = deepcopy(self.rows[identity])
        if self.kinds[identity] != "steps":
            row.pop("id", None)
        return row

    def summary(self, identity: str) -> dict:
        row = self.rows[identity]
        kind = self.kinds[identity]
        result = {
            "kind": kind,
            "name": row.get("name"),
            "description": row.get("description"),
            "identity": {key: deepcopy(row[key]) for key in sorted(FILTERS[kind]) if key in row},
            "candidate_status": "valid" if self.reusable(identity) else "invalid",
            "reusable": self.reusable(identity),
        }
        if kind == "steps" and row.get("id"):
            result["id"] = row["id"]
        return result

    def search(
        self,
        query: str | None = None,
        *,
        kind: str | None = None,
        filters: dict | None = None,
        top_k: int = 8,
        cursor: str | None = None,
    ) -> dict:
        """Match literal terms and kind-specific filters, then page in stable name order."""

        if kind is not None and kind not in COLLECTIONS:
            raise ValueError("unknown object kind")
        filters = filters or {}
        if not (query and query.strip()) and not filters:
            raise ValueError("provide query or structural filters")
        if filters and kind is None:
            raise ValueError("structural filters require an object kind")
        if kind is not None and set(filters) - FILTERS[kind]:
            raise ValueError(f"unsupported filters for {kind}")
        for expected in filters.values():
            values = expected if isinstance(expected, list) else [expected]
            if not values or any(not isinstance(value, str) for value in values):
                raise ValueError("filter values must be strings or nonempty string lists")
        query_terms = terms(query or "")
        exact = self.name_index.get(normalize_name(query or ""), set())
        matches = []
        for identity, row in self.rows.items():
            if kind is not None and self.kinds[identity] != kind:
                continue
            if query and identity not in exact and not query_terms & self._terms[identity]:
                continue
            accepted = True
            for field, expected in filters.items():
                actual = row.get(field)
                values = expected if isinstance(expected, list) else [expected]
                actuals = actual if isinstance(actual, list) else [actual]
                if not set(values) <= set(actuals):
                    accepted = False
                    break
            if accepted:
                matches.append(self.summary(identity))
        matches.sort(
            key=lambda row: (
                COLLECTIONS.index(row["kind"]),
                row.get("name") or "",
                row.get("id") or "",
                tuple(
                    sorted(
                        (key, str(value))
                        for key, value in (row.get("identity") or {}).items()
                    )
                ),
            )
        )
        return paginate(
            matches,
            top_k,
            cursor,
            [
                query,
                kind,
                filters,
                self.revision,
                matches,
            ],
        )

    def lookup(self, identity: str, *, depth: int = 1, limit: int = 8) -> dict:
        """Return a full definition and bounded reference closure, including nested ownership."""

        if type(depth) is not int or depth < 0 or type(limit) is not int or limit < 1:
            raise ValueError("lookup depth must be nonnegative and limit must be positive")
        resolved = self.resolve(identity)
        owner = self.owners.get(resolved)
        if owner is None:
            named = sorted(set(self.named(identity)) | set(self.mentioning_field(identity)))
            if len(named) == 1:
                resolved = named[0]
                owner = self.owners.get(resolved)
            elif len(named) > 1:
                inventory = {kind: [] for kind in COLLECTIONS}
                for candidate in named[:limit]:
                    inventory[self.kinds[candidate]].append(self.public_row(candidate))
                return {
                    "status": "candidates",
                    "object": None,
                    "inventory": inventory,
                    "omitted_count": max(0, len(named) - limit),
                    "candidate_names": [self._label(candidate) for candidate in named[:limit]],
                }
        if owner is None:
            return {
                "status": "missing",
                "object": None,
                "inventory": {},
                "omitted_count": 0,
                "candidate_names": [],
            }
        visited = {owner}
        frontier = {owner}
        for _ in range(depth):
            next_level = {
                owner
                for root in frontier
                for ref in references(self.rows[root])
                for owner in self.owners_for_ref(ref)
            } - visited
            visited.update(next_level)
            frontier = next_level
        selected = [owner] + sorted(visited - {owner})
        omitted = selected[limit:]
        inventory = {kind: [] for kind in COLLECTIONS}
        for root in selected[:limit]:
            inventory[self.kinds[root]].append(self.public_row(root))
        return {
            "status": "ok",
            "object": self.public_row(resolved),
            "reusable": self.reusable(resolved),
            "inventory": inventory,
            "omitted_count": len(omitted),
            "candidate_names": [],
        }
