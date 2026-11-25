"""Name, field-carrier, endpoint and network-action checks shared with scoped merging."""

import json
from collections import defaultdict

from .contract import COLLECTIONS, record_identity

NETWORK_ACTIONS = ("listen", "connect", "close")


def reference_errors(records: dict) -> list[str]:
    return [issue["description"] for issue in reference_issues(records)]


def reference_issues(records: dict, bindings=None, *, resolved=None) -> list[dict]:
    """Return locations and involved record keys without choosing ambiguous definitions.

    Merge supplies its established (record key, value path) bindings. Published models
    resolve names globally. Both use these same role, endpoint and field-carrier checks.
    """
    model = {kind: [k for k, r in records.items() if r["kind"] == kind] for kind in COLLECTIONS}
    names = {}
    for kind, keys in model.items():
        names[kind] = {}
        if kind != "step_dependencies":
            for key in keys:
                _, name = record_identity(kind, records[key]["value"])
                names[kind].setdefault(name, []).append(key)
    issues = []

    def issue(description, *keys, path, type_="structure"):
        issues.append(
            {
                "type": type_,
                "description": description,
                "records": list(dict.fromkeys(k for k in keys if k is not None)),
                "path": list(path),
            }
        )

    def targets(owner, path, kind, name):
        return (bindings or {}).get((owner, path), names[kind].get(name, []))

    def ref(owner, path, kind, name):
        matches = targets(owner, path, kind, name)
        if len(matches) != 1:
            issue(
                f"{kind} reference {name!r} has {len(matches)} matches",
                owner,
                *matches,
                path=path,
                type_="missing_reference" if not matches else "ambiguous_reference",
            )
            return None
        if resolved is not None:
            resolved[(owner, path)] = matches[0]
        return matches[0]

    for kind, values in names.items():
        for name, keys in values.items():
            if len(keys) != 1:
                issue(f"ambiguous {kind} name {name!r}", *keys, path=("name",), type_="same_name")
    for key in model["states"]:
        ref(key, ("party_name",), "parties", records[key]["value"]["party_name"])
    for key in model["channels"]:
        c = records[key]["value"]
        for i, party in enumerate(c["endpoint_party_names"]):
            ref(key, ("endpoint_party_names", i), "parties", party)
        for field in ("initiator_party_name", "acceptor_party_name"):
            party = c[field]
            if party is not None:
                ref(key, (field,), "parties", party)
                if party not in c["endpoint_party_names"]:
                    issue(f"channel {c['name']} role is not an endpoint", key, path=(field,))
        if c["connection"] == "connectionless" and (
            c["initiator_party_name"] is not None or c["acceptor_party_name"] is not None
        ):
            issue("connectionless channel has connection roles", key, path=("connection",))
        for i, source in enumerate(c["endpoint_sources"]):
            if resolved is not None:
                path = ("endpoint_sources", i, "party_name")
                matches = targets(key, path, "parties", source["party_name"])
                if len(matches) == 1:
                    resolved[key, path] = matches[0]
            if source["party_name"] not in c["endpoint_party_names"]:
                issue(
                    "endpoint source party is not a channel endpoint",
                    key,
                    path=("endpoint_sources", i, "party_name"),
                )
    network_nodes = {}
    state_reads = []
    state_writes = set()
    for key in model["steps"]:
        s = records[key]["value"]
        action = s.get("action")
        channel_key = ref(key, ("channel_name",), "channels", s.get("channel_name"))
        channel = records[channel_key]["value"] if channel_key is not None else None
        if action == "send":
            message_key = ref(key, ("message_name",), "messages", s.get("message_name"))
            fields = (
                set(records[message_key]["value"]["associated_fields"])
                if message_key is not None
                else set()
            )
            for hook, operations in s.get("processing", {}).items():
                party = s["receiver_name" if hook == "on_receive" else "sender_name"]
                for i, operation in enumerate(operations):
                    for access in ("reads", "writes"):
                        for j, value in enumerate(operation[access]):
                            path = ("processing", hook, i, access, j)
                            namespace, _, name = value.partition(".")
                            if namespace == "state" and name:
                                state = ref(key, path, "states", (party, name))
                                if state is not None:
                                    if access == "reads":
                                        state_reads.append((key, path, state))
                                    else:
                                        state_writes.add(state)
                            elif namespace == "message" and name:
                                if access == "writes" and hook != "before_send":
                                    issue(
                                        "only before_send may write message fields", key, path=path
                                    )
                                if message_key is not None and name not in fields:
                                    issue(
                                        f"field {name!r} is not carried by {s['name']!r}",
                                        key,
                                        message_key,
                                        path=path,
                                    )
                            elif value == "message_bytes":
                                if access != "reads" or hook == "before_send":
                                    issue(
                                        "message_bytes is read-only after send or on receive",
                                        key,
                                        path=path,
                                    )
                            else:
                                issue(f"invalid processing reference {value!r}", key, path=path)
            party_fields = ("sender_name", "receiver_name")
        elif action in NETWORK_ACTIONS:
            party_fields = ("party_name",)
        else:
            issue(
                f"step {s.get('name')!r} action is {action!r}, "
                "expected send, listen, connect, or close",
                key,
                path=("action",),
            )
            continue
        parties = [ref(key, (field,), "parties", s.get(field)) for field in party_fields]
        if action in NETWORK_ACTIONS and channel_key is not None and parties[0] is not None:
            identity = (parties[0], channel_key, action)
            if identity in network_nodes:
                issue(
                    f"duplicate network action {action} for party {s.get('party_name')!r} "
                    f"on channel {s.get('channel_name')!r}",
                    key,
                    network_nodes[identity],
                    channel_key,
                    path=("action",),
                )
            network_nodes[identity] = key
            role = {"connect": "initiator_party_name", "listen": "acceptor_party_name"}.get(action)
            if channel.get("connection") == "connection_oriented" and role:
                expected = channel[role]
                bound = targets(channel_key, (role,), "parties", expected)
                if expected is not None and len(bound) == 1 and parties[0] != bound[0]:
                    issue(
                        f"step {s['name']!r} {action} party {s.get('party_name')!r} "
                        f"contradicts channel {channel['name']!r} {role}={expected!r}; "
                        "use a distinct channel for a source-supported reversed-role lifecycle",
                        key,
                        channel_key,
                        parties[0],
                        bound[0],
                        path=("party_name",),
                    )
        if channel:
            endpoints = [
                targets(channel_key, ("endpoint_party_names", i), "parties", party)
                for i, party in enumerate(channel["endpoint_party_names"])
            ]
            if all(len(end) == 1 for end in endpoints):
                allowed = {end[0] for end in endpoints}
                for field, party in zip(party_fields, parties, strict=True):
                    if party is not None and party not in allowed:
                        issue(
                            f"step {s['name']} party is not a channel endpoint",
                            key,
                            channel_key,
                            party,
                            path=(field,),
                        )
    for key, path, state in state_reads:
        definition = records[state]["value"]
        if definition["source"] == "processing" and state not in state_writes:
            issue(
                f"state {definition['name']!r} has no processing producer",
                key,
                state,
                path=path,
            )
    for key in model["step_dependencies"]:
        d = records[key]["value"]
        ends = {
            field: ref(key, (field,), "steps", d[field])
            for field in ("prior_step", "dependent_step")
        }
        known = [step for step in ends.values() if step is not None]
        if d["field_relations"] and any(
            records[step]["value"]["action"] != "send" for step in known
        ):
            issue(
                "field relations require send steps on both ends of "
                f"{d['prior_step']} -> {d['dependent_step']}",
                key,
                *known,
                path=("field_relations",),
            )
            continue
        for i, relation in enumerate(d["field_relations"]):
            for side, step_field in (
                ("prior_field", "prior_step"),
                ("dependent_field", "dependent_step"),
            ):
                step_key = ends[step_field]
                if step_key is None:
                    continue
                step = records[step_key]["value"]
                messages = targets(
                    step_key, ("message_name",), "messages", step.get("message_name")
                )
                if len(messages) != 1:
                    continue
                m = records[messages[0]]["value"]
                allowed = set(m["associated_fields"])
                allowed |= {f"{m['name']}.{name}" for name in tuple(allowed)}
                field = relation[side]
                if field not in allowed:
                    issue(
                        f"field {field!r} is not carried by {d[step_field]}",
                        key,
                        step_key,
                        messages[0],
                        path=("field_relations", i, side),
                    )
    return issues


