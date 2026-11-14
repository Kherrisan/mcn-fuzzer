r"""Convert post-PTA SVF DOTs to mcn-fuzzer's LLVM callgraph/per-function CFG format.

The original bitcode is required: compact SVF labels do not identify LLVM basic
blocks. Rebuilding SVFIR recovers that identity without rerunning pointer analysis.
No build manifest is generated; it must belong to the matching instrumented build.

Example (from the repository root)::

    fuzz/.venv/bin/python fuzz/python/svf_to_llvm.py \
        --bitcode fuzz/targets/lightftp/profuzzgym-svf/target.bc \
        --icfg fuzz/targets/lightftp/svf-graphs/icfg.post-pta.dot \
        --callgraph fuzz/targets/lightftp/svf-graphs/callgraph.post-pta.dot \
        --output-dir fuzz/targets/lightftp/llvm-compatible

Outputs: callgraph.dot, cfg/*.dot, cfg/source-map.json, node-map.json,
callsite-targets.json, conversion.json. The output directory must be empty.
Unnamed blocks receive entry/bb.N names in the output only. CFGs contain
intraprocedural edges; resolved calls remain in the CallGraph and callsite sidecar.
The current coverage reader accepts indirect edges, but flow.py only follows
direct calls. Native SVF dumps can lose mixed direct/indirect classifications;
prefer the compact exports from svf_pta_graphs.py for full edge-kind information.
"""

import argparse
import ctypes as ct
import ctypes.util
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote

from coverage_analysis.cfg import source_map


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def llvm_module(bitcode, library=None):
    """Read real LLVM block order and terminator successors using the LLVM C API."""
    library = library or os.environ.get("LLVM_LIBRARY") or ct.util.find_library("LLVM-21")
    if not library:
        candidate = Path.home() / "SVF/llvm-21.1.0.obj/lib/libLLVM.so"
        if candidate.is_file():
            library = str(candidate)
    library = library or ct.util.find_library("LLVM")
    if not library:
        raise ValueError("Cannot find libLLVM; pass --llvm-library matching the bitcode version")
    lib = ct.CDLL(str(library))
    ptr = ct.c_void_p

    def api(name, result, *args):
        func = getattr(lib, "LLVM" + name)
        func.restype, func.argtypes = result, args
        return func

    context_new = api("ContextCreate", ptr)
    context_free = api("ContextDispose", None, ptr)
    buffer_new = api("CreateMemoryBufferWithContentsOfFile", ct.c_int, ct.c_char_p,
                     ct.POINTER(ptr), ct.POINTER(ptr))
    buffer_free = api("DisposeMemoryBuffer", None, ptr)
    parse = api("ParseBitcodeInContext2", ct.c_int, ptr, ptr, ct.POINTER(ptr))
    module_free = api("DisposeModule", None, ptr)
    message_free = api("DisposeMessage", None, ptr)
    first_fun = api("GetFirstFunction", ptr, ptr)
    next_fun = api("GetNextFunction", ptr, ptr)
    first_bb = api("GetFirstBasicBlock", ptr, ptr)
    next_bb = api("GetNextBasicBlock", ptr, ptr)
    bb_value = api("BasicBlockAsValue", ptr, ptr)
    value_name = api("GetValueName2", ptr, ptr, ct.POINTER(ct.c_size_t))
    set_name = api("SetValueName2", None, ptr, ct.c_char_p, ct.c_size_t)
    terminator = api("GetBasicBlockTerminator", ptr, ptr)
    successor_count = api("GetNumSuccessors", ct.c_uint, ptr)
    successor = api("GetSuccessor", ptr, ptr, ct.c_uint)
    print_module = api("PrintModuleToString", ptr, ptr)

    def name(value):
        size = ct.c_size_t()
        data = value_name(value, ct.byref(size))
        return ct.string_at(data, size.value).decode()

    context, buffer, module, error = context_new(), ptr(), ptr(), ptr()
    try:
        if buffer_new(os.fsencode(bitcode), ct.byref(buffer), ct.byref(error)):
            message = ct.string_at(error).decode()
            message_free(error)
            raise ValueError(message)
        if parse(context, buffer, ct.byref(module)):
            raise ValueError(f"libLLVM could not parse {bitcode}")
        functions, symbols = {}, set()
        fun = first_fun(module)
        while fun:
            symbol = name(fun)
            symbols.add(symbol)
            blocks, pointers = [], []
            bb = first_bb(fun)
            while bb:
                pointers.append(bb)
                original = name(bb_value(bb))
                # Safe names keep the existing source-map reader unambiguous.
                if not re.fullmatch(r"[A-Za-z_.$][\w.$-]*", original):
                    desired = ("entry" if not blocks else f"bb.{len(blocks) + 1}").encode()
                    set_name(bb_value(bb), desired, len(desired))
                blocks.append({"name": name(bb_value(bb)), "original_name": original})
                bb = next_bb(bb)
            if blocks:
                indices = {address: index + 1 for index, address in enumerate(pointers)}
                edges = set()
                for index, address in enumerate(pointers, 1):
                    term = terminator(address)
                    if not term:
                        raise ValueError(f"Unterminated LLVM block: {symbol}:{index}")
                    edges.update((index, indices[successor(term, i)])
                                 for i in range(successor_count(term)))
                functions[symbol] = {"blocks": blocks, "edges": edges}
            fun = next_fun(fun)
        printed = print_module(module)
        try:
            locations = source_map(ct.string_at(printed).decode())
        finally:
            message_free(printed)
        for symbol, data in functions.items():
            mapped = locations["functions"].get(symbol, {}).get("blocks", {})
            if set(mapped) != {block["name"] for block in data["blocks"]}:
                raise ValueError(f"Source-map parser cannot represent LLVM function {symbol!r}")
        return functions, symbols, locations
    finally:
        if module:
            module_free(module)
        if buffer:
            buffer_free(buffer)
        context_free(context)


