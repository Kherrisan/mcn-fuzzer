# Persistent MCP extraction pipeline

The current implementation is `rsc.workflow`. Python controls the source set, phase, assignment, model revision and review round. Two independent interactive agent sessions supply extraction and semantic review. The service never asks an LLM to choose which phase may run next.

## State and gates

| Phase | Actor | Gate and next action |
|---|---|---|
| preprocess | parser | All inputs parsed; immutable snapshots match; chapter batches cover all nonempty text exactly once and fit token limits. Advance to parse. |
| parse | parser | Every batch read and reported; all model records have evidence; schema, IDs, references and channel roles valid. Freeze a revision and start review 1. |
| review | reviewer | Every assigned batch read/reported; complete model and required diff pages delivered; submitted resolutions decided. Publish, or assign repair if open findings remain and rounds permit. |
| repair | parser | All open findings have source-based resolutions; updated model passes hard gates. Freeze the revision and increment the round for review. |
| published | neither | Read-only; source/model changes require a new task. |

First review covers every source batch. Later review covers batches cited by changes and resolutions, retains the initial full-source review, and reads the current model and complete diff. Findings retain their history even when rejected, superseded or still unresolved. Parser resolutions never close a finding by themselves.

At the configured round limit (default 5, including the first review), a completed review with remaining semantic findings can publish a partial model. `rsc run --max-review-rounds N` overrides the config when creating a task. Resuming retains the persisted limit. Hard gate failures and unfinished reviews cannot be converted into partial success. `review_limit_reached` is recorded separately from `meta.complete`.

## MCP tools

The authenticated connection determines the role. A successful assignment claim is required before task tools. Phase and role checks execute server-side for every call, independently of the client's cached tool catalog.

| Tool | Purpose |
|---|---|
| `workflow_status()` | Phase, round, revision, assignment, currently allowed tools and gate blockers. |
| `claim_assignment(dispatch_id)` | Idempotently claim the exact current assignment. |
| `check_gate()` | Report blockers without advancing. |
| `advance_phase()` | Revalidate and submit this phase; server chooses the destination. |
| `prepare_sources()` | Parse selected inputs and freeze chapter batches. |
| `list_batches(filter, cursor)` | Paginated source batches and pending work. |
| `read_batch(batch_id)` | Complete source text plus assignment-bound receipt. |
| `read_batches(batch_ids, max_tokens, format)` | Complete source windows in source order, including nonadjacent IDs; lossless compact or records format. Only delivered batches receive receipts. |
| `search_source(query, filters, cursor)` | Supplementary source retrieval; does not satisfy batch-read requirements. |
| `query_model(kind, filters, record_ids, batch_ids, detail, cursor)` | Definitions, internal IDs and source evidence. Reviewer must read every unfiltered detail page; source filters do not satisfy that gate. |
| `apply_changes(expected_revision, operation_id, changes)` | Atomic parser-only add/update/patch/delete operations; legacy arrays or compact tables. Internal record IDs are server-assigned; steps are referenced by immutable names. |
| `complete_batch(batch_id, receipt_id, report)` or `(batches)` | Report parsing work or explicitly justify finding no information; atomic report arrays can arrive out of order. |
| `list_findings(filters, cursor)` | Findings, resolutions and decisions. |
| `get_changes(from_revision, to_revision, cursor)` | Complete revision diff and delivery tracking. |
| `submit_findings(reviewed_revision, findings)` | Reviewer-only source-backed findings. |
| `complete_review_batch(batch_id, receipt_id, report)` or `(batches)` | Source/model review reports, including explicit no-findings results; same atomic array support. |
| `submit_resolutions(expected_revision, resolutions)` | Parser's applied/not_applicable/unresolved explanations. |
| `review_resolutions(reviewed_revision, decisions)` | Reviewer acceptance/rejection with reasons. |

Evidence entries can be short citations copied from source segments (for example `"e42"`)
or legacy `{batch_id, block_id}` objects. Citations identify exact pairs even when different
documents reuse block names. The server expands citations before validation and persists
canonical objects only; a citation does not replace a current-assignment reading receipt.
Changes contain `op`, `kind` for additions, an existing-record selector for edits, `value` and
`evidence` for additions/updates, and a `reason` for deletion. Ambiguities can be stored as
source-backed `kind=unresolved` records. The final protocol schema remains unchanged.

Named bulk mutations use `changes={groups:[{op,kind,defaults,rows}]}`, where each row is
`{value:{...},evidence:["e42"]}`. Optional `defaults` contains shared value fields only;
each row's value overrides those fields. Expanded values pass the same complete validation.
Keep send and network-action variants in separate groups. Named rows avoid positional errors.

Legacy compact mutations use `changes={evidence:[{batch_id,block_id}],groups:[{op,kind,columns,rows}]}`.
Each row maps column names to value fields. `@evidence` contains indexes into the evidence
dictionary, `@record_id` selects an existing record, and `@reason` explains deletion. Cells
retain their native JSON types, including nested arrays, objects and null. Expanded changes
and compact changes share schema/evidence validation, atomicity and idempotence; the tool
description includes exact nested value shapes for clients that hide JSON Schema references.
Mutation results return names/record IDs and incremental draft-reference errors. Draft forward
references are allowed, but all final reference gates remain mandatory.
`@evidence` also accepts source citation strings directly; the evidence dictionary can then
be omitted. Ownership and receipt failures across mutation rows are reported together before
any record is changed. Source bodies retain all original text and metadata.