def coverage_issues(records: dict, resolved=None, ready=None) -> list[dict]:
    """Coverage rules: every declared object must take part in the interaction it models.

    Extraction gates these rules by scan stage (``ready``) because the objects they
    compare arrive in separate passes; a published model is always fully ready.
    """
    resolved = resolved or {}
    ready = tuple(COLLECTIONS) if ready is None else tuple(ready)
    by_kind = defaultdict(list)
    incoming = defaultdict(list)
    outgoing = defaultdict(list)
    actions = defaultdict(set)
    for key, row in records.items():
        by_kind[row["kind"]].append(key)
    for (owner, path), target in resolved.items():
        edge = {"from": owner, "field": list(path), "to": target}
        incoming[target].append(edge)
        outgoing[owner].append(edge)
    for step in by_kind["steps"]:
        value = records[step]["value"]
        channel = resolved.get((step, ("channel_name",)))
        party = resolved.get((step, ("party_name",)))
        if channel is not None and party is not None:
            actions[channel, value["action"]].add(party)

    def referenced_by(key, kind, field=None):
        return any(
            records[edge["from"]]["kind"] == kind
            and (field is None or edge["field"][0] == field)
            for edge in incoming[key]
        )

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

    def check_channel(key):
        value = records[key]["value"]
        name = value["name"]
        if value["connection"] != "connection_oriented":
            if not referenced_by(key, "steps", "channel_name"):
                add("channel_without_step", key, f"Channel {name!r} has no step")
            return
        for action, role in (
            ("connect", "initiator_party_name"),
            ("listen", "acceptor_party_name"),
        ):
            expected = resolved.get((key, (role,)))
            if expected is None or expected not in actions[key, action]:
                add(
                    f"channel_requires_{action}",
                    key,
                    f"Channel {name!r} requires a {action} step by its declared {role}",
                    field=(role,),
                )
        endpoints = {
            edge["to"] for edge in outgoing[key] if edge["field"][0] == "endpoint_party_names"
        }
        if not endpoints & actions[key, "close"]:
            add(
                "channel_requires_close",
                key,
                f"Channel {name!r} requires an endpoint close step",
                field=("connection",),
            )

    def check_state(key):
        value = records[key]["value"]
        name = value["name"]
        accesses = {
            edge["field"][3] for edge in incoming[key] if edge["field"][0] == "processing"
        }
        if not accesses:
            description = f"State {name!r} has no hook reads or writes"
            if value["source"] == "processing":
                description += " and no processing producer"
            add("state_without_access", key, description)

    if "channels" in ready:
        for key in by_kind["parties"]:
            if not referenced_by(key, "channels", "endpoint_party_names"):
                name = records[key]["value"]["name"]
                add("party_without_channel", key, f"Party {name!r} is not a channel endpoint")

    if "steps" in ready:
        for key in by_kind["channels"]:
            check_channel(key)
        for key in by_kind["messages"]:
            if not referenced_by(key, "steps", "message_name"):
                name = records[key]["value"]["name"]
                add("message_without_send", key, f"Message {name!r} has no send step")
        for key in by_kind["states"]:
            check_state(key)
        # Advisory: the source may prescribe a bare transmission, so the reviewer decides
        # whether an empty hook is a real gap. Kept visible but never blocking.
        for key in by_kind["steps"]:
            value = records[key]["value"]
            if value["action"] != "send" or any((value.get("processing") or {}).values()):
                continue
            add(
                "step_without_processing",
                key,
                f"Step {value['name']!r} sends {value['message_name']!r} with no processing",
                advisory=True,
            )

    if "step_dependencies" in ready:
        for key in by_kind["steps"]:
            if not referenced_by(key, "step_dependencies"):
                name = records[key]["value"]["name"]
                add("step_without_dependency", key, f"Step {name!r} has no dependency links")
    return checks


def duplicate_issues(records: dict) -> list[dict]:
    """Identical declarations the message/dependency schema cannot reject by itself."""
    issues = []
    seen = {}
    for key, row in records.items():
        value = row["value"]
        if row["kind"] == "step_dependencies":
            duplicate = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if duplicate in seen:
                issues.append(
                    {
                        "type": "duplicate_dependency",
                        "records": [key, seen[duplicate]],
                        "path": [],
                        "description": "duplicate dependency",
                    }
                )
            seen[duplicate] = key
        if row["kind"] == "messages":
            names = list(value["associated_fields"])
            if len(set(names)) != len(names):
                issues.append(
                    {
                        "type": "duplicate_field",
                        "records": [key],
                        "path": ["associated_fields"],
                        "description": "message field names must be unique",
                    }
                )
    return issues
