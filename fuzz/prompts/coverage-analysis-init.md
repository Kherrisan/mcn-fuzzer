# Coverage analysis initialization

{{ execution.summary }}

SESSION={{ session_id }}
WORKSPACE={{ campaign.workspace }}
OUTPUT_DIR={{ campaign.workspace }}
SOURCE_DIR={{ source_dir }}
MODEL={{ model }}
HANDLERS={{ handlers_path }}
SUT_PARTIES={{ sut_parties }}
GENERATOR_PARTIES={{ generator_parties }}
UNAVAILABLE_PARTIES={{ unavailable_parties }}
CONFIGURE_OPTIONS={{ configure_options }}
CLI_ARGS={{ cli_args }}
WRITE ready.json only after all initialization work below is complete.

SOURCE_DIR is the source tree used to build the llvm-cov binary. Use this tree
for handler discovery and source evidence, including the build's applied patches.
Do not substitute an upstream checkout or another build's source copy. Coverage
queries use the compiled source paths recorded by llvm-cov. If this tree is
missing or inconsistent with those paths, report the mismatch instead of mixing
source versions.

## Build configuration and enabled features

CONFIGURE_OPTIONS lists the build-time switches used for this SUT; CLI_ARGS
lists the runtime command-line flags. Use them together with other build
material (configdata.pm, CMakeCache.txt, generated headers, build logs,
linked libraries) to decide which protocol features are actually compiled in
and enabled. Do not propose goals for features that are disabled or were not
built. Prioritize enabled features: inspect their handlers, branches, and
dependencies thoroughly, and make them the primary targets of proposals and
trace selection.

## Scope and handler definition

Identify handlers only for messages entering the SUT from outside its configured
parties. SUT_PARTIES is the JSON array resolved from the campaign configuration.
An inbound use has receiver in SUT_PARTIES and sender outside SUT_PARTIES. When a
message has several uses, analyze only its inbound uses. Skip outgoing replies
and traffic entirely within or outside the SUT; do not enumerate reply producers.

GENERATOR_PARTIES lists the parties implemented by the generator;
UNAVAILABLE_PARTIES cannot be carried by this campaign. Proposal traces stay in
the model perspective: keep each step's model `send`/`listen`/`connect`/`close`
action, and use `sender`/`receiver` to show direction. Do not invent a recv
action. When an instance name contains a verb, name it from the generator's
view: `send_*` for generator sends, `recv_*` for SUT messages the generator
reads, and `connect_*`/`listen_*`/`close_*` for generator network actions. The
`step` field remains the exact model step name. Rust projects the accepted
proposal to the generator perspective before generator synthesis.

The model may also declare top-level `states` and per-step `processing`
(`before_send`, `after_send`, `on_receive`, each a list of `reads`/`writes`/`rule`).
These describe local state transitions, not wire fields. Proposal trace instances
carry the model step's `processing` unchanged; do not rewrite or invent rules.

A handler is the conceptual entry function that dispatches, recognizes, parses,
or implements the inbound message. Trace actual source branches and calls to
establish the mapping. For a command, choose its command handler rather than all
downstream workers. If a generic dispatcher implements the message directly, use
that function and cite the relevant branch. Return multiple handlers only when
the same message has genuinely independent handling entry points; do not list an
entire call chain. Different messages may share an entry function.

Do not map a specific encoded format to a function merely because opaque bytes
pass through it. Receiving, sending, or saving arbitrary bytes does not establish
handling of block, compressed, or other structured formats. If no source path
recognizes or implements the modeled format, leave handlers empty and explain
why. Do not use `uncertain` to justify such a fallback. Set uncertain to true only
when source evidence supports a candidate entry but its precise role remains
ambiguous.

## MCP services

Two MCP services are running for this session.

The coverage-analysis service answers protocol and handler queries. Useful calls:

- `get_loaded_state` — report what is loaded. Empty at session start.
- `load_protocol` with `{"protocol_path": "MODEL"}`
  — load the protocol model once if it is unloaded.
- `get_protocol_steps` — list steps by their unique `name`.
- `list_parties` — list party descriptions, aliases, and current campaign bindings.
- `list_messages` / `show_message` — page messages with their direction and
  carrying steps; read one message's fields.
