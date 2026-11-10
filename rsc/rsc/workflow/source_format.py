"""Lossless column encoding for complete source batches (no text summarization)."""

from copy import deepcopy


def encode_source_batch(batch):
    result = {key: deepcopy(value) for key, value in batch.items() if key != "segments"}
    groups = []
    for original in batch["segments"]:
        segment = deepcopy(original)
        derived = "evidence" in segment
        if derived:
            evidence = segment.pop("evidence")
            if evidence != {"batch_id": batch["batch_id"], "block_id": segment["block_id"]}:
                raise ValueError("source segment evidence must match its batch and block")
        columns = sorted(segment)
        # Consecutive groups preserve source order even when optional keys differ.
        if (
            not groups
            or groups[-1]["columns"] != columns
            or groups[-1]["derive_evidence"] != derived
        ):
            groups.append({"columns": columns, "derive_evidence": derived, "rows": []})
        groups[-1]["rows"].append([segment[column] for column in columns])
    result["segments"] = {"encoding": "columns-v1", "groups": groups}
    return result


def decode_source_batch(batch):
    if isinstance(batch["segments"], list):
        return deepcopy(batch)
    payload = batch["segments"]
    if payload.get("encoding") != "columns-v1":
        raise ValueError("unknown source segment encoding")
    result = {key: deepcopy(value) for key, value in batch.items() if key != "segments"}
    segments = []
    for group in payload["groups"]:
        columns = group["columns"]
        if len(columns) != len(set(columns)):
            raise ValueError("duplicate source columns")
        for row in group["rows"]:
            segment = dict(zip(columns, deepcopy(row), strict=True))
            if group["derive_evidence"]:
                segment["evidence"] = {
                    "batch_id": batch["batch_id"],
                    "block_id": segment["block_id"],
                }
            segments.append(segment)
    result["segments"] = segments
    return result
