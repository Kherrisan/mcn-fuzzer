# Iteration log

## Cleanup after the stopped optimization goal (2026-11-10)

- Iteration 23 was stopped at the user's request, not a completed benchmark.
- Removed the source-plan/report reuse path, background reviewer preparation,
  shared source requirements and construction-window feedback. Their configuration,
  tool arguments, scheduling branches, prompts and feature-specific tests are removed.
- Kept all 18 MCP tools, atomic batch reports, source receipts, canonical evidence,
  schema/reference checks, full model/diff reading and independent resolution decisions.
- Kept historical delivery auditing in the offline benchmark script. New runs use
  the remaining workflow settings; old trials retain their frozen packages/configs.
- Validation: 202 tests and 78 subtests passed. No new live RFC benchmark was started;
  this cleanup establishes no end-to-end performance improvement.

## Initial diagnostic run: iteration-01

- Runner: `rsc/scripts/benchmark_ftp_runtime.py`.
- Artifacts: ignored local directory `rsc/runs/runtime-stability/iteration-01`.
- Container: `rsc-ftp-runtime-iteration-01-1`; live RSC started 2026-11-09 16:17:55 UTC.
- Both inputs match the archived baseline RFC SHA256 values exactly. This runner fixes
  processing order to base RFC 959 followed by extensions RFC 3659; old baselines supplied
  RFC 3659 first. Performance comparisons therefore include this deliberate source-order
  choice and cannot isolate it from prompt/tool changes.
- The package snapshot is immutable within the running container. Local follow-up edits
  are not injected into it and reset the future consecutive-run count.
- At 3 minutes, the parser had completed 4/30 batches and saved 19 records. One 19-record
  atomic mutation initially failed because an unresolved object contained an invalid `name`.
  Its next corrected submission succeeded. The old union-schema error was unhelpfully broad.
- Follow-up implementation now emits the selected object's schema error, including the
  change index and kind, and documents the unresolved shape. It also preserves supplemental
  source reads outside the affected-batch subset during later reviews.
- Source-read audit now includes the single-batch tool. Phase reports distinguish supervisor
  observations from agent calls and count repeated delivered items.
- Removed duplicated inline definitions from the mutation tool schema using local `$ref`s:
  5353 -> 4003 tokens (about 25% smaller), with equivalent validation on valid/invalid
  examples across protocol object kinds.
- Performance and semantic acceptance remain pending. The first diagnostic snapshot cannot
  establish a passing streak for the follow-up package version.

### Confirmed transport problem and deliberate diagnostic stop

The Codex session showed two outer `exec` truncations (original output ~19028 and
~15380 tokens). Per-tool MCP limits of 31024 did not protect the outer code-mode output.
Raw MCP-envelope printing introduced nested JSON escaping and outer default budgets were
smaller than source windows. The parser reread source batches and then separately fetched
receipts, so receipt issuance alone did not prove the model saw full source text.

The diagnostic run was deliberately stopped after 557 seconds, in parse at revision 5,
64 records and 10/30 completed batches. It cannot count as a quality/performance success.
Its result and SQLite are preserved under iteration-01. This was a confirmed delivery
failure, not an observation timeout or a fuzzer crash.

The next package explicitly instructs code-mode cells to request 30000 output tokens,
decode `content[0].text`, and print each complete decoded page once. Metadata-only rereads
cannot recover unseen source. Source receipts remain valid across model mutations within
the same dispatch. The runner now also fails delivery-integrity acceptance when Codex
session tool-output records contain a truncation warning.

## Follow-up candidate: iteration-02

- Package snapshot: `fd8da2327d9a2c6bf0cd2dd1a32f9aae37fcb708dfae0388d132d322e1abbfc4`.
- Live CLI start: 2026-11-09 16:28:06 UTC. Container `rsc-ftp-runtime-iteration-02-1`.
- Fixed input order RFC 959 then RFC 3659; same RFC content, Luna high, batching and review limits.
- Includes code-mode delivery-budget guidance, clearer kind-specific errors, source receipt reuse, smaller mutation schema and observer attribution.
- Targeted workflow/MCP/supervisor/paging checks: 49 passed. Latest full suite before the delivery-prompt tweak: 149 passed, 78 subtests passed. Changed-file Ruff and diff whitespace checks passed.
- Three sequential trials are requested; the runner stops on a failed time/gate/delivery test. Full semantic audit is additional and remains pending.

### Iteration-02 result and follow-up

Source delivery was fixed: a 21342-token page was printed as a decoded payload with a
30000-token outer budget; no truncation warnings were seen. A separate issue emerged:
MCP success content was JSON but error content was plain text. The same JSON decoder then
masked precise required-field errors with a SyntaxError. Two unchanged submissions omitted
party descriptions before the parser corrected them. The diagnostic was deliberately stopped
at 221.9 seconds (parse revision 1, 19 records, 3 completed batches), with artifacts preserved.

Follow-up candidate makes both success and error MCP text valid JSON while preserving
`isError=true`. The actual stdio regression now decodes and checks the returned diagnostic.
The mutation tool description also exposes required value fields generated from the actual
schemas, so dynamic tool discovery does not force the model to guess object shapes.
The targeted regression suite passes 49 tests; the candidate is being run as iteration-03.

## Candidate iteration-03

- Package snapshot: `d509ea3dc770fbb6a0d91ee7194860d1869cfc6db6d81fd511c28e251c26badf`.
- CLI start: 2026-11-09 16:32:48 UTC; container `rsc-ftp-runtime-iteration-03-1`.
- All 18 tools present; snapshot includes JSON-encoded error payloads and schema-derived required-field descriptions.
- Full regression: 149 passed, 78 subtests passed in 43.81 seconds. Targeted checks: 49 passed.
- Live observation at 56 seconds: parse phase, 17 events, latest source read succeeded. Performance/delivery/semantic acceptance still pending.

At 255 seconds in iteration-03: parse revision 2, 29 records, 4 completed batches.
There was one rejected update attempting to change an immutable step name; the subsequent
mutation succeeded. This is a recoverable argument error, so the diagnostic is continuing
rather than being restarted. No package changes will be injected into this run. If elapsed
or quality acceptance fails, the next candidate should also clarify immutable step names
and evaluate a smaller source-reading window against the observed duplicate reads.

### Iteration-03 result: failed 20-minute limit

CLI wall time 1202.024s including timeout termination; exit=124, timeout=true, final phase=review round=1, revision=17, records=138. No published protocol; not a passing trial.

Parser processed all 30 batches but spent its last minutes correcting accumulated reference errors. Observed blockers included field relations involving network actions and fields not declared on the carried messages. These constraints were enforced correctly by the final gate, but were not explained or returned early enough to prevent accumulating invalid draft edges.

- preprocess round 0: 28.5s; 11 tool calls.
- parse round 0: 1151.3s; 102 tool calls.

## Candidate iteration-04

- Source responses add an optional lossless compact encoding; legacy single reads and records format remain unchanged. On all 30 real batches, exact round-trip verified, response tokens drop from 124939 to 79261 (36.56%). Source words and metadata are not summarized or dropped.
- Source windows use 16000 tokens; full model/diff paging remains 24000. Code-mode output budget stays 30000. Receipts and record ledgers are cached under dispatch-specific keys to avoid metadata retrieval reads.
- Mutation replies expose incremental draft reference errors and exact message field names. Prompt explains field-carrier/network constraints and immutable step names before extraction. Legitimate forward references remain allowed while drafting; publication gates are unchanged.
- Full regression: 153 passed, 78 subtests passed in 43.07 seconds. Targeted checks: 53 passed.

- Frozen iteration-04 package: `29589d5e44d95eded79eca80611508bd0bd6e69817499a10e7f1b32a19b184ce`. Container `rsc-ftp-runtime-iteration-04-1`, CLI start 2026-11-09 16:59:59 UTC.
- At 137s: parse revision 2, 28 records, zero rejections and zero draft reference errors in either mutation. The initial four source batches were delivered once as a 12214-token compact page.
- Added offline model-visible source-delivery audit; its focused test passed. It does not change the RSC package or tool set and will be applied to completed candidate traces before acceptance.

### Iteration-04 live observations and optional next experiment

At 551 seconds, the candidate had parsed 14 batches and retained 126 records. One evidence
ownership error had been rejected and corrected; no accumulated draft reference errors were
reported. The package remains frozen pending its complete time/quality result.

An offline mutation-table prototype was measured separately (not installed into the running
candidate). On iteration-03's 186 mutations across 17 operations, grouped columns/rows plus
an evidence dictionary reduced expanded argument tokens from 26050 to 19868 (23.73%), with
exact round trips. These are JSON argument measurements, not a claimed runtime speedup.
If the current candidate misses 20 minutes, a compact mutation input accepted by the existing
apply_changes tool is a justified next experiment; it must retain legacy inputs, atomicity,
idempotence, full schema/evidence validation and the unchanged 18-tool set.

### Iteration-04 final result

Timed out at 1202.025s, in repair round 1 at revision 13; not published and not a passing trial. Delivery integrity passed (no outer truncation warning).
Actual phase times: preprocess r0 35.6s, parse r0 737.7s, review r1 169.8s.

Offline delivery audit verified every issued batch receipt: 30 parse sources, 30 first-review sources and 3 repair sources. Codex traces contained no compaction event. The final repair interval showed streamed reasoning but no committed mutation before timeout; it cannot be credited as completion.