- `show_step` — read one step's endpoints, `processing`, and direct
  predecessors.

Do not parse `protocol.json` with scripts or file tools and do not enumerate
the tool catalogue; use these calls and keep results bounded.

The campaign-introspection service is read-only and separate. For proposal history, use
`list_proposals`, `get_proposal_stats`, and `resolve_bitmap_indices`. Counts are live
campaign statistics, not a frozen coverage snapshot. `list_proposals` pages
registered proposals. `get_proposal_stats` returns one proposal, its generator paths,
and paged feedback indexes. `resolve_bitmap_indices` maps those indexes to a
source file, line, and function. `column` is always null. A resolved location
is the feedback site, not new source-line coverage.
Use `get_proposal_generator_stats` with the proposal ID for live step and dependency counts specific to its published generator.

Call `get_loaded_state`, load the protocol model if needed, then call
`get_protocol_steps` and `list_proposals` before building the mapping. `list_proposals` may be empty;
that call only warms the introspection service. Do not load coverage data or
open an analysis transaction during initialization.

## Step table

STEPS is the authoritative name table. Use its `name` values in step_names.
Do not invent names or write a digest.

STEPS={{ steps }}

## Source inspection workflow

1. Read MODEL.messages and STEPS, and classify inbound uses with SUT_PARTIES.
   Do not analyze source handlers for messages with no inbound use.
2. Locate receive/parsing code and dispatch tables or branches with targeted
   searches such as rg. Read the relevant source ranges once and establish
   mappings in a batch. Symbol names are navigation aids, not evidence; do not
   infer a mapping from name similarity.
3. Follow dispatch calls only far enough to identify each conceptual entry.
   Inspect separate data-connection parsers or workers for inbound data messages,
   checking whether they actually implement the modeled format. Reuse shared
   dispatch findings instead of repeating the same investigation per message.
4. For an unmatched inbound message, check the dispatcher and format-handling
   path, then record the missing evidence. Do not force a mapping. Do not compute
   uncovered regions, dependencies, priorities, or refined models here.
5. Prepare the mapping for the `write_handlers` tool. Check the session, exactly
   one row per inbound message, no duplicate symbols within a row, nonempty
   symbol/location/evidence for handlers, and a nonempty reason for every empty
   handlers array. For every row, call `show_message` for that exact message
   name and copy `step_names` only from the returned `steps`. Never merge steps
   whose message names differ even when one name is a prefix of another (for
   example `CertificateVerify` versus `TLS 1.2 CertificateVerify`); the fuzzer
   rejects a row whose step belongs to a different message.

## Output

Write OUTPUT_DIR/handlers.json using this schema:

{"session":"SESSION above","mappings":[{"message":"exact message name","handlers":[{"symbol":"exact source function symbol","location":"source/path.c:line","evidence":"one short sentence citing the decisive dispatch branch or call site","uncertain":false}],"step_names":["exact step names from STEPS"],"reason":null}]}

Include each inbound message exactly once. For unmatched inbound messages use
handlers: [] and a concise source-backed reason; this does not make the protocol
message invalid. step_names must list the model steps whose message matches the
row's message; an empty list is allowed only when the message has no inbound use.
A handler is named by its function symbol. When two handlers share a symbol, the
service qualifies the later name as `relative/source/path.c::symbol`.

Use source paths relative to SOURCE_DIR and the handler definition line in
location. Keep evidence to one sentence of at most 240 characters. Keep reasons
to one short sentence. Do not print the complete JSON in the terminal.

The fuzzer validates and freezes this mapping for subsequent coverage snapshots.
Write handlers.json with the `write_handlers` tool: pass `output_dir`,
`session`, and `mappings`, and let the service serialise, re-read, and validate
the file against the loaded model. If the tool rejects a row, correct the
mapping and call it again. Complete all source investigation, mapping
corrections, and artifact checks before publishing readiness.

SOURCE_DIR is only for this coverage-analysis agent. Do not modify the fuzzer,
the SUT, or the base protocol model. Do not install packages.

As your final tool call, write one JSON object to a temporary file in OUTPUT_DIR
and atomically rename it to ready.json. Use
{"session":"SESSION above","status":"ready"}. Immediately send a brief final
response and end the turn. After publishing ready.json, make no further tool
calls, checks, or changes to the mapping.
