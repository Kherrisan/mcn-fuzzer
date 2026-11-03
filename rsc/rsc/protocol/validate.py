"""Schema, graph, role and field checks for protocol artifacts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from rsc.artifacts import read_json
from rsc.schemas import validate_instance

from .context import document_digest
from .contract import COLLECTIONS, DEFINITIONS, protocol_schema
from .merge import issue
from .models import PROTOCOL_VERSION


def object_errors(
    model: dict, documents: dict[str, dict], schemas_dir=None
) -> dict[str, list[str]]:
    """Schema-shape errors only. Name references are not resolved or rejected."""

    del documents
    schema = protocol_schema(schemas_dir)
    result: dict[str, list[str]] = defaultdict(list)
    step_ids = [row.get("id") for row in model.get("steps", []) if isinstance(row, dict)]
    if len(step_ids) != len(set(step_ids)):
        for row in model.get("steps", []):
            if isinstance(row, dict) and step_ids.count(row.get("id")) > 1:
                result[str(row.get("id"))].append("step IDs must be unique")
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
            token = row["id"] if key == "steps" and "id" in row else f"{key}[{index}]"
            result[token].extend(errors)
    return {key: errors for key, errors in result.items() if errors}


def prune_invalid(
    model: dict, documents: dict[str, dict], schemas_dir=None
) -> tuple[dict, list[dict]]:
    model = deepcopy(model)
    issues = []
    while errors := object_errors(model, documents, schemas_dir):
        for key in COLLECTIONS:
            retained = []
            for index, row in enumerate(model[key]):
                token = (
                    row["id"]
                    if key == "steps" and isinstance(row, dict) and "id" in row
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


def validate_protocol(model: dict, documents: dict[str, dict], schemas_dir=None) -> list[str]:
    if (
        isinstance(model, dict)
        and isinstance(model.get("meta"), dict)
        and model["meta"].get("schema_version") != PROTOCOL_VERSION
    ):
        return [
            f"unsupported protocol schema version; recompile RFC sources for {PROTOCOL_VERSION}"
        ]
    errors = validate_instance(Draft202012Validator(protocol_schema(schemas_dir)), model)
    if errors:
        return errors
    step_ids = [row["id"] for row in model["steps"]]
    if len(step_ids) != len(set(step_ids)):
        errors.append("step IDs must be unique")
    expected = {source["doc_id"] for source in model["meta"]["documents"]}
    if expected != set(documents) or len(expected) != len(model["meta"]["documents"]):
        errors.append("source document set does not match meta.documents")
    for source in model["meta"]["documents"]:
        doc = documents.get(source["doc_id"])
        if doc and (
            document_digest(doc) != source["document_sha256"]
            or doc["meta"]["input_sha256"] != source["input_sha256"]
            or doc["meta"]["source_id"] != source["doc_id"]
            or doc["meta"]["rfc"] != source["rfc"]
        ):
            errors.append(f"source identity/checksum mismatch: {source['doc_id']}")
    errors.extend(
        f"{key}: {error}"
        for key, values in object_errors(model, documents, schemas_dir).items()
        for error in values
    )
    if model["meta"]["complete"] and model["unresolved"]:
        errors.append("complete model cannot contain unresolved issues")
    if not any(model[key] for key in COLLECTIONS):
        errors.append("protocol model must not be empty")
    return errors


def validate_protocol_file(path: str | Path, schemas_dir=None) -> list[str]:
    path = Path(path).expanduser()
    try:
        model = read_json(path)
        if (
            isinstance(model, dict)
            and isinstance(model.get("meta"), dict)
            and model["meta"].get("schema_version") != PROTOCOL_VERSION
        ):
            return [
                f"unsupported protocol schema version; recompile RFC sources for {PROTOCOL_VERSION}"
            ]
        errors = validate_instance(Draft202012Validator(protocol_schema(schemas_dir)), model)
        if errors:
            return errors
        documents = {}
        for source in model["meta"]["documents"]:
            document_path = (path.parent / source["path"]).resolve()
            if not document_path.is_relative_to(path.parent.resolve()):
                return ["source snapshot path escapes model directory"]
            documents[source["doc_id"]] = read_json(document_path)
        return validate_protocol(model, documents, schemas_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"cannot validate protocol sources: {exc}"]
