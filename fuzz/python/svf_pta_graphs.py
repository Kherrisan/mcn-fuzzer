"""Run SVF pointer analysis and export the updated ICFG and call graph as DOT."""

import argparse
import hashlib
import json
import resource
import time
from importlib.metadata import version
from pathlib import Path

import pysvf
from pysvf.pysvf import PTATY


def icfg_counts(icfg):
    counts = {"nodes": 0, "intra_edges": 0, "call_edges": 0, "return_edges": 0}
    for node in icfg.getNodes():
        counts["nodes"] += 1
        for edge in node.getOutEdges():
            if edge.isCallCFGEdge():
                counts["call_edges"] += 1
            elif edge.isRetCFGEdge():
                counts["return_edges"] += 1
            else:
                counts["intra_edges"] += 1
    return counts


def write_dot_graphs(icfg, callgraph, output, thread_edges=()):
    """Stream graph topology without expanding whole global LLVM initializers."""
    with (output / "icfg.post-pta.dot").open("w") as stream:
        stream.write('digraph ICFG {\n  node [shape=box];\n')
        for node in icfg.getNodes():
            kind = "global"
            for predicate, candidate in (("isFunEntry", "entry"), ("isFunExit", "exit"),
                                         ("isCall", "call"), ("isRet", "return")):
                if getattr(node, predicate)():
                    kind = candidate
                    break
            else:
                if node.getFun() is not None:
                    kind = "intra"
            fun = node.getFun()
            label = f"{node.getId()} {kind}"
            if fun is not None:
                label += f"\n{fun.getName()}\n{node.getSourceLoc()}"
            stream.write(f'  n{node.getId()} [label={json.dumps(label)}, kind="{kind}"];\n')
            for edge in node.getOutEdges():
                edge_kind, color = "intra", "black"
                if edge.isCallCFGEdge():
                    edge_kind, color = "call", "red"
                elif edge.isRetCFGEdge():
                    edge_kind, color = "return", "blue"
                stream.write(f'  n{node.getId()} -> n{edge.getDstNode().getId()} '
                             f'[kind="{edge_kind}", color="{color}"];\n')
        for source, target in sorted(set(thread_edges)):
            stream.write(f'  n{source} -> n{target} [kind="thread_spawn", color="purple", style="dashed"];\n')
        stream.write('}\n')
    with (output / "callgraph.post-pta.dot").open("w") as stream:
        stream.write('digraph CallGraph {\n')
        for node in callgraph.getNodes():
            stream.write(f'  n{node.getId()} [label={json.dumps(node.getName())}];\n')
            for edge in node.getOutEdges():
                direct = len(edge.getDirectCalls())
                indirect = len(edge.getIndirectCalls())
                kind = "direct+indirect" if direct and indirect else "indirect" if indirect else "direct"
                stream.write(f'  n{node.getId()} -> n{edge.getDstID()} '
                             f'[kind="{kind}", direct_calls={direct}, indirect_calls={indirect}, '
                             f'style="{"dashed" if indirect else "solid"}"];\n')
        stream.write('}\n')