def node_kind(node):
    for predicate, kind in (("isFunEntry", "entry"), ("isFunExit", "exit"),
                            ("isCall", "call"), ("isRet", "return")):
        if getattr(node, predicate)():
            return kind
    return "intra" if node.getFun() is not None else "global"


def svf_node_map(bitcode, functions):
    import pysvf

    pysvf.get_svfir(str(bitcode))
    nodes, blocks = {}, defaultdict(dict)
    for node in pysvf.getICFG().getNodes():
        fun, bb = node.getFun(), node.getBB()
        symbol = fun.getName() if fun is not None else None
        kind = node_kind(node)
        owner = None
        if bb is not None and symbol in functions and kind not in {"entry", "exit", "global"}:
            block_id = bb.getId()
            blocks[symbol][block_id] = bb
            original = functions[symbol]["blocks"]
            if 1 <= block_id <= len(original):
                if bb.getName() != original[block_id - 1]["original_name"]:
                    raise ValueError(f"SVF/LLVM block order mismatch: {symbol}:{block_id}")
                owner = (symbol, block_id)
            elif bb.getName() not in {"UnifiedReturnBlock", "UnifiedUnreachableBlock"}:
                raise ValueError(f"Unrecognized SVF auxiliary block: {symbol}:{bb.getName()}")
        nodes[node.getId()] = {"function": symbol, "kind": kind, "owner": owner,
                              "block_entry": bool(owner and bb.front().getId() == node.getId())}
    # Check the ordinal mapping against the actual BB graph, not source lines.
    for symbol, data in functions.items():
        count = len(data["blocks"])
        present = {number for number in blocks[symbol] if number <= count}
        if present != set(range(1, count + 1)):
            raise ValueError(f"SVF has no complete original body for {symbol}; cannot merge safely")
        projected = {(number, dst.getId()) for number, bb in blocks[symbol].items()
                     if number <= count
                     for dst in bb.getSuccessors() if dst.getId() <= count}
        if projected != data["edges"]:
            raise ValueError(f"SVF preprocessing changed original CFG topology for {symbol}")
    return nodes


_ATTR = re.compile(r'(\w+)\s*=\s*("(?:\\.|[^"\\])*"|[^,\]\s]+)')
_ID = r'("(?:\\.|[^"\\])*"|[\w.]+)(?::[\w:]+)?'
_EDGE = re.compile(r'^\s*' + _ID + r'\s*->\s*' + _ID + r'\s*(?:\[(.*)\])?\s*;\s*$')
_NODE = re.compile(r'^\s*' + _ID + r'\s*\[(.*)\]\s*;\s*$')


def unquote(value):
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            # Native LLVM DOT uses record escapes such as \l, outside JSON.
            return re.sub(r'\\(["\\])', r'\1', value[1:-1])
    return value


