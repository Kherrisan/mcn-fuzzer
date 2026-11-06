# Coverage analysis session

{{ execution.summary }}

SESSION={{ session_id }}
WORKSPACE={{ campaign.workspace }}
OUTPUT_DIR={{ campaign.workspace }}
SOURCE_DIR={{ source_dir }}
MODEL={{ model }}
SUT_PARTIES={{ sut_parties }}
WRITE ready.json

## Scope and handler definition

Identify handlers only for messages entering the SUT from outside its configured parties. SUT_PARTIES is the JSON array resolved from the campaign configuration, including any wildcard expansion. Use MODEL.steps: an inbound use has receiver_name in SUT_PARTIES and sender_name outside SUT_PARTIES. When a message has several uses, analyze only its inbound uses. Skip outgoing replies and traffic entirely within or outside the SUT; do not enumerate reply producers.

A handler is the conceptual entry function that dispatches, recognizes, parses, or implements the inbound message. Trace actual source branches and calls to establish the mapping. For a command, choose its command handler rather than all downstream workers. If a generic dispatcher implements the message directly, use that function and cite the relevant branch. Return multiple handlers only when the same message has genuinely independent handling entry points; do not list an entire call chain. Different messages may share an entry function when its source handles each message.

Do not map a specific encoded format to a function merely because opaque bytes pass through it. Receiving, sending, or saving arbitrary bytes does not establish handling of block, compressed, or other structured formats. If no source path recognizes or implements the modeled format, leave handlers empty and explain why. An uncertainty flag must not be used to justify such a fallback. Set uncertain to true only when source evidence supports a candidate entry but its precise role remains ambiguous.

## Source inspection workflow

1. Read MODEL.messages and MODEL.steps with a small script to list message names and classify their inbound uses using SUT_PARTIES. Do not analyze source handlers for messages with no inbound use.
2. Locate receive/parsing code and dispatch tables or branches with targeted searches such as rg. Read the relevant source ranges once and establish command mappings in a batch. Symbol names are navigation aids, not evidence; do not infer mappings from name similarity or a symbol-matching algorithm.
3. Follow dispatch calls only far enough to identify each conceptual entry. Inspect separate data-connection parsers or workers for inbound data messages, checking whether they actually implement the modeled format. Reuse shared dispatch findings instead of repeating the same investigation for each message.
4. For an unmatched inbound message, check the relevant dispatcher and format-handling path, then record the missing evidence. Do not force a mapping. During init, do not compute uncovered regions, dependencies, priorities, or refined models.
5. Construct handlers.json, then use a local script to check valid JSON, version/session, exactly one row per MODEL message, no duplicate symbols within a row, nonempty symbol/location/evidence for handlers, and a nonempty reason for every empty handlers array. Check that messages without inbound uses have empty handlers. Publish ready.json only after these checks pass.

## Output

Write OUTPUT_DIR/handlers.json using this schema:
{"version":1,"session":"SESSION above","mappings":[{"message":"exact message name from MODEL","handlers":[{"symbol":"exact LLVM/source function symbol","location":"source/path.c:line","evidence":"one short sentence citing the decisive dispatch branch or call site","uncertain":false}],"reason":null}]}

Include every MODEL message exactly once, including skipped messages. For messages without an inbound use, set handlers to [] and give a short reason such as "No inbound use to configured SUT parties." For unmatched inbound messages, use handlers: [] and a concise source-backed reason; this does not make the protocol message invalid.

Use source paths relative to SOURCE_DIR and the handler definition line in location. Keep evidence to one sentence of at most 240 characters, citing the decisive branch/call location; avoid repeated dispatch-table explanations, long excerpts, and whole call chains. Keep each reason to one short sentence.

The fuzzer validates and freezes this mapping for subsequent coverage snapshots. Write handlers.json atomically, then publish ready.json atomically as the last action, with {"session":"SESSION above","status":"ready"}. Do not print the complete JSON in the terminal.
SOURCE_DIR is only for this coverage-analysis agent.
Do not modify the fuzzer, the SUT, or the base protocol model.
Do not install packages.
