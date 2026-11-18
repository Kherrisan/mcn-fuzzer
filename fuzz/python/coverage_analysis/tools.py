"""MCP tool catalogue and dispatch for the in-memory coverage service."""

import json
from pathlib import Path

from coverage_analysis import coverage as coverage_mod
from coverage_analysis import source as source_mod
from pydantic import TypeAdapter, ValidationError

from coverage_analysis import schemas
from coverage_analysis.state import StateError

PAGE_DEFAULT = 20
PAGE_MAX = 100


TOOL_DESCRIPTIONS = {'get_loaded_state': 'Report which datasets are loaded and their versions.',
 'load_coverage_json': "Load a snapshot's coverage counters, exporting them from the stored profile when "
                       'needed.',
 'load_cfg': 'Parse per-function CFG DOT files and their source map.',
 'load_callgraph': 'Parse the callgraph DOT file.',
 'load_protocol': 'Load the protocol model and optional checkpoint.',
 'load_handlers': 'Load the initialized inbound message to handler mapping.',
 'write_handlers': 'Serialise handlers.json from mapping data, validate it against the loaded protocol, '
                   'and optionally load it as the request mapping.',
 'show_overview_code_coverage': 'Project-wide branch coverage plus per-module summaries.',
 'show_module_code_coverage': 'Coverage of one module or its children.',
 'show_file_code_coverage': 'Coverage of files and the functions they contain.',
 'show_handler_code_coverage': 'Coverage of one handler and its cumulative reachable set.',
 'show_func_code_coverage': 'Coverage of one function and its cumulative reachable set.',
 'get_reachable_functions': 'Functions reachable from one function, with distance.',
 'get_reaching_handlers': 'Handlers that can reach a function, with one shortest path each.',
 'describe_handler': 'Summarize one handler in the active analysis, including its mapped steps, coverage, '
                     'and source window.',
 'describe_trace_steps': 'Read model steps, distinct message fields, and transaction dependencies among '
                         'those steps in the active analysis.',
 'show_source_coverage': 'Source lines and branch true/false counts in a range. Function queries use the '
                         'C/C++ syntax definition range, with bounded windows and remaining ranges.',
 'get_protocol_steps': 'Protocol steps from the catalog, optionally filtered.',
 'list_parties': 'List protocol parties with descriptions, aliases, and current campaign bindings.',
 'get_step_dependencies': 'Dependencies before and after one step in the current transaction.',
 'list_messages': 'Page model messages with direction and the steps that carry them.',
 'show_message': 'Read one message: fields and the steps that carry it.',
 'show_step': 'Read one step: action, channel, endpoints, processing, and direct predecessors.',
 'update_step_annotations': 'Replace analysis annotations and constraints on one step.',
 'upsert_dependency': 'Add or replace a dependency edge in the analysis transaction.',
 'remove_dependency': 'Remove a dependency edge in the analysis transaction.',
 'begin_analysis': 'Open the graph transaction and make the request current for target tools.',
 'select_analysis_target': 'Select the primary handler for the active analysis.',
 'get_analysis_target': "Read the active analysis's primary handler and selection reason.",
 'switch_analysis_target': "Replace the active analysis's primary handler with a different handler.",
 'show_function_source': "Read one or more functions' source with branch counts; whole=true reads the full "
                         'body.',
 'show_replay_logs': "Read a bounded, line-numbered window of a request's frozen replay logs.",
 'submit_proposal': 'Validate and freeze a proposal, ending graph edits.'}

TOOL_NAMES = list(TOOL_DESCRIPTIONS)
OUTPUT_ADAPTERS = {name: TypeAdapter(output) for name, (_, output) in schemas.TOOL_MODELS.items()}


