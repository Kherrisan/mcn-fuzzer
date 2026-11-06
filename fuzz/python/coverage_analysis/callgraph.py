"""Callgraph DOT parsing and reachability queries.

LLVM's ``dot-callgraph`` emits one record node per function and plain edges.
The graph is kept as adjacency dictionaries so reachability stays cheap.
"""

import re
from dataclasses import dataclass

_NODE = re.compile(r'^\s*(\S+)\s*\[.*?label="(.*?)".*\]\s*;?\s*$')
_EDGE = re.compile(r"^\s*(\S+?)(?::\S+)?\s*->\s*(\S+?)(?::\S+)?\s*(?:\[.*\])?;?\s*$")
_LABEL = re.compile(r"^\{(.*)\}$")


class CallgraphError(ValueError):
    pass


def _symbol(label):
    match = _LABEL.match(label.strip())
    text = match[1] if match else label
    return text.replace("\\l", "").strip()


@dataclass
class Callgraph:
    symbols: dict
    successors: dict
    predecessors: dict
    node_count: int = 0
    edge_count: int = 0
    digest: str = ""

    def resolve(self, name):
        if name in self.symbols.values():
            return name
        return None

    def has(self, symbol):
        return symbol in self.successors

    def reachable(self, symbol, include_self=False):
        if symbol not in self.successors:
            raise CallgraphError(f"unknown function {symbol}")
        distances = {symbol: 0} if include_self else {}
        queue = [symbol]
        while queue:
            current = queue.pop(0)
            distance = distances.get(current, 0)
            for target in self.successors.get(current, ()):
                if target in distances:
                    continue
                distances[target] = distance + 1
                queue.append(target)
        if include_self:
            distances.pop(symbol, None)
        return [{"symbol": name, "distance": value}
                for name, value in sorted(distances.items(), key=lambda item: (item[1], item[0]))]

    def reaching_handlers(self, symbol, handlers):
        """BFS backwards; report one shortest path per handler that reaches it."""
        if symbol not in self.predecessors:
            raise CallgraphError(f"unknown function {symbol}")
        wanted = set(handlers)
        found = {}
        queue = [(symbol, [symbol])]
        visited = {symbol}
        while queue:
            current, path = queue.pop(0)
            if current in wanted and current not in found:
                found[current] = path
                if len(found) == len(wanted):
                    break
            for caller in self.predecessors.get(current, ()):
                if caller in visited:
                    continue
                visited.add(caller)
                queue.append((caller, [caller, *path]))
        return [{"handler": handler, "path": path}
                for handler, path in sorted(found.items())]


def load_callgraph(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    symbols = {}
    successors = {}
    predecessors = {}
    edges = set()
    for line in text.splitlines():
        node = _NODE.match(line)
        if node and "->" not in line:
            symbols[node.group(1)] = _symbol(node.group(2))
            continue
        edge = _EDGE.match(line)
        if not edge:
            continue
        source, target = edge.group(1), edge.group(2)
        if source == target or "graph" in (source, target):
            continue
        if (source, target) in edges:
            continue
        edges.add((source, target))
    for source, target in edges:
        src = symbols.get(source)
        dst = symbols.get(target)
        if not src or not dst:
            continue
        successors.setdefault(src, set()).add(dst)
        predecessors.setdefault(dst, set()).add(src)
    for name in [value for value in symbols.values() if value.startswith("llvm.")]:
        successors.pop(name, None)
        predecessors.pop(name, None)
    symbols = {node: symbol for node, symbol in symbols.items() if not symbol.startswith("llvm.")}
    for name in symbols.values():
        successors.setdefault(name, set())
        predecessors.setdefault(name, set())
    for name in list(successors):
        successors[name] = {target for target in successors[name] if not target.startswith("llvm.")}
    for name in list(predecessors):
        predecessors[name] = {source for source in predecessors[name] if not source.startswith("llvm.")}
    ordered_successors = {key: frozenset(value) for key, value in successors.items()}
    ordered_predecessors = {key: frozenset(value) for key, value in predecessors.items()}
    return Callgraph(symbols, ordered_successors, ordered_predecessors,
                     node_count=len(symbols), edge_count=len(edges))
