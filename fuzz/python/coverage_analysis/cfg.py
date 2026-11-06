"""CFG topology and source locations from the same LLVM bitcode as the DOTs."""

import argparse
import json
import re
import subprocess
from pathlib import Path

SOURCE_MAP = "source-map.json"


def graph_inputs(cfg_dir):
    root = Path(cfg_dir)
    paths = sorted(root.glob("*.dot"))
    if (root / SOURCE_MAP).is_file():
        paths.append(root / SOURCE_MAP)
    return paths


def source_map(ir):
    """Resolve debug scopes, then key locations by function/block, not !dbg IDs.

    LLVM's DOT printer renumbers metadata, so its numeric IDs cannot be joined
    directly to llvm-dis output.
    """
    metadata = dict(re.findall(r'^!(\d+) = (.+)$', ir, re.M))

    def scope_file(key, seen=None):
        seen = set() if seen is None else seen
        if key in seen:
            return None
        seen.add(key)
        text = metadata.get(key, "")
        filename = re.search(r'!DIFile\(filename: "([^"]+)"', text)
        if filename:
            directory = re.search(r'directory: "([^"]*)"', text)
            return str(Path(directory[1] if directory else "") / filename[1])
        parent = re.search(r'(?:file|scope): !(\d+)', text)
        return scope_file(parent[1], seen) if parent else None

    def location(line):
        dbg = re.search(r'!dbg !(\d+)', line)
        text = metadata.get(dbg[1], "") if dbg else ""
        match = re.search(r'!DILocation\(line: (\d+)(?:, column: (\d+))?, scope: !(\d+)', text)
        if not match or int(match[1]) == 0:
            return None
        return {"file": scope_file(match[3]), "line": int(match[1]),
                "column": int(match[2] or 0)}

    functions = {}
    function = block = None
    definitions = {}
    for line in ir.splitlines():
        match = re.match(r'define\b.*?@(?:"([^"]+)"|([\w.$-]+))\(', line)
        if match:
            function = functions.setdefault(match[1] or match[2], {"blocks": {}})
            block = None
            definitions = {}
            continue
        if function is None:
            continue
        if line == "}":
            function = block = None
            continue
        match = re.match(r'([\w.$-]+):', line)
        if match or (block is None and line.startswith("  ")):
            name = match[1] if match else "entry"
            block = function["blocks"].setdefault(name, {"locations": [], "calls": []})
        if block is None:
            continue
        loc = location(line)
        if loc and loc not in block["locations"]:
            block["locations"].append(loc)
        definition = re.match(r'\s*(%[\w.$-]+) =', line)
        if definition:
            definitions[definition[1]] = loc
        branch = re.search(r'\bbr i1 (%[\w.$-]+)', line)
        if branch:
            block["condition"] = definitions.get(branch[1]) or loc
        if re.search(r'\b(?:call|invoke|callbr)\b', line):
            callee = re.search(r'@(?:"([^"]+)"|([\w.$-]+))\(', line)
            symbol = (callee[1] or callee[2]) if callee else None
            if symbol and symbol.startswith("llvm."):
                continue
            block["calls"].append({"target": symbol, "location": loc})
    return {"version": 1, "functions": functions}


def write_source_map(bitcode, cfg_dir, llvm_dis="llvm-dis-18"):
    ir = subprocess.run([llvm_dis, str(bitcode), "-o", "-"],
                        check=True, capture_output=True, text=True).stdout
    path = Path(cfg_dir) / SOURCE_MAP
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(source_map(ir), indent=2) + "\n")
    temporary.replace(path)


def _text(value):
    return str(value).strip('"')


def parse_cfg(path, locations):
    import networkx as nx

    graph = nx.nx_pydot.read_dot(str(path))
    title = _text(graph.graph.get("name", ""))
    match = re.search(r"CFG for '(.+)' function", title)
    symbol = match[1] if match else Path(path).name.removeprefix(".").removesuffix(".dot")
    source = locations.get("functions", {}).get(symbol, {}).get("blocks", {})
    blocks, names = {}, {}
    for node, attrs in graph.nodes(data=True):
        if ":" in str(node):
            continue
        label = _text(attrs.get("label", node))
        match = re.match(r'\{([^:]+):', label)
        name = match[1] if match else str(node)
        names[str(node)] = name
        blocks[name] = {"id": name, **source.get(name, {}), "locations": source.get(name, {}).get("locations", []),
                        "calls": source.get(name, {}).get("calls", [])}
    edges = []
    for src, dst, attrs in graph.edges(data=True):
        # pydot/networkx expose record ports as part of the node reference.
        src = str(src).split(":", 1)[0]
        dst = str(dst).split(":", 1)[0]
        if src in names and dst in names:
            edges.append({"src": names[src], "dst": names[dst]})
    return symbol, {"entry": next(iter(blocks), None), "blocks": blocks, "edges": edges}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitcode", type=Path, required=True)
    parser.add_argument("--cfg-dir", type=Path, required=True)
    parser.add_argument("--llvm-dis", default="llvm-dis-18")
    args = parser.parse_args()
    write_source_map(args.bitcode, args.cfg_dir, args.llvm_dis)


if __name__ == "__main__":
    main()
