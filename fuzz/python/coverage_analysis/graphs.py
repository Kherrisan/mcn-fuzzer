"""Convert DOT graphs once at startup; load two JSON graph files for measurements."""

import argparse
import json
from pathlib import Path


from coverage_analysis.cfg import SOURCE_MAP, parse_cfg


def read_graphs(callgraph, cfg_dir=None):
    import networkx as nx

    graph = nx.nx_pydot.read_dot(str(callgraph))
    functions = [
        {"node": str(node), "symbol": str(attrs.get("label", node)).strip('"{}')}
        for node, attrs in graph.nodes(data=True)
    ]
    edges = [
        {"source": str(source), "target": str(target),
         "kind": str(attrs.get("kind", "direct")).strip('"')}
        for source, target, attrs in graph.edges(data=True)
    ]
    cfgs = {}
    if cfg_dir is not None:
        root = Path(cfg_dir)
        source_map = root / SOURCE_MAP
        locations = json.loads(source_map.read_text()) if source_map.is_file() else {}
        cfgs = dict(parse_cfg(path, locations) for path in sorted(root.glob("*.dot")))
    return {"functions": functions, "edges": edges, "cfgs": cfgs}


def export_graphs(callgraph, cfg_dir, output_dir):
    """Atomically publish callgraph.json and cfg.json from the DOT bundle."""
    graphs = read_graphs(callgraph, cfg_dir)
    documents = {"callgraph.json": {"functions": graphs["functions"], "edges": graphs["edges"]},
                 "cfg.json": {"functions": graphs["cfgs"]}}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        path = output_dir / name
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, separators=(",", ":")) + "\n")
        temporary.replace(path)
    return graphs


def load_graphs(callgraph_json, cfg_json):
    """Read JSON only; DOTs and the source-map sidecar are not runtime inputs."""
    callgraph = json.loads(Path(callgraph_json).read_text())
    cfg = json.loads(Path(cfg_json).read_text())
    if not isinstance(callgraph.get("functions"), list) or not isinstance(callgraph.get("edges"), list):
        raise ValueError("callgraph.json requires functions and edges arrays")
    if not isinstance(cfg.get("functions"), dict):
        raise ValueError("cfg.json requires a functions object")
    return {"functions": callgraph["functions"], "edges": callgraph["edges"], "cfgs": cfg["functions"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callgraph-dot", type=Path, required=True)
    parser.add_argument("--cfg-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    graphs = export_graphs(args.callgraph_dot, args.cfg_dir, args.output_dir)
    print(json.dumps({"functions": len(graphs["functions"]), "calls": len(graphs["edges"]),
                      "cfg_functions": len(graphs["cfgs"])}))


if __name__ == "__main__":
    main()
