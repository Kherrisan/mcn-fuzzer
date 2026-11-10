"""Name, field-carrier, endpoint and network-action checks shared with scoped merging."""

from .contract import COLLECTIONS

NETWORK_ACTIONS = ("listen", "connect", "close")


def reference_errors(records: dict) -> list[str]:
    return [issue["description"] for issue in reference_issues(records)]


def reference_issues(records: dict, bindings=None) -> list[dict]:
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
                names[kind].setdefault(records[key]["value"]["name"], []).append(key)
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
        return matches[0]

    for kind, values in names.items():
        for name, keys in values.items():
            if len(keys) != 1:
                issue(f"ambiguous {kind} name {name!r}", *keys, path=("name",), type_="same_name")
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
            if source["party_name"] not in c["endpoint_party_names"]:
                issue(
                    "endpoint source party is not a channel endpoint",
                    key,
                    path=("endpoint_sources", i, "party_name"),
                )
    network_nodes = {}
    for key in model["steps"]:
        s = records[key]["value"]
        action = s.get("action")
        channel_key = ref(key, ("channel_name",), "channels", s.get("channel_name"))
        channel = records[channel_key]["value"] if channel_key is not None else None
        if action == "send":
            ref(key, ("message_name",), "messages", s.get("message_name"))
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
                allowed = {f["name"] for f in m["associated_fields"]}
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