def dot_records(path):
    """Stream the flat, one-statement-per-line DOT emitted by SVF exporters.

    Do not use pydot for the complete instruction-level ICFG: large targets
    contain millions of statements. Reject unsupported input instead of skipping it.
    """
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line or line.startswith(("//", "digraph ")) or line == "}":
                continue
            match = _EDGE.match(line)
            if match:
                yield "edge", unquote(match[1]), unquote(match[2]), {
                    key: unquote(value) for key, value in _ATTR.findall(match[3] or "")}
                continue
            match = _NODE.match(line)
            if match:
                if match[1] not in {"node", "edge", "graph"}:
                    yield "node", unquote(match[1]), None, {
                        key: unquote(value) for key, value in _ATTR.findall(match[2])}
                continue
            if re.match(r'^(label|rankdir|size)\s*=', line):
                continue
            raise ValueError(f"Unsupported DOT statement at {path}:{number}: {line[:100]}")


def icfg_identity(dot_id, attrs):
    label = attrs.get("label", "")
    if "kind" in attrs and re.fullmatch(r"n\d+", dot_id):
        lines = label.splitlines()
        return int(dot_id[1:]), attrs["kind"], lines[1] if len(lines) > 1 else None
    label = re.sub(r'\\([{}<>|])', r'\1', label)
    match = re.search(r'(Global|FunEntry|FunExit|Call|Ret|Intra)ICFGNode\s*(\d+)', label)
    if not match:
        raise ValueError(f"Cannot identify SVF ICFG node {dot_id}")
    kinds = {"Global": "global", "FunEntry": "entry", "FunExit": "exit",
             "Call": "call", "Ret": "return", "Intra": "intra"}
    function = re.search(r'\{fun: ([^\s{}]+)', label)
    return int(match[2]), kinds[match[1]], function[1] if function else None


def merge_icfg(path, mapping, functions):
    ids, seen = {}, set()
    native = False
    stats = Counter()
    for record, src, _, attrs in dot_records(path):
        if record != "node":
            continue
        native |= "kind" not in attrs
        ident, kind, symbol = icfg_identity(src, attrs)
        expected = mapping.get(ident)
        # SVF appends extapi model bodies in hash-table order. Their IDs can
        # move between processes; all are discarded, and their DOT identity is
        # authoritative. Original program nodes still require an exact match.
        if (expected is not None and expected["owner"] is None
                and expected["function"] not in functions and symbol is not None
                and symbol not in functions):
            expected = mapping[ident] = {**expected, "function": symbol, "kind": kind}
        if expected is None or kind != expected["kind"] or (
                symbol is not None and symbol != expected["function"]):
            raise ValueError(f"ICFG node {src} does not match the bitcode/SVFIR")
        if ident in seen:
            raise ValueError(f"Duplicate ICFG node {ident}")
        ids[src] = ident
        seen.add(ident)
        stats["input_nodes"] += 1
        if expected["owner"] is None:
            stats["discarded_auxiliary_nodes"] += 1
    missing = set(mapping) - seen
    if (missing and not native) or any(mapping[ident]["owner"] is not None for ident in missing):
        raise ValueError("ICFG node set differs from rebuilt SVFIR; use matching bitcode and pysvf")
    stats["hidden_auxiliary_nodes"] = len(missing)
    merged = defaultdict(set)
    callsites = set()
    for record, src, dst, attrs in dot_records(path):
        if record != "edge":
            continue
        stats["input_edges"] += 1
        if src not in ids or dst not in ids:
            raise ValueError(f"ICFG edge references missing node: {src} -> {dst}")
        left, right = mapping[ids[src]], mapping[ids[dst]]
        kind = attrs.get("kind") or {"red": "call", "blue": "return"}.get(attrs.get("color"), "intra")
        if kind not in {"intra", "call", "return", "thread_spawn"}:
            raise ValueError(f"Unknown ICFG edge kind: {kind}")
        if kind != "intra":
            stats["removed_interprocedural_edges"] += 1
            if kind in {"call", "thread_spawn"} and left["owner"]:
                callsites.add((ids[src], *left["owner"], right["function"], kind))
            continue
        a, b = left["owner"], right["owner"]
        if a is None or b is None:
            stats["removed_auxiliary_edges"] += 1
            continue
        if a[0] != b[0]:
            raise ValueError(f"Unexpected intra edge crossing functions: {a} -> {b}")
        pair = (a[1], b[1])
        if a == b:
            # A same-block edge back to its first instruction is a loop.
            # Other instruction edges disappear when their nodes are folded.
            if not right["block_entry"]:
                stats["collapsed_internal_edges"] += 1
                continue
        merged[a[0]].add(pair)
        stats["projected_edges"] += 1
    for symbol, data in functions.items():
        actual = merged[symbol]
        if actual != data["edges"]:
            missing, extra = data["edges"] - actual, actual - data["edges"]
            raise ValueError(f"Merged CFG differs from LLVM for {symbol}: missing={sorted(missing)[:8]}, "
                             f"extra={sorted(extra)[:8]}")
    stats["output_nodes"] = sum(len(data["blocks"]) for data in functions.values())
    stats["output_edges"] = sum(len(edges) for edges in merged.values())
    stats["merged_nodes"] = stats["input_nodes"] - stats["discarded_auxiliary_nodes"] - stats["output_nodes"]
    stats["duplicate_projected_edges"] = stats["projected_edges"] - stats["output_edges"]
    return merged, sorted(callsites), dict(stats)


