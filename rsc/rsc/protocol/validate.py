"""Schema, graph, role and field checks for protocol artifacts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from rsc.artifacts import read_json
from rsc.schemas import resolve_schemas_dir, validate_instance, validator

from .contract import COLLECTIONS, DEFINITIONS, protocol_schema
from .references import coverage_issues, duplicate_issues, reference_issues


def issue(description, objects=()):
    return {"description": description, "object_names": list(dict.fromkeys(objects))}


def object_errors(
    model: dict, documents: dict[str, dict] | None, schemas_dir=None
) -> dict[str, list[str]]:
    """Schema-shape errors only. Name references are not resolved or rejected."""

    del documents
    schema = protocol_schema(schemas_dir)
    result: dict[str, list[str]] = defaultdict(list)
    for key in COLLECTIONS:
        check = Draft202012Validator(
            {"$ref": f"#/$defs/{DEFINITIONS[key]}", "$defs": schema["$defs"]}
        )
        for index, row in enumerate(model.get(key, [])):
            if not isinstance(row, dict):
                result[f"{key}[{index}]"].append("object must be a mapping")
                continue
            errors = validate_instance(check, row)
            if key == "channels" and not errors and row.get("connection") == "connectionless":
                if (
                    row.get("initiator_party_name") is not None
                    or row.get("acceptor_party_name") is not None
                ):
                    errors.append("connectionless channels must have null connection roles")
            token = row.get("name") if key == "steps" and row.get("name") else f"{key}[{index}]"
            result[token].extend(errors)
    return {key: errors for key, errors in result.items() if errors}


def prune_invalid(
    model: dict, documents: dict[str, dict], schemas_dir=None
) -> tuple[dict, list[dict]]:
    model = deepcopy(model)
    issues = []
    while errors := object_errors(model, documents, schemas_dir):
        for key in COLLECTIONS:
            if key not in model:
                continue
            retained = []
            for index, row in enumerate(model[key]):
                token = (
                    row.get("name")
                    if key == "steps" and isinstance(row, dict) and row.get("name")
                    else f"{key}[{index}]"
                )
                if token in errors:
                    names = [row.get("name")] if isinstance(row, dict) and row.get("name") else []
                    issues.append(
                        issue(
                            "; ".join(errors[token]),
                            names,
                        )
                    )
                else:
                    retained.append(row)
            model[key] = retained
    return model, issues


def _source_errors(sources, documents, schemas_dir):
    errors = []
    expected = {source["doc_id"] for source in sources}
    if expected != set(documents) or len(expected) != len(sources):
        errors.append("source document set does not match meta.documents")
    source_check = validator("document", resolve_schemas_dir(schemas_dir))
    for source in sources:
        doc = documents.get(source["doc_id"])
        if doc is None:
            continue
        source_errors = validate_instance(source_check, doc)
        if source_errors:
            errors.extend(f"{source['doc_id']}: {error}" for error in source_errors)
            continue
        if doc and (
            doc["meta"]["source_id"] != source["doc_id"]
            or doc["meta"]["rfc"] != source["rfc"]
        ):
            errors.append(f"source identity mismatch: {source['doc_id']}")
    return errors


def validate_protocol(
    model: dict,
    documents: dict[str, dict] | None = None,
    schemas_dir=None,
    *,
    diagnostics=None,
    coverage: bool = False,
) -> list[str]:
    """Check the model, with additional publication checks when sources are supplied.

    ``coverage`` also applies the interaction-coverage rules that extraction enforces
    (channels have lifecycles and steps, messages are sent, states are accessed and
    produced, steps have dependency links). It is opt-in because partial or
    hand-written inventories legitimately leave those links open; the ``validate``
    command turns it on for published artifacts.
    """
    errors = validate_instance(Draft202012Validator(protocol_schema(schemas_dir)), model)
    if errors:
        return errors
    if documents is not None:
        errors.extend(_source_errors(model["meta"]["documents"], documents, schemas_dir))
    errors.extend(
        f"{key}: {error}"
        for key, values in object_errors(model, documents, schemas_dir).items()
        for error in values
    )
    if not errors:
        records = {
            f"{kind}:{index}": {"kind": kind, "value": value}
            for kind in COLLECTIONS
            for index, value in enumerate(model.get(kind, []))
        }
        resolved: dict[tuple[str, tuple], str] = {}
        for issue in reference_issues(records, resolved=resolved):
            errors.append(issue["description"])
            if diagnostics is not None:
                diagnostics.append(issue)
        if coverage:
            for issue in coverage_issues(records, resolved):
                # Advisory rows are visible in extraction but never block publication.
                if issue.get("advisory"):
                    continue
                errors.append(issue["description"])
                if diagnostics is not None:
                    diagnostics.append(issue)
        for issue in duplicate_issues(records):
            errors.append(issue["description"])
            if diagnostics is not None:
                diagnostics.append(issue)
    if documents is not None:
        if model["meta"]["complete"] and model["unresolved"]:
            errors.append("complete model cannot contain unresolved issues")
        if not any(model.get(key) for key in COLLECTIONS):
            errors.append("protocol model must not be empty")
    return errors


def validate_protocol_file(
    path: str | Path, schemas_dir=None, *, coverage: bool = False
) -> list[str]:
    """Validate a standalone protocol JSON file without opening source snapshots.

    Pass ``coverage=True`` to also require the interaction-coverage links that
    extraction enforces; ``rsc validate`` does so for published artifacts.
    """
    path = Path(path).expanduser()
    try:
        return validate_protocol(read_json(path), schemas_dir=schemas_dir, coverage=coverage)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"cannot validate protocol: {exc}"]
