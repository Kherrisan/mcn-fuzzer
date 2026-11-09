"""Name, field-carrier, endpoint and network-action checks for published models."""

from collections import Counter

from .contract import COLLECTIONS

NETWORK_ACTIONS = ("listen", "connect", "close")


def reference_errors(records: dict) -> list[str]:
    model = {
        kind: [r["value"] for r in records.values() if r["kind"] == kind] for kind in COLLECTIONS
    }
    counts = {
        kind: Counter(r["name"] for r in rows)
        for kind, rows in model.items()
        if kind != "step_dependencies"
    }
    errors = []

    def ref(kind, name):
        if counts[kind][name] != 1:
            errors.append(f"{kind} reference {name!r} has {counts[kind][name]} matches")

    for kind, values in counts.items():
        for name, n in values.items():
            if n != 1:
                errors.append(f"ambiguous {kind} name {name!r}")
    channels = {r["name"]: r for r in model["channels"]}
    messages = {r["name"]: r for r in model["messages"]}
    steps = {r["name"]: r for r in model["steps"]}
    for c in model["channels"]:
        for p in c["endpoint_party_names"]:
            ref("parties", p)
        for p in [c["initiator_party_name"], c["acceptor_party_name"]]:
            if p is not None and p not in c["endpoint_party_names"]:
                errors.append(f"channel {c['name']} role is not an endpoint")
        if c["connection"] == "connectionless" and (
            c["initiator_party_name"] is not None or c["acceptor_party_name"] is not None
        ):
            errors.append("connectionless channel has connection roles")
        for source in c["endpoint_sources"]:
            if source["party_name"] not in c["endpoint_party_names"]:
                errors.append("endpoint source party is not a channel endpoint")
    network_nodes = {}
    for s in model["steps"]:
        action = s.get("action")
        ref("channels", s.get("channel_name"))
        channel = channels.get(s.get("channel_name"))
        if action == "send":
            ref("messages", s.get("message_name"))
            ref("parties", s.get("sender_name"))
            ref("parties", s.get("receiver_name"))
            parties = (s.get("sender_name"), s.get("receiver_name"))
        elif action in NETWORK_ACTIONS:
            ref("parties", s.get("party_name"))
            parties = (s.get("party_name"),)
            key = (s.get("party_name"), s.get("channel_name"), action)
            if key in network_nodes:
                errors.append(
                    f"duplicate network action {action} for party {s.get('party_name')!r} "
                    f"on channel {s.get('channel_name')!r}"
                )
            network_nodes[key] = s.get("name")
        else:
            errors.append(
                f"step {s.get('name')!r} action is {action!r}, expected send, listen, connect, or close"
            )
            continue
        if channel and any(party not in channel["endpoint_party_names"] for party in parties):
            errors.append(f"step {s['name']} party is not a channel endpoint")
    for d in model["step_dependencies"]:
        ref("steps", d["prior_step"])
        ref("steps", d["dependent_step"])
        ends = [steps.get(d["prior_step"], {}), steps.get(d["dependent_step"], {})]
        known = [step for step in ends if step]
        if d["field_relations"] and any(step.get("action") != "send" for step in known):
            errors.append(
                f"field relations require send steps on both ends of {d['prior_step']} -> {d['dependent_step']}"
            )
            continue
        for relation in d["field_relations"]:
            for side, step_key in [
                ("prior_field", "prior_step"),
                ("dependent_field", "dependent_step"),
            ]:
                s = steps.get(d[step_key], {})
                m = messages.get(s.get("message_name"), {})
                field = relation[side]
                allowed = {f["name"] for f in m.get("associated_fields", [])}
                allowed |= {f"{m.get('name')}.{name}" for name in tuple(allowed)}
                if field not in allowed:
                    errors.append(f"field {field!r} is not carried by {d[step_key]}")
    return errors
