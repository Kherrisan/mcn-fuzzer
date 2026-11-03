"""Deterministic, atomic ID reservations shared by one compilation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

PREFIXES = {
    "steps": "step",
}


class IdConsistencyError(RuntimeError):
    """A reservation collision must abort publication of the compilation."""


@dataclass(frozen=True, slots=True)
class AllocationKey:
    protocol: str
    kind: str
    stage: str
    list_id: str | None
    origin: str
    local_id: str
    owner_key: AllocationKey | None = None

    def __post_init__(self) -> None:
        if self.kind not in PREFIXES:
            raise ValueError(f"unsupported ID kind: {self.kind}")
        if not all(
            isinstance(v, str) and v
            for v in (self.protocol, self.stage, self.origin, self.local_id)
        ):
            raise ValueError("allocation identity fields must be nonempty strings")
        if not self.local_id.startswith("local:") or self.local_id == "local:":
            raise ValueError("new IDs must have a nonempty local: name")
        if self.list_id is not None and (not isinstance(self.list_id, str) or not self.list_id):
            raise ValueError("list_id must be null or a nonempty string")
        if self.owner_key is not None:
            raise ValueError("step IDs do not use an owner allocation key")


def deterministic_id(key: AllocationKey) -> str:
    payload = json.dumps(asdict(key), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return PREFIXES[key.kind] + "-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


class GlobalIdAllocator:
    """Reserve IDs without sharing candidate contents or depending on arrival order."""

    def __init__(
        self,
        protocol: str,
        *,
        id_factory: Callable[[AllocationKey], str] = deterministic_id,
    ) -> None:
        if not isinstance(protocol, str) or not protocol.strip():
            raise ValueError("protocol must be a nonempty string")
        self.protocol = protocol
        self._id_factory = id_factory
        self._allocations: dict[AllocationKey, str] = {}
        self._reservations: dict[str, AllocationKey] = {}
        self._lock = asyncio.Lock()

    @property
    def allocations(self) -> Mapping[AllocationKey, str]:
        return MappingProxyType(self._allocations)

    @property
    def reservations(self) -> Mapping[str, AllocationKey]:
        return MappingProxyType(self._reservations)

    async def reserve_many(self, keys: Iterable[AllocationKey]) -> dict[AllocationKey, str]:
        """Reserve an entire batch or nothing; identical retry keys retain their IDs."""

        batch = tuple(dict.fromkeys(keys))
        if any(key.protocol != self.protocol for key in batch):
            raise ValueError("allocation protocol does not match this compilation")
        async with self._lock:
            allocated: dict[AllocationKey, str] = {}
            reserved: dict[str, AllocationKey] = {}
            for key in batch:
                identifier = self._allocations.get(key)
                if identifier is None:
                    identifier = self._id_factory(key)
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier.startswith("local:")
                ):
                    raise IdConsistencyError("ID factory returned an invalid formal ID")
                prior = reserved.get(identifier, self._reservations.get(identifier))
                if prior is not None and prior != key:
                    raise IdConsistencyError(f"ID reservation collision: {identifier}")
                allocated[key] = identifier
                reserved[identifier] = key
            self._allocations.update(allocated)
            self._reservations.update(reserved)
            return allocated