Per-response usage attributed to completed phase windows: parse had 39 model responses,
37673 output tokens including 12132 reasoning tokens; review had 18 responses, 7682 output
tokens including 4556 reasoning tokens. Iteration-03 parse had 68 responses and 57498 output
tokens. This measures reduced generated work, not an isolated inference-latency improvement
or proof of equivalent semantic quality (the independent reviewer still found four omissions).

Four review findings concerned persistent multi-transfer connections, base-RFC restart markers, abort cleanup/reply outcomes, and distinct restart-safety checks. These are concrete source-supported requirements, not grounds for weakening acceptance.

## Candidate iteration-05

- Existing apply_changes accepts either legacy arrays or compact evidence dictionaries with ordered operation groups and value columns/rows. Decoding precedes the same full validation and transactional logic; canonical expansion preserves idempotence across formats. Tool names/count remain unchanged.
- Errors aggregate bad evidence pairs in a change so a caller can correct related pairs together. Bool/negative/out-of-range pool indexes, duplicate columns and malformed rows are rejected.
- Generic prompt guidance retains non-obsoleted base mechanisms, persistent connection reuse, and prescribed abort/error cleanup. Repair works through source-coherent findings, applying/resolving them before unrelated rewrites.
- Full regression before the final repair-procedure wording change: 162 tests and 78 subtests passed. Focused supervisor and compact-codec checks rerun after that wording change.

### Iteration-05 diagnostic result

Frozen package `c106b06c163fcb10630ab761f3209d2f255f954a49349cfe7065b1cb2b8f2e1f`;
container `rsc-ftp-runtime-iteration-05-1`, CLI start 2026-11-09 17:28:30 UTC.
Intentionally stopped at 373.441 seconds to correct a confirmed discovery-contract defect.
Final phase=parse, revision=3, 21 records, 4/30 parsed batches, six rejected calls;
not a performance or quality pass. The CLI wrapper returned zero after termination, but
the runner correctly rejected it because no protocol was published. No delivery truncation.

The actual model-visible ALL_TOOLS discovery renders `$ref` value types as `unknown` and
untyped compact rows as string arrays. Only top-level required names were in the description.
The first five rejections were row-width and nested-schema guesses: string lists instead
of arrays, strings instead of field objects, invented object keys, endpoint-source shapes,
and null keys that are forbidden on the selected step variant. Each attempt rewrote a large
transaction. A sixth rejection was an evidence ownership mismatch. This is direct tool
delivery evidence, not an inference that model reasoning or SQLite became slower.

## Candidate iteration-06

- Existing apply_changes description now includes schema-generated exact nested value shapes,
  native object/array/null compact cells and closed step variants. Row schema exposes native
  JSON cell unions rather than misleading string-only cells. The same 18 names remain.
- Nested errors identify field/index paths; step errors validate the specified action variant
  instead of reporting the entire union. Publication and evidence checks are unchanged.
- Prompt caches proposed transactions before submission so correcting rejected rows need not
  regenerate unchanged values. Explicitly names submit_resolutions in the repair procedure.
- A full regression caught that the last iteration-05 repair wording had omitted this tool
  name, despite earlier focused checks passing. This is corrected before the next live test.
- Full regression: 164 passed, 78 subtests passed (43.04 seconds); changed-file Ruff and
  whitespace checks passed. Frozen package `3c04835e38415355c9d17880264b6cb3a4067b9f9ac82c84aae6ac2927425b5b`.
- Container `rsc-ftp-runtime-iteration-06-1` started at 2026-11-09 17:37:38 UTC; fresh task,
  RFC 959 then RFC 3659, unchanged Luna high and review limits. Runtime acceptance pending.

### Iteration-06 final result

Timed out at 1202.025 seconds in parse, round 0, revision 12, 261 records, 21/30 reports
complete; no published protocol, not a passing run. Eight rejected calls: step-variant
null columns, two malformed table rows, partial values sent as replacement updates, an
opaque record ID in a report, and three evidence ownership attempts (two sharing an error).
Tool wall time was 26.753 seconds. Actual complete source delivery was independently verified
for all 25 batches whose receipts had been issued; no outer-output truncation was detected.

Usage: 60518 output tokens including 30199 reasoning tokens, versus iteration-04's completed
parse 37673 including 12132. Model workload also differs: this candidate retained 146
dependencies, 90 steps, 10 channels, 8 parties, 6 messages and one unresolved draft record.
The data does not isolate format as the cause of more reasoning. It does show that reducing
literal argument keys alone has not controlled overall generated work or semantic granularity.

## Candidate iteration-07

- The same apply_changes tool gains explicit `op=patch` for selected fields, with shallow
  array replacement and retention of prior plus new evidence. Legacy update remains full
  replacement. Merged values retain complete validation, size limits, immutable step names,
  transaction rollback and cross-format idempotence. New evidence still requires a receipt
  in the current dispatch; old provenance is preserved for unchanged model content.
- Compact mutation tables remain supported but are no longer prescribed for heterogeneous
  shapes. Prompt encourages short source-supported local templates instead of repeated
  schema boilerplate; compact send/network groups use different closed shapes.
- Discovery descriptions correctly state that record/finding ID lists in batch reports are
  optional. Source-specific summaries, receipts and all source/review gates remain mandatory.
- Message-scope and associated-field semantic rules are sent directly with phase instructions,
  since code-mode discovery also hid those descriptions inside referenced definitions.
- Implementation was prepared in `/tmp/mcn-rsc-runtime-v7` while iteration-06 stayed frozen,
  then copied into the workspace only after the failed run exited. The staged suite initially
  lacked the two existing offline-model artifacts; 166 tests passed and that artifact-only
  test failed. After supplying the unchanged artifacts, focused paging/patch/supervisor checks
  passed 16 tests. Workspace full regression is recorded separately before live launch.
- Workspace full regression: 167 passed, 78 subtests passed in 42.76 seconds; Ruff and
  whitespace checks passed. Frozen package `f35d41b4d1ce27ca4806f1597f011b2777da8ef2ef73b42284c43fe8e9cbd131`.
- Fresh container `rsc-ftp-runtime-iteration-07-1` started at 2026-11-09 17:59:46 UTC.
  The harness requests three sequential trials, stopping at the first failed boundary/gate.
  Performance, full workflow and semantic acceptance remain pending.

### Iteration-07 final result

Timed out at 1202.023 seconds in repair round 1, revision 15, 272 records. No publication;
not a passing run. Preprocess 32.1s, parse 925.0s, first review 175.7s. Source reports were
complete at about 595s with 154 records, but subsequent source-supported additions raised
the final count to 272. Those additions and optional whole-model reads also consume the budget.

Five rejected calls concerned a mixed step shape, row width, and three evidence ownership
errors. Two ownership errors occurred in different rows of the same logical transaction,
requiring separate retries despite prior per-row error aggregation. There was no observed
Codex compaction or output truncation. Parser final context input was about 218k tokens,
below its reported 258400-token window; this is not evidence of a compaction stall.

The independent reviewer required distinct resumed-data lifecycle roles and the separate
store-resumption modification-time safety check. Both remain required. Offline delivery
audit verified all 30 parse sources, 30 review sources, two repair sources and all three
reviewer model pages. Complete delivery still did not make the unfinished run acceptable.

## Candidate iteration-08

- Source reads expose deterministic short citations alongside complete text, metadata and
  legacy evidence pairs. Existing mutation/finding/resolution tools expand citations to
  canonical pairs before validation/storage. Current-dispatch receipts, ownership checks,
  idempotence and independent review are preserved; all 18 tool names remain unchanged.
- Compact mutation rows can use citation strings without constructing an evidence pool.
  Legacy expanded objects and integer pools remain supported. Mutation preflight aggregates
  ownership/receipt failures across changes before changing any record.
- Source-window responses remind extraction to retain each direction/mode's own preconditions
  and source-required restart lifecycles. Completion responses point to remaining work and
  phase submission; generic instructions contain no FTP-specific expected findings.
- Offline round trips on the 30 real source batches and 315 reconstructed full-value mutations
  preserved exact content/canonical mutations. Compact source bodies increase from 79261 to
  84027 tokens with citation labels. Counterfactual full-value arguments decrease from 18792
  (integer pools) to 15990 (direct citations), or 33507 to 24923 for expanded objects.
  These are representation measurements, not actual generated-code or runtime improvements.
- Full regression: 173 tests and 78 subtests passed in 43.15 seconds. A subsequent repair-text
  line wrap changes no procedure; Ruff and whitespace checks passed before live launch.
- Frozen package `f49256a0f1c9b044a35a949f99ced798925c4e4d8efbbc1dd114661c73246d16`;
  fresh container `rsc-ftp-runtime-iteration-08-1` started at 2026-11-09 18:28:30 UTC.
  At 42 seconds it was in parse with zero rejected calls. Complete timing/quality acceptance
  remains pending; no trial is counted as a success merely for starting or parsing its sources.

### Iteration-08 final result

Timed out at 1202.024 seconds while still in parse, revision 18, 295 records. No published
protocol; not a passing run. Until the last minute there were no rejected calls; the final
rejection correctly disallowed a condition property on a message definition. Tool execution
totalled 28.805 seconds. Session output was 60437 tokens including 30993 reasoning tokens.
Short citations were actually used in mutation arguments and all 30 issued source batches
passed the full-delivery audit. This solved a source-ownership friction point without yet
meeting overall runtime acceptance.

### Controlled order experiment: iteration-08-order

The original 32-minute baseline database and the later task 9e982a76 both record input order
RFC 3659 then RFC 959. Thus order does not explain the historical difference between those
two runs. The newer trials used RFC 959 then RFC 3659; this experiment isolates whether the
opposite order reduces backtracking in the current implementation.

