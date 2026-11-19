"""Compute a review assignment from the frozen Fix input and validated output."""

from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher

from .store import dumps


def label(item):
    kind, value = item["kind"], item["value"]
    if kind == "step_dependencies":
        parts = {k: value[k] for k in ("prior_step", "dependent_step", "condition")}
    else:
        parts = {"name": value["name"]}
        if kind == "states":
            parts["party_name"] = value["party_name"]
    return {"kind": kind, **parts}


def changed_rows(before, after):
    """Compare multisets, retaining removals and their old evidence."""
    remaining = Counter(map(dumps, after))
    for item in before:
        key = dumps(item)
        if remaining[key]:
            remaining[key] -= 1
        else:
            yield item


def changed_operations(before, after):
    """Match unchanged operations within each hook even when an insertion shifts indices."""
    old, new = before.get("processing", {}), after.get("processing", {})
    for hook in sorted(old.keys() | new.keys()):
        owner = "receiver_name" if hook == "on_receive" else "sender_name"
        context = ("action", "channel_name", "condition", owner)
        left, right = old.get(hook, []), new.get(hook, [])
        if any(before.get(k) != after.get(k) for k in context):
            yield from ((before, hook, op, None) for op in left)
            yield from ((after, hook, op, None) for op in right)
            continue
        matcher = SequenceMatcher(
            a=list(map(dumps, left)), b=list(map(dumps, right)), autojunk=False
        )
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_ops, new_ops = left[old_start:old_end], right[new_start:new_end]
            if (
                tag == "replace"
                and len(old_ops) == len(new_ops)
                and before.get("message_name") == after.get("message_name")
            ):
                for old_op, new_op in zip(old_ops, new_ops, strict=True):
                    yield before, hook, old_op, new_op
                    yield after, hook, new_op, old_op
            elif tag != "equal":
                yield from ((before, hook, op, None) for op in old_ops)
                yield from ((after, hook, op, None) for op in new_ops)
            elif before.get("message_name") != after.get("message_name"):
                for old_op, new_op in zip(old_ops, new_ops, strict=True):
                    if any(
                        ref == "message_bytes" or ref.startswith("message.")
                        for ref in [*old_op["reads"], *old_op["writes"]]
                    ):
                        yield before, hook, old_op, None
                        yield after, hook, new_op, None


def access_keys(step, hook, op, access):
    owner = step["receiver_name" if hook == "on_receive" else "sender_name"]
    for ref in op[access]:
        if ref.startswith("state."):
            yield "states", owner, ref[6:]
        elif ref.startswith("message."):
            yield "field", step["name"], ref[8:]
        elif ref == "message_bytes":
            yield "bytes", step["name"], ""


