"""Preview per-Issue completion contexts from saved Agent artifacts, without model calls.

This is an offline context audit, not resumption or semantic evaluation. Preview sizes
exclude tool/output schemas and future tool history; actual sends enforce final budgets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rsc.artifacts import read_json, read_jsonl, write_json, write_jsonl
from rsc.protocol.agent_runtime import EventWriter, encode
from rsc.protocol.completion import collect_current, repair_context
from rsc.protocol.context import Corpus
from rsc.protocol.contract import COLLECTIONS
from rsc.protocol.extraction import ExtractionRuntime
from rsc.protocol.ids import GlobalIdAllocator
from rsc.protocol.keywords import KeywordIndex
from rsc.protocol.options import AgentOptions, ContextOptions, resolve_limits
from rsc.protocol.planning import BlockList, BlockListPlan, FocusBlock
from rsc.protocol.proposals import CandidateState
from rsc.protocol.store import ExtractionStore


def replay(model_dir: Path, out: Path) -> dict:
    model_dir, out = model_dir.resolve(), out.resolve()
    if model_dir == out or model_dir.is_relative_to(out) or out.is_relative_to(model_dir):
        raise ValueError("replay output must be separate from original artifacts")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("replay output must be empty")
    report = read_json(model_dir / "compile-report.json")
    documents = {}
    for path in sorted((model_dir / "sources").glob("*.json")):
        if not path.name.endswith(".index.json"):
            document = read_json(path)
            documents[document["meta"]["source_id"]] = document
    if not documents:
        raise ValueError("no saved source documents")
    corpus = Corpus(documents)
    raw_plan = read_json(model_dir / "block-lists.json")
    plan = BlockListPlan(
        raw_plan["requested_lists"],
        tuple(
            BlockList(
                row["list_id"], row["order"], tuple(FocusBlock(**block) for block in row["blocks"])
            )
            for row in raw_plan["lists"]
        ),
        raw_plan["partition_version"],
    )
    keywords = KeywordIndex(report["protocol"], corpus)
    saved_keywords = read_json(model_dir / "keyword-index.json")
    if saved_keywords["meta"] != keywords.snapshot()["meta"]:
        raise ValueError("keyword source checksums differ from saved documents")
    focuses = plan.focus_map()
    for row in saved_keywords["records"]:
        focus = focuses[row["doc_id"], row["block_id"]]
        if row["status"] == "failed":
            keywords.commit(focus, error=row["error"], phase=row.get("phase", "objects"))
        else:
            keywords.commit(
                focus,
                {"keywords": [{"text": term["text"]} for term in row["keywords"]]},
                phase=row.get("phase", "objects"),
            )
    store = ExtractionStore()
    for row in read_jsonl(model_dir / "block-extractions.jsonl"):
        store.commit(row)

    def forbidden(*args):
        raise AssertionError("offline preview must never construct a model")

    runtime = ExtractionRuntime(
        report["protocol"],
        corpus,
        plan,
        forbidden,
        GlobalIdAllocator(report["protocol"]),
        EventWriter(),
        resolve_limits(report.get("request_limits")),
        agent_options=AgentOptions(**report.get("agent_options", {})),
        context_options=ContextOptions(**report.get("context_options", {})),
    )
    out.mkdir(parents=True, exist_ok=True)
    phases, redirects = {}, {}
    for phase in ("objects", "interactions"):
        folder = model_dir / f"{phase}-link"
        mapping = read_json(folder / "id-map.json")
        redirects = {source: mapping.get(target, target) for source, target in redirects.items()}
        redirects.update({source: target for source, target in mapping.items() if source != target})
        state = CandidateState(
            model=read_json(folder / "model.json"),
            issues=read_json(folder / "unresolved.json"),
            redirects=redirects,
        )
        active = collect_current(state, runtime, phase)
        state.history.refresh(active, 0)
        worklist, requests = [], []
        for current in state.history.ordered(active):
            tracking_id = state.history.key(current)
            _, payload, editable = repair_context(
                runtime,
                state,
                current,
                phase,
                keywords,
                store,
                1,
                None,
            )
            worklist.append(tracking_id)
            requests.append(
                {
                    "tracking_id": tracking_id,
                    "description": current["description"],
                    "object_names": list(current.get("object_names") or []),
                    "editable_object_names": sorted(editable),
                    "payload": payload,
                    "preview_payload_bytes": len(encode(payload).encode()),
                }
            )
        destination = out / phase
        destination.mkdir()
        write_json(destination / "model.json", state.model)
        write_json(destination / "unresolved.json", state.issues)
        write_json(destination / "worklist.json", worklist)
        write_jsonl(destination / "requests.jsonl", requests)
        phases[phase] = {
            "objects": {kind: len(state.model[kind]) for kind in COLLECTIONS},
            "issues": len(active),
            "contexts": len(requests),
            "max_preview_payload_bytes": max(
                (row["preview_payload_bytes"] for row in requests),
                default=0,
            ),
        }
    summary = {"protocol": report["protocol"], "network_calls": 0, "phases": phases}
    write_json(out / "replay-report.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.model_dir, args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