RSC package remains exactly `f49256a0f1c9b044a35a949f99ced798925c4e4d8efbbc1dd114661c73246d16`.
Only benchmark input order changes, explicitly recorded in the manifest and CLI command;
both immutable files, model/effort, batches, gates and deadlines are unchanged. Container
`rsc-ftp-runtime-iteration-08-order-1` started at 2026-11-09 18:50:14 UTC. At 357 seconds:
parse, 9/30 reported batches, 76 records and no rejected calls. Acceptance remains pending.

An unapplied phase-prompt simplification is kept in `/tmp/mcn-rsc-v9-instructions.py` while
the order experiment stays frozen. It retains full protocol semantics and all review/source
gates, removes irrelevant mutation instructions from reviewer/preprocess assignments, and
focuses parser work by window. Dispatch checks using the staged function passed 13 tests.
This is candidate preparation, not a measured runtime improvement or a change to this trial.

### Order-control result

Timed out at 1202.023 seconds in parse, revision 12, 228 records. All 30 sources were
delivered, but only 25 reports were complete; there was one missing-record mutation error.
Tool execution was 25.773 seconds. Session output reached 56210 tokens including 32564
reasoning tokens. A 14k-token last window covered five remaining batches and preceded a
long model-generation interval. Order alone did not meet the deadline; no publication or
quality pass is credited. The source-delivery audit verified all 30 issued receipts.

## Candidate iteration-09

- Simplifies role/phase instructions without removing canonical protocol semantics or
  source/model/diff/resolution gates. Preprocess gets only security, handoff and deterministic
  preparation; reviewer no longer gets mutation-format instructions. Current procedure is last.
- Parser source window budget becomes 8000, with explicit retry at the reported required
  budget for a larger complete batch. Original source preparation stays at 8000/12000;
  review/repair reading stays at 16000 and model/diff pages stay at 24000.
- Describes steps as reusable operations rather than copies of scenario occurrences.
  Identical message/channel/actor sends reuse a definition with source-specific constraints
  on edges, retaining all source-supported paths. Earlier-occurrence dependencies are already
  accepted by both RSC references and the Rust importer; no protocol schema/gate changed.
- Promotes compact mutation rows with direct source citations, exact-name queries for a
  missing cached record, and window-local extraction. Independent full review remains mandatory;
  known omissions/blockers must be addressed before handoff.
- Prompt-size proxy: preprocess 1967 -> 166 tokens, parse 2337 -> 1712, review 2119 -> 1355,
  repair 2149 -> 1596. These are text measurements, not measured runtime gains.
- Full regression: 173 passed, 78 subtests passed in 41.93 seconds. Ruff and whitespace
  checks passed. Uses the same extensions-first order as the immediately preceding control,
  fixed file contents, Luna high, review limits and the same strict deadline.
- Frozen package `466ea7e3464aa31bb422c839acc026f216e78bf97f1fa9413e7d9d2711559c1c`;
  container `rsc-ftp-runtime-iteration-09-1` started at 2026-11-09 19:14:12 UTC.
  At 86 seconds: parse, two source reports, no records or rejections yet. Performance and
  semantic acceptance remain pending, with the three-run streak still unproven.

### Iteration-09 final result

Timed out at 1202.025 seconds in parse, revision 19, 186 records. Only 26 source batches
had been delivered; the offline delivery audit verified all 26 issued receipts, but the
remaining four sources and all independent review work were unfinished. Nine rejected calls
included four positional-array width errors, three missing record IDs, one network-step
shape error, and one evidence-index error.
Tool execution totalled 26.580 seconds. Smaller windows and prompt trimming did not meet
runtime acceptance; no passing run is credited.

## Candidate iteration-10

- Existing apply_changes accepts grouped named rows with optional shared value defaults.
  Each row retains explicit field names and its own evidence. Legacy expanded operations and
  positional compact tables remain supported. Full schema/source/reference gates are retained.
- Existing-record edits accept exact kind/target value fields, normally name; dependency
  selectors use their source/target steps and additional fields if needed. Zero or ambiguous
  matches fail atomically. This avoids copying opaque IDs without introducing fuzzy matches.
  Operation retries retain bindings across renames/deletions, including old stored signatures.
- Legacy positional width errors are aggregated with actual/expected widths and column names,
  so a retry can fix every reported row together. New instructions prefer named rows and
  direct citations, avoiding positional columns and evidence-pool indexes.
- Parser source window returns to 16000 after the 8000-token trial fragmented work without
  reaching the gate. Initial source batching, review requirements, model/effort, input files,
  extensions-first order, and strict 1200-second deadline stay unchanged. The same 18 tools
  are used; no tool was added.
- Five new tests cover named/default expansion, strict unique selectors, atomic rejection,
  dependency disambiguation, and operation compatibility/retries after rename/deletion.
  Focused mutation/workflow suite: 55 passed. Full regression: 178 tests and 78 subtests
  passed in 42.37 seconds. Scoped Ruff and whitespace checks passed. Existing unrelated
  audit.py has a pre-existing line-length lint finding; it is not part of this change.
  Live timing and semantic acceptance pending; the success streak remains zero.
- Frozen package `b8716211d393f3edf7e76f79948eaeb764c73efe06eec592f705a418eb296c68`;
  fresh container `rsc-ftp-runtime-iteration-10-1`. At 53 seconds it was in parse,
  with its first full source window read and no rejected calls.
- At 258 seconds: revision 2, 37 records, 7/30 reports, no rejected calls. Actual
  Codex tool inputs use named groups/defaults and exact dependency targets. The second
  mutation corrects two semantic field relations rather than retrying a rejected schema.
  Usage so far: 12300 output tokens including 8339 reasoning tokens. This confirms the new
  format is used, but generation remains the larger cost and semantic review is still pending.
- At 640 seconds: revision 5, 114 records and 20 reports. One rejected call reported both
  missing required field_relations arrays and was corrected. Usage: 32679 output tokens,
  including 20308 reasoning tokens; avoid attributing the remaining wall time to tool execution.

An isolated possible next trial is staged at `/tmp/mcn-rsc-v11-instructions.py`, not installed
into this running candidate. It changes only complete source presentation from positional
compact tables to named records. Earlier pre-table runs used fewer reasoning tokens, but
other changes confound that comparison; this is a hypothesis to test, not a proven cause.
Both representations preserve full source text/metadata and use identical delivery gates.
The compact source format remains supported. No trial or success streak changes mid-run.

### Iteration-10 final result

Timed out at 1202.024 seconds in parse, revision 7, 226 records. All 30 source batches were
fully delivered (verified against actual Codex outputs), but only 25 source reports were
submitted and independent review had not started. No publication; no passing run is credited.
Tool execution totalled 25.015 seconds. Output reached 62557 tokens including 37838 reasoning
tokens. Three rejected calls: two rows missing field_relations in one call, then misplaced
row-level fields in two later calls. Cached proposed changes allowed small corrective code;
there was no need to regenerate each entire failed bulk transaction. Reducing identity/width
errors did not bound first-pass semantic generation. Do not interpret partial record counts
as an equal-quality throughput comparison with another unfinished run.

## Candidate iteration-11: source presentation control

- Changes only shared source-reading instructions from compact column tables to named records.
  Mutation syntax, schema, source batching, full-text delivery, citations, receipts, phase
  gates, independent review, RFC order/contents, Luna high and deadline remain unchanged.
- Offline all-30-batch exact round trip: named records 131915 tokens versus compact tables
  84027, including canonical evidence and short citations. This costs more input and tests
  whether named fields reduce interpretation/reasoning effort; no speed benefit is assumed.
- Prior core regression remains 178 tests plus 78 subtests. The changed instructions passed
  12 isolated supervisor tests. Root dispatch/delivery tests are rerun before live launch.
- The success streak remains zero. Complete runtime, delivery and semantic acceptance pending.
- Root dispatch/source-format/delivery-audit checks: 19 passed in 0.42 seconds;
  Ruff and whitespace checks passed. Frozen package
  `003d942d1d9c6b82cbafc5b3ca031d1de8ebe737be49052c55a178b1ad4d50f1`.
  Container `rsc-ftp-runtime-iteration-11-1` started 2026-11-09 20:12:35 UTC.
  Runner requests up to three fresh sequential runs, stopping on the first failure.
- At 368 seconds: revision 4, 63 records, 8 source reports, no rejections; output 17049
  tokens including 10713 reasoning tokens. More named source bytes alone has not established
  a throughput improvement, and the all-source/model/review checks remain unfinished.

A follow-up tool-schema correction is staged only in `/tmp/mcn-rsc-v10` (scratch tree):
named mutation rows now expose value/evidence/target/record_id metadata instead of an untyped
object, with concise errors naming each misplaced field and row. Integer evidence references
remain valid; expansion, full validation and atomicity are unchanged. Focused suite: 56 passed.
This is not installed into iteration-11 or claimed as a measured runtime improvement.
- At 685 seconds: revision 9, 95 records, 22 source reports, still no rejected calls;
  output 34664 tokens including 22462 reasoning tokens. Lack of tool errors has not removed
  substantial generation time. The version stays frozen until the deadline/publication.

The staged follow-up also tests a different work order: read all complete source windows first,
then construct canonical definitions/interactions using the available complete inputs and cached
receipts, rather than repeatedly building a provisional graph after each window. This preserves
all source coverage and independent review; it is a hypothesis about planning organization,
not permission to omit source facts or report unfinished work. Each window must still be printed
completely in its own exec to avoid outer clipping. The source response hint is aligned with
that procedure. The full staged schema regression passed 179 tests and 78 subtests; after
procedure changes, focused dispatch/workflow/source/delivery checks passed 55 tests.

