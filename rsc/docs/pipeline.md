# Parallel source extraction and independent review

One `rsc.workflow` service owns the SQLite state and write connection. The coordinator advances global phases; workers submit only their own assignments. Parse and Review each default to three workers and run in separate phases. Merge reuses one Parser. Setting both worker counts to 1 uses the same implementation.

## Source ownership and phases

The caller's document order, chapters and original text positions determine the source batch list. Batches preserve chapters where possible; oversized chapters split at contiguous subsections/paragraphs and oversized paragraphs at Unicode boundaries. Every nonempty source range occurs exactly once. Batch budgets include delivery metadata; scheduling weights use raw source tokens.

Each role receives `min(workers, batches)` nonempty contiguous ranges. Code first minimizes the maximum range weight M, then permits at most 110% of M to minimize chapter cuts, document cuts and finally cut-index order. Assignments are fixed, without work stealing or an extra source-task layer. Workers process their batches in order. Supplementary reads never transfer reporting ownership.

| Phase | Work and submission gate |
| --- | --- |
| Preprocess | Coordinator freezes sources and verifies coverage/token bounds before creating assignments. No agent calls or receipts. |
| Parse | Private drafts; each assigned batch needs full delivery and an extraction report. Local schema/evidence are mandatory; foreign forward references may wait for Merge. |
| Merge | Deterministic assembly of sealed inputs, then one Parser resolves scoped conflicts and cross-range obligations. Names, references, roles, schema, evidence and report associations must pass before freezing. |
| Review | Every source batch and model record has an independent owner. Reviewers compare requirements and model contexts, finish cross-range checks and decide assigned resolutions. |
| Fix | Findings with overlapping known objects are grouped; independent groups produce private patches from the same frozen base. Every finding needs a resolution at the final local revision. |
| Published | Complete or explicitly partial protocol and audit artifacts; no further agent work. |

Worker `advance_phase` returns `stop_turn`; only the coordinator advances globally after all submissions and gates pass. The default five-review limit counts the initial Review; transport retries do not consume rounds. At the limit, remaining semantic findings produce explicit unresolved entries. Missing reports, unreviewed resolutions or engineering/schema/reference failures cannot be waived.

## Scoped names and merge

Candidates retain `scope + kind + local name`, with an assignment scope such as `parse-0/parser-2`. Scope is absent from final protocol names. References prefer their own draft's definitions. Missing or ambiguous foreign references remain conflicts. Only exactly equal definitions with identical unique bindings deduplicate automatically, retaining evidence and origins; deduplication repeats after upstream references unify.

The conflict view includes same-name differences, missing/ambiguous references, structural violations, overlapping Fix writes and reported obligations. It does not claim to detect all synonyms or natural-language contradictions. The merge Parser consults relevant original sources; independent Review still checks the result.

Merge-only `rename` and `merge` atomically update definitions, typed references, dependency endpoints, qualified field relations and origin mappings. Potential prose references become explicit checks, never blind replacements. `resolve` records an evidence-backed decision for a registered check/obligation; it cannot waive structural conflicts. Original extraction reports stay sealed. Their association view follows final names or explicit deletion provenance. Fix patches reuse the same mechanism; original resolutions retain current-name/deletion associations for acceptance.

## The 13 agent tools

Credentials bind worker, role, assignment and attempt. Before claim, the phase catalog offers only `claim_assignment`. Its response includes revision, batches, pending reports/checks/findings and `allowed_tools`.

| Tool | Contract |
| --- | --- |
| `claim_assignment(name, attempt)` | Claim exactly the dispatched assignment; safe repeated claim. |
| `advance_phase()` | Validate and seal this assignment; end the turn on success. |
| `list_batches(assigned_only, cursor, revision, max_tokens)` | Source locations, ownership and progress; no receipt. |
| `read_batches(batch_ids, max_tokens)` | Complete batches with attempt-bound receipts; follow `remaining`. A one-element list reads one batch. |
| `search_source(query, batch_ids, cursor, revision, max_tokens)` | Supplementary context; snippets do not replace complete reading. |
| `query_model(view, kind, filters, batch_ids, scope, check_name, cursor, revision, view_revision, max_tokens)` | Current draft/frozen model, sealed drafts, name index, merge conflicts or report associations. |
| `get_changes(from_revision, cursor, revision, max_tokens)` | Draft changes and merge decisions, or frozen before/after records. |
| `apply_changes(expected_revision, operation_id, changes)` | Atomic named mutation array with exact selectors; only Parser drafts are writable. |
| `complete_batch(revision, reports)` | Ordered extraction/review reports or assigned cross-range conclusions. |
| `list_findings(cursor, revision, max_tokens)` | Findings, resolutions, independent decisions and current resolution associations. |
| `submit_findings(revision, findings)` | Source-backed discrepancies and acceptance criteria; returns qualified finding names. |
| `submit_resolutions(revision, resolutions)` | Fix proposals: `fixed`, `not_applicable` or source-supported `unresolved`; never self-acceptance. |
| `review_resolutions(revision, reviews)` | Independent acceptance/rejection against current source/model context. |