def build_scope(before, after, batches, findings, *, before_path, after_path):
    scope = {
        "before": before_path,
        "after": after_path,
        "previous_findings": findings,
        "changed": [],
        "affected": [],
    }
    fallback = None
    if before is None:
        fallback = "initial review or missing Fix input"
    else:
        old, new = before["items"], after["items"]
        removed, added = list(changed_rows(old, new)), list(changed_rows(new, old))
        scope["changed"] = [
            {"side": side, "object": label(item), "evidence": item["evidence"]}
            for side, rows in (("before", removed), ("after", added))
            for item in rows
        ]
        if any(f["global"] for f in findings if f["category"] == "correctable"):
            fallback = "reviewer could not bound a finding's impact"
        elif {dumps(x) for x in before["unresolved"]} != {dumps(x) for x in after["unresolved"]}:
            fallback = "unresolved issues changed without explicit source bindings"
        else:
            common = Counter(
                dumps(row["value"]) for row in old if row["kind"] in {"parties", "channels"}
            )
            updated = Counter(
                dumps(row["value"]) for row in new if row["kind"] in {"parties", "channels"}
            )
            if common != updated:
                fallback = "party or channel definitions changed"
    if fallback:
        return {**scope, "batches": batches, "full_review_reason": fallback}

    evidence = {b for f in findings if f["category"] == "correctable" for b in f["batch_ids"]}
    changed = [*removed, *added]
    for item in changed:
        evidence.update(e["batch_id"] for e in item["evidence"])

    indexed = defaultdict(list)
    for item in [*old, *new]:
        indexed[dumps(label(item))].append(item)

    affected = {}

    def include(item, reason):
        obj = label(item)
        key = dumps(obj)
        for version in indexed[key]:
            evidence.update(e["batch_id"] for e in version["evidence"])
        entry = affected.setdefault(key, {"object": obj, "reasons": []})
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    def named(rows, kind):
        return {row["value"]["name"]: row for row in rows if row["kind"] == kind}

    old_steps, new_steps = named(old, "steps"), named(new, "steps")
    old_messages, new_messages = named(old, "messages"), named(new, "messages")
    control_steps = set()
    inputs, outputs = set(), set()
    for name in sorted(old_steps.keys() | new_steps.keys()):
        left = old_steps.get(name, {}).get("value", {})
        right = new_steps.get(name, {}).get("value", {})
        old_control = {k: v for k, v in left.items() if k != "processing"}
        new_control = {k: v for k, v in right.items() if k != "processing"}
        if old_control != new_control:
            control_steps.add(name)
        for step, hook, op, matching in changed_operations(left, right):
            reads = set(access_keys(step, hook, op, "reads"))
            writes = set(access_keys(step, hook, op, "writes"))
            if matching and op["rule"] == matching["rule"] and op["reads"] == matching["reads"]:
                # A write-list edit with the same inputs and rule changes only
                # the added or removed write targets.
                writes.difference_update(access_keys(step, hook, matching, "writes"))
            if writes or matching is None or op["reads"] != matching["reads"]:
                inputs.update(reads)
            outputs.update(writes)

    message_fields = {}
    for name in sorted(old_messages.keys() | new_messages.keys()):
        left = old_messages.get(name, {}).get("value", {})
        right = new_messages.get(name, {}).get("value", {})
        before_fields = {f["name"]: f for f in left.get("associated_fields", [])}
        after_fields = {f["name"]: f for f in right.get("associated_fields", [])}
        fields = {
            key
            for key in before_fields.keys() | after_fields.keys()
            if before_fields.get(key) != after_fields.get(key)
        }
        if fields:
            message_fields[name] = fields

    readers, writers = defaultdict(list), defaultdict(list)

    state_definitions = defaultdict(list)
    for item in [*old, *new]:
        if item["kind"] == "states":
            val = item["value"]
            state_definitions[("states", val["party_name"], val["name"])].append(item)
    for item in changed:
        if item["kind"] == "states":
            val = item["value"]
            versions = indexed[dumps(label(item))]
            if len(versions) == 1 or versions[0]["value"] != versions[-1]["value"]:
                outputs.add(("states", val["party_name"], val["name"]))

    # Evidence-only edits select their source batches without changing data flow.
    edge_values = {
        side: {dumps(row["value"]) for row in rows if row["kind"] == "step_dependencies"}
        for side, rows in (("before", old), ("after", new))
    }
    edge_relations = {
        side: {
            (dumps(label(row)), dumps(relation))
            for row in rows
            if row["kind"] == "step_dependencies"
            for relation in row["value"].get("field_relations", [])
        }
        for side, rows in (("before", old), ("after", new))
    }

    def include_edge(item, reason):
        include(item, reason)
        edge = item["value"]
        for field in ("prior_step", "dependent_step"):
            for steps in (old_steps, new_steps):
                if step := steps.get(edge[field]):
                    include(step, "endpoint of affected dependency")

    def field_key(step, field):
        message = step.get("message_name")
        prefix = f"{message}."
        return ("field", step["name"], field.removeprefix(prefix) if message else field)

    for side, rows, steps in (("before", old, old_steps), ("after", new, new_steps)):
        other = "after" if side == "before" else "before"
        for item in steps.values():
            step = item["value"]
            for field in sorted(message_fields.get(step.get("message_name"), ())):
                outputs.add(("field", step["name"], field))
                include(item, "uses a changed message field")
            for hook, ops in step.get("processing", {}).items():
                for op in ops:
                    writes = set(access_keys(step, hook, op, "writes"))
                    for key in access_keys(step, hook, op, "reads"):
                        readers[key].append((item, writes))
                    for key in writes:
                        writers[key].append(item)
        for item in (row for row in rows if row["kind"] == "step_dependencies"):
            edge = item["value"]
            prior, dependent = edge["prior_step"], edge["dependent_step"]
            changed_control = bool({prior, dependent} & control_steps)
            if dumps(edge) not in edge_values[other] or changed_control:
                include_edge(item, "changed dependency or step control field")
            for relation in edge.get("field_relations", []):
                source = field_key(steps[prior]["value"], relation["prior_field"])
                target = field_key(steps[dependent]["value"], relation["dependent_field"])
                readers[source].append((item, {target}))
                writers[target].append(item)
                # A changed edge can change the value supplied to the dependent step.
                if (
                    changed_control
                    or (dumps(label(item)), dumps(relation)) not in edge_relations[other]
                ):
                    inputs.add(source)
                    outputs.add(target)

    def include_producers(key):
        for item in writers[key]:
            if item["kind"] == "step_dependencies":
                include_edge(item, "supplies a field used by affected processing")
            else:
                include(item, "produces a field or state used by affected processing")
        for item in state_definitions[key]:
            include(item, "state used by affected processing")
        if key[0] in {"field", "bytes"}:
            for steps, messages in ((old_steps, old_messages), (new_steps, new_messages)):
                step = steps.get(key[1])
                if step and (message := messages.get(step["value"].get("message_name"))):
                    include(message, "defines a message used by affected processing")

    for key in sorted(inputs):
        include_producers(key)

    # Follow consumers only from changed outputs. An unchanged input producer
    # supplies context without causing all the other readers to be reviewed.
    queue = deque(sorted(outputs))
    seen = set(outputs)
    while queue:
        key = queue.popleft()
        include_producers(key)
        consumers = readers[key]
        if key[0] == "field":
            consumers = [*consumers, *readers[("bytes", key[1], "")]]
        for item, writes in consumers:
            if item["kind"] == "step_dependencies":
                include_edge(item, "carries an affected message field")
            else:
                include(item, "reads an affected field or state")
            pending = writes - seen
            queue.extend(sorted(pending))
            seen.update(pending)

    selected = [batch for batch in batches if batch in evidence]
    return {
        **scope,
        "affected": list(affected.values()),
        "batches": selected or batches,
        "full_review_reason": None if selected else "no source evidence localized the review",
    }
