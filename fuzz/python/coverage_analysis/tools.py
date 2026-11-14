"""MCP tool catalogue and dispatch for the in-memory coverage service."""

import json
from pathlib import Path

from coverage_analysis import coverage as coverage_mod
from coverage_analysis.state import StateError

PAGE_DEFAULT = 20
PAGE_MAX = 100


def _schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


REQUEST = {"type": "string", "description": "registered analysis request id"}
STEP = {"type": "string", "description": "exact step name from the catalog"}
FIELD_KIND = {"type": "string", "enum": ["string", "bytes", "integer", "boolean", "list"]}
BYTES_ENCODING = {"type": "string", "enum": ["hex", "base64"]}
MUTABLE_FIELD = _schema({
    "field": {"type": "string", "minLength": 1},
    "kind": FIELD_KIND,
    "encoding": BYTES_ENCODING,
    "default": {},
    "source": {"type": "string"},
}, ("field",))
FIELD_MUTATION = _schema({
    "op": {"type": "string", "enum": ["field"]},
    "steps": {"type": ["array", "string"]},
    "field": {"type": "string", "minLength": 1},
    "suggestion": {"type": "string", "minLength": 1},
    "evidence": {"type": "string", "minLength": 1},
    "kind": FIELD_KIND,
    "encoding": BYTES_ENCODING,
    "default": {},
}, ("op", "steps", "field", "suggestion", "evidence"))
TRACE_STEP = _schema({
    "name": {"type": "string", "description": "unique step instance name"},
    "step": STEP,
    "channel": {"type": "string"},
    "sender": {"type": "string"},
    "receiver": {"type": "string"},
    "party": {"type": "string"},
    "reason": {"type": "string"},
    "constraints": {"type": "array", "items": {"type": "string"}},
    "mutable_fields": {"type": "array", "items": MUTABLE_FIELD},
}, ("name", "step", "channel", "reason"))
PAGE_PROPS = {
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "default": PAGE_DEFAULT},
}