### Iteration-11 final result

Timed out at 1202.026 seconds in parse, revision 13, 222 records and 28/30 source reports.
Zero tool rejections, no output truncation, and all 30 complete source deliveries independently
verified against actual Codex outputs. Independent review never started, so there is no workflow
or semantic pass. Tools totalled 25.484 seconds; output reached 61267 tokens including 39131
reasoning tokens. Named source records did not meet the deadline even after format errors
were eliminated. This is not proof that source representation has no effect, only that this
controlled candidate is insufficient. The success streak remains zero.

## Candidate iteration-12: complete reading before graph construction

- Reads every complete input window first, then chooses canonical definitions and constructs
  source-supported interactions in bulk. Follows read_batches.remaining rather than report-based
  pending_batches; prints one complete window per exec. Cached receipts remain dispatch-scoped.
- Preserves each source-stated mode/direction's state checks and distinct initial/resumed roles.
  Clarifies existing Party/Channel schema semantics: direct communication roles, underlying
  transport, terse role/lifecycle descriptions. No facts or trace variants are silently omitted.
- Named mutation rows expose explicit value/evidence/selector metadata in discovery. Strict
  closed rows report misplaced fields by row/path without dumping the entire transaction.
  Legacy integer pools/expanded operations remain supported, with identical full validation.
- Same 18 tool names, two RFCs/order, Luna high, source batching, independent review/diff/
  resolution gates and strict deadline. Source representation stays named records so this
  tests work organization instead of another source-format reversal.
- Staged regression: 179 tests and 78 subtests passed; focused post-procedure checks: 55 passed.
  Root full regression and actual timing are checked before launch. No success credited yet.
- Root full regression: 179 passed, 78 subtests passed in 41.72 seconds. Changed-file
  Ruff and whitespace checks passed. Live performance and semantic acceptance pending.
- Frozen package `97a987a5b98945faf6b0abfa3d8572289ff098deac2fb34af549194955e39b84`;
  fresh container `rsc-ftp-runtime-iteration-12-1` started 2026-11-09 20:34:34 UTC.
  At 97 seconds: 26/30 sources delivered across 10 read calls, revision 0, no records or
  rejected calls. The new source-first work order is actually in use; acceptance is pending.
- Early live source audit verified all 30 complete model-visible batches with exact text and
  metadata, no missing deliveries. Saved inside output/early-source-delivery-audit.json;
  the runner will copy it with final diagnostics. At 186 seconds: all sources read, revision 0,
  no rejections, reported output 3992 tokens including 1335 reasoning tokens. The first
  unified model generation is still pending; source delivery is not semantic acceptance.

### Iteration-12 final result

Timed out at 1202.025 seconds in repair round 1, revision 7, 283 records. Parse completed in
535.318 seconds with all 30 source reports; independent review took 338.081 seconds and found
five valid source-backed issues: default data ports/optional PORT, third-party transfers,
direction-dependent base restart markers, resumed-STOR modification-time safety, and fieldless
reply classification message types. Only two resolutions were submitted before timeout;
three findings remained open and no resolution had yet been independently accepted.

Offline delivery audit verified all 30 parse sources, all 30 reviewer sources, four repair
sources and all three logged model/diff responses. The outer-output warning at 20:44:14 was
**tool discovery metadata**, not missing source/model text: the reviewer printed a broad
ALL_TOOLS match list. The strict harness still fails any truncation warning, and the CLI also
failed the independent runtime limit. Historical timing JSON labels the warning as source
delivery truncation; that label was overly specific and is corrected for future reports.
No passing run is credited. Tool time was 43.565 seconds. Reviewer also requested optional
initial history despite only later reviews requiring it; the unconditional get_changes tool
description conflicted with phase guidance. Finding-reference rejection cost about 18.8s.

## Candidate iteration-13: source obligations and precise review routing

- Retains complete-source-first construction. While reading, caches brief cited interaction
  obligations (defaults/negotiation, per-action comparisons, direction-dependent outcomes,
  additional peers) in store/load, preserving comparison operands and fallback qualifiers.
  Notes guide construction and never replace full text or become invented protocol fields.
- Generic extraction rules clarify defaults versus optional negotiation, distinct source peers,
  local state versus wire fields, and classifications of a shared reply form. No FTP facts,
  record caps, dropped source branches or reduced review limits are embedded in the prompt.
- Supervisor forwards the actual assignment round. Initial reviewer gets full frozen-model
  reading without historical diff; later reviewers still get every full-model/diff page,
  assigned source and resolution. get_changes description now matches that distinction.
- Tool discovery is scoped to one exact namespace/name description. Complete source/model/diff
  bodies use separate exec cells. Optional finding record IDs are omitted unless cached exactly;
  proposed findings are cached before submission to allow local correction.
- Repair reads the union of cited sources, batches independent supported patches and resolutions,
  and preserves unchanged constraints. All existing source/receipt/schema/reference/final
  independent-review gates remain. Total tools: same 18 names.
- Benchmark diagnostics classify metadata-discovery truncation separately and use a generic
  failed_output_truncation label. **Any truncation still fails acceptance**; no rule was weakened.
- Root full regression before final repair wording: 179 passed plus 78 subtests in 41.58 seconds.
  After the repair wording named submit_resolutions explicitly, 13 dispatch/contract checks passed
  in 0.24 seconds; final wrap-only change passed the same 13 checks. Ruff/whitespace checks passed.
  Timing and semantic acceptance remain unproven; success streak zero.
- Frozen package `e2dad2127485d916286e0c404bda1347e4f43d492702fce4a1bd5c028fe10b8c`;
  fresh container `rsc-ftp-runtime-iteration-13-1` started 2026-11-09 21:03:43 UTC.
  At 65 seconds: parse, five source batches read, no model mutations or rejections. The
  candidate remains frozen; no performance or semantic pass is credited for startup.
- Early live audit verified all 30 complete model-visible source batches; saved as
  output/early-source-delivery-audit.json. At 200 seconds only full pages/receipts had explicit
  store calls; no separate obligation-note cache was observed yet. Do not claim that requested
  internal method is followed solely because the prompt asks for it. At 300 seconds: first
  mutation, 46 records, no rejected calls; model construction and acceptance still incomplete.
- First mutation contains 9 party definitions, 31 messages and 6 channels. The actual values
  already preserve the default user/server data ports as alternatives to PORT and explicitly
  represent the two-server control/data topology. The earlier fieldless transfer reply-family
  definitions are absent, and a source-defined block restart-marker unit is present. Steps,
  dependencies and resumed lifecycle roles are not yet complete; this is only a draft observation,
  not a semantic pass or proof that every requested note was explicitly cached.

### Iteration-13 final result

Timed out at 1202.023 seconds in first review, revision 12, 336 records. Preprocess 43.913s,
parse 950.113s. No tool rejection or output truncation. Offline audit verified all 30 parse
sources, all 30 reviewer sources and both full model pages. The initial reviewer made zero
get_changes calls, confirming actual round-specific routing. Review reports/findings were not
submitted before timeout, so this is not a performance, workflow or semantic pass.

Parser output was 51372 tokens including 28109 reasoning tokens; reviewer output 8439 including
4902 reasoning tokens. After all 30 parse reports were already saved, revision 10 added LIST,
NLST, STOU, their output messages and a page transfer unit, with related flow additions. Late
work is source-supported and cannot simply be dropped. Explicit per-window obligation-note
storage was not observed; the requested internal-note method is not credited as implemented
agent behavior. This trial's complete-message inventory still needs independent validation.

## Candidate iteration-14: inventory before interactions; precise predicates

- Retains all-source-first reading but replaces the unobserved separate-note requirement with
  explicit staged construction: complete canonical party/message/channel inventory first, then
  all interactions, then batch reports. Ordinary actual message types need not have retained
  associated fields; parameter values/examples do not create additional message types.
- Explains that an unconditional ordering edge already carries the ordering fact and can have
  condition="". Step action/message/channel facts need no repeated narrative. Real additional
  state, alternative/branch and comparison guards are preserved; no condition length cap exists.
- Source-required new reconnect/resumption lifecycles get distinct channel.name values and
  step.channel_name references; only genuinely unknown restoration details stay unresolved.
- Existing 18 tools, all source/model/diff/receipt/schema/reference/resolution gates, fixed
  RFC files/order, source batching and Luna high remain. No nodes, relations, required reads,
  review rounds or genuine source facts are removed. Success streak is still zero.
- This candidate changes instruction/description text, with no protocol-schema or validator
  change. Focused workflow/dispatch/source/delivery regression: 55 passed. Ruff and whitespace
  checks passed. Prior full-code regression remains 179 tests plus 78 subtests.
- Frozen package `4caef42b4468b2e58c28e5bd430fd8107f635449a9f34edb9f49037332b32151`;
  fresh container `rsc-ftp-runtime-iteration-14-1` started 2026-11-09 21:35:31 UTC.
  At 81 seconds: parse, 21 source batches read, no mutations or rejections. The candidate
  is frozen while live timing and all quality gates are tested; no success is credited yet.
- Early source audit verified all 30 complete model-visible batches with exact text/metadata,
  saved as output/early-source-delivery-audit.json. At 370 seconds: revision 3 with 9 parties,
  43 message types and 10 channels, no steps/dependencies yet and no rejected calls. The staged
  inventory-first construction order is actually in use. Semantic completeness and total runtime
  remain pending; counts alone are not quality evidence.