def merge_callgraph(path, symbols):
    names, edges = {}, set()
    stats = Counter()
    for record, src, _, attrs in dot_records(path):
        if record == "node":
            label = re.sub(r'\\([{}<>|])', r'\1', attrs.get("label", ""))
            match = re.search(r'\{fun: (.*?)\}', label)
            names[src] = match[1] if match else label.strip("{}")
            stats["input_nodes"] += 1
    for record, src, dst, attrs in dot_records(path):
        if record != "edge":
            continue
        stats["input_edges"] += 1
        if src not in names or dst not in names:
            raise ValueError("CallGraph edge references missing function")
        if names[src] not in symbols or names[dst] not in symbols:
            stats["discarded_auxiliary_edges"] += 1
            continue
        kind = attrs.get("kind") or {"red": "indirect", "blue": "thread_spawn",
                                      "green": "thread_join"}.get(attrs.get("color"), "direct")
        if kind not in {"direct", "indirect", "direct+indirect", "thread_spawn", "thread_join"}:
            raise ValueError(f"Unknown CallGraph edge kind: {kind}")
        for part in kind.split("+"):
            edges.add((names[src], names[dst], part))
    kept = set(names.values()) & symbols
    stats["discarded_auxiliary_nodes"] = sum(name not in symbols for name in names.values())
    stats["output_nodes"], stats["output_edges"] = len(kept), len(edges)
    return sorted(kept), sorted(edges), dict(stats)


def write_graphs(output, functions, cfg_edges, symbols, cg_edges, locations):
    cfg_dir = output / "cfg"
    cfg_dir.mkdir()
    for symbol, data in functions.items():
        filename = quote(symbol, safe="._-")
        if len(filename.encode()) > 200:
            filename = "function-" + hashlib.sha256(symbol.encode()).hexdigest()
        with (cfg_dir / f"{filename}.dot").open("w") as stream:
            title = f"CFG for '{symbol}' function"
            stream.write(f'digraph {json.dumps(title)} {{\n')
            stream.write('  node [shape=record];\n')
            for index, block in enumerate(data["blocks"], 1):
                # Deliberate DOT record \l escape (not a JSON-escaped backslash).
                stream.write(f'  Node{index} [label="{{{block["name"]}:\\l}}"];\n')
            for src, dst in sorted(cfg_edges[symbol]):
                stream.write(f'  Node{src} -> Node{dst};\n')
            stream.write('}\n')
    (cfg_dir / "source-map.json").write_text(json.dumps(locations, indent=2) + "\n")
    ids = {symbol: f"Node{index}" for index, symbol in enumerate(symbols)}
    with (output / "callgraph.dot").open("w") as stream:
        stream.write('digraph "Call graph" {\n  node [shape=record];\n')
        for symbol in symbols:
            stream.write(f'  {ids[symbol]} [label={json.dumps("{" + symbol + "}")}];\n')
        for src, dst, kind in cg_edges:
            style = "solid" if kind == "direct" else "dashed"
            stream.write(f'  {ids[src]} -> {ids[dst]} [kind="{kind}", style="{style}"];\n')
        stream.write('}\n')


