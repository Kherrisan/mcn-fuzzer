# Coverage analysis initialization

{{ execution.summary }}

SESSION={{ session_id }}
WORKSPACE={{ campaign.workspace }}
OUTPUT_DIR={{ campaign.workspace }}
SOURCE_DIR={{ source_dir }}
MODEL={{ model }}
CATALOG={{ catalog }}
HANDLERS={{ handlers_path }}
SUT_PARTIES={{ sut_parties }}
WRITE ready.json

## Scope and handler definition

Identify handlers only for messages entering the SUT from outside its configured
parties. SUT_PARTIES is the JSON array resolved from the campaign configuration.
An inbound use has receiver in SUT_PARTIES and sender outside SUT_PARTIES. When a
message has several uses, analyze only its inbound uses. Skip outgoing replies
and traffic entirely within or outside the SUT; do not enumerate reply producers.

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

## MCP service

A coverage-analysis MCP service is running for this session. Useful calls:

- `get_loaded_state` — report what is loaded. Empty at session start.
- `load_protocol` with `{"protocol_path": "MODEL", "catalog_path": "CATALOG"}`
  — load the model and the stable ID catalog.
- `get_protocol_steps` — list steps with canonical 8-character `step_id` values.

Call `get_loaded_state`, then `load_protocol`, then `get_protocol_steps` before
building the mapping. Do not load coverage data or open an analysis transaction
during initialization.

## Step table

STEPS is the authoritative ID table. Use its `step_id` values in step_ids; never
use the model's `source_id`, a step name, or a made-up id in that field.

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
5. Construct handlers.json, then check valid JSON, version/session, exactly one
   row per inbound message, no duplicate symbols within a row, nonempty
   symbol/location/evidence for handlers, and a nonempty reason for every empty
   handlers array. Publish ready.json only after these checks pass.

## Output

Write OUTPUT_DIR/handlers.json using this schema:

{"version":1,"session":"SESSION above","mappings":[{"message":"exact message name","handlers":[{"symbol":"exact source function symbol","location":"source/path.c:line","evidence":"one short sentence citing the decisive dispatch branch or call site","uncertain":false}],"step_ids":["8-character step ids from STEPS"],"reason":null}]}

Include each inbound message exactly once. For unmatched inbound messages use
handlers: [] and a concise source-backed reason; this does not make the protocol
message invalid. step_ids must list the catalog steps whose message matches the
row's message; an empty list is allowed only when the message has no inbound use.

Use source paths relative to SOURCE_DIR and the handler definition line in
location. Keep evidence to one sentence of at most 240 characters. Keep reasons
to one short sentence. Do not print the complete JSON in the terminal.

The fuzzer validates and freezes this mapping for subsequent coverage snapshots.
Write handlers.json atomically, then publish ready.json atomically as the last
action, with {"session":"SESSION above","status":"ready"}.

SOURCE_DIR is only for this coverage-analysis agent. Do not modify the fuzzer,
the SUT, or the base protocol model. Do not install packages.