TOOLS = [
    ("get_loaded_state", "Report which datasets are loaded and their versions.",
     _schema({})),
    ("load_coverage_json", "Load a snapshot's coverage counters, exporting them from the stored profile when needed.",
     _schema({"request_id": REQUEST, "snapshot_dir": {"type": "string"}},
             ("request_id", "snapshot_dir"))),
    ("load_cfg", "Parse per-function CFG DOT files and their source map.",
     _schema({"cfg_dir": {"type": "string"}}, ("cfg_dir",))),
    ("load_callgraph", "Parse the callgraph DOT file.",
     _schema({"callgraph_dot": {"type": "string"}}, ("callgraph_dot",))),
    ("load_protocol", "Load the protocol model, stable id catalog, and optional checkpoint.",
     _schema({"protocol_path": {"type": "string"}, "catalog_path": {"type": "string"},
              "checkpoint_path": {"type": "string"}}, ("protocol_path", "catalog_path"))),
    ("load_handlers", "Load the initialized inbound message to handler mapping.",
     _schema({"handlers_path": {"type": "string"}}, ("handlers_path",))),
    ("show_overview_code_coverage", "Project-wide branch coverage plus per-module summaries.",
     _schema({"request_id": REQUEST, **PAGE_PROPS})),
    ("show_module_code_coverage", "Coverage of one module or its children.",
     _schema({"path": {"type": "string", "description": "relative module directory"},
              "request_id": REQUEST, **PAGE_PROPS})),
    ("show_file_code_coverage", "Coverage of files and the functions they contain.",
     _schema({"file": {"type": "string", "description": "absolute or relative source file"},
              "request_id": REQUEST, **PAGE_PROPS})),
    ("show_handler_code_coverage", "Coverage of one handler and its cumulative reachable set.",
     _schema({"message": {"type": "string"}, "handler_id": {"type": "string"},
              "request_id": REQUEST,
              "sort": {"type": "string", "enum": ["missed", "name"]}, **PAGE_PROPS})),
    ("show_func_code_coverage", "Coverage of one function and its cumulative reachable set.",
     _schema({"function": {"type": "string"}, "sort": {"type": "string", "enum": ["missed", "name"]},
              "request_id": REQUEST, **PAGE_PROPS})),
    ("get_reachable_functions", "Functions reachable from one function, with distance.",
     _schema({"function": {"type": "string"}, "request_id": REQUEST,
              "include_self": {"type": "boolean", "default": False},
              "limit": {"type": "integer", "minimum": 1, "maximum": 2000}})),
    ("get_reaching_handlers", "Handlers that can reach a function, with one shortest path each.",
     _schema({"function": {"type": "string"}, "request_id": REQUEST})),
    ("show_source_coverage", "Source lines and branch true/false counts in a range.",
     _schema({"function": {"type": "string"}, "file": {"type": "string"}, "request_id": REQUEST,
              "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
              "max_lines": {"type": "integer", "minimum": 1,
                            "maximum": coverage_mod.MAX_SOURCE_LINES}})),
    ("get_protocol_steps", "Protocol steps from the catalog, optionally filtered.",
     _schema({"message": {"type": "string"}, "party": {"type": "string"},
              "request_id": REQUEST, **PAGE_PROPS})),
    ("get_step_dependencies", "Dependencies before and after one step in the current transaction.",
     _schema({"request_id": REQUEST, "step": STEP}, ("request_id", "step"))),
    ("update_step_annotations", "Replace analysis annotations and constraints on one step.",
     _schema({"request_id": REQUEST, "step": STEP,
              "annotations": {"type": "array", "items": {"type": "string"}},
              "constraints": {"type": "array", "items": {"type": "string"}}},
             ("request_id", "step"))),
    ("upsert_dependency", "Add or replace a dependency edge in the analysis transaction.",
     _schema({"request_id": REQUEST,
              "prior_step": STEP, "dependent_step": STEP,
              "condition": {"type": "string"},
              "field_relations": {"type": "array", "items": _schema({
                  "prior_field": {"type": "string"},
                  "dependent_field": {"type": "string"},
                  "derive": {"type": "string", "description": "Calculation from the prior field; empty means direct reuse"},
              }, ("prior_field", "dependent_field"))}},
             ("request_id", "prior_step", "dependent_step"))),
    ("remove_dependency", "Remove a dependency edge in the analysis transaction.",
     _schema({"request_id": REQUEST, "prior_step": STEP, "dependent_step": STEP},
             ("request_id", "prior_step", "dependent_step"))),
    ("begin_analysis", "Open the graph transaction for a registered request.",
     _schema({"request_id": REQUEST}, ("request_id",))),
    ("submit_proposal", "Validate and freeze a proposal, ending graph edits.",
     _schema({"request_id": REQUEST,
              "origin": {"type": "string", "enum": ["coverage", "autonomous"]},
              "goal": {"type": "object"},
              "parties": {"type": "object"},
              "channels": {"type": "object"},
              "mutations": {"type": "array", "items": FIELD_MUTATION},
              "dependencies": {"type": "array", "items": _schema({
                  "prior": {"type": "string"}, "dependent": {"type": "string"},
                  "kind": {"type": "string", "enum": ["order", "data", "resource", "state"]},
                  "condition": {"type": "string"},
                  "fields": {"type": "array", "items": {"type": "string", "minLength": 1},
                             "description": "State keys saved by prior and read by dependent"},
              }, ("prior", "dependent"))},
              "trace": {"type": "array", "items": TRACE_STEP}},
             ("request_id", "trace", "parties", "channels"))),
]

TOOL_NAMES = [name for name, _, _ in TOOLS]
TOOL_BY_NAME = {name: (description, schema) for name, description, schema in TOOLS}


def mcp_tool_list():
    return [{"name": name, "description": description, "inputSchema": schema}
            for name, description, schema in TOOLS]


def _missing(key, *tools):
    return StateError("not_loaded", f"{key} is not loaded", missing=list(tools))


def _require(state, key, request_id=None, load_tool=None):
    try:
        return state.dataset(key, request_id).value
    except StateError:
        raise _missing(key, load_tool or f"load_{key}")


def _coverage(state, request_id=None):
    return _require(state, "coverage", request_id, "load_coverage_json")