Edits select exactly one record with `kind` and `target:{name:"exact name"}`, or a legacy
`record_id`. Dependencies can select by `prior_step`, `dependent_step` and, when needed,
`condition`. Targets match existing value fields exactly against the transaction's starting
snapshot; missing/ambiguous targets fail atomically. Cached bindings preserve operation retries
after rename/deletion, including retries of older ID-based operations. There is no fuzzy matching.

`update` replaces the entire value. `patch` takes a selector, a nonempty partial `value`,
and current-source `evidence`. It shallow-merges fields with the existing record, replaces
whole arrays, and retains original evidence plus new evidence without duplicates. The merged
record passes the same complete schema, immutable-step-name, size and final-reference checks.
New evidence must have a current-dispatch receipt. Both formats support patch; compact patch
groups include `@record_id`, `@evidence`, and only the value columns being changed.

Batch report arrays contain `{batch_id,receipt_id,report}` entries. Reports are persisted in
source order; every required batch still needs its own complete source delivery and report.
Each report needs a source-specific summary; `record_ids` and `finding_ids` are optional and
validated when supplied.

Receipts remain usable across model revisions within the same dispatch. Filtered model queries
and supplementary source reads never replace required full-review coverage. Phase-transition
results expose `stop_turn`; successful advancement always ends the current agent turn.

Parser instructions read all assigned sources in bounded complete windows before constructing
the canonical model. Each window is printed separately and cached with its receipt; callers
follow `read_batches.remaining`, since unreported batches are not necessarily unread batches.
The complete canonical inventory of parties, actual message types and channels is committed
before declaring reusable steps and constructing dependencies coherently against all cached
sources. Related groups may use separate RPCs. Every source needs its actual extraction report;
unfinished work remains pending, and all original independent-review gates apply. An unconditional ordering edge uses an empty condition; additional state,
branch and comparison guards remain explicit. Source-required new reconnect/resumption
lifecycles use distinct channel names and corresponding step references. This changes work order, not the complete-source, schema/reference or independent
review requirements. Supervisor forwards the actual round: the initial review reads the current snapshot without
historical diffs, while later rounds still read every model/diff page and resolution. Tool
discovery prints one exact namespace/name description, and page bodies use separate exec cells.
Repair reads the union of cited sources and batches independent supported patches/resolutions.
Named mutation rows expose their metadata fields in the tool schema;
misplaced fields produce concise per-row errors instead of dumping the whole proposal.

## HC handoff

The supervisor starts a private core and parser/reviewer terminals, persists their real IDs and forwards assignments through `hc send`. It uses `hc wait` rather than polling screens. Only the MCP claim acknowledges work acceptance; terminal `done` alone cannot advance a gate.

The first message establishes the role and dispatch ID. Agents finish each turn after advancing their phase. A review requiring repair causes a message to the original parser terminal; submitting repair causes a message to the original reviewer. Both processes remain available throughout the normal loop.

A send intent is saved before delivery. An uncertain send, an exited/blocked agent, or an idle agent with an uncompleted claimed assignment pauses scheduling for inspection. On restart, the supervisor consults persisted IDs, claims and phase before sending anything. It never replaces another HC session or infers task success from screen text.

## Migration

The provider-driven compile pipeline, per-block extraction agents, linking/completion passes and their SDK dependencies have been removed. Use `rsc run`; `rsc compile` emits migration guidance. Parsing, indexing, protocol schema validation, statistics, and hand-annotated fixtures remain useful independently.

## Batch navigation and full delivery

`list_batches(detail=true)` returns only the current dispatch's already-issued receipts.
It never issues receipts, counts as reading, or completes reports. After completing reports
with a pending filter, restart at cursor zero because the filtered collection has changed.

`read_batches(include_evidence=false)` retains every original named source field and short
citation, omitting only the redundant derived evidence object. The default retains that object;
receipts and canonical block ownership are unchanged. The server selects complete batches
within the requested budget and returns the remaining IDs for subsequent reads.

Instructions request named model records and cache every complete page before filtering.
Source filters and indexes help navigate records but do not satisfy the full-model review gate.
First review reads all original sources and the frozen model; later reviews also read the full
diff, affected sources and resolutions. Reports describe actual source/model comparisons.

## Connection-role consistency

For connection-oriented channels with known roles, `connect.party_name` must match
`initiator_party_name`, and `listen.party_name` must match `acceptor_party_name`. Unknown roles
remain unknown, and either endpoint may close. Violations appear in draft-reference feedback
and block phase advancement and publication. Reversed source-supported roles need distinct
channels and their own steps.

## Deterministic coordinator preprocessing

The supervisor performs the existing claim/prepare/advance tools for a fresh preprocessing
assignment, after launching its two agents. It journals ownership before RPC. A lost reply can
resume by inspecting persisted source/phase state; already claimed or uncertain agent
assignments keep their original ownership. Source, snapshot and batching gates still apply.
This creates no reading receipts, extracted records or parsing reports. Coordinator-origin
events are labelled separately from agent calls.

## Removed experiment options

Source preplanning, overlapping reviewer preparation, source-fact sharing and construction-window
hints have been removed. The three experiment config keys (`require_source_plans`,
`prefetch_review_sources`, `share_source_requirements`) and completion arguments `stage` and
`planned_batch_ids` are no longer accepted. Start a fresh task with the simplified config;
use each experiment's frozen package to reproduce or resume its old workflow. Historical
artifacts and their offline delivery-audit support remain available. The MCP tool count is 18.
