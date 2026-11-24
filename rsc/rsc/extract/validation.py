"""Registry relations and coverage checks that become ready as categories finish."""

from collections import defaultdict

from rsc.protocol.references import reference_issues

# The second category is the earliest point when both sides can be checked.
COVERAGE_STAGES = {
    "party_channel": ("parties", "channels"),
    "channel_step": ("channels", "steps"),
    "message_send": ("messages", "steps"),
    "state_processing": ("states", "steps"),
    "channel_lifecycle": ("steps", "steps"),
    "step_dependency": ("steps", "step_dependencies"),
}


class RegistryIndex:
    def __init__(self, records):
        self.records = records
        self.by_kind = defaultdict(list)
        self.incoming = defaultdict(list)
        self.outgoing = defaultdict(list)
        self.actions = defaultdict(set)
        self.resolved = {}
        self.issues = reference_issues(records, resolved=self.resolved)
        for key, row in records.items():
            self.by_kind[row["kind"]].append(key)
        for (owner, path), target in self.resolved.items():
            edge = {"from": owner, "field": list(path), "to": target}
            self.incoming[target].append(edge)
            self.outgoing[owner].append(edge)
        for step in self.by_kind["steps"]:
            value = records[step]["value"]
            channel = self.resolved.get((step, ("channel_name",)))
            party = self.resolved.get((step, ("party_name",)))
            if channel is not None and party is not None:
                self.actions[channel, value["action"]].add(party)

    def referenced_by(self, key, kind, field=None):
        return any(
            self.records[edge["from"]]["kind"] == kind
            and (field is None or edge["field"][0] == field)
            for edge in self.incoming[key]
        )

    def coverage(self, ready):
        checks = []

        def add(rule, key, description, *, field=(), advisory=False):
            row = {
                "type": rule,
                "check_ref": f"{rule}:{key}",
                "records": [key],
                "path": list(field),
                "description": description,
            }
            if advisory:
                row["advisory"] = True
            checks.append(row)

        if "channels" in ready:
            for key in self.by_kind["parties"]:
                if not self.referenced_by(key, "channels", "endpoint_party_names"):
                    name = self.records[key]["value"]["name"]
                    add("party_without_channel", key, f"Party {name!r} is not a channel endpoint")

        if "steps" in ready:
            for key in self.by_kind["channels"]:
                self.check_channel(key, add)
            for key in self.by_kind["messages"]:
                if not self.referenced_by(key, "steps", "message_name"):
                    name = self.records[key]["value"]["name"]
                    add("message_without_send", key, f"Message {name!r} has no send step")
            for key in self.by_kind["states"]:
                self.check_state(key, add)
            # Advisory: visible while the interaction unit is written, never blocking.
            # The source may prescribe a bare transmission, so the reviewer decides
            # whether an empty hook is a real gap.
            for key in self.by_kind["steps"]:
                value = self.records[key]["value"]
                if value["action"] != "send" or any(
                    (value.get("processing") or {}).values()
                ):
                    continue
                add(
                    "step_without_processing",
                    key,
                    f"Step {value['name']!r} sends {value['message_name']!r} "
                    "with no processing",
                    advisory=True,
                )

        if "step_dependencies" in ready:
            for key in self.by_kind["steps"]:
                if not self.referenced_by(key, "step_dependencies"):
                    name = self.records[key]["value"]["name"]
                    add("step_without_dependency", key, f"Step {name!r} has no dependency links")
        return checks

    def check_channel(self, key, add):
        value = self.records[key]["value"]
        name = value["name"]
        if value["connection"] != "connection_oriented":
            if not self.referenced_by(key, "steps", "channel_name"):
                add("channel_without_step", key, f"Channel {name!r} has no step")
            return
        for action, role in (
            ("connect", "initiator_party_name"),
            ("listen", "acceptor_party_name"),
        ):
            expected = self.resolved.get((key, (role,)))
            if expected is None or expected not in self.actions[key, action]:
                add(
                    f"channel_requires_{action}",
                    key,
                    f"Channel {name!r} requires a {action} step by its declared {role}",
                    field=(role,),
                )
        endpoints = {
            edge["to"] for edge in self.outgoing[key] if edge["field"][0] == "endpoint_party_names"
        }
        if not endpoints & self.actions[key, "close"]:
            add(
                "channel_requires_close",
                key,
                f"Channel {name!r} requires an endpoint close step",
                field=("connection",),
            )

    def check_state(self, key, add):
        value = self.records[key]["value"]
        name = value["name"]
        accesses = {
            edge["field"][3] for edge in self.incoming[key] if edge["field"][0] == "processing"
        }
        if not accesses:
            description = f"State {name!r} has no hook reads or writes"
            if value["source"] == "processing":
                description += " and no processing producer"
            add("state_without_access", key, description)