def _callgraph(state, request_id=None):
    return _require(state, "callgraph", request_id, "load_callgraph")


def _callgraph_optional(state, request_id=None):
    loaded = state.latest.get("callgraph")
    return loaded.value if loaded else None


def _handlers(state, request_id=None):
    return _require(state, "handlers", request_id, "load_handlers")


def _protocol(state, request_id=None):
    return _require(state, "protocol", request_id, "load_protocol")


def _canonical(coverage, name):
    if coverage is None:
        return name
    return coverage.aliases.get(name, name)


def _resolve_function(coverage, callgraph, name):
    canonical = _canonical(coverage, name)
    if coverage is not None and canonical in coverage.functions:
        return canonical
    if callgraph is not None and callgraph.has(name):
        return name
    if callgraph is not None and callgraph.has(canonical):
        return canonical
    raise StateError("unknown_function", f"unknown function {name}")


def _function_row(coverage, callgraph, name):
    canonical = _resolve_function(coverage, callgraph, name)
    row = {"symbol": canonical, "files": [], "own": None, "cumulative": None,
           "status": "ok"}
    if coverage is not None and canonical in coverage.functions:
        row["files"] = coverage.functions[canonical].files
        row["own"] = coverage.function_stats(canonical)
    else:
        row["status"] = "missing_data"
    if callgraph is not None and callgraph.has(canonical):
        reachable = callgraph.reachable(canonical, include_self=True)
        symbols = [item["symbol"] for item in reachable]
        row["reachable_functions"] = len(symbols)
        if coverage is not None:
            row["cumulative"] = coverage.cumulative(symbols)
    return row


def _handler_rows(coverage, callgraph, handlers, message=None, handler_id=None):
    rows = []
    for row in handlers.rows:
        if message and row.message != message:
            continue
        for handler in row.handlers:
            if handler_id and handler.id != handler_id:
                continue
            symbols = []
            if callgraph is not None and callgraph.has(handler.symbol):
                symbols = [item["symbol"] for item in callgraph.reachable(handler.symbol, include_self=True)]
            elif coverage is not None and handler.symbol in coverage.functions:
                symbols = [handler.symbol]
            own = None
            cumulative = None
            if coverage is not None and handler.symbol in coverage.functions:
                own = coverage.function_stats(handler.symbol)
                cumulative = coverage.cumulative(symbols or [handler.symbol])
            rows.append({"message": row.message, "handler": handler.view(),
                         "own": own, "cumulative": cumulative,
                         "reachable_functions": len(symbols),
                         "status": "ok" if own else "missing_data"})
    return rows


def _sort_rows(rows, sort):
    if sort == "name":
        return sorted(rows, key=lambda item: (item.get("message", ""), item.get("symbol", "")))
    def missed(item):
        stats = item.get("cumulative") or item.get("own") or {}
        return -(stats.get("missed_branches") or 0)
    return sorted(rows, key=missed)


def call(state, name, arguments):
    arguments = arguments or {}
    if name not in TOOL_BY_NAME:
        raise StateError("unknown_tool", f"unknown tool {name}")
    handler = globals().get(f"tool_{name}")
    if handler is None:
        raise StateError("unknown_tool", f"tool {name} has no implementation")
    return handler(state, arguments)


# -- load tools ---------------------------------------------------------
def tool_get_loaded_state(state, arguments):
    return state.loaded_state()


def tool_load_coverage_json(state, arguments):
    return state.load_coverage_json(arguments["request_id"], arguments["snapshot_dir"])


def tool_load_cfg(state, arguments):
    return state.load_cfg(arguments["cfg_dir"])


def tool_load_callgraph(state, arguments):
    return state.load_callgraph(arguments["callgraph_dot"])


def tool_load_protocol(state, arguments):
    return state.load_protocol(arguments["protocol_path"], arguments["catalog_path"],
                               arguments.get("checkpoint_path"))


def tool_load_handlers(state, arguments):
    return state.load_handlers(arguments["handlers_path"])


# -- coverage tools -----------------------------------------------------
def _stats_block(rows, extra=None):
    return {**(extra or {}), **rows}