def convert(bitcode, icfg, callgraph, output, llvm_library=None, analysis_path=None):
    started = time.monotonic()
    bitcode, icfg, callgraph = [Path(path).resolve(strict=True) for path in (bitcode, icfg, callgraph)]
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output directory must be empty: {output}")
    input_hashes = {key: digest(path) for key, path in
                    (("bitcode", bitcode), ("icfg", icfg), ("callgraph", callgraph))}
    analysis_path = Path(analysis_path) if analysis_path else icfg.parent / "analysis.json"
    analysis = json.loads(analysis_path.read_text()) if analysis_path.is_file() else {}
    if analysis:
        if analysis.get("bitcode_sha256") != input_hashes["bitcode"]:
            raise ValueError("analysis.json bitcode hash mismatch")
        if analysis.get("pysvf_version") != version("pysvf"):
            raise ValueError("Rebuild the mapping with the pysvf version used for graph export")
        for key in ("icfg", "callgraph"):
            expected = analysis.get("dot_validation", {}).get(key, {}).get("sha256")
            if expected and expected != input_hashes[key]:
                raise ValueError(f"analysis.json {key} hash mismatch")
    print("Reading original LLVM basic blocks", flush=True)
    functions, llvm_symbols, locations = llvm_module(bitcode, llvm_library)
    print("Rebuilding SVFIR node identity (no pointer analysis)", flush=True)
    mapping = svf_node_map(bitcode, functions)
    print("Merging ICFG and CallGraph", flush=True)
    cfg_edges, callsites, icfg_stats = merge_icfg(icfg, mapping, functions)
    symbols, cg_edges, cg_stats = merge_callgraph(callgraph, llvm_symbols)
    indirect_path = icfg.parent / "indirect-calls.jsonl"
    targets = [json.loads(line) for line in indirect_path.read_text().splitlines() if line.strip()] if indirect_path.is_file() else []
    for row in targets:
        node = mapping.get(row["callsite_icfg_id"])
        if node is None or not node["owner"] or node["function"] != row["caller"]:
            raise ValueError("indirect-calls.jsonl does not match the ICFG")
        row["block"] = functions[node["function"]]["blocks"][node["owner"][1] - 1]["name"]
    output.mkdir(parents=True, exist_ok=True)
    write_graphs(output, functions, cfg_edges, symbols, cg_edges, locations)
    (output / "callsite-targets.json").write_text(json.dumps({
        "version": 1, "resolved_indirect_targets": targets,
        "icfg_calls": [{"icfg_id": ident, "caller": symbol,
                        "block": functions[symbol]["blocks"][block - 1]["name"],
                        "callee": target, "kind": kind}
                       for ident, symbol, block, target, kind in callsites]}, indent=2) + "\n")
    # Stream the map: OpenSSL has over a million ICFG nodes.
    with (output / "node-map.json").open("w") as stream:
        stream.write("{\n")
        for index, (ident, row) in enumerate(mapping.items()):
            owner = row["owner"]
            if owner:
                owner = [owner[0], functions[owner[0]]["blocks"][owner[1] - 1]["name"]]
            if index:
                stream.write(",\n")
            stream.write(f'  "{ident}": ' + json.dumps({**row, "owner": owner}))
        stream.write("\n}\n")
    report = {"version": 1, "inputs": {"bitcode": str(bitcode), "icfg": str(icfg), "callgraph": str(callgraph)},
              "input_sha256": input_hashes, "pysvf_version": version("pysvf"),
              "analysis": analysis.get("analysis"), "pta_rerun": False,
              "indirect_call_limit_reached": analysis.get("indirect_call_limit_reached"),
              "warnings": analysis.get("warnings", []) + ([] if analysis else ["No analysis.json: PTA provenance and edge limit are unknown"]),
              "icfg": icfg_stats, "callgraph": cg_stats, "cfg_functions": len(functions),
              "all_cfg_edges_verified_against_original_llvm": True,
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    (output / "conversion.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bitcode", required=True, type=Path)
    parser.add_argument("--icfg", required=True, type=Path)
    parser.add_argument("--callgraph", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--llvm-library", type=Path)
    parser.add_argument("--analysis-json", type=Path)
    args = parser.parse_args()
    try:
        convert(args.bitcode, args.icfg, args.callgraph, args.output_dir,
                args.llvm_library, args.analysis_json)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Conversion failed: {error}\n")


if __name__ == "__main__":
    main()
