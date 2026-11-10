"""Lossless compact mutation arguments; expansion precedes normal validation."""

import json
from copy import deepcopy

from .evidence import expand_evidence

RESERVED = {"@record_id", "@target", "@reason", "@evidence"}


def encode_changes(changes):
    pool, indexes, groups = [], {}, []
    for change in changes:
        op = change["op"]
        if op == "delete" and "value" in change:
            raise ValueError("compact deletes cannot carry an unused value")
        value = deepcopy(change.get("value", {}))
        if any(key.startswith("@") for key in value):
            raise ValueError("value fields cannot use compact reserved names")
        for key in ("record_id", "target", "reason"):
            if key in change:
                value["@" + key] = change[key]
        if "evidence" in change:
            refs = []
            for item in change["evidence"]:
                key = json.dumps(item, sort_keys=True)
                if key not in indexes:
                    indexes[key] = len(pool)
                    pool.append(deepcopy(item))
                refs.append(indexes[key])
            value["@evidence"] = refs
        columns = sorted(value)
        kind = change.get("kind")
        if (
            not groups
            or groups[-1]["op"] != op
            or groups[-1].get("kind") != kind
            or groups[-1]["columns"] != columns
        ):
            groups.append({"op": op, "columns": columns, "rows": []})
            if kind is not None:
                groups[-1]["kind"] = kind
        groups[-1]["rows"].append([value[column] for column in columns])
    return {"evidence": pool, "groups": groups}


def decode_changes(payload, citations=None):
    if (
        not isinstance(payload, dict)
        or "groups" not in payload
        or set(payload) - {"evidence", "groups"}
    ):
        raise ValueError("compact changes require groups and optional evidence")
    citations = {} if citations is None else citations
    pool, groups = payload.get("evidence", []), payload["groups"]
    if not isinstance(pool, list) or not isinstance(groups, list) or not groups:
        raise ValueError("compact evidence must be an array and groups a nonempty array")
    pool = expand_evidence(pool, citations, path="compact evidence")
    changes = []
    widths = []
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        columns, rows = group.get("columns"), group.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            for ri, row in enumerate(rows):
                if isinstance(row, list) and len(row) != len(columns):
                    widths.append(
                        f"compact groups[{gi}].rows[{ri}] length must equal columns length: "
                        f"got {len(row)}, expected {len(columns)} ({columns})"
                    )
    if widths:
        raise ValueError("; ".join(widths))
    for gi, group in enumerate(groups):
        if not isinstance(group, dict) or set(group) - {
            "op",
            "kind",
            "columns",
            "rows",
            "defaults",
        }:
            raise ValueError(f"compact groups[{gi}] has unknown keys")
        op, columns, rows = group.get("op"), group.get("columns"), group.get("rows")
        if op not in {"add", "update", "patch", "delete"}:
            raise ValueError(f"compact groups[{gi}] has invalid op")
        if columns is not None:
            if (
                not isinstance(columns, list)
                or not columns
                or any(not isinstance(c, str) or not c for c in columns)
                or len(set(columns)) != len(columns)
            ):
                raise ValueError(f"compact groups[{gi}] requires unique nonempty columns")
            if any(c.startswith("@") and c not in RESERVED for c in columns):
                raise ValueError(f"compact groups[{gi}] has an unknown reserved column")
        defaults = group.get("defaults", {})
        if not isinstance(defaults, dict) or any(key.startswith("@") for key in defaults):
            raise ValueError(f"compact groups[{gi}] defaults must contain only value fields")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"compact groups[{gi}] requires nonempty rows")
        if op == "add" and not isinstance(group.get("kind"), str):
            raise ValueError(f"compact groups[{gi}] add requires kind")
        for ri, row in enumerate(rows):
            path = f"compact groups[{gi}].rows[{ri}]"
            if isinstance(row, dict):
                unknown = set(row) - {
                    "op",
                    "kind",
                    "value",
                    "evidence",
                    "record_id",
                    "target",
                    "reason",
                }
                if unknown:
                    raise ValueError(
                        f"{path} unknown row keys {sorted(unknown)}; "
                        "protocol fields belong in value"
                    )
                if (
                    "op" in row
                    and row["op"] != op
                    or "kind" in row
                    and row["kind"] != group.get("kind")
                ):
                    raise ValueError(f"{path} op/kind must match the group")
                if not isinstance(row.get("value", {}), dict):
                    raise ValueError(f"{path} value must be an object")
                value = {**deepcopy(defaults), **deepcopy(row.get("value", {}))}
                if any(key.startswith("@") for key in value):
                    raise ValueError(f"{path} value cannot contain reserved metadata")
                for key in ("record_id", "target", "reason"):
                    if key in row:
                        value["@" + key] = deepcopy(row[key])
                if "evidence" in row:
                    value["@evidence"] = deepcopy(row["evidence"])
            elif isinstance(row, list) and isinstance(columns, list):
                value = {**deepcopy(defaults), **dict(zip(columns, deepcopy(row), strict=True))}
            else:
                raise ValueError(f"{path} requires a named row or an array with columns")
            if op in {"update", "patch", "delete"} and (
                ("@record_id" in value) == ("@target" in value)
            ):
                raise ValueError(f"{path} requires exactly one record_id or target")
            required = {"@reason"} if op == "delete" else {"@evidence"}
            if not required.issubset(value):
                raise ValueError(f"{path} missing metadata {sorted(required - set(value))}")
            change = {"op": op}
            if "kind" in group:
                change["kind"] = group["kind"]
            for key in ("record_id", "target", "reason"):
                if "@" + key in value:
                    change[key] = value.pop("@" + key)
            if "@evidence" in value:
                refs = value.pop("@evidence")
                if not isinstance(refs, list) or not refs:
                    raise ValueError(f"{path} @evidence requires valid nonnegative pool indexes")
                evidence = []
                for ref in refs:
                    if type(ref) is int and 0 <= ref < len(pool):
                        evidence.append(deepcopy(pool[ref]))
                    elif isinstance(ref, str):
                        evidence.extend(expand_evidence([ref], citations, path=path + " @evidence"))
                    elif isinstance(ref, dict):
                        evidence.extend(expand_evidence([ref], citations, path=path + " evidence"))
                    else:
                        raise ValueError(
                            f"{path} @evidence requires valid nonnegative pool indexes"
                        )
                change["evidence"] = evidence
            if op == "delete":
                if value:
                    raise ValueError(f"{path} delete cannot carry value columns")
            else:
                change["value"] = value
            changes.append(change)
    return changes
