"""Issue worklist tracking. Tracking state stays outside the Issue record."""

from __future__ import annotations

from copy import deepcopy

from .merge import canonical, stable_id


class GapHistory:
    """Keep attempts stable across rounds without embedding them in an Issue."""

    def __init__(self):
        self.lineage = {}
        self.entries = {}
        self._known = {}
        self._signatures = {}

    def remap(self, mapping):
        """Step-ID lineage is diagnostic only and does not rewrite object names."""

        for old, new in sorted(mapping.items()):
            origins = {self.lineage.get(old, old), self.lineage.get(new, new)}
            origin = min(origins)
            for identity, previous in list(self.lineage.items()):
                if previous in origins:
                    self.lineage[identity] = origin
            self.lineage[old] = self.lineage[new] = origin

    def signature(self, item):
        return stable_id(
            "gap",
            [item.get("description", ""), sorted(item.get("object_names") or [])],
        )

    def key(self, item):
        signature = self.signature(item)
        tracking_id = self._signatures.get(signature, signature)
        self._signatures.setdefault(signature, tracking_id)
        self._known[tracking_id] = deepcopy(item)
        return tracking_id

    def refresh(self, items, round_number, *, reset_pending=True):
        active = {self.key(item) for item in items}
        for key, entry in self.entries.items():
            if key not in active:
                entry["status"] = "resolved"
        for item in items:
            key = self.key(item)
            entry = self.entries.setdefault(
                key, {"tracking_id": key, "attempts": 0, "first_round": round_number}
            )
            status = (
                "pending"
                if reset_pending or entry.get("status") == "resolved"
                else entry.get("status", "pending")
            )
            entry.update(
                description=item.get("description", ""),
                object_names=list(item.get("object_names") or []),
                status=status,
            )

    def defer(self, items, reason, attempted=False):
        for item in items:
            entry = self.entries[self.key(item)]
            entry.update(status="deferred", last_feedback=reason[:1000])
            entry["attempts"] += int(attempted)

    def ordered(self, items):
        return sorted(
            items,
            key=lambda item: (
                self.entries[self.key(item)]["attempts"],
                item.get("description", ""),
                tuple(item.get("object_names") or []),
            ),
        )

    def snapshot(self):
        return sorted(deepcopy(list(self.entries.values())), key=canonical)
