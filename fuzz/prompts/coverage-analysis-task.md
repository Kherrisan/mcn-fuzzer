# Coverage analysis request

REQUEST={{ request_id }}
SESSION={{ session_id }}
OUTPUT_DIR={{ output_dir }}
SNAPSHOT={{ snapshot }}
SNAPSHOT_DIR={{ snapshot_dir }}
BUILD={{ build }}
MANIFEST={{ manifest }}
CALLGRAPH_DOT={{ callgraph_dot }}
CFG_DIR={{ cfg_dir }}
BINARY={{ binary }}
MODEL={{ model }}
CATALOG={{ catalog }}
HANDLERS={{ handlers }}
PROTOCOL={{ protocol_id }}
SOURCE_DIR={{ source_dir }}

The fuzzer already replayed this frozen snapshot and exported coverage JSON. Do
not re-run coverage, call a measurement command, or open the full coverage JSON
or CFG files directly. Use the MCP tools; they parse and page the data.

## Load the request

A coverage-analysis MCP service is running. Start with `get_loaded_state`, then
load exactly this request's inputs:

1. `load_protocol` with `{"protocol_path": "MODEL", "catalog_path": "CATALOG"}`.
2. `load_handlers` with `{"handlers_path": "HANDLERS"}`.
3. `load_cfg` with `{"cfg_dir": "CFG_DIR", "manifest_path": "MANIFEST"}`.
4. `load_callgraph` with `{"callgraph_dot": "CALLGRAPH_DOT", "manifest_path": "MANIFEST"}`.
5. `load_profdata` with `{"request_id": "REQUEST", "snapshot_dir": "SNAPSHOT_DIR", "manifest_path": "MANIFEST"}`.
6. `begin_analysis` with `{"request_id": "REQUEST"}`.

If a tool returns `not_loaded`, load the missing dataset and retry. If it returns
`incompatible_data`, stop and write an error result; do not mix builds.
Pass `request_id` to every query tool after `load_profdata` so the request keeps
reading the snapshot version it registered, even if another load happens later.

## Explore

Budget matters: inspect summaries first and drill down only where the evidence
justifies it.

1. `show_overview_code_coverage` — project and module branch coverage.
2. `show_handler_code_coverage` — handler own and cumulative reachable coverage,
   sorted by missed branches. Prefer handlers that are not yet exercised and have
   a larger reachable missed area; there is no fixed percentage threshold.
3. `show_func_code_coverage` and `get_reachable_functions` — locate heavy missed
   callees behind a handler.
4. `show_source_coverage` — read a bounded source slice with true/false counts
   for the conditions that block progress.
5. `get_reaching_handlers` and `get_step_dependencies` — find which messages can
   reach a function and which prerequisites the model already knows.
6. `get_protocol_steps` — obtain canonical 8-character step IDs. Only these IDs
   are accepted in the proposal; do not invent IDs or use step names.

Use protocol dependencies, sender/receiver roles, and source conditions to build
one coherent trace: the goal handler's own inbound step(s) plus the smallest set
of prerequisite and reply steps that make the interaction meaningful. A step may
carry several handlers; a prefix, environment, or reply step may carry none but
must have a reason. Do not add a dependency that the evidence does not support.

Optional graph edits in this transaction (they are committed only if Rust accepts
the proposal):

- `update_step_annotations` — record a condition or constraint on a step.
- `upsert_dependency` / `remove_dependency` — fix a dependency the analysis
  established. Do not turn an uncertain condition into a required edge.

## Propose

Call `submit_proposal` with:

{"request_id": "REQUEST",
 "origin": "coverage",
 "goal": {"handler_ids": ["handler id from show_handler_code_coverage"],
          "reason": "which handler/internal path to reach and the coverage evidence"},
 "trace": [{"step_id": "8-char id from get_protocol_steps",
            "handler_ids": ["handler ids associated with this step's message"],
            "reason": "target exploration or prefix/reply role",
            "constraints": ["field/state conditions"]}]}

The service validates step IDs, handler associations, trace identity, and freezes
proposal.json. `submit_proposal` returns the proposal_id and trace_id; use those
exact values below. If no defensible proposal exists, do not submit one.

## Result

Write OUTPUT_DIR/result.json atomically. Identity fields must match the request
exactly; `proposal_id` and `trace_id` are required for status "ok".

Success:
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","protocol_id":"PROTOCOL","proposal_id":"...","trace_id":"...","status":"ok"}

No proposal or failure:
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","protocol_id":"PROTOCOL","status":"no_proposal","error":"source-backed reason"}
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","protocol_id":"PROTOCOL","status":"error","error":"specific reason"}

Publish result.json only after submit_proposal succeeds (status ok) or after
deciding there is no proposal. That publication ends the task. Do not edit
proposal.json after submission, and do not print full coverage or graph data.