def mcp_tool_list():
    return [{"name": name, "description": description,
             "inputSchema": schemas.TOOL_MODELS[name][0].model_json_schema(),
             "outputSchema": {**OUTPUT_ADAPTERS[name].json_schema(), "type": "object"}}
            for name, description in TOOL_DESCRIPTIONS.items()]


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
    if name not in schemas.TOOL_MODELS:
        raise StateError("unknown_tool", f"unknown tool {name}")
    input_model, _ = schemas.TOOL_MODELS[name]
    try:
        validated = input_model.model_validate(arguments or {})
    except ValidationError as error:
        raise StateError("bad_request", str(error)) from error
    handler = globals().get(f"tool_{name}")
    if handler is None:
        raise StateError("unknown_tool", f"tool {name} has no implementation")
    result = handler(state, validated.model_dump(exclude_unset=True))
    try:
        return OUTPUT_ADAPTERS[name].validate_python(result, strict=True).model_dump(exclude_unset=True)
    except ValidationError as error:
        raise StateError("internal", f"invalid {name} output: {error}") from error


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
    return state.load_protocol(arguments["protocol_path"],
                               arguments.get("checkpoint_path"))


def tool_load_handlers(state, arguments):
    return state.load_handlers(arguments["handlers_path"])


def tool_write_handlers(state, arguments):
    return state.write_handlers(arguments["output_dir"], arguments["session"],
                                arguments["mappings"], arguments.get("load", True))


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
    page = coverage_mod.page(rows, arguments.get("offset", 0), arguments.get("limit", PAGE_DEFAULT))
    return _stats_block(data.all_stats(), {"modules": page["items"],
                                           "total": page["total"],
                                           "next_offset": page["next_offset"],
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
    return _source_window(state, arguments, coverage_mod.DEFAULT_SOURCE_LINES)


# -- protocol tools -----------------------------------------------------
def tool_list_parties(state, arguments):
    graph = _protocol(state)
    binding = graph.catalog.get("binding") or {}
    parties = graph.base["model"].get("parties") or []
    return {"parties": [
        {"name": party["name"], "description": party.get("description", ""),
         "alias": list(party.get("alias") or []), "binding": binding.get(party["name"])}
        for party in parties
    ]}


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
    direction = arguments.get("direction", "both")
    if direction not in ("before", "after", "both"):
        raise StateError("bad_request", "direction must be before, after, or both")
    trace = arguments.get("trace_steps")
    allowed = None if trace is None else {graph.resolve(name) for name in trace}
    result = {"step": step, "before": [], "after": [], "graph_revision": graph.revision}
    for side, edges in (("before", graph.graph.in_edges(step, data=True)),
                        ("after", graph.graph.out_edges(step, data=True))):
        if direction in (side, "both"):
            for source, target, edge in edges:
                neighbor = source if side == "before" else target
                if allowed is None or neighbor in allowed:
                    result[side].append({"step": neighbor,
                                         "condition": edge.get("condition", ""),
                                         "field_relations": edge.get("field_relations", [])})
    return result


def _binding(graph):
    return graph.catalog.get("binding") or {}


def tool_list_messages(state, arguments):
    graph = _protocol(state, arguments.get("request_id"))
    index = graph.message_index()
    roles = _binding(graph)
    wanted = arguments.get("direction") or "all"
    rows = []
    for name in sorted(index["fields"]):
        carriers = index["steps"].get(name) or []
        direction = graph.direction(carriers[0], roles) if carriers else "unbound"
        if wanted != "all" and direction != wanted:
            continue
        rows.append({
            "message": name,
            "direction": direction,
            "fields": index["fields"][name],
            "steps": [row["step"] for row in carriers],
        })
    offset = max(0, int(arguments.get("offset") or 0))
    limit = min(max(1, int(arguments.get("limit") or 20)), 50)
    return {"total": len(rows), "offset": offset, "limit": limit,
            "items": rows[offset:offset + limit]}


def tool_show_message(state, arguments):
    graph = _protocol(state, arguments.get("request_id"))
    index = graph.message_index()
    name = str(arguments["message"])
    if name not in index["fields"]:
        raise StateError("not_found", f"unknown message {name}")
    roles = _binding(graph)
    carriers = index["steps"].get(name) or []
    return {
        "message": name,
        "fields": index["fields"][name],
        "steps": [{**row, "direction": graph.direction(row, roles)} for row in carriers],
    }


def tool_show_step(state, arguments):
    request_id = arguments.get("request_id")
    graph = state.txn_graph(request_id) if request_id else _protocol(state)
    name = graph.resolve(str(arguments["step"]))
    step = graph.step(name)
    before = graph.dependencies(name)["before"]
    needs = []
    for row in before[:8]:
        condition = str(row.get("condition") or "")
        needs.append({"step": row["step"],
                      "action": row.get("action") or "send",
                      "condition": condition[:160]})
    return {
        "step": name,
        "action": step.get("action") or "send",
        "message": step.get("message_name"),
        "channel": step.get("channel_name"),
        "sender": step.get("sender_name"),
        "receiver": step.get("receiver_name"),
        "party": step.get("party_name"),
        "condition": str(step.get("condition") or "")[:200],
        "processing": step.get("processing"),
        "total_predecessors": len(before),
        "needs": needs,
    }


def tool_describe_handler(state, arguments):
    request = state._active_request()
    request_id = request.request_id
    graph = state.txn_graph(request_id)
    handler_id = arguments.get("handler_id")
    if handler_id is None:
        if request.analysis_target is None:
            raise StateError("analysis_target_required", "select a handler or provide handler_id")
        handler_id = request.analysis_target["handler_id"]
    handlers = state._request_handlers(request)
    handler = handlers.by_id.get(handler_id)
    if handler is None:
        raise StateError("unknown_handler", f"unknown handler {handler_id!r} for this request")
    if "coverage" not in request.bindings:
        raise StateError("not_loaded", "the active request's coverage is not loaded",
                         missing=["load_coverage_json"])
    coverage = _coverage(state, request_id)
    callgraph = _callgraph_optional(state, request_id)
    rows = _handler_rows(coverage, callgraph, handlers, handler_id=handler_id)
    mappings = []
    for row in handlers.rows:
        for mapped in row.handlers:
            if mapped.id == handler_id:
                steps = []
                for name in row.step_names:
                    view = graph.view(name)
                    prior = list(graph.graph.in_edges(name, data=True))
                    view["predecessors"] = [
                        {"step": source, "condition": edge.get("condition", "")}
                        for source, _, edge in prior[:5]
                    ]
                    view["predecessors_total"] = len(prior)
                    steps.append(view)
                mappings.append({"message": row.message, "handler": mapped.view(),
                                 "steps": steps})
    limit = min(max(1, int(arguments.get("limit", 10))), 50)
    source = None
    source_error = None
    try:
        source = _source_window(state, {"request_id": request_id, "function": handler.symbol,
                                       "max_lines": arguments.get("source_lines", 60)}, 60)
    except (StateError, coverage_mod.CoverageError) as error:
        source_error = {"code": getattr(error, "code", "source_error"), "message": str(error)}
    return {"request_id": request_id, "handler": handler.view(),
            "mappings": coverage_mod.page(mappings, arguments.get("offset", 0), limit),
            "coverage": {key: rows[0][key] for key in ("own", "cumulative", "reachable_functions", "status")},
            "source": source, "source_error": source_error, "graph_revision": graph.revision}


def tool_describe_trace_steps(state, arguments):
    request = state._active_request()
    graph = state.txn_graph(request.request_id)
    steps = arguments.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise StateError("bad_request", "steps must contain 1 to 50 model step names")
    names = [graph.resolve(name) for name in steps]
    selected = set(names)
    index = graph.message_index()
    items = []
    messages = {}
    for name in names:
        view = graph.view(name)
        model = graph.step(name)
        view["processing"] = model.get("processing")
        predecessors = list(graph.graph.predecessors(name))
        other = [prior for prior in predecessors if prior not in selected]
        view["other_predecessors"] = other[:8]
        view["other_predecessors_total"] = len(other)
        items.append(view)
        message = view["message"]
        if message and message not in messages:
            messages[message] = index["fields"].get(message, [])
    dependencies = graph.induced_dependencies(selected)
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", PAGE_DEFAULT)
    return {"request_id": request.request_id, "graph_revision": graph.revision,
            "steps": coverage_mod.page(items, offset, limit),
            "messages": messages,
            "dependencies": coverage_mod.page(dependencies, arguments.get("dependency_offset", 0), 100)}


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


def _coverage_source(paths, requested, root):
    requested = Path(requested)
    exact = [path for path in paths if Path(path) == requested]
    if requested.is_absolute() and root is not None:
        root = Path(root).resolve()
        if requested.is_relative_to(root):
            requested = requested.relative_to(root)
    matches = exact or [
        path for path in paths
        if Path(path).parts[-len(requested.parts):] == requested.parts
    ]
    if not matches:
        raise StateError("unknown_file", f"unknown source file {requested}")
    if len(matches) != 1:
        raise StateError("ambiguous_file", f"multiple coverage files match {requested}")
    return matches[0]


def _source_path(root, covered_path):
    """Read the compiled source named by coverage, never an alternate copy."""
    covered = Path(covered_path)
    if not covered.is_absolute():
        if root is None:
            raise StateError("not_found", f"compiled source root is required for {covered}")
        covered = Path(root) / covered
    if covered.is_file():
        return covered.resolve()
    raise StateError("not_found", f"compiled source is missing: {covered}; "
                     "restore the source used by the llvm-cov binary; alternate source copies are not used")


def _line_range(start, end):
    return {"start": start, "end": end} if start <= end else None


def _function_file(coverage, canonical, paths, root):
    """Pick the file that really defines the function.

    ``filenames[0]`` is not always the translation unit: coverage lists header
    inlines first for many OpenSSL functions. Try each candidate file and keep
    the one whose syntax range explains the branch records.
    """
    ordered = sorted(
        paths,
        key=lambda path: (path.lower().endswith((".h", ".hpp", ".hh", ".hxx")), path),
    )
    problems = []
    fallback = None
    with_records = []
    without_records = []
    for covered in ordered:
        records = [record for record in coverage.branch_records(canonical)
                   if record.file == covered]
        if records:
            with_records.append((covered, records))
        else:
            without_records.append((covered, []))
    # Files with branch records disambiguate duplicate definitions; a function
    # that the snapshot never entered has none, so try those afterwards.
    for covered, records in with_records + without_records:
        if records and fallback is None:
            fallback = (covered, records)
        try:
            path = _source_path(root, covered)
            first, last = source_mod.function_range(path, canonical, records)
        except (OSError, source_mod.SourceError) as error:
            problems.append(f"{covered}: {error}")
            continue
        return covered, records, path, first, last, "tree_sitter"
    # Macros and fully inlined helpers have no function definition. Report the
    # coverage span instead of failing, and label the basis so callers can tell
    # the range is not a syntax definition.
    if fallback is not None:
        covered, records = fallback
        path = _source_path(root, covered)
        first = min(record.line for record in records)
        last = max(max(record.line, record.end_line) for record in records)
        return covered, records, path, first, last, "coverage_branches"
    raise StateError("source_range_unavailable",
                     "; ".join(problems[:3]) or f"no source file defines {canonical}")


def _source_window(state, arguments, default_width):
    coverage = _coverage(state, arguments.get("request_id"))
    root = state.source_root or coverage.source_root
    function = arguments.get("function")
    requested = arguments.get("file")
    first, last = 1, None
    if function:
        canonical = _resolve_function(coverage, None, function)
        paths = coverage.functions[canonical].files
        if not paths:
            raise StateError("not_found", f"function {canonical} has no source file")
        if requested:
            covered = _coverage_source(paths, requested, root)
            records = [record for record in coverage.branch_records(canonical)
                       if record.file == covered]
            path = _source_path(root, covered)
            try:
                first, last = source_mod.function_range(path, canonical, records)
            except (OSError, source_mod.SourceError) as error:
                raise StateError("source_range_unavailable", str(error)) from error
            basis = "tree_sitter"
        else:
            covered, records, path, first, last, basis = _function_file(
                coverage, canonical, paths, root)
    elif requested:
        covered = _coverage_source(coverage.files, requested, root)
        records = coverage.files[covered]
    else:
        raise StateError("bad_request", "show_source_coverage needs function or file")

    if not function:
        path = _source_path(root, covered)

    width = arguments.get("max_lines", arguments.get("radius", default_width))
    width = max(1, min(int(width), coverage_mod.MAX_SOURCE_LINES))
    around = arguments.get("around_line")
    start_line = arguments.get("start_line")
    end_line = arguments.get("end_line")
    if around is not None:
        if start_line is not None or end_line is not None:
            raise StateError("bad_request", "use around_line or an explicit interval")
        if around < first or (last is not None and around > last):
            raise StateError("bad_request", f"around_line {around} is outside source range {first}–{last or 'EOF'}")
        start = max(first, around - width // 2)
        if last is not None:
            start = min(start, max(first, last - width + 1))
    else:
        start = max(first, start_line or first)
    end = start + width - 1
    if end_line is not None:
        end = min(end, end_line)
    if last is not None:
        end = min(end, last)
    if end < start:
        raise StateError("bad_request", f"requested interval does not intersect source range {first}–{last or 'EOF'}")

    window = coverage_mod.source_slice(None, records, path, start, end)
    if not window["lines"] or (around is not None and around > window["total_lines"]):
        raise StateError("bad_request", f"requested window is outside {path} (1–{window['total_lines']})")
    window["remaining"] = {
        "before": _line_range(first, window["start"] - 1),
        "after": _line_range(window["end"] + 1, last if last is not None else window["total_lines"]),
    }
    if function:
        window.update(function=canonical, file=window["path"], stats=coverage.function_stats(canonical),
                      range={"start": first, "end": last, "basis": basis,
                             "first_branch_line": min((row.line for row in records), default=None),
                             "last_branch_line": max((row.end_line for row in records), default=None)})
    return window


def tool_show_function_source(state, arguments):
    if arguments.get("functions") is not None or arguments.get("whole"):
        return _source_bundle(state, arguments)
    if not arguments.get("function"):
        raise StateError("bad_request", "show_function_source needs function or functions")
    return _source_window(state, arguments, 120)


def _function_window(coverage, root, name, arguments):
    """Resolve one function to a bounded window inside its syntax range."""
    canonical = _resolve_function(coverage, None, name)
    paths = coverage.functions[canonical].files
    if not paths:
        raise StateError("not_found", f"function {canonical} has no source file")
    covered, records, path, first, last, basis = _function_file(
        coverage, canonical, paths, root)
    width = int(arguments.get("radius") or coverage_mod.DEFAULT_SOURCE_LINES)
    width = max(10, min(width, coverage_mod.MAX_SOURCE_LINES))
    if arguments.get("whole"):
        start, end = first, last
    else:
        start = first
        end = min(max(last, start), start + width - 1)
    return canonical, path, records, first, last, start, end, basis


def _source_bundle(state, arguments):
    """Read several functions in one call, respecting a shared line budget."""
    coverage = _coverage(state, arguments.get("request_id"))
    root = state.source_root or coverage.source_root
    names = arguments.get("functions")
    if names is None:
        names = [arguments["function"]]
    if not isinstance(names, list) or not names or len(names) > 8:
        raise StateError("bad_request", "functions must hold 1 to 8 names")
    budget = int(arguments.get("max_lines") or 400)
    budget = max(10, min(budget, 2000))
    results = []
    spent = 0
    for name in names:
        canonical, path, records, first, last, start, end, basis = _function_window(
            coverage, root, str(name), arguments)
        room = budget - spent
        if room < 1:
            results.append({"function": canonical, "file": str(path),
                            "range": {"start": first, "end": last, "basis": basis},
                            "skipped": True, "reason": "line budget exhausted"})
            continue
        end = min(end, start + room - 1)
        window = coverage_mod.source_slice(None, records, path, start, end)
        spent += len(window["lines"])
        entry = {
            "function": canonical,
            "file": window["path"],
            "range": {"start": first, "end": last, "basis": basis,
                      "first_branch_line": min((row.line for row in records), default=None),
                      "last_branch_line": max((row.end_line for row in records), default=None)},
            "stats": coverage.function_stats(canonical),
            "lines": window["lines"],
            "branches": window["branches"],
            "truncated": window["end"] < last,
            "remaining_after": _line_range(window["end"] + 1, last),
        }
        results.append(entry)
    return {"functions": results, "max_lines": budget, "lines_returned": spent,
            "files": sorted({entry["file"] for entry in results
                             if entry.get("file")})}


def _read_log(path):
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError as error:
        raise StateError("not_found", f"cannot read {path}: {error}") from error


def tool_show_replay_logs(state, arguments):
    request = state.request(arguments["request_id"])
    channel = arguments.get("source") or "both"
    wanted = []
    if channel in ("stdout", "both"):
        wanted.append(("stdout", request.snapshot_dir / "sut.stdout.log"))
    if channel in ("stderr", "both"):
        wanted.append(("stderr", request.snapshot_dir / "sut.stderr.log"))
    max_lines = min(max(10, int(arguments.get("max_lines") or 200)), 500)
    case_id = arguments.get("case_id")
    merged = []
    cases = []
    for source, path in wanted:
        lines = _read_log(path)
        if case_id:
            span = _case_span(lines, str(case_id))
            if span is None:
                continue
            start, end = span
        else:
            start = max(0, int(arguments.get("start_line") or 1) - 1)
            end = len(lines)
        chunk = lines[start:min(end, start + max_lines)]
        for offset, text in enumerate(chunk):
            merged.append({"source": source, "line": start + offset + 1, "text": text})
        for case in _case_index(lines):
            if case["start"] < end and case["end"] > start:
                cases.append({**case, "source": source})
    if not merged:
        raise StateError("not_found", f"no replay log lines for case {case_id}")
    return {
        "snapshot": f"{request.snapshot:06}",
        "sources": [source for source, _ in wanted],
        "lines": merged[:max_lines],
        "truncated": len(merged) > max_lines,
        "cases": cases[:20],
        "case_count": len(cases),
    }


def _case_index(lines):
    """Locate case blocks by their MCN_REPLAY markers."""
    cases = []
    current = {}
    for index, text in enumerate(lines):
        if "MCN_REPLAY" not in text:
            continue
        try:
            payload = json.loads(text.split("MCN_REPLAY", 1)[1].split("===", 1)[0].strip())
        except (ValueError, IndexError):
            continue
        event = payload.get("event")
        case = payload.get("case_id")
        if event == "case_start" and case:
            current[case] = index
        elif event == "case_end" and case in current:
            cases.append({"case_id": case, "start": current.pop(case) + 1,
                          "end": index + 1, "outcome": payload.get("outcome")})
    for case, start in current.items():
        cases.append({"case_id": case, "start": start + 1, "end": len(lines), "outcome": None})
    return cases


def _case_span(lines, case_id):
    for case in _case_index(lines):
        if case["case_id"] == case_id:
            return case["start"] - 1, case["end"]
    return None


def tool_begin_analysis(state, arguments):
    return state.begin_analysis(arguments["request_id"])


def tool_select_analysis_target(state, arguments):
    return state.select_analysis_target(arguments["handler_id"], arguments["reason"])


def tool_get_analysis_target(state, arguments):
    return state.get_analysis_target()


def tool_switch_analysis_target(state, arguments):
    return state.switch_analysis_target(arguments["handler_id"], arguments["reason"])


def tool_submit_proposal(state, arguments):
    return state.submit_proposal(arguments["request_id"], arguments)