def tool_show_overview_code_coverage(state, arguments):
    data = _coverage(state, arguments.get("request_id"))
    modules = {}
    for path, records in data.files.items():
        module = data.module_of(path)
        modules.setdefault(module, []).extend(records)
    rows = [{"module": module, **data.stats_for(records)}
            for module, records in sorted(modules.items())]
    return _stats_block(data.all_stats(), {"modules": rows,
                                           "source_root": str(data.source_root or ""),
                                           "files": len(data.files),
                                           "functions": len(data.functions)})


def tool_show_module_code_coverage(state, arguments):
    data = _coverage(state, arguments.get("request_id"))
    prefix = (arguments.get("path") or "").strip("/")
    modules, files, selected = {}, [], []
    for path, records in data.files.items():
        relative = data.relative(path)
        if prefix and not (relative == prefix or relative.startswith(prefix + "/")):
            continue
        selected.extend(records)
        remainder = relative[len(prefix):].lstrip("/") if prefix else relative
        if "/" in remainder:
            module = "/".join(filter(None, [prefix, remainder.split("/", 1)[0]]))
            modules.setdefault(module, []).extend(records)
        else:
            files.append({"file": relative, **data.stats_for(records)})
    rows = [{"module": module, **data.stats_for(records)}
            for module, records in sorted(modules.items())]
    rows.extend(sorted(files, key=lambda item: -item["missed_branches"]))
    return _stats_block(data.stats_for(selected),
                        {"path": "." if not prefix else prefix,
                         "children": coverage_mod.page(
                             rows, arguments.get("offset", 0),
                             arguments.get("limit", PAGE_DEFAULT),
                             key=lambda item: -item["missed_branches"])})


def tool_show_file_code_coverage(state, arguments):
    data = _coverage(state, arguments.get("request_id"))
    wanted = arguments.get("file")
    if wanted:
        match = None
        for path in data.files:
            if path == wanted or data.relative(path) == wanted or Path(path).name == wanted:
                match = path
                break
        if match is None:
            raise StateError("unknown_file", f"unknown source file {wanted}")
        functions = []
        for name, function in data.functions.items():
            if match in function.files:
                functions.append({"function": name,
                                  **_stats_block(data.function_stats(name))})
        functions.sort(key=lambda item: -item["missed_branches"])
        return _stats_block(data.file_stats(match),
                            {"file": data.relative(match), "functions": functions})
    rows = [{"file": data.relative(path), **data.stats_for(records)}
            for path, records in data.files.items()]
    return _stats_block(data.all_stats(),
                        {"files": coverage_mod.page(rows, arguments.get("offset", 0),
                                                    arguments.get("limit", PAGE_DEFAULT),
                                                    key=lambda item: -item["missed_branches"])})


def tool_show_handler_code_coverage(state, arguments):
    coverage = _coverage(state, arguments.get("request_id"))
    handlers = _handlers(state, arguments.get("request_id"))
    callgraph = _callgraph_optional(state, arguments.get("request_id"))
    rows = _handler_rows(coverage, callgraph, handlers, arguments.get("message"),
                         arguments.get("handler_id"))
    if not rows and (arguments.get("message") or arguments.get("handler_id")):
        raise StateError("not_found", "no handler matches the filter")
    rows = _sort_rows(rows, arguments.get("sort", "missed"))
    return {"handlers": coverage_mod.page(rows, arguments.get("offset", 0),
                                          arguments.get("limit", PAGE_DEFAULT)),
            "total": len(rows)}


def tool_show_func_code_coverage(state, arguments):
    coverage = _coverage(state, arguments.get("request_id"))
    callgraph = _callgraph_optional(state, arguments.get("request_id"))
    if arguments.get("function"):
        return _function_row(coverage, callgraph, arguments["function"])
    rows = []
    for name in coverage.functions:
        try:
            rows.append(_function_row(coverage, callgraph, name))
        except StateError:
            continue
    rows = _sort_rows(rows, arguments.get("sort", "missed"))
    return {"functions": coverage_mod.page(rows, arguments.get("offset", 0),
                                           arguments.get("limit", PAGE_DEFAULT)),
            "total": len(rows)}


