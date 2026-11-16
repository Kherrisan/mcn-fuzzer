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
HANDLERS={{ handlers }}
SUT_PARTIES={{ sut_parties }}
GENERATOR_PARTIES={{ generator_parties }}
UNAVAILABLE_PARTIES={{ unavailable_parties }}
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

## Registered proposals

The campaign-introspection MCP service is read-only. Start this request by
calling `list_proposals`. Then call `get_proposal` for the rows you need.
Read `generator.py` and the params file only when `get_proposal` returns those
paths, and use the file tools on bounded ranges. Use `SUT_STDOUT_LOG` and
`SUT_STDERR_LOG` the same way. Do not scan the analysis directory for historical
`proposal.json` or `result.json` files, and do not modify an older proposal,
generator, or params file.

Introspection counts are live and can lag this frozen snapshot. Decide coverage
from the loaded snapshot and the replay logs. Use introspection for proposal
status and how the generator behaved.

`feedback_novelty_indices` lists bitmap indexes whose hit-count bucket changed,
including the first hit. That is not a set of newly covered source lines.
`resolve_bitmap_indices` locates a feedback change. `column` is always null.
Use `show_source_coverage` when you need source-line counts.

A proposal with no corpus contribution is not automatically an error. Distinguish:

- not yet run: status is `proposing`, `queued`, or `synthesizing`, or `executions.started` is 0
- insufficient budget: the generator ran, but few changed candidates were evaluated
- exploration saturation: executions completed, novelty stopped growing, and neither the generator status nor the replay log shows a concrete fault
- concrete implementation error: `last_error`, `generator_statuses`, `generator_exits`, or the replay log shows a crash, exception, rejected message, or other specific fault

Do not submit another proposal for the same exploration while an existing
proposal is `queued` or `synthesizing`.

Correct a generator only by submitting a new proposal. In `goal.reason`, cite
the old proposal id, the evidence, and the concrete correction. Reuse an
earlier trace only when the evidence is a concrete implementation error. If
the earlier exploration is saturated or still running, pick a different goal
or return `status: no_proposal` and cite the proposal id.

Treat two proposals as the same exploration when the goal handler set, the
ordered model-step sequence, and the concrete mutable fields match.
Renaming instances, changing comments, or reordering equivalent steps does not
make a proposal different.

## Load the request

A coverage-analysis MCP service is also running. After `list_proposals`, call
`get_loaded_state`, then load exactly this request's inputs:

1. `load_protocol` with `{"protocol_path": "MODEL"}`.
2. `load_handlers` with `{"handlers_path": "HANDLERS"}`.
3. `load_cfg` with `{"cfg_dir": "CFG_DIR"}`.
4. `load_callgraph` with `{"callgraph_dot": "CALLGRAPH_DOT"}`.
5. `load_coverage_json` with `{"request_id": "REQUEST", "snapshot_dir": "SNAPSHOT_DIR"}`.
6. `begin_analysis` with `{"request_id": "REQUEST"}`.

If a tool returns `not_loaded`, load the missing dataset and retry. If it returns
`incompatible_data`, stop and write an error result; do not mix builds.
Pass `request_id` to every query tool after `load_coverage_json` so the request keeps
reading the snapshot it registered, even if another load happens later.

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
registered proposals. If you reuse an earlier trace because of a concrete
implementation error, `goal.reason` must name that proposal id, the evidence,
and the correction.

The proposal trace is in model perspective. Use each model step's
`send`/`listen`/`connect`/`close` action and its `sender`/`receiver` fields to
express direction; do not add a `recv` action. Rust projects the accepted trace
to the generator perspective during synthesis, turning SUT-sent messages into
recv steps and dropping SUT setup steps.

Instance `name` values are trace-local labels. If a name contains a verb, use
the generator's perspective: `send_*` when the generator sends, `recv_*` when
the generator reads a SUT message, and `connect_*`/`listen_*`/`close_*` for
generator network actions. Do not name a SUT-sent model step from the SUT's
view (for example `server_sends_hello` or `server_listen`); name it
`recv_server_hello` instead. `step` still carries the exact model step name and
`action` still carries the model action; only the instance name expresses the
generator-view verb.

Call `submit_proposal` with parties, channels, an ordered trace of step
instances, and optional `dependencies`. Instance names are semantic and stay
stable if you reorder the trace. `step` is the model step name. Channel
endpoints are party instance names. Repeat a model step by adding another
instance with its own name. Do not put handler ids on a trace item. Dependencies
use instance names and may carry state keys in `fields`, as an array of non-empty
strings. A producer saves each key with `ctx.save_state(key, value)`; its
dependent reads that same key with `ctx.load_depending_state(key)`. For a model
field relation with different source and destination field names, use the source
field name as the state key and let the handler populate the destination field
using the model relation's `derive` when specified. A data dependency needs at
least one key; do not bind the same key to multiple producers for one dependent
instance.

Declare the mutable fields on each generator-filled send instance itself:

~~~json
{"origin": "coverage",
 "goal": {"handler_ids": ["handler name from show_handler_code_coverage"],
          "reason": "which handler/internal path to reach and the coverage evidence"},
 "parties": {"client_1": "client", "server_1": "server"},
 "channels": {"control_1": {"channel": "control", "endpoints": ["client_1", "server_1"]}},
 "trace": [{"name": "login_first", "step": "exact step name", "channel": "control_1",
            "sender": "client_1", "receiver": "server_1",
            "reason": "target exploration or prefix/reply role",
            "constraints": ["field/state conditions"],
            "mutable_fields": [
              {"field": "username", "kind": "string",
               "suggestion": "explore empty and length boundaries",
               "evidence": "src/auth.c:120"},
              {"field": "body", "kind": "bytes", "encoding": "hex",
               "suggestion": "mutate the encoded payload nibbles",
               "evidence": "src/auth.c:140"}]}],
 "dependencies": [
   {"prior": "greet", "dependent": "login_first", "kind": "order", "condition": "",
    "fields": []}]}
~~~

Rules for `mutable_fields`:

- They are allowed only on `send` instances whose model sender is a generator
  party. Leave `mutable_fields` out, or empty, on SUT-sent steps and on
  `listen`/`connect`/`close` instances.
- A field need not appear in the model. Give the field name, a `suggestion`, and
  a source `evidence` location.
- Name the same field at most once per instance. Instance names must not
  contain `:[]`.
- Each field may declare `kind` and, for bytes only, `encoding`:

  - `string` (default): free-form text.
  - `bytes`: an encoded byte string; `encoding` is `hex` (default) or `base64`.
    Use this for protocol fields that are raw bytes.
  - `integer`: signed 64-bit integer.
  - `boolean`: true/false flag.
  - `list`: array value; items may be scalars, lists, or objects.
  - `map`: object value whose member names are protocol structure. Mutation
    rewrites member values recursively and never adds or removes a member, so
    declare every member the generator must keep.
- A `default` may accompany the field. It must match the declared `kind` and be
  representable as a string, signed 64-bit integer, boolean, list, or object.

The service derives nothing from these fields: it validates each one, copies it
into the frozen proposal, and hands it to synthesis unchanged.

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
