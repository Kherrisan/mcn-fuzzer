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

SOURCE_DIR is the source tree used by the llvm-cov build, also used during
initialization. Read source evidence from that tree or the exact compiled paths
returned by coverage tools. Do not substitute an upstream source copy. Report
missing compiled sources or a source-version mismatch instead of mixing versions.

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
and enabled. Consider only handlers for features that are compiled in and
enabled when choosing the primary target.

## Registered proposals

The campaign-introspection MCP service is read-only. Start this request by
calling `list_proposals`. Then call `get_proposal` with `summary_only: true` for the execution feedback
you need. Request the full proposal when you need to compare its trace.
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

## Step and dependency execution

Three read-only tools describe how the protocol steps actually ran. Counts are
live and campaign-wide; coverage replay is excluded.

- `list_step_stats` returns each model step with `present_cnt` (instances in
  published generator traces), `exec_cnt` (instances in ordinary execs,
  including instances never reached after an earlier failure), and `succ_cnt`
  (handlers that returned normally). Filter with `parties` and `channels`;
  values inside one filter are a union and different filters intersect.
- `list_step_dependency_stats` returns directed model dependencies with the
  same counts. A dependency matches when either endpoint satisfies the filters.
- `get_step_dependency_stats` reads incoming and/or outgoing dependencies for
  given steps, deduplicated, with `direction` labelled. Its `steps` array accepts
  only model step names, never trace instance names: statistics are aggregated by
  model step, so an instance name returns no rows.

Use them to separate three cases before proposing a trace: a step that is never
present in published generators, a step that is present and included in execs
but never succeeds (`succ_cnt` stays 0), and a dependency whose endpoints
succeed in the wrong order. `unobservable_runs` counts execs whose generator has
no step handlers or reports its own internal step names, so their success state
is unknown rather than failed; legacy params-only generators stay in that bucket
by design and are not attributed to model steps.

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

Treat two proposals as the same exploration when the primary handler, the
ordered model-step sequence, and the concrete mutable fields match.
Renaming instances, changing comments, or reordering equivalent steps does not
make a proposal different.

## Load the request

After `list_proposals`, call `get_loaded_state`. Initialization already wrote
and loaded the handler mapping with `write_handlers`, along with `MODEL`.
Load only static datasets marked unloaded, using `MODEL` for `load_protocol`,
`CFG_DIR` for `load_cfg`, and `CALLGRAPH_DOT` for `load_callgraph`. Never call
`load_handlers`: the mapping is already loaded, and a request whose registered
file is byte-identical resolves to it.

For each request, call `load_coverage_json` with `request_id` and `snapshot_dir`,
then call `begin_analysis` with `request_id` to open a transaction based on the
latest committed graph.

If a tool returns `not_loaded`, load that dataset and retry, except for handlers:
that error means the service and the campaign disagree about the mapping, so
write an error result instead of calling `load_handlers`. If a tool returns
`incompatible_data`, stop and write an error result; do not mix builds.
Pass `request_id` to snapshot queries that accept it so the request keeps reading
its registered snapshot. The target tools use the request opened by `begin_analysis`.

## Select the primary target

After `begin_analysis`, use `show_overview_code_coverage` and
`show_handler_code_coverage` to review project, module, and handler branch
coverage. Compare the summaries with registered proposals and the known build
and runtime configuration. Prefer a feasible handler with a large reachable
missed area, especially if it has yet to execute.

Call `select_analysis_target` with one `handler_id` and a `reason` before
reading source, message fields, or detailed dependencies. Use
`get_analysis_target` to recover the selection when reopening a request. If you
find a concrete blocker, call `switch_analysis_target` with a different
`handler_id` and use `reason` to explain the evidence for the new choice.
Follow the `no_proposal` flow if no handler is feasible.

## Explore the selected handler

Call `describe_handler` without arguments to inspect the selected handler's
mapping, coverage, associated model steps and compiled source window. The tool
uses the request bound by `begin_analysis`; it does not take a request id.
Page the mappings when needed. A source error reports the missing evidence.

Choose the model steps for the target and its prerequisites, then call
`describe_trace_steps` with their exact names in the `steps` array. It uses
the current transaction and returns the steps, distinct message fields, and
their mutual dependencies. Page the step list if needed. Repeat the call when
you add steps. Page dependencies separately with `dependency_offset`.
Confirm the trace ordering and whether additional predecessors are necessary.

For remaining gaps, use `show_function_source`, `show_source_coverage`,
`get_reachable_functions`, `get_reaching_handlers`, `show_step`,
`get_step_dependencies`, `show_message`, or `show_replay_logs` as needed.
Read every function you still need in one `show_function_source` call with
`functions: [...]`, `whole: true`, and a `max_lines` budget; fall back to a
single `function` with `around_line` only for one specific branch. Do not read
the same function twice through overlapping windows.
Pass `request_id` to these tools when their schemas accept it. Prefer targeted
queries over rereading the same source window or step.

Do not read `protocol.json` with scripts or file tools, do not enumerate the
tool catalogue, and do not dump unbounded output: pass `offset`/`limit` to the
paged tools and keep any single tool result under roughly 8k characters.

Use protocol dependencies, sender/receiver roles, and source conditions to build
one coherent trace: the goal handler's own inbound step(s) plus the smallest set
of prerequisite and reply steps that make the interaction meaningful. Include
steps that reach other handlers when they are necessary prerequisites. The service
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
registered proposals. Confirm the prerequisites, dependencies, and key fields.
Set `goal.handler_ids` to an array containing only the selected handler ID.
If you reuse an earlier trace because of a concrete
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
 "goal": {"handler_ids": ["selected primary handler ID"],
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