def tool_get_reachable_functions(state, arguments):
    callgraph = _callgraph(state, arguments.get("request_id"))
    rows = callgraph.reachable(arguments["function"], bool(arguments.get("include_self")))
    limit = min(int(arguments.get("limit") or 500), 2000)
    return {"function": arguments["function"], "functions": rows[:limit],
            "total": len(rows), "truncated": len(rows) > limit}


def tool_get_reaching_handlers(state, arguments):
    callgraph = _callgraph(state, arguments.get("request_id"))
    handlers = _handlers(state, arguments.get("request_id"))
    symbols = sorted({handler.symbol for handler in handlers.by_id.values()})
    rows = callgraph.reaching_handlers(arguments["function"], symbols)
    for row in rows:
        row["handler_ids"] = [handler.id for handler in handlers.by_id.values()
                              if handler.symbol == row["handler"]]
    return {"function": arguments["function"], "handlers": rows}


def tool_show_source_coverage(state, arguments):
    coverage = _coverage(state, arguments.get("request_id"))
    function = arguments.get("function")
    path = arguments.get("file")
    records = []
    if function:
        canonical = _resolve_function(coverage, None, function)
        records = coverage.branch_records(canonical)
        if not path:
            files = coverage.functions[canonical].files
            path = files[0] if files else None
    elif path:
        match = next((candidate for candidate in coverage.files
                      if candidate == path or coverage.relative(candidate) == path), None)
        if match is None:
            raise StateError("unknown_file", f"unknown source file {path}")
        records = coverage.files[match]
        path = match
    else:
        raise StateError("bad_request", "show_source_coverage needs function or file")
    root = state.source_root or coverage.source_root
    return coverage_mod.source_slice(root, records, path,
                                     arguments.get("start_line"), arguments.get("end_line"))


# -- protocol tools -----------------------------------------------------
def tool_get_protocol_steps(state, arguments):
    graph = _protocol(state, arguments.get("request_id"))
    rows = []
    for step in graph.catalog["steps"]:
        view = graph.view(step["name"])
        if arguments.get("message") and view["message"] != arguments["message"]:
            continue
        if arguments.get("party") and arguments["party"] not in (
                view["sender"], view["receiver"], view["party"]):
            continue
        rows.append(view)
    rows.sort(key=lambda item: item["name"])
    return {"protocol_name": graph.base.get("protocol_name"),
            "binding": graph.catalog.get("binding") or {},
            "steps": coverage_mod.page(rows, arguments.get("offset", 0),
                                       arguments.get("limit", PAGE_DEFAULT)),
            "total": len(rows)}


def tool_get_step_dependencies(state, arguments):
    graph = state.txn_graph(arguments["request_id"])
    step = graph.resolve(arguments["step"])
    return {"step": graph.view(step), **graph.dependencies(step),
            "graph_revision": graph.revision}


def tool_update_step_annotations(state, arguments):
    graph = state.txn_graph(arguments["request_id"])
    if "annotations" not in arguments and "constraints" not in arguments:
        raise StateError("bad_request", "provide annotations or constraints")
    result = graph.annotate(arguments["step"], arguments.get("annotations"),
                            arguments.get("constraints"))
    return {"step": result, "graph_revision": graph.revision}


def tool_upsert_dependency(state, arguments):
    graph = state.txn_graph(arguments["request_id"])
    row = graph.upsert_dependency(arguments["prior_step"],
                                  arguments["dependent_step"],
                                  arguments.get("condition", ""),
                                  arguments.get("field_relations"))
    return {**row, "graph_revision": graph.revision}


def tool_remove_dependency(state, arguments):
    graph = state.txn_graph(arguments["request_id"])
    row = graph.remove_dependency(arguments["prior_step"],
                                  arguments["dependent_step"])
    return {**row, "graph_revision": graph.revision}


def tool_begin_analysis(state, arguments):
    return state.begin_analysis(arguments["request_id"])


def tool_submit_proposal(state, arguments):
    return state.submit_proposal(arguments["request_id"], arguments)