- Terminal: elapsed 1202.027s, timeout in first review, revision 11, 435 records; parse
  815.176s, preprocess 44.441s. Three rejected calls: string reports with a spurious 31st batch,
  its missing receipt, and a finding referencing a missing record. Four open findings: invented
  PASV+PORT user listener, unconditional SIZE setup, missing PORT-B acknowledgment before
  STOR-A, and unsafe restart comparison predicates guarding transfers. No pass is credited.
- Full actual delivery audit verifies 30 parse batches, 30 review batches and five reviewer
  model pages; no clipping. This proves delivery, not semantic acceptance.

## Candidate iteration-15: persistent source-local requirements

- Existing complete_batch adds stage=plan and final planned_batch_ids. Source reports retain
  summaries and requirements with verified same-batch evidence/receipts; planning does not mark
  extraction complete. Finalization reuses original report validation.
- Opt-in require_source_plans enforces planning after delivery before fresh reads, and all plans
  before model mutations. Complete rereads remain available. The setting persists on resume.
- Report schemas expose summary and requirements directly. Tool names remain exactly 18;
  protocol schema and all source/model/diff/receipt/reference/resolution/review gates remain.
- Hypothesis: durable source obligations reduce late additions and incorrect comparison/branch
  modeling. Runtime and quality benefit remain unproven. Isolated full regression: 185 tests
  and 78 subtests pass; eight planning tests (including two later additions) and scoped Ruff pass.
- Root full regression after all additions: 187 tests plus 78 subtests passed in 42.16s;
  changed-file Ruff and whitespace checks passed. All 18 tool input schemas validate.
- Frozen package `516b3be65483dfa822f047ba685e2d12b74532f76bb8ea3a88f62213a2202b86`;
  fresh container `rsc-ftp-runtime-iteration-15-1` started 2026-11-09 22:08:11 UTC.
  Actual task state confirms require_source_plans=true. Model/effort remain Luna/high;
  both fixed RFCs and extensions-first order remain. Source package stays frozen while live.
- At parse elapsed 422s all 30 source plans were saved, containing 90 cited requirements;
  processed=0 and revision=0, confirming planning did not bypass model construction or reports.
  Four rejected calls occurred: one receipt mismatch and three foreign-batch plan citations.
  Complete source rereads/corrections remained possible. Early delivery audit verifies every
  one of the 30 actual parser source bodies without clipping. Time/semantic acceptance pending.

- Iteration-15 terminal: 1202.024s, still parse revision 6; 369 records (8 parties, 58 messages,
  6 channels, 118 steps, 179 dependencies), 30 plans/90 requirements but zero extraction reports.
  Parser generated 62535 output tokens including 30270 reasoning tokens. Four rejected calls:
  one receipt mismatch and three foreign-batch plan citations. Actual full-source audit verifies
  all 30 parser bodies; no clipping. No runtime/semantic pass. Serial planning is disabled in
  the next candidate because this run did not demonstrate reduced rework or critical-path time.

## Candidate iteration-16: overlap independent reviewer source preparation

- Existing reviewer prepares immutable sources while parser builds the model; there is no added
  agent or tool. Its read-only preparation cannot query/edit the draft, submit findings, complete
  formal reports or publish. Parser tools never expose reviewer requirements.
- Preparation and first formal review retain the SAME reviewer dispatch and receipts. Formal
  review activates only after complete preparation and valid parser submission; an early parser
  submission freezes model writes while waiting. Full original source/schema/reference and
  frozen-model reading/report/resolution gates remain; later reviews retain fresh dispatches,
  full models/diffs and affected sources. Preparation is never formal comparison completion.
- Actual source delivery audit allows that dispatch's preparation interval; full model/diff
  responses must still be delivered after frozen review activation. Timing groups phase+dispatch
  to separate overlapping preparation/review, while retaining legacy event compatibility.
- Conditions explicitly enable their dependent action: an operation's disqualifying comparison
  must not be its enabling guard. Specified error/abort behavior remains. Receipt and plan-evidence
  errors now identify the owning batch to guide corrections.
- Benchmark disables require_source_plans and enables prefetch_review_sources. Both RFC hashes,
  order, 8000/12000 batching, Luna high, max review 5 and 1200s clock remain unchanged. It records
  explicit workflow/runner versions and rejects source OR runner edits during a success streak.
- Isolated full regression: 200 tests plus 78 subtests pass. Further final instruction/receipt
  wording checks pass; scoped Ruff passes. New tests cover real concurrent calls and socket
  catalogs, rollback, role isolation, parser-first/preparation-first barriers, full frozen model
  gates, same-dispatch source delivery and rejection of early model pages. Runtime still unproven.
- Root full regression after final edits: 200 tests plus 78 subtests passed in 42.93s;
  scoped Ruff, whitespace and all 18 input-schema checks passed. Fresh container
  `rsc-ftp-runtime-iteration-16-1` started 2026-11-09 22:34:50 UTC. Frozen package
  `b78f4c2459c730adb577b27711b0731e3f22f8c0e7c849942e9858858cdbb3d1`;
  runner `689dfaa45d7c1bd114ccd6843e5ebd1ff3b303b01731a48da677b82cc38c05ca`.
  At parse elapsed 31s reviewer preparation is claimed and parser has read six batches;
  no tool rejects. Actual preparation overlap and full runtime/semantic acceptance are pending.
- At parse elapsed 106s actual overlap is confirmed: parser has read all 30 batches while
  reviewer has independently read/planned six batches; no rejected calls. This is scheduling
  evidence only, not a timing or semantic pass.
- Audit follow-up isolated under /tmp/mcn-rsc-review-prefetch: an unfinished background
  preparation assignment also needs enumeration in live/failed-run delivery audits, before
  its completion adds phase history. The published-run audit already covers completed
  preparation. Do not call a parser-only live audit proof of all reviewer receipts. A targeted
  verifier regression/fix is being staged; root source/runner stay frozen during iteration-16.
- At parse elapsed 472s preparation is complete: all 30 reviewer plans in 394.2s, overlapped
  with parser work. Parser has 8 parties, 53 messages, 7 channels, 81 steps and 194 dependencies;
  extraction reports remain zero. One format rejection (nine send steps missing action) was
  corrected. This is progress toward a full run, not evidence of semantic acceptance.

- Iteration-16 terminal: 1202.024s timeout in first review, revision 12, 445 records;
  preprocess 42.456s, parse 807.469s, reviewer source preparation 394.192s overlapped parse.
  All 30 parse reports were submitted, but formal review submitted no findings or reports.
  It read all 445 model records in two complete pages and did not reread source batches.
  Final full delivery audit verifies all 30 parser sources, all 30 reviewer sources under
  preparation and formal-review windows, and both frozen model pages. No clipping. This is
  delivery proof only; runtime and semantic acceptance failed. Success streak remains zero.
- Parser generated 42472 tokens including 25852 reasoning tokens. Reviewer generated 33600,
  including 17851 reasoning tokens; prep-end counters were 17427/3396, so unfinished formal
  review added about 16173 output tokens including 14455 reasoning tokens. These are session
  counter differences, not isolated API inference timings. The formal-review tool work was
  claim 0.537s and query_model 10.085s + 0.467s; most remaining time was outside MCP execution.
- The reviewer's auxiliary compact-table helper concatenated groups from two pages then used
  find(kind=step_dependencies), selecting only the first page's dependency group. Both pages
  were actually delivered; this is a local analysis-view omission risk, not missing delivery.
  It also reconstructed positional evidence indexes. This motivates a named-field control.

## Candidate iteration-17: named model records and durable review navigation

- Use the existing records model format (no new codec or tool). All canonical fields/evidence
  remain explicit; full unfiltered pages and revision-aware pagination remain required. Cache
  all page.items arrays before filtering, then index by canonical names and source batch.
- Extend existing list_batches with opt-in detail to recover own current-dispatch receipts and,
  in reviewer phases only, its independently saved source plans. Parser APIs cannot expose
  reviewer facts. This metadata never issues receipts or replaces complete sources/model pages.
- Formal review uses notes to navigate full source comparison, reports each compared window's
  actual findings/no-findings outcome, and avoids rewriting source facts in completion reports.
- Fix live/failed-run delivery audits to enumerate unfinished preparation assignments too;
  completed/published-run audit rules and strict actual-body verification remain.
- Isolated full regression: 203 tests plus 78 subtests pass in 43.06s; scoped Ruff passes.
  New tests verify own-note/receipt isolation, no receipt issuance, copy isolation, exact bounded
  metadata pagination, unchanged formal-review gates and unfinished-preparation delivery proof.
  Runtime benefit and semantic acceptance remain unproven; all original quality gates remain.

- Offline 445-record pagination measurement: compact is 383 + 62 records (227 + 62 dependency
  rows), records is 227 + 218 records; both use two bounded 24000-token pages. The observed
  first-group helper therefore omits 62 dependency rows from its auxiliary filter. Full
  delivery and semantic analysis remain distinct. Raw record payload is 42278 tokens versus
  compact 23468; the control deliberately trades input size for named fields, with no data loss.
- The 30 saved reviewer notes/receipts occupy one exact 14539-token metadata page under the
  16000-token request. Note listing still issues no receipt or formal completion. Source and
  tests copied into the workspace match the full-regression package; workspace socket/audit
  checks pass (16 tests), all 18 input schemas validate, and Ruff/whitespace checks pass.