Optional paging arguments can be omitted. Pages default to 8,000 tokens and at most 50 items, with requested budgets up to 30,000. Continue `next_cursor` at the same revision. Sealed pages additionally return `view_revision`, pinning the sealed-draft list while other workers finish. Index pages do not prove full model delivery. The Merge-only reports view keeps Parser reasoning separate from reviewer evidence.

Catalog changes generate MCP notifications. Cached clients may use `--catalog role`; all calls still check phase permissions. Coordinator operations `prepare_sources`, `workflow_status`, `record_send`, `replace_attempt` and global advancement have a separate credential and no agent schemas. Removed `read_batch`, `check_gate` and `complete_review_batch` entry points cannot execute.

## Named operations and reports

Only one mutation encoding is accepted:

```json
{
  "expected_revision": 0,
  "operation_id": "define-client",
  "changes": [{
    "op": "add", "kind": "parties",
    "value": {"name": "Client", "description": "Request sender"},
    "evidence": ["e1"]
  }]
}
```

Use actual returned citations, or exact `{batch_id, block_id}` pairs with complete delivery in this attempt. `update` replaces a whole value; `patch` supplies nonempty changed fields, replaces arrays in full and preserves evidence. `delete` needs an exact target and reason. Targets use value fields, usually `{name: ...}`; dependencies use exact endpoints/condition/field relationships. Missing/ambiguous selectors fail.

Merge edits also specify scope. `rename` uses `source:{scope,kind,target}`, `name` and evidence; `merge` uses `sources:[...]`, final `value` and evidence. `resolve` uses a registered check's `name`, `decision` and evidence. Ordinary Parse and Fix cannot use these operations.

Independently checkable row errors are collected and the whole mutation rolls back. An operation ID binds identical arguments to one assignment attempt. Compact columns, evidence indexes, grouped defaults and old record IDs are not accepted; historical decoders remain audit-only.

Source reports are `reports:[{batch_id,receipt,report}]`. Parse reports contain `summary`, `objects:[{kind,target}]` and optional `obligations:[{description,objects,evidence}]`. They describe completed extraction, not future work. Cross-range Review uses `{check_name,report}`. Review reports include actual `requirements:[{requirement,objects,result,findings}]` and optional absence `queries:[{kind,filters}]`. Every owned model record must be compared. A receipt alone creates no pass; unfinished extraction is not source ambiguity.

## Independent review and reuse

Reviewers can read any source/model region and use the global name index to detect omissions. Assigned contexts contain source-associated objects and reference closure. Cross-range checks own every model record exactly once. Findings preserve requirement, discrepancy, objects, evidence and acceptance criterion. Missing-object claims need complete delivery of the searched scope, including empty results.

Conclusions retain source/model dependencies and negative query scopes. Changes invalidate affected records and reverse references, related source reports and relevant absence queries. Uncertain prose changes expand review conservatively. Carried conclusions preserve their predecessor and original delivery proof, without fresh fabricated receipts; new ownership/context must still fit that proof. Resolutions always require independent acceptance, even if all other reports carry forward.

## Persistence and recovery

Storage version 2 separates assignments, attempts, local records/history, frozen snapshots, receipts/reports, conclusions and findings. Short transactions exclude reasoning, HC calls and page serialization. Revision and attempt checks fence writes; another worker does not invalidate a local revision.

Workers have private launch settings, rotating credentials and separate MCP namespaces. The supervisor uses `hc` with native Codex/Pi adapters. Initialization/ready checks are serial; dispatch is parallel and waits rotate. HC terminal and Codex thread names match. Phase transitions release processes and preserve named sessions for restoration.

Send intent precedes `hc send`; MCP claim acknowledges receipt. Restart consults durable state rather than screen text. An uncertain response is not blindly resent. Confirmed exited/missing terminals permit at most two replacements per assignment, revoking the old attempt first and retaining draft/report progress. New attempts need new source receipts. Observation timeout, blocked status or an unfinished idle turn requires diagnosis and never permits a concurrent replacement.

Resume the same `rsc run --task-dir ...`. Sources, worker counts, profiles and review limits stay fixed. `--retry-worker parser-1` refuses a live process and cannot bypass the retry limit. Task-local locks reject competing services/supervisors. Inspect the private core using `hc_env` in `supervisor.json`; cleanup must target only that campaign.

## Migration and validation

New tasks use the current config. Historical singleton databases are not upgraded in place: reproduce/resume using their archived source and original config. `rsc audit` retains historical read-only auditing. New contracts do not execute old tool names/encodings. Current phase vocabulary is Fix/`fix`; historical artifacts retain their original terminology.

Audit reconstructs mutations and frozen merges, ownership, evidence, review conclusions and independent decisions, then compares exported artifacts with authoritative state. This proves delivery/workflow invariants, not semantic completeness. Phase metrics distinguish agent/supervisor/health calls and record payload tokens, repeated deliveries/queries, conflicts, fresh/carried conclusions and claimed-worker time. These do not measure API cost or inference utilization.

Ordinary pytest uses fixtures and simulated HC. Opt-in `scripts/benchmark_parallel_rsc.py --out runs/new-trials` freezes code and FTP RFC inputs, then runs three fresh `gpt-6-luna/high` trials with a 40-minute limit each. The 20-minute target additionally requires complete publication and the FTP semantic checklist. Timeouts, partial models and failed semantic inspection remain failures; there is no unlimited tuning loop.