def export_graphs(bitcode, output, pta="andersen", dot_style="compact"):
    bitcode = bitcode.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    cpu_started = time.process_time()
    timings = {}
    print(f"Building SVFIR: {bitcode}", flush=True)
    pysvf.get_svfir(str(bitcode))
    timings["svfir_seconds"] = round(time.monotonic() - started, 3)
    icfg = pysvf.getICFG()
    before = icfg_counts(icfg)
    print(f"ICFG before PTA: {before}", flush=True)

    # This binding runs analysis to completion and then calls
    # ICFG::updateCallGraph(currentPta->getCallGraph()). Do not export the
    # initial SVFIR call graph: it does not have resolved indirect calls.
    analysis_type = {"andersen": PTATY.AndersenWaveDiff_WPA,
                     "steensgaard": PTATY.Steensgaard_WPA}[pta]
    analysis_name = {"andersen": "AndersenWaveDiff_WPA",
                     "steensgaard": "Steensgaard_WPA"}[pta]
    print(f"Running {analysis_name} pointer analysis", flush=True)
    pta_started = time.monotonic()
    pysvf.run_pta(analysis_type)
    timings["pta_and_icfg_update_seconds"] = round(time.monotonic() - pta_started, 3)
    print(f"PTA finished in {timings['pta_and_icfg_update_seconds']} seconds", flush=True)
    audit_started = time.monotonic()
    callgraph = pysvf.getCallGraph()
    icfg = pysvf.getICFG()
    after = icfg_counts(icfg)

    graph_nodes = callgraph.getNodes()
    direct_pairs = 0
    indirect_pairs = 0
    internal_pairs = 0
    missing = []
    indirect_sites = set()
    edge_count = 0
    thread_edges = []
    # Audit every resolved indirect call target against the updated ICFG.
    # External functions have no body; SVF models their call-to-return edge.
    with (output / "indirect-calls.jsonl").open("w") as stream:
        for node in graph_nodes:
            for edge in node.getOutEdges():
                edge_count += 1
                direct_pairs += len(edge.getDirectCalls())
                callee = edge.getDstNode().getFunction()
                external = pysvf.isExtCall(callee)
                for site in edge.getIndirectCalls():
                    indirect_pairs += 1
                    indirect_sites.add(site.getId())
                    present = None
                    called = site.getCalledFunction()
                    thread_spawn = called is not None and called.getName() in {"pthread_create", "thrd_create"}
                    if not external:
                        entry = icfg.getFunEntryICFGNode(callee)
                        if thread_spawn:
                            # SVF's ordinary ICFG omits concurrent thread-start
                            # edges. Export these resolved PTA targets explicitly
                            # as spawn edges, without inventing return edges.
                            thread_edges.append((site.getId(), entry.getId()))
                        else:
                            internal_pairs += 1
                            present = icfg.getICFGEdge(site, entry, 1) is not None
                            if not present:
                                missing.append([site.getId(), callee.getName()])
                    stream.write(json.dumps({
                        "callsite_icfg_id": site.getId(),
                        "caller": node.getName(),
                        "callee": edge.getDstNode().getName(),
                        "external": external,
                        "kind": "thread_spawn" if thread_spawn else "indirect_call",
                        "icfg_call_edge_present": present,
                    }) + "\n")
    if missing:
        raise RuntimeError(f"ICFG missing {len(missing)} PTA targets: {missing[:10]}")
    timings["graph_audit_seconds"] = round(time.monotonic() - audit_started, 3)

    dump_started = time.monotonic()
    if dot_style == "native":
        icfg.dump(str(output / "icfg.post-pta"))
        callgraph.dump(str(output / "callgraph.post-pta"))
    else:
        write_dot_graphs(icfg, callgraph, output, thread_edges)
    timings["dot_export_seconds"] = round(time.monotonic() - dump_started, 3)
    for name in ("icfg.post-pta.dot", "callgraph.post-pta.dot"):
        if not (output / name).is_file():
            raise RuntimeError(f"SVF did not produce {name}")
    with bitcode.open("rb") as stream:
        bitcode_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    # SVF's default in the installed pysvf 1.0.0.59 is 50,000 resolved
    # indirect call edges. Hitting it must not be presented as exhaustive.
    indirect_limit = 50000
    warnings = []
    if indirect_pairs >= indirect_limit:
        warnings.append("SVF's default indirect-call edge limit was reached; "
                        "indirect-call resolution is not exhaustive.")
    summary = {
        "bitcode": str(bitcode),
        "bitcode_sha256": bitcode_digest,
        "pysvf_version": version("pysvf"),
        "analysis": analysis_name,
        "dot_style": dot_style,
        "graphs_exported_after_pointer_analysis": True,
        "icfg_before_pta": before,
        "icfg_after_pta": after,
        "callgraph_nodes": len(graph_nodes),
        "callgraph_edges": edge_count,
        "direct_callsite_target_pairs": direct_pairs,
        "resolved_indirect_callsites": len(indirect_sites),
        "indirect_callsite_target_pairs": indirect_pairs,
        "indirect_call_edge_limit": indirect_limit,
        "indirect_call_limit_reached": indirect_pairs >= indirect_limit,
        "warnings": warnings,
        "internal_indirect_pairs_verified_in_icfg": internal_pairs,
        "thread_spawn_icfg_edges_added": len(set(thread_edges)) if dot_style == "compact" else 0,
        "missing_indirect_icfg_call_edges": len(missing),
        "phase_timings": timings,
        "cpu_seconds": round(time.process_time() - cpu_started, 3),
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    (output / "analysis.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bitcode", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pta", choices=["andersen", "steensgaard"], default="andersen")
    parser.add_argument("--dot-style", choices=["compact", "native"], default="compact")
    args = parser.parse_args()
    export_graphs(args.bitcode, args.output_dir, args.pta, args.dot_style)


if __name__ == "__main__":
    main()