- Fresh container `rsc-ftp-runtime-iteration-17-1` started 2026-11-09 23:01:25 UTC. Frozen
  package `bdea3826f259ecf490b8cb4cae2ef53408f707909bd1d78dbb539c06f3c9f69b`;
  runner `da8fd9bb60e0773b69cb1daba136883c69a1bfa70a63ff11dac90ef0c91031de`.
  Same fixed image, RFC hashes/order, Luna high, batching, review limit and total clock apply.
  Runtime/semantic acceptance pending; zero passing runs are credited.
- Live iteration-17: preprocess 43.175s, independent source preparation 386.240s overlapped
  parse. Actual complete-source audit verifies all 30 parser and all 30 reviewer bodies.
  At parse elapsed 688s: 8 parties, 51 messages, 6 channels, 86 steps, 222 dependencies,
  zero extraction reports. One reviewer-preparation rejection involved a requirement citing
  batches 10 and 11 together; the current source-plan rule requires every citation to be local
  to the report batch. It was corrected. No runtime/semantic pass is credited; code stays frozen.

- Iteration-17 terminal: 1202.027s timeout in first review; preprocess 43.175s, parse
  729.663s, overlapped source preparation 386.240s; revision 9, 375 records. All 30 parsing
  reports were submitted. First review retrieved all 30 notes in one 15991-token metadata
  page and read all 375 frozen records in two named pages, but submitted no findings/reports.
  Full delivery audit verifies both roles' 30 source bodies, formal-review source proof and
  both model pages. No clipping; no runtime/semantic pass; success streak remains zero.
- Formal-review delta: 19654 output tokens including 18181 reasoning tokens, from cumulative
  counters (prep 17233/3754, final 36887/21935). All 14 external helper/MCP calls completed,
  totalling about 11s with none pending; timestamps show repeated model activity between calls.
  This rules out a hung helper as the observed cause, without claiming isolated inference time.
- Independent structural inspection found four known-role contradictions: passive listen/connect
  steps share initial/resumed channels whose fixed roles specify active connection direction.
  A prototype generic reference check flags exactly those four records. The model may still
  have other semantic issues; a lower record count alone is not acceptance evidence.

## Candidate iteration-18: early role diagnostics and bounded review work

- Generic reference validation ties connect to a channel's known initiator and listen to its
  known acceptor. Unknown roles remain unknown and either endpoint may close. Draft feedback
  names each contradictory step/channel/role; the same check gates phase changes/publication.
  Source-supported alternatives reversing roles get distinct lifecycle channels and steps.
- Prepared review still reads the entire named frozen model, then compares one pending-notes
  window at a time using 4000 tokens of navigation metadata. It reports that actual source
  window before requesting another. Report feedback returns the next window action. All
  original sources/model/diff/resolutions remain; no facts, nodes or source batches are capped.
- Pending-filter collections change after reports: restart notes at cursor zero, preserving
  every remaining batch. A regression covers bounded item pages and no skipped reports.
- Isolated full regression: 210 tests plus 78 subtests pass in 43.35s; scoped Ruff passes.
  New role tests cover both valid/invalid open directions, unknown roles, either-side close,
  reversed lifecycles and draft-feedback/phase-gate repair. No schema field or tool is added;
  fixed RFCs/order, Luna high, batching/review limits and 1200s total clock are unchanged.
- Workspace source matches the full-regression package; 32 focused role/socket/handoff tests
  pass, all 18 input schemas validate, and Ruff/whitespace checks pass. Offline pending-note
  navigation on iteration-17 yields five windows (7, 6, 10, 5, 2 batches), each <=4000 tokens,
  covering all 30 batches exactly once. This is metadata navigation, not a semantic pass.
- Fresh container `rsc-ftp-runtime-iteration-18-1` started 2026-11-09 23:27:28 UTC. Frozen
  package `d691dabf1e77911f7fa9c4a5fb61d6ac4a800f1040dce7a88bc4669067442255`;
  runner remains `da8fd9bb60e0773b69cb1daba136883c69a1bfa70a63ff11dac90ef0c91031de`.
  At parse elapsed 82s parser has read 30 batches, reviewer six, with no rejected calls.
  The candidate is frozen during real timing; no passing run is yet credited.

- Iteration-18 terminal: 1202.026s timeout in first review; preprocess 40.383s, parse
  873.147s, source preparation 391.433s overlapped parse. Revision 8 has 354 records. The
  bounded review window produced six open findings and seven actual reports (batches 1–7),
  covering request-path/encoding correlation, reply-supplied MDTM state, SIZE target binding,
  REST reset/error timing, REST STREAM advertisement and TVFS constraints. It did not finish
  the remaining windows or repairs. One source-plan cross-batch citation retry; no role errors.
- Full audit verifies all 30 sources for both roles and all eight frozen-model responses.
  The first large page was complete; the reviewer then reread from cursor zero in 7000-token
  pages. There was NO output truncation. Earlier concern about clipping was a hypothesis and
  is not credited as an observed cause. No performance/semantic pass; success streak is zero.

## Candidate iteration-19: deterministic preprocessing and shared source constraints

- Supervisor executes the same deterministic claim/prepare/advance tools before sending the
  first parser task. Original source/snapshot/batch gates remain; no body receipts, records
  or extraction reports are issued. Durable intent preserves ownership through lost replies;
  legacy/uncertain agent-owned dispatches are left on their original path.
- Preprocess-only actual two-RFC RPC microbenchmark: 4.035s, exactly the same 30 batch objects
  as the fixed baseline, phase=parse with zero records/receipts/reports. This is not a full-run
  performance claim. It removes a deterministic model round trip while preserving the work.
- Optional share_source_requirements enables one-way reuse of the source-only reviewer's
  committed factual outputs. Parser access requires its own complete source receipt. Only
  summary/requirements are copied; its receipt remains its own. The reviewer still cannot
  read the draft before freeze, and the parser cannot modify reviewer artifacts. Default
  privacy remains unchanged; sharing requires independent preparation to be enabled.
- This deliberately changes the experimental private-note policy into opt-in factual reuse,
  not the independent review gate: both roles still read every source body, and formal review
  still reads the full frozen model and checks every original source, not only shared notes.
  No source/model/diff/report/reference/resolution/publication requirements are reduced.
- Parser checks source constraints against original bodies, refreshes ready facts at stage
  transitions without waiting, and retains request selectors identifying objects whose metadata
  is used later. This targets repeated source derivation and the observed selector/supplier
  omissions; the actual runtime/quality benefit remains unproven.
- Isolated full regression: 224 tests plus 78 subtests pass in 43.92s; scoped Ruff passes.
  Tests cover real authenticated preprocessing RPC, lost claim/prepare/advance replies, legacy
  ownership, tampered snapshots, opt-in/dependency validation, own-body proof before fact reuse,
  read-only projections, draft-blind reviewer access, mutation idempotence and new-dispatch gates.
- Benchmark retains Luna high, both RFCs/order, 8000/12000 source batching, max review 5 and
  the total 1200s clock, and opts into shared source facts. There are still exactly 18 tools.
- Workspace source matches the full-regression package; 43 focused transport/ownership/fact
  tests pass, all 18 input schemas validate, and Ruff/whitespace checks pass. Fresh container
  `rsc-ftp-runtime-iteration-19-1` started 2026-11-10 00:01:02 UTC. Frozen package
  `f2b5ac7656f08c0ce0397e0a8685e94ccb38c437f0f7cf49e6f0648600871af6`;
  runner `b6a695324f51c1a193ee2a1a810af63b6daaaf290dbf8e20687f2ec7b5abf0eb`.
- Actual container preprocessing completed in 12.660s (iteration-18: 40.383s), with coordinator
  tool calls and the unchanged source gates. At parse elapsed 25s no calls are rejected.
  This is an observed phase result, not a full-run or semantic pass; success streak is zero.
- Actual source-fact delivery audit matches four complete parser metadata responses against
  server response hashes, with independent factual outputs visible for batches 1–24. This
  confirms the reuse path is executed rather than only configured. It does not satisfy any
  source/body/extraction/model-review/semantic gate. Parser owns all 30 source receipts.

## Candidate iteration-20: bounded construction and actionable rejection feedback

- Iteration-19 ended at 1202.025s in parse, revision 20, 442 records and zero extraction
  reports. Preprocess was 12.660s and independent preparation 312.123s overlapped parse.
  Tool execution total was 41.959s. Six rejected calls: three source-plan mixed-batch
  citations, two malformed associated_fields patches and one transaction with 32 array-valued
  dependency conditions. Parser output was 56590 tokens including 32527 reasoning tokens;
  reviewer preparation output 14035 including 2700 reasoning. These are observed totals,
  not an isolation of inference latency. Both roles' 30 complete sources verify; no clipping.
- One-way facts were actually delivered to the parser (batches 1–24 audited), yet this did
  not make full construction finish within the budget. In late parse, revision 14–20 added
  resumed lifecycle definitions and dependencies in separate transactions, while reports
  remained zero. Small fixes alone cannot substantiate the performance goal.
- Preserve complete source reading and the complete inventory first, then construct interactions
  in bounded source windows and submit each window's actual extraction reports. Retain linked
  context, all assigned batches and cross-window forward references; truly blocked reports stay
  pending. Avoid another whole-document planning pass and rewriting the graph for each window.
  This changes work checkpoints, not coverage, structural gates or independent review.
- Source-plan requirements must include evidence from their own report batch; additional
  supporting batches are allowed only with unchanged canonical ownership and complete current
  reviewer receipts. Foreign-only evidence remains rejected. Shared facts additionally require
  the parser's own complete receipts for EVERY cited batch before becoming visible.
