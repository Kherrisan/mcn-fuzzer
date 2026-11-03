#!/usr/bin/env python3
"""Compare a real model with a selected hand-annotated acceptance subset.

This is strict structural matching, not a semantic accuracy estimate. Review misses
and the saved source documents manually; equivalent wording may score as a miss.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rsc.artifacts import read_json
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.validate import validate_protocol_file


def project(model):
    def transform(value):
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            return {
                key: transform(item)
                for key, item in value.items()
                if key not in {"description", "id"}
            }
        if isinstance(value, str):
            return " ".join(value.split()).casefold()
        return value

    return {key: [transform(row) for row in model[key]] for key in COLLECTIONS}


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and subset(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(subset(e, a) for a in actual) for e in expected)
    return expected == actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--gold", type=Path, required=True)
    args = parser.parse_args()
    errors = validate_protocol_file(args.model)
    if errors:
        print(json.dumps({"validation_errors": errors}, indent=2))
        return 2
    actual = read_json(args.model)
    gold = read_json(args.gold)
    if actual["meta"]["protocol"] != gold["protocol"]:
        parser.error("model and annotation protocol identifiers differ")
    values = {**gold["objects"], **gold["interactions"]}
    expected = {kind: values.get(kind, []) for kind in COLLECTIONS}
    expected, actual = project(expected), project(actual)
    missing = {
        key: [row for row in expected[key] if not any(subset(row, a) for a in actual[key])]
        for key in COLLECTIONS
    }
    total = sum(len(rows) for rows in expected.values())
    misses = sum(len(rows) for rows in missing.values())
    print(
        json.dumps(
            {
                "annotated_objects": total,
                "strict_matches": total - misses,
                "missing_or_differently_worded": missing,
                "note": ("Subset coverage only; inspect conditions and extra facts manually."),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
