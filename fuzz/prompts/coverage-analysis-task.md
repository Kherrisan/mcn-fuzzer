# Coverage analysis request

REQUEST={{ request_id }}
SESSION={{ session_id }}
OUTPUT_DIR={{ output_dir }}
SNAPSHOT={{ snapshot }}
SNAPSHOT_DIR={{ snapshot_dir }}
BUILD={{ build }}
CALLGRAPH_DOT={{ callgraph_dot }}
CFG_DIR={{ cfg_dir }}
BINARY={{ binary }}
MODEL={{ model }}
CATALOG={{ catalog }}
HANDLERS={{ handlers }}
SOURCE_DIR={{ source_dir }}
SUT_STDOUT_LOG={{ sut_stdout_log }}
SUT_STDERR_LOG={{ sut_stderr_log }}
CONFIGURE_OPTIONS={{ configure_options }}
CLI_ARGS={{ cli_args }}

The fuzzer already replayed this frozen snapshot. The MCP service exports
the detailed counters from the stored profile when needed. Do not re-run
coverage, call a measurement command, or open coverage JSON or CFG files
directly. Use the MCP tools; they parse and page the data.

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

## Build configuration and enabled features

CONFIGURE_OPTIONS lists the build-time switches used for this SUT; CLI_ARGS
lists the runtime command-line flags. Use them together with other build
material (configdata.pm, CMakeCache.txt, generated headers, build logs,
linked libraries) to decide which protocol features are actually compiled in
and enabled. Do not propose goals for features that are disabled or were not
built. Prioritize enabled features: inspect their handlers, branches, and
dependencies thoroughly, and make them the primary targets of proposals and
trace selection.

## Load the request

A coverage-analysis MCP service is running. Start with `get_loaded_state`, then
load exactly this request's inputs:

1. `load_protocol` with `{"protocol_path": "MODEL", "catalog_path": "CATALOG"}`.
2. `load_handlers` with `{"handlers_path": "HANDLERS"}`.
3. `load_cfg` with `{"cfg_dir": "CFG_DIR"}`.
4. `load_callgraph` with `{"callgraph_dot": "CALLGRAPH_DOT"}`.
5. `load_coverage_json` with `{"request_id": "REQUEST", "snapshot_dir": "SNAPSHOT_DIR"}`.
6. `begin_analysis` with `{"request_id": "REQUEST"}`.

If a tool returns `not_loaded`, load the missing dataset and retry. If it returns
`incompatible_data`, stop and write an error result; do not mix builds.
Pass `request_id` to every query tool after `load_coverage_json` so the request keeps
reading the snapshot it registered, even if another load happens later.

## Previously submitted proposals

Earlier requests in this analysis directory each contain a frozen
`proposal.json` and a completion `result.json`. Before choosing a goal, inspect
them with the file tools; do not open the full coverage JSON or CFG files.

```text
rg --files {{ output_dir }}/..
cat {{ output_dir }}/../*/proposal.json
cat {{ output_dir }}/../*/result.json
```

For every earlier request, extract the goal handler ids, the ordered model-step
sequence, the concrete `fields`, and the `mutations`. Treat a proposal as a
duplicate when the goal handler set, the ordered model-step sequence, and those
concrete values or mutations match. Renaming instances, changing comments, or
reordering equivalent steps does not make it different.

Do not submit a duplicate or substantially similar proposal. If an earlier
proposal was submitted (`result.json` status `ok`) but the current snapshot
shows its target handler still at the same missed branches or reached
functions, you may reuse that trace only when you change a concrete detail that
can alter the executed path, for example:

- a field value, length, encoding, or boundary that was previously null,
  omitted, or left at the model default;
- the order of a prerequisite step, or an added or removed condition;
- a dependency or state key that changes a derived field;
- an additional instance of a prerequisite step, or a different reply or data-path step.

In `goal.reason`, name the earlier request id and state the exact changed
detail and why it should reach different coverage. Do not merely rename
instances or rewrite the rationale. If no concrete change is defensible, return
`status: no_proposal` citing the earlier request id instead of resubmitting.

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

Before `submit_proposal`, compare the goal, trace, and concrete fields with the
earlier proposals as described above. If you reuse an earlier trace,
`goal.reason` must name that request id and the changed detail.

Call `submit_proposal` with parties, channels, an ordered trace of step
instances, mutations, and optional `dependencies`. Instance names are semantic and stay stable if you reorder
the trace. `step` is the model step name. Channel endpoints are party instance
names. Repeat a model step by adding another instance with its own name. Do not
put handler ids on a trace item. Dependencies use instance names and may carry
state keys in `fields`, as an array of non-empty strings. A producer saves each
key with `ctx.save_state(key, value)`; its dependent reads that same key with
`ctx.load_depending_state(key)`. For a model field relation with different source
and destination field names, use the source field name as the state key and let
the handler populate the destination field using the model relation's `derive`
when specified. A data dependency needs at least one
key; do not bind the same key to multiple producers for one dependent instance.
{"origin": "coverage",
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
 "mutations": [
   {"op": "field", "steps": ["login_first"], "field": "username",
    "kind": "string",
    "suggestion": "explore empty and length boundaries", "evidence": "src/auth.c:120"},
   {"op": "field", "steps": ["login_first"], "field": "body",
    "kind": "bytes", "encoding": "hex",
    "suggestion": "mutate the encoded payload nibbles", "evidence": "src/auth.c:140"}]}

`steps` is either an array of instance names or `"[start:end]"`, including both
ends. Whitespace around the colon is allowed. Only `op: "field"` mutations are
supported. A field mutation applies to the selected send instances and may name
a field the model does not list; give the field name, a suggestion, and a source
location. Declare the same field at most once per instance. Instance names must
not contain `:[]`.

Each field mutation may declare `kind` and, for bytes only, `encoding`:

- `string` (default): free-form text.
- `bytes`: an encoded byte string; `encoding` is `hex` (default) or `base64`.
  Use this for protocol fields that are raw bytes.
- `integer`: signed 64-bit integer.
- `boolean`: true/false flag.
- `list`: array value.

A `default` value may accompany the mutation. Rust passes kind and encoding to
synthesis so the generator validates the value and mutation preserves the
declared representation.

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