- Record validation errors now identify nested value paths. Instructions show the actual
  associated_fields patch object and require string conditions, targeting observed retries.
  Tool names/count remain 18; fixed RFCs/order, Luna high, batching, review limit and clock remain.
- Isolated full regression: 227 tests plus 78 subtests pass in 44.30s. After instruction-only
  checkpoint changes, 63 relevant tests pass; final patch/string guidance has 52 focused tests
  passing and scoped Ruff passes. New tests preserve atomic foreign-only rejection, reject
  unread supporting evidence, accept fully read mixed support, enforce parser all-source proof
  and expose nested schema error locations. Performance/semantic passing streak remains zero.
- Workspace has 52 focused tests passing (2.99s), all 18 tool schemas valid, and scoped
  Ruff/whitespace checks pass. Fresh `rsc-ftp-runtime-iteration-20-1` started
  2026-11-10 00:22:38 UTC with package
  `01d751a42c46d8438c8182470cd4b5928cbbb9d1432c252b67254f48c7fa93ce` and unchanged runner
  `b6a695324f51c1a193ee2a1a810af63b6daaaf290dbf8e20687f2ec7b5abf0eb`.
  Configuration is frozen during this sequence; performance/semantic acceptance is pending.

### Offline follow-on candidate while iteration-20 is frozen

- Full named source responses repeat each segment's batch/block pair in addition to its
  canonical batch_id, block_id and short citation. On the same 30 batches/1135 segments,
  omitting only this derived evidence object reduces per-batch serialization from 131915 to
  104675 tokens (20.65%); exact reference reconstruction passes. All original named fields,
  source text, offsets, section paths and citation mapping remain. Legacy compact is 84027
  tokens; it already derives evidence and receives no comparable reduction. See
  `named-source-citation-measurement.json`. This is serialization evidence, not a timing gain.
- Isolated `/tmp/mcn-rsc-cited-sources` adds optional read_batches(include_evidence=false),
  default true for legacy output, and uses this in candidate instructions. No new tool or
  source batching change. Six tests check both formats, complete canonical body equality,
  identical legacy receipts, canonical citation expansion, budget boundaries, undelivered
  receipt absence and invalid option rejection. Full isolated suite: 233 tests plus 78
  subtests pass in 45.55s; scoped Ruff passes. This candidate is NOT promoted while iteration-20
  runs and does not count toward performance/semantic acceptance.
- Iteration-20 live evidence at parse ~473s: zero extraction reports. The first interaction
  transactions referenced 10–14 batches across both documents and later deleted/revised
  definitions. The prompt alone has not yet produced bounded completion checkpoints; this
  is an observed work pattern, not a judgment of the unfinished model's semantics.
- The isolated follow-on candidate therefore adds construction_window feedback to the existing
  mutation/report/status responses after all own source bodies are read and records exist.
  It names two pending batches as an advisory task, retaining all linked context, every source
  requirement and unrestricted report order. Blocked batches can be deferred honestly; the
  hint never grants a receipt/report or changes publication/independent-review gates. Mutation
  retries keep their original cached result even after navigation advances. One new test
  covers these invariants, partial-source absence, nonmutation, out-of-order reporting and
  phase isolation. Full isolated suite now 234 tests plus 78 subtests passes in 44.72s.
  This candidate remains isolated; iteration-20 package/runner are still frozen.
- Offline real read_batches pagination with a 16000-token budget and all remaining batches
  requested: legacy named responses use 12 windows/135853 tokens; cited named responses use
  9 windows/108034 tokens. All 30 canonical batches and delivery receipts are exactly equal,
  all pages fit their budgets. This method-level simulation does not exercise model choices,
  RPC or semantic review and does not predict actual window counts. See
  `named-source-window-measurement.json`.
- Iteration-20 rejected a target using a mismatched long condition and one dependency batch
  missing required field_relations. The isolated candidate explicitly shows
  defaults:{field_relations:[]} for dependency groups, requiring every actual relation to
  override that default; schema remains unchanged. After this instruction-only addition,
  46 relevant tests and scoped Ruff pass. No new schema defaults or relaxed validation.
- At iteration-20 parse ~983s all 30 extraction reports were submitted near the end rather
  than per construction window. A subsequent structural check_gate was ready, but parser then
  added resumed active/passive channels and related steps/dependencies at revision 17. Thus
  the post-report time includes concrete model construction, not simply idle polling or an
  empty model turn. The isolated candidate's final completion hint tells it to fix concrete
  known omissions/errors and then advance without another broad planning pass; it does not
  excuse known missing work or automatically advance a structurally ready draft. After this
  feedback-only change 38 focused tests and scoped Ruff pass.

## Candidate iteration-21: complete named source windows and concrete construction feedback

- Iteration-20 terminal: 1202.025s timeout in parse, revision 21, 345 records, all 30 extraction
  reports but no formal review. Preprocess 12.577s, independent preparation 371.744s overlapped;
  tool total 43.061s. Full audit verifies all 30 sources for both roles; no truncation. Parser
  produced 60653 output tokens including 29927 reasoning; preparation 15604 including 2282.
  Late construction and four rejected transactions (mismatched target, missing field_relations
  twice, misplaced row-level field_relations) consumed time. Success streak remains zero.
- Promote the isolated cited named source option, advisory construction feedback, explicit
  dependency defaults and final handoff guidance described above. Preserve all source text,
  original fields, canonical ownership, receipts, model/diff/review/resolution gates and 18 tools.
- Preparation in iteration-20 used 30 separate reads and 30 plan submissions despite window
  support. Initial full reading now explicitly passes ALL unread assigned batch IDs: the server
  cuts the bounded complete prefix and returns remaining IDs. Prepare/report the whole delivered
  window together; every original batch still receives its own report. Single-batch reads remain
  supported, and repair still reads its required source union. This is a navigation change only.
- Workspace exactly matches isolated candidate package. Full feature regression was 234 tests
  plus 78 subtests in 44.72s; subsequent guidance-only changes pass 60 relevant workflow,
  source/citation, navigation, transport and delivery-audit tests in 7.80s. All 18 schemas validate;
  scoped Ruff and whitespace checks pass. Final package
  `5866251cc06e4576d3f7bdb870f5fba2169af629974d9dc143fd3828fb130892`.
- Real performance and semantic benefit remains unproven. Benchmark retains the pinned image,
  RFC content/order, Luna high, 8000/12000 source batching, five review rounds and total 1200s.
- Fresh container `rsc-ftp-runtime-iteration-21-1` started 2026-11-10 00:44:47 UTC; manifest
  confirms the final candidate package above and unchanged runner
  `b6a695324f51c1a193ee2a1a810af63b6daaaf290dbf8e20687f2ec7b5abf0eb`.
  Package/runner remain frozen during the sequence. No passing run has yet been credited.
- Iteration-21 live: parser uses bounded multi-batch windows (first four reads deliver 17
  batches), reviewer first read delivers six, confirming the intended batching path is used.
  Actual full-delivery audit verifies parser 30/30 and reviewer 17/17 read so far; no current
  output-truncation warning. Parser reread two batches, so no claim of exactly nine calls is
  made. One preparation requirement cited only a foreign batch and was correctly rejected;
  the own-batch evidence requirement remains. These are delivery/workflow observations,
  not completion or semantic acceptance. The candidate remains frozen during the real run.
- Iteration-21 source preparation completed in 318.256s (iteration-20 371.744s), still
  overlapped with parse. At parse ~199s the first two actual extraction reports were saved;
  at ~312s four were saved. This is an observed checkpoint improvement, not an overall
  performance/semantic pass. Current mutation feedback names batches 5/6 as the concrete
  next construction task, and parser is constructing their interactions.

### Isolated source-plan diagnostics while iteration-21 is frozen

- One foreign-only batch-22 requirement was rejected twice in iteration-21. The existing
  diagnostic names the batch but not the bad requirement index. In isolated
  `/tmp/mcn-rsc-plan-errors`, source-plan validation now collects errors across reports and
  requirements, naming batches[index], actual batch ID and report.requirements[index].
  Every original report/receipt/description/citation/block-ownership/own-batch gate remains.
  A submission with any error still persists no source plans or completions; valid rows are
  not partially committed. This improves actionable error feedback without relaxing evidence.
- Two tests cover multiple bad requirement locations, mixed bad receipt/citation reports,
  unchanged persisted state after rejection and subsequent valid completion. Full isolated
  regression: 236 tests plus 78 subtests pass in 51.21s; scoped Ruff passes. Not promoted
  while iteration-21 runs; no new tool or performance claim.
- Offline existing-format comparison on all 195 dependency additions in iteration-20 (including
  later revised/deleted records) uses identical groups/defaults/short citations. Named-row
  arguments total 10915 tokens; existing column arguments 7632, a 30.08% reduction. Both decode
  exactly to every original record/evidence pair. This measures serialized arguments, not
  model-generated JavaScript helpers, reasoning tokens or wall time. See
  `dependency-table-measurement.json`.
- Isolated `/tmp/mcn-rsc-plan-errors` additionally documents the already-supported four-column
  dependency-addition form, explicit field_relations defaults and full relation-column overrides.
  Inventories/steps/patches/selectors remain named; source/model delivery formats remain named.
  No new tool, codec or relaxed validation. Actual runtime/semantic benefit remains unproven;
  candidate stays isolated while iteration-21 runs. Existing native-format and diagnostic
  regressions are rerun after the instruction-only change.
