"""The new artifact format preserves validation and source locators without digests."""

from copy import deepcopy

import pytest
from protocol_samples import TEXT, base, document, interactions
from test_schemas_cli import SAMPLES

from rsc.artifacts import read_json, write_json
from rsc.index import build_structural_index, validate_index
from rsc.ingest import IngestOptions, ingest_path, validate_document
from rsc.protocol.context import load_batch
from rsc.protocol.models import Protocol
from rsc.protocol.validate import validate_protocol, validate_protocol_file
from rsc.schemas import (
    REMOVED_METADATA,
    default_schemas_dir,
    legacy_metadata_errors,
    validate_instance,
    validator,
)
from rsc.stats import collect_protocol, compute_stats


def published():
    model = base()
    model.update(interactions(model))
    model["meta"] = {
        "protocol": "demo",
        "complete": True,
        "documents": [{"doc_id": "rfc9999", "rfc": "RFC 9999", "path": "sources/doc.json"}],
    }
    return model


def test_publication_preserves_sources_names_and_payload_field_names(tmp_path):
    doc = document()
    model = published()
    model["messages"][0]["associated_fields"] += [
        {"name": "hash"},
        {"name": "version"},
        {"name": "schema_version"},
    ]
    assert set(Protocol.model_validate(model).meta.model_dump()) == {
        "protocol",
        "complete",
        "documents",
    }
    assert validate_protocol(model, {"rfc9999": doc}) == []
    path = tmp_path / "protocol.json"
    (tmp_path / "sources").mkdir()
    write_json(tmp_path / "sources/doc.json", doc)
    write_json(path, model)
    assert validate_protocol_file(path) == []
    assert collect_protocol(tmp_path) == (path, model)
    stats = compute_stats(model)
    assert not REMOVED_METADATA.intersection(stats)
    assert stats["counts"]["steps"] == len(model["steps"])


@pytest.mark.parametrize("key", sorted(REMOVED_METADATA))
@pytest.mark.parametrize("container", ["meta", "source"])
def test_legacy_publication_metadata_is_rejected_everywhere(tmp_path, key, container):
    model = published()
    target = model["meta"] if container == "meta" else model["meta"]["documents"][0]
    target[key] = "old"
    errors = validate_protocol(model, {"rfc9999": document()})
    assert any(key in error and "legacy metadata" in error for error in errors)
    path = tmp_path / "protocol.json"
    write_json(path, model)
    assert "legacy metadata" in "; ".join(validate_protocol_file(path))
    with pytest.raises(ValueError, match="legacy metadata"):
        collect_protocol(path)
    with pytest.raises(ValueError, match="legacy metadata"):
        compute_stats(model)


@pytest.mark.parametrize("stage", sorted(SAMPLES))
def test_schema_rejects_legacy_artifacts(stage):
    sample = deepcopy(SAMPLES[stage])
    target = sample.get("meta", sample)
    target["schema_version"] = "old"
    check = validator(stage, default_schemas_dir())
    assert list(check.iter_errors(sample))
    assert "legacy metadata" in "; ".join(validate_instance(check, sample))


def test_source_identity_schema_and_path_validation_remain(tmp_path):
    model = published()
    doc = document()
    changed = deepcopy(doc)
    changed["meta"]["source_id"] = "another"
    assert "source identity mismatch" in "; ".join(validate_protocol(model, {"rfc9999": changed}))
    changed = deepcopy(doc)
    changed["blocks"][0]["order"] = "invalid"
    assert validate_protocol(model, {"rfc9999": changed})
    changed = deepcopy(doc)
    changed["meta"]["input_sha256"] = "old"
    assert "legacy metadata" in "; ".join(validate_protocol(model, {"rfc9999": changed}))
    model["meta"]["documents"][0]["path"] = "../outside.json"
    write_json(tmp_path / "protocol.json", model)
    assert validate_protocol_file(tmp_path / "protocol.json") == [
        "source snapshot path escapes model directory"
    ]


def test_name_and_field_reference_checks_remain():
    model = published()
    model["steps"][0]["receiver_name"] = "missing"
    assert "reference 'missing' has 0 matches" in "; ".join(
        validate_protocol(model, {"rfc9999": document()})
    )
    model = published()
    model["parties"].append(deepcopy(model["parties"][0]))
    assert "ambiguous parties name" in "; ".join(validate_protocol(model, {"rfc9999": document()}))
    model = published()
    model["step_dependencies"][0]["field_relations"][0]["prior_field"] = "missing"
    assert "not carried" in "; ".join(validate_protocol(model, {"rfc9999": document()}))


def test_metadata_checks_do_not_inspect_payloads_or_source_attributes():
    doc = document()
    doc["blocks"][0]["attrs"] = {"hash": "wire value", "schema_version": "payload value"}
    assert legacy_metadata_errors(doc) == []
    assert validate_document(doc, source_text=TEXT, line_count=len(TEXT.splitlines())) == []
    assert validate_instance(validator("document", default_schemas_dir()), doc) == []
    index = build_structural_index(doc)
    assert validate_index(index, doc) == []
    assert not REMOVED_METADATA.intersection(index["meta"])
    old = deepcopy(index)
    old["meta"]["input_sha256"] = "old"
    assert "legacy metadata" in "; ".join(validate_index(old, doc))


def test_ingest_reparses_changed_sources_and_keeps_distinct_paths(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("first.txt", "second.txt"):
        (inputs / name).write_text(TEXT)
    out = tmp_path / "out"
    options = IngestOptions(out_dir=out)
    result = ingest_path(inputs, options)
    assert result.exit_code() == 0
    assert [row.doc_id for row in result.documents] == ["rfc9999", "rfc9999-second.txt"]
    batch = read_json(out / "batch.json")
    assert legacy_metadata_errors(batch) == []
    documents = load_batch(out, default_schemas_dir())
    assert set(documents) == {"rfc9999", "rfc9999-second.txt"}
    assert all(not REMOVED_METADATA.intersection(doc["meta"]) for doc in documents.values())
    for row in result.documents:
        assert not REMOVED_METADATA.intersection(read_json(row.report_path))
    (inputs / "first.txt").write_text(TEXT.replace("over TCP", "over UDP"))
    rerun = ingest_path(inputs, options)
    assert [row.doc_id for row in rerun.documents] == [row.doc_id for row in result.documents]
    assert all(row.status == "ok" for row in rerun.documents)
    assert any(
        "over UDP" in b["text"] for b in read_json(rerun.documents[0].document_path)["blocks"]
    )
    batch["documents"][0]["doc_id"] = "wrong"
    write_json(out / "batch.json", batch)
    with pytest.raises(ValueError, match="source identity mismatch"):
        load_batch(out, default_schemas_dir())


def test_legacy_ingest_output_requires_explicit_regeneration(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(TEXT)
    options = IngestOptions(out_dir=tmp_path / "out")
    result = ingest_path(source, options)
    path = result.documents[0].document_path
    old = read_json(path)
    old["meta"]["schema_version"] = "old"
    write_json(path, old)
    rejected = ingest_path(source, options)
    assert rejected.exit_code() == 1
    assert "legacy metadata" in rejected.documents[0].error
    assert read_json(path) == old
    regenerated = ingest_path(source, IngestOptions(out_dir=options.out_dir, force=True))
    assert regenerated.exit_code() == 0
    assert legacy_metadata_errors(read_json(path)) == []
