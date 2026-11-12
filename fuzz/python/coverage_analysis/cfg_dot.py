"""Function CFG DOT parsing that keeps successor ports (T/F branches).

LLVM's ``dot-cfg-only`` prints record nodes whose label ends in
``{<s0>T|<s1>F}`` and edges as ``Node:s0 -> Node``. Dropping the port loses
which branch direction an edge belongs to, so this parser keeps it.
"""

import json
import re
from pathlib import Path

NODE = re.compile(r'^\s*(\S+)\s*\[.*?label="(.*)"\]\s*;?\s*$')
EDGE = re.compile(r"^\s*(\S+?)(?::(\w+))?\s*->\s*(\S+?)(?:\[|;|$)")
BLOCK = re.compile(r"^\{([^:|]+)")
PORT = re.compile(r"<s(\d+)>([TF])")


class CfgError(ValueError):
    pass


def _ports(label):
    return {int(index): direction == "T" for index, direction in PORT.findall(label)}


def load_source_map(cfg_dir):
    path = Path(cfg_dir) / "source-map.json"
    if not path.is_file():
        return {"functions": {}}
    return json.loads(path.read_text())


def parse_cfg_dot(path, source_map):
    text = Path(path).read_text(errors="replace")
    title = text.splitlines()[0] if text else ""
    match = re.search(r"CFG for '(.+?)' function", title)
    symbol = match[1] if match else Path(path).name.removeprefix(".").removesuffix(".dot")
    known = (source_map.get("functions") or {}).get(symbol, {}).get("blocks", {})
    nodes, names, ports = {}, {}, {}
    for line in text.splitlines():
        parsed = NODE.match(line)
        if not parsed or "->" in line:
            continue
        node, label = parsed.group(1), parsed.group(2)
        block = BLOCK.match(label.replace('\\l', "\n"))
        name = block[1].strip() if block else node
        names[node] = name
        ports[node] = _ports(label)
        nodes[name] = {
            "id": name,
            "locations": list(known.get(name, {}).get("locations") or []),
            "calls": list(known.get(name, {}).get("calls") or []),
            "condition": known.get(name, {}).get("condition"),
        }
    edges = []
    for line in text.splitlines():
        parsed = EDGE.match(line)
        if not parsed:
            continue
        source, port, target = parsed.group(1), parsed.group(2), parsed.group(3)
        if source not in names or target not in names:
            continue
        value = None
        if port and port.startswith("s") and port[1:].isdigit():
            value = ports.get(source, {}).get(int(port[1:]))
        edges.append({"src": names[source], "dst": names[target],
                      "port": port, "true": value})
    entry = "entry" if "entry" in nodes else next(iter(nodes), None)
    return symbol, {"entry": entry, "blocks": nodes, "edges": edges}


def load_cfg_dir(cfg_dir, manifest=None):
    root = Path(cfg_dir)
    if not root.is_dir():
        raise CfgError(f"CFG directory {root} does not exist")
    source_map = load_source_map(root)
    functions = {}
    for path in sorted(root.glob("*.dot")):
        symbol, cfg = parse_cfg_dot(path, source_map)
        functions[symbol] = cfg
    return {
        "functions": functions,
        "edge_count": sum(len(cfg["edges"]) for cfg in functions.values()),
        "block_count": sum(len(cfg["blocks"]) for cfg in functions.values()),
    }