- Iteration-21 at parse ~871s has 14 reports; at ~924s it has 20. The rejected OPTS target
  refers to a missing dependency, not a missing send step (both OPTS steps were declared at
  revision 10). No claim is made that step absence caused this rejection. Source plans checked
  for the long MLSx section mainly retain interaction state/selection/lifecycle facts; the model
  uses grouped pathname/facts fields rather than expanding a complete wire grammar, so there is
  insufficient evidence to shorten scope by discarding those source requirements.
- A further isolated instruction hypothesis declares the complete reusable send/network step
  inventory after parties/messages/channels, then constructs dependency windows against that
  ledger. It explicitly uses add for new edges and patch/delete only for existing edges, with
  concrete omissions corrected when discovered. This reorganizes declarations and relations,
  retaining all supported lifecycles, modes, fields, sources and independent review. The existing
  construction hint reflects that prerequisite. Runtime/quality benefit must be measured; this
  is not credited merely because unit tests pass and is not active in iteration-21.

## Candidate iteration-22: declarations before relations and existing compact dependency arguments

- Iteration-21 ended at 1202.024s in parse, revision 18, 269 records and 20 extraction reports,
  with no formal review. Both roles' 30 complete source batches verify; no output truncation.
  Tool total 37.361s. Parser output 59759 tokens including 33473 reasoning; preparation output
  15496 including 4283. Compared with iteration-20, visible output decreased but reasoning
  increased, and the completed work differs; this is not an equal-quality speedup claim.
- Source preparation improved from 371.744s to 318.256s and reports appeared earlier, yet
  fixed small construction windows did not make the full run finish. Six rejected calls:
  four source-plan affiliation errors, one absent edge selector and one ambiguous selector.
  Source scope and semantic gates are retained; no passing run is credited.
- Promote isolated aggregated source-plan diagnostics and instruction changes described above.
  Complete reusable step definitions before building dependencies, then use concrete source
  windows and the cached ledger; correct discovered real omissions. Dependency bulk additions
  prefer the already-supported explicit column representation with exact citations and full
  field-relation overrides. Inventories/patches/selectors and source/model outputs stay named.
  This is a new runtime hypothesis, not a performance claim based on offline token reduction.
- Root package matches the isolated candidate. Full feature suite was 236 tests +78 subtests
  (51.21s); subsequent guidance/feedback changes pass 76 relevant root tests (3.76s), all 18
  schemas validate, and scoped Ruff/whitespace pass. No tool names/count, source completeness,
  schema/ownership/reference/receipt, independent review/diff/resolution gates are changed.
  Candidate package `e3fbe679a46b718a6892a874b9218cfb5685de34774c4b32b878254baef6d4b7`.
- Fresh container `rsc-ftp-runtime-iteration-22-1` started 2026-11-10 01:08:13 UTC; manifest
  verifies the final package above and unchanged runner
  `b6a695324f51c1a193ee2a1a810af63b6daaaf290dbf8e20687f2ec7b5abf0eb`.
  Package/runner/config remain frozen during this sequence. Passing streak remains zero.
- Iteration-22 live at parse ~230s: eight parties, 52 messages, six channels and 79 step
  definitions are committed, with zero dependency edges/reports yet. This confirms declarations
  precede dependency construction in this run; it is not proof of semantic completeness or total
  runtime improvement. Aggregated diagnostics are exercised with exact report/requirement paths;
  one foreign-only requirement still repeats, so actionable paths alone do not eliminate retries.
  The candidate/runner remain frozen while the dependency and review phases are awaited.
- Actual parser call at 01:13:38 uses the existing dependency-column form in apply_changes,
  with one group for empty relations and another with a full field_relations column. Thus
  the intended argument format is exercised, not merely available. At parse ~480s eight
  source reports are complete and 79 step definitions/57 dependencies exist. Iteration-21
  reached eight reports around 646s, but the draft scopes differ and neither is semantically
  accepted; this is a checkpoint observation only. Preparation completed in 312.593s.
- Iteration-22 used an inconsistent four-column header with five-value rows once; all 28
  mismatched rows were rejected atomically with their exact indices/expected columns. At parse
  ~623s 15 reports were saved; at ~772s 20 reports, 87 steps and 162 dependencies exist.
  No claim of complete semantics or a performance pass is made from these partial counts.

### Isolated fixed dependency-column guidance

- `/tmp/mcn-rsc-five-columns` changes only the instruction example to one fixed five-column
  dependency form: prior_step, dependent_step, condition, field_relations, @evidence. Every row
  explicitly includes the full relation array (empty only when appropriate), avoiding mixed-width
  group mappings. No decoder/schema/gate/tool changes or silent row coercion.
- On the same 195 historical additions, a single fixed-width serialization is 7166 tokens versus
  7632 for the previous variable-width grouping and 10915 for named rows. It decodes exactly
  to all original values and evidence; additions include later revised/deleted records, so this
  is a codec/payload comparison, not an executed transaction or model-code/runtime measurement.
  See `fixed-dependency-table-measurement.json`. Fourteen existing format/selector tests and
  scoped Ruff pass after the guidance-only change. Candidate remains isolated while iteration-22
  runs; no passing streak is credited.

### Isolated unified-dependency construction comparison

- `/tmp/mcn-rsc-unified-edges` retains complete source reading, source-only independent
  preparation, canonical declarations and the fixed five-column argument form, but treats all
  pending batches as one dependency-construction coverage checklist rather than fixed pairs.
  Related edge groups may still use separate RPCs; no oversized single call is forced. Actual
  source-specific reports remain required and never substitute for modeling. Named source/model
  delivery, every mode/lifecycle/state/field constraint and all original review gates remain.
- This is the plan's comparison of incremental versus full-reading/unified construction. The
  historical best parse (iteration-12, ~535s) motivates testing coherent construction again with
  current source constraints/format fixes, but had five semantic findings and was NOT an accepted
  run. No benefit is assumed. The live iteration-22 is unchanged.
- The advisory feedback lists every pending batch, never grants receipts/reports or restricts
  report order. One four-source test checks complete checklist/nonmutation/out-of-order updates
  and retained pending gate. Full isolated regression: 237 tests plus 78 subtests pass in 45.60s;
  47 relevant tests/scoped Ruff also pass. Candidate remains isolated during iteration-22.

## Candidate iteration-23: coherent dependency construction and fixed five-column arguments

- Iteration-22 terminal: 1202.024s in parse, revision 22, 475 records, all 30 extraction reports,
  no formal review. Full actual delivery verifies 30/30 sources for both roles; no truncation.
  Tool total 43.502s; preparation 312.593s. Parser output 54819 tokens including 27669 reasoning,
  preparation output 13725 including 2473. Larger modeled work and lower token totals still did
  not yield a completed run; partial node/report counts are not semantic or speed acceptance.
- Promote the isolated fixed five-column and unified-dependency variants described above.
  Keep complete source reading and declarations first. All pending source IDs form a coverage
  checklist for coherent dependency construction; related RPC groups and out-of-order actual
  reports remain allowed. Refresh ready source facts only when availability increases, without
  waiting/polling or replacing original source work. All original quality gates and 18 tools remain.
- Full isolated feature suite: 237 tests plus 78 subtests pass in 45.60s. Root package matches
  isolated package; 66 related root tests pass in 3.61s, 18 schemas validate, scoped Ruff and
  whitespace pass. Package `2b58e83e897152993e0be84c81bfc1dbbe7db302d55917fb21605fcb2447d7a4`.
- Fresh `rsc-ftp-runtime-iteration-23-1` started 2026-11-10 01:29:34 UTC; manifest verifies this
  package and unchanged runner `b6a695324f51c1a193ee2a1a810af63b6daaaf290dbf8e20687f2ec7b5abf0eb`.
  Same pinned image, fixed two RFCs/order, Luna high, 8000/12000 batching, five review rounds,
  source-preparation/sharing flags and 1200s CLI deadline. Frozen during the sequence; zero
  passing runs credited. Runtime and semantic benefit remains to be demonstrated.

### Isolated atomic final-report handoff

- Historical runs sometimes remained in parse after their last reports; some of that time
  included real omitted work, so no claim is made that it was all idle. Isolated
  `/tmp/mcn-rsc-finish-report` adds optional finish=true to the two existing completion tools,
  default false. It saves actual complete reports and invokes the ORIGINAL advance_phase
  gate/assignment/publication path in the same transaction. Any failure rolls back this call's
  reports. Source plans cannot use it; no automatic acceptance or publication is introduced.
- Successful handoff returns stop_turn/has_work and records phase_submitted in server audit.
  Phase metrics recognizes this real submission; if parser waits for source preparation, the
  phase is still timed through actual handoff, not its early submission. Original tool count 18.
- Twelve new tests exercise report-only compatibility, durable handoff, partial/reference/snapshot
  rollback, full frozen-model review, repair routing, mandatory later full diff and independent
  resolution decisions, parser freeze/wait timing, planned finalization and invalid booleans.
  Full isolated regression: 249 tests +78 subtests pass in 45.66s. Two further preparation-role
  rejection cases plus transport/supervisor tests pass (34 focused tests, 5.47s). All schemas
  validate and scoped Ruff passes. No runtime improvement is credited; candidate is isolated
  while iteration-23 runs.
- Iteration-23 had null evidence in a generated mixed-width send/network tuple mapping. The
  actual code destructured both five-value network rows and seven-value send rows with one
  signature, losing some citations. Server validation rejected them. Isolated instructions
  require separate named-parameter helpers and defined citations; no evidence is synthesized.
- At iteration-23 parse ~645s, 83 steps and 179 dependencies exist, with zero completion
  reports yet. Candidate/runner remain frozen and the full performance/semantic goal is open.
