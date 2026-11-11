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
SOURCE_DIR={{ source_dir }}
SUT_STDOUT_LOG={{ sut_stdout_log }}
SUT_STDERR_LOG={{ sut_stderr_log }}

The fuzzer already replayed this frozen snapshot and exported coverage JSON. Do
not re-run coverage, call a measurement command, or open the full coverage JSON
or CFG files directly. Use the MCP tools; they parse and page the data.

## SUT replay output

SUT_STDOUT_LOG and SUT_STDERR_LOG point at the frozen snapshot's cumulative
stdout and stderr logs from the coverage replay runs up to this snapshot. They
are required for this snapshot and are always present for snapshots published
by this fuzzer.
Each case is delimited by single-line markers:

`=== MCN_REPLAY {"event":"case_start",...} ===` and the matching `case_end`
with `case_id` and `outcome`; `batch_start`/`batch_end` delimit replay attempts.
Output from failed attempts remains in the cumulative file, so a case marker
without a corresponding snapshot entry can still explain a failed replay.

Use the file tools to search for errors, case IDs, or protocol keywords and read
bounded ranges around them. Do not dump the whole cumulative log into the
conversation. Absence of output does not prove that a handler or path did not
execute; combine the log with coverage, source, handlers, and protocol
dependencies.

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
6. `get_protocol_steps` — obtain exact step names. Only these names are accepted
   in the proposal; do not invent names or use any digest.

Use protocol dependencies, sender/receiver roles, and source conditions to build
one coherent trace: the goal handler's own inbound step(s) plus the smallest set
of prerequisite and reply steps that make the interaction meaningful. The service
derives message-to-handler associations from the loaded handler mapping; do not
include handler IDs on trace steps. A prefix, environment, or reply step must
still have a reason. Do not add a dependency that the evidence does not support.

Optional graph edits in this transaction (they are committed only if Rust accepts
the proposal):

- `update_step_annotations` — record a condition or constraint on a step.
- `upsert_dependency` / `remove_dependency` — fix a dependency the analysis
  established. Do not turn an uncertain condition into a required edge.

## Propose

Call `submit_proposal` with parties, channels, an ordered trace of step
instances, mutations, and optional `dependencies` and `global_context`. Instance names are semantic and stay stable if you reorder
the trace. `step` is the model step name. Channel endpoints are party instance
names. Repeat a model step by adding another instance with its own name. Do not
put handler ids on a trace item. Dependencies use instance names and may carry
field bindings; global_context is for constants, environment inputs, and
generated values not owned by a step.

{"request_id": "REQUEST",
 "origin": "coverage",
 "goal": {"handler_ids": ["handler name from show_handler_code_coverage"],
          "reason": "which handler/internal path to reach and the coverage evidence"},
 "parties": {"client_1": "client", "server_1": "server"},
 "channels": {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}},
 "trace": [{"name": "login_first", "step": "exact step name", "channel": "control_1",
            "sender": "client_1", "receiver": "server_1",
            "reason": "target exploration or prefix/reply role",
            "constraints": ["field/state conditions"]}],
 "dependencies": [
   {"prior": "greet", "dependent": "login_first", "kind": "order", "condition": "",
    "fields": []}],
 "global_context": [
   {"name": "username", "kind": "string", "value": "ubuntu",
    "source": "constant"}],
 "mutations": [
   {"op": "field", "steps": ["login_first"], "field": "username",
    "suggestion": "explore empty and length boundaries", "evidence": "src/auth.c:120"},
   {"op": "duplicate", "steps": "[login_first:login_first]", "count_param": "login_repeats"}]}

`steps` is either an array of instance names or `"[start:end]"`, including both
ends. Whitespace around the colon is allowed. A field mutation applies to the
selected send instances and may name a field the model does not list; give the
field name, a suggestion, and a source location. A duplicate array repeats each
selected instance in place. A duplicate range repeats that whole block after
the original range. `count_param` is the extra-copy count. Do not overlap
duplicate selections or declare the same field twice on one instance. Field and
duplicate may name the same instance. Instance names must not contain `:[]`.

The service fills action, condition, and message, checks endpoints and campaign
party binding, and freezes proposal.json for this request. If no defensible
proposal exists, do not submit one.

## Result

Write OUTPUT_DIR/result.json atomically. Identity fields must match the request
exactly. Do not include a protocol, proposal, or trace digest.

Success:
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","status":"ok"}

No proposal or failure:
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","status":"no_proposal","error":"source-backed reason"}
{"request_id":"REQUEST","session":"SESSION","build":"BUILD","snapshot":"SNAPSHOT","status":"error","error":"specific reason"}

Publish result.json only after submit_proposal succeeds (status ok) or after
deciding there is no proposal. That publication ends the task. Do not edit
proposal.json after submission, and do not print full coverage or graph data.
