"""Registry relations and coverage checks that become ready as categories finish."""

from collections import defaultdict

from rsc.protocol.references import coverage_issues, reference_issues

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
        return coverage_issues(self.records, self.resolved, ready)
