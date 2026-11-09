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
| `search_source(query, filters, cursor)` | Supplementary source retrieval; does not satisfy batch-read requirements. |
| `query_model(kind, filters, record_ids, detail, cursor)` | Definitions, internal IDs and source evidence. Reviewer must read every unfiltered detail page. |
| `apply_changes(expected_revision, operation_id, changes)` | Atomic parser-only add/update/delete operations; records need evidence; step IDs are server-assigned. |
| `complete_batch(batch_id, receipt_id, report)` | Report parsing work or explicitly justify finding no information. |
| `list_findings(filters, cursor)` | Findings, resolutions and decisions. |
| `get_changes(from_revision, to_revision, cursor)` | Complete revision diff and delivery tracking. |
| `submit_findings(reviewed_revision, findings)` | Reviewer-only source-backed findings. |
| `complete_review_batch(batch_id, receipt_id, report)` | Source/model review report, including explicit no-findings results. |
| `submit_resolutions(expected_revision, resolutions)` | Parser's applied/not_applicable/unresolved explanations. |
| `review_resolutions(reviewed_revision, decisions)` | Reviewer acceptance/rejection with reasons. |

Evidence entries are `{batch_id, block_id}`. Read the cited batch in the current assignment before using it. Changes contain `op`, `kind` for additions, `record_id` for updates/deletions, `value` and `evidence` for additions/updates, and a `reason` for deletion. Ambiguities can be stored as source-backed `kind=unresolved` records. The final schema remains unchanged.

## HC handoff

The supervisor starts a private core and parser/reviewer terminals, persists their real IDs and forwards assignments through `hc send`. It uses `hc wait` rather than polling screens. Only the MCP claim acknowledges work acceptance; terminal `done` alone cannot advance a gate.

The first message establishes the role and dispatch ID. Agents finish each turn after advancing their phase. A review requiring repair causes a message to the original parser terminal; submitting repair causes a message to the original reviewer. Both processes remain available throughout the normal loop.

A send intent is saved before delivery. An uncertain send, an exited/blocked agent, or an idle agent with an uncompleted claimed assignment pauses scheduling for inspection. On restart, the supervisor consults persisted IDs, claims and phase before sending anything. It never replaces another HC session or infers task success from screen text.

## Migration

The provider-driven compile pipeline, per-block extraction agents, linking/completion passes and their SDK dependencies have been removed. Use `rsc run`; `rsc compile` emits migration guidance. Parsing, indexing, protocol schema validation, statistics, and hand-annotated fixtures remain useful independently.
