"""Compare converted SVF graphs with LLVM DOTs exported from the same bitcode.

Export LLVM graphs with opt -passes='dot-callgraph,function(dot-cfg-only)'.
Both CFG printers enumerate blocks in LLVM function order. Compare using this
ordinal identity, since unnamed LLVM blocks have numeric labels and the SVF
adapter gives them entry/bb.N names. Ignore DOT addresses and record ports.
Keep raw edge counts as well as unique adjacency counts: switch cases can
produce parallel edges, while the adapter intentionally merges those edges.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from svf_to_llvm import digest, dot_records


def cfg(path):
    with path.open() as stream:
        title = stream.readline().strip()
    match = re.fullmatch(r'digraph (".*") \{', title)
    if not match:
        raise ValueError(f"Cannot parse CFG title: {path}")
    title = json.loads(match[1])
    match = re.fullmatch(r"CFG for '(.*)' function", title)
    if not match:
        raise ValueError(f"Cannot identify CFG function: {path}")
    nodes, edges = {}, []
    for record, src, dst, attrs in dot_records(path):
        if record == "node":
            if src in nodes:
                raise ValueError(f"Duplicate CFG node in {path}: {src}")
            nodes[src] = len(nodes)
        else:
            edges.append((src, dst))
    if any(src not in nodes or dst not in nodes for src, dst in edges):
        raise ValueError(f"CFG edge references a missing block: {path}")
    return match[1], {"nodes": len(nodes), "raw_edges": len(edges),
                      "edges": {(nodes[src], nodes[dst]) for src, dst in edges}}


def cfgs(root):
    result = {}
    for path in sorted(root.glob("*.dot")):
        symbol, data = cfg(path)
        if symbol in result:
            raise ValueError(f"Duplicate CFG function: {symbol}")
        result[symbol] = data
    return result


def callgraph(path):
    nodes, pending = {}, []
    for record, src, dst, attrs in dot_records(path):
        if record == "node":
            label = re.sub(r'\\([{}<>|])', r'\1', attrs["label"])
            nodes[src] = label[1:-1] if label.startswith("{") and label.endswith("}") else label
        else:
            pending.append((src, dst, attrs.get("kind", "direct")))
    if any(src not in nodes or dst not in nodes for src, dst, _ in pending):
        raise ValueError(f"CallGraph edge references a missing node: {path}")
    return set(nodes.values()), {(nodes[src], nodes[dst], kind) for src, dst, kind in pending}, len(pending)


def compare(llvm_dir, converted_dir, output):
    llvm_dir, converted_dir, output = [Path(path).resolve() for path in (llvm_dir, converted_dir, output)]
    provenance = json.loads((converted_dir / "conversion.json").read_text())
    if digest(provenance["inputs"]["bitcode"]) != provenance["input_sha256"]["bitcode"]:
        raise ValueError("Converted graph bitcode has changed")
    extraction_path = llvm_dir / "extraction.json"
    extraction = json.loads(extraction_path.read_text()) if extraction_path.is_file() else {}
    if (extraction.get("bitcode_sha256") is not None
            and extraction["bitcode_sha256"] != provenance["input_sha256"]["bitcode"]):
        raise ValueError("LLVM and converted SVF graphs came from different bitcode")
    print("Comparing every function CFG", flush=True)
    llvm_cfg, svf_cfg = cfgs(llvm_dir / "cfg"), cfgs(converted_dir / "cfg")
    common = set(llvm_cfg) & set(svf_cfg)
    cfg_differences = []
    for symbol in sorted(common):
        left, right = llvm_cfg[symbol], svf_cfg[symbol]
        if left["nodes"] != right["nodes"] or left["edges"] != right["edges"]:
            cfg_differences.append({"function": symbol, "llvm_nodes": left["nodes"],
                                    "svf_nodes": right["nodes"],
                                    "llvm_only_edges": sorted(left["edges"] - right["edges"]),
                                    "svf_only_edges": sorted(right["edges"] - left["edges"])})
    print("Comparing function nodes and typed call relations", flush=True)
    llvm_cg = list(llvm_dir.glob("*.callgraph.dot"))
    if len(llvm_cg) != 1:
        raise ValueError("Expected exactly one LLVM *.callgraph.dot")
    llvm_nodes, llvm_edges, llvm_raw = callgraph(llvm_cg[0])
    svf_nodes, svf_edges, svf_raw = callgraph(converted_dir / "callgraph.dot")
    llvm_pairs = {(a, b) for a, b, _ in llvm_edges}
    svf_pairs = {(a, b) for a, b, _ in svf_edges}
    direct = {(a, b) for a, b, kind in svf_edges if kind == "direct"}
    indirect = {(a, b) for a, b, kind in svf_edges if kind == "indirect"}
    diffs = {"llvm_only_nodes": sorted(llvm_nodes - svf_nodes),
             "svf_only_nodes": sorted(svf_nodes - llvm_nodes),
             "llvm_pairs_missing_from_svf": sorted(llvm_pairs - svf_pairs),
             "llvm_pairs_not_marked_direct_in_svf": sorted(llvm_pairs - direct),
             "svf_direct_pairs_absent_from_llvm": sorted(direct - llvm_pairs),
             "svf_indirect_pairs_absent_from_llvm": sorted(indirect - llvm_pairs),
             "svf_indirect_pairs_also_in_llvm": sorted(indirect & llvm_pairs)}
    indirect_kinds = defaultdict(set)
    for row in json.loads((converted_dir / "callsite-targets.json").read_text())["resolved_indirect_targets"]:
        indirect_kinds[row["caller"], row["callee"]].add(row["kind"])
    diffs["svf_indirect_details"] = [
        {"caller": a, "callee": b, "callsite_kinds": sorted(indirect_kinds[a, b])}
        for a, b in sorted(indirect)]
    summary = {
        "bitcode": provenance["inputs"]["bitcode"],
        "bitcode_sha256": provenance["input_sha256"]["bitcode"],
        "llvm_dir": str(llvm_dir), "converted_svf_dir": str(converted_dir),
        "llvm_cfg_pass": extraction.get("cfg_pass", "dot-cfg-only"),
        "llvm_names_only_prepass": extraction.get("names_only_prepass"),
        "block_identity": "LLVM block order within each function",
        "cfg": {"llvm_functions": len(llvm_cfg), "svf_functions": len(svf_cfg),
                "compared_functions": len(common),
                "identical_functions": len(common) - len(cfg_differences),
                "llvm_only_functions": sorted(set(llvm_cfg) - set(svf_cfg)),
                "svf_only_functions": sorted(set(svf_cfg) - set(llvm_cfg)),
                "different_functions": len(cfg_differences),
                "llvm_nodes": sum(row["nodes"] for row in llvm_cfg.values()),
                "svf_nodes": sum(row["nodes"] for row in svf_cfg.values()),
                "llvm_raw_edges": sum(row["raw_edges"] for row in llvm_cfg.values()),
                "llvm_unique_edges": sum(len(row["edges"]) for row in llvm_cfg.values()),
                "svf_raw_edges": sum(row["raw_edges"] for row in svf_cfg.values()),
                "svf_unique_edges": sum(len(row["edges"]) for row in svf_cfg.values())},
        "callgraph": {"llvm_nodes": len(llvm_nodes), "svf_nodes": len(svf_nodes),
                      "llvm_only_nodes": len(llvm_nodes - svf_nodes), "svf_only_nodes": len(svf_nodes - llvm_nodes),
                      "llvm_raw_edges": llvm_raw, "llvm_unique_pairs": len(llvm_pairs),
                      "svf_raw_edges": svf_raw, "svf_unique_pairs": len(svf_pairs),
                      "svf_edge_kinds": dict(Counter(kind for _, _, kind in svf_edges)),
                      **{key: len(value) for key, value in diffs.items() if key != "svf_indirect_details"}},
        "pta_analysis": provenance["analysis"],
        "indirect_call_limit_reached": provenance["indirect_call_limit_reached"],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "cfg-differences.json").write_text(json.dumps(cfg_differences, indent=2) + "\n")
    (output / "callgraph-differences.json").write_text(json.dumps(diffs, indent=2) + "\n")
    (output / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-dir", required=True, type=Path)
    parser.add_argument("--converted-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    compare(args.llvm_dir, args.converted_dir, args.output_dir)


if __name__ == "__main__":
    main()
