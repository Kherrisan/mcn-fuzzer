# Parallel Parse/Review acceptance — in progress

This is a validation record, not a completion claim. The governing requirements remain
[`plans/rsc-parallel-parse-review.md`](../../../plans/rsc-parallel-parse-review.md).

## Real FTP trials

Three sequential RFC 959 + RFC 3659 trials started on 2026-11-10. Each uses
`gpt-6-luna/high`, three Parse workers, three Review workers, five maximum Review rounds,
8,000/12,000-token source batches and a 2,400-second diagnostic deadline. The target is
complete, semantically accepted publication within 1,200 seconds. A timeout, partial model
or failed semantic inspection is a failure, not a fast successful run.

Raw output (ignored by Git):

`rsc/runs/parallel-ftp-20261110T053039Z/`

`manifest.json` identifies the frozen code/input/configuration snapshot; `current.json`
identifies the current trial. The runner writes per-trial `timing-result.json` and an aggregate
`results.json` on completion. All three trials use that same frozen code. Subsequent worktree
fixes must not be described as having run in this snapshot.

The first trial, `ftp-1110053040-r1`, timed out at **2,400.009 seconds**, with exit `-15`,
during the first Fix. No protocol was published. It failed the timing/publication acceptance;
semantic acceptance was not reached. Trial 2 also timed out; trial 3 remains pending.

| Phase | Round | Wall seconds | Completed |
| --- | ---: | ---: | --- |
| Preprocess | 0 | 1.2 | yes |
| Parse | 0 | 839.3 | yes |
| Merge | 0 | 354.6 | yes |
| Review | 1 | 1,150.9 | yes |
| Fix | 1 | 54.1 | no; interrupted at deadline |

The following Parse measurements come from its actual batches, drafts and attempt timestamps:

| Worker | Raw source tokens | Batches | Claimed-to-submitted seconds | Local model records |
| --- | ---: | ---: | ---: | ---: |
| parser-1 | 15,953 | 7 | 269.1 | 236 |
| parser-2 | 17,583 | 12 | 797.9 | 272 |
| parser-3 | 19,784 | 10 | 182.5 | 36 |

Balanced raw text does not guarantee balanced extraction work. These measurements identify a
Parse long tail and a nontrivial subsequent Merge; they do not establish semantic quality or
justify changing the agreed contiguous chapter partition algorithm. Claimed-worker time measures
assignment occupancy, not inference utilization. Merge started with 436 entries: 7 same-name
conflicts, 416 structural errors and 13 reported obligations. The count is not 436 independent
semantic defects; shared reference errors can affect many downstream records.

| Phase | Notable agent calls/rejections | Repeated delivered items |
| --- | --- | ---: |
| Parse | 34 `apply_changes`, 14 rejected; 4 `complete_batch`, 1 rejected | 498 |
| Merge | 10 `apply_changes`, 6 rejected; 12 `query_model` | 1,632 |
| Review | 220 `query_model`; 48 `complete_batch`, 31 rejected; 8 `submit_findings`, 1 rejected | 4,339 |
| Fix (partial) | 5 `query_model`, 2 rejected | 11 |

Review's 48 report calls stored 32 source/check reports. Of its 31 rejected report calls,
17 lacked the declared complete negative-query scope, 7 lacked associated model context,
2 had schema errors, 2 omitted owned model records, and one each lacked comparisons, used
the wrong source order or used an unresolved-object selector that matched no record.
The schema errors echoed large report payloads, while delivery failures omitted exact query
parameters. Those interface costs are measurable; they do not by themselves explain all
1,150.9 seconds or justify removing delivery/independence gates.

The final native Codex usage counters across six sessions were 58,755,174 input tokens
(56,543,616 cached), 236,446 output tokens (105,447 reasoning), and 58,991,620 total tokens.
Cached/reasoning counters are components of the native totals, not additional tokens to add.
MCP payload counters are separate and must not be treated as API usage or monetary cost.

### First-trial semantic observations

There were 9 open findings at timeout, including three reviewers reporting overlapping MLSD
endpoint issues. Selected findings were independently checked against frozen model revision 1
and the saved source blocks; this is a failure analysis, not acceptance of an unpublished model:

| Source checked | Frozen model evidence | Assessment |
| --- | --- | --- |
| RFC 3659 §7.2, RFC 959 §3.2 and PASV in §4.1.2 | `FTP MLSD data connection` has PI endpoints and null initiator/acceptor; `Send MLSD data listing` and its close use PIs | Incorrect. MLSD delegates data setup to RFC 959; the DTP endpoints and active/passive roles are specified. They cannot be left as an extraction obligation labelled source ambiguity. |
| RFC 3659 §7.3.1 | The successful `Send MLST command` → `Send MLST control response` edge equates pathname fields | Too strict. TVFS permits a fully qualified path for the same file; object identity does not imply raw pathname equality. |
| RFC 959 §5.2 third-party transfer example | `FTP control connection to Server A/B` exist but have no channel-specific network steps | Missing lifecycle. The source explicitly establishes both control connections before PASV/PORT and transfer commands. |

Remaining checklist areas were not accepted. Fix had just begun and no independent resolution
acceptance had completed.

### Second trial

`ftp-1110061041-r2` timed out at **2,400.009 seconds**, exit `-15`, during the first Review.
It did not enter Fix or publish a protocol, so it failed timing/publication acceptance. All 29
source reports were stored, but only reviewer-3 completed its cross-range check and assignment;
reviewer-1 and reviewer-2 still had their cross-range checks pending. There were 9 open findings.

| Phase | Wall seconds | Notable calls/rejections | Repeated delivered items |
| --- | ---: | --- | ---: |
| Preprocess | 1.2 | coordinator only | 0 |
| Parse | 721.3 | 35 `apply_changes`, 12 rejected; 7 report calls, 4 rejected | 204 |
| Merge | 536.7 | 486 initial entries; 14 `apply_changes`, 4 rejected; 23 model queries | 848 |
| Review (partial) | 1,140.9 | 205 model queries; 55 report calls, 34 rejected | 6,356 |

Parser claimed times were 679.9 / 614.4 / 318.9 seconds, with 272 / 362 / 46 local records.
Review's rejected reports comprised 8 schema errors, 9 missing model contexts, 8 missing
negative-query scopes, 3 source-order errors, 3 missing/ambiguous selectors, 2 incomplete owned
record comparisons and 1 missing requirement comparison. Thirty conclusions were stored
(29 source reports plus one cross-range report); 55 calls do not mean 55 completed reports.
There were 20 repeated model queries. Native usage was 57,039,844 input tokens (54,439,040
cached), 271,080 output tokens (120,972 reasoning), and 57,310,924 total tokens.

Selected findings were checked against saved sources and frozen revision 1:

| Source checked | Frozen model evidence | Assessment |
| --- | --- | --- |
| RFC 3659 §7.3.1 | `send MLST command` → `send MLST listing reply` equates the pathname fields when the argument is supplied | Again excludes the allowed fully qualified TVFS representation of the same file. |
| RFC 3659 §§5.3/5.5; RFC 959 §5.2 | `Server DTP opens resumed active data connection` has only outgoing data-send edges; passive resumed listener/connect steps are linked to each other and later data sends, but not the restarted transfer request or PASV setup | The distinct resumed channels exist, yet the restart/endpoint setup is not connected to their lifecycle. Merely adding channel names did not complete this requirement. |
| RFC 3659 §7.2; RFC 959 §§3.2/3.3 | `MLSD data connection` and `MLSD PASV data connection` use DTP roles but have empty endpoint sources; the PASV connect depends only on the MLSD command, with no endpoint reply prerequisite | Endpoint roles improved relative to trial 1, but the passive host/port source and setup relationship are missing. |

These observations establish failures in the unpublished model, not a completed semantic audit.
The direct-terminal-role finding and other open findings have not been accepted merely because
reviewers reported them. Trial 3 uses the same frozen code and will be assessed separately.

### Third trial: intermediate observation

At 2026-11-10 07:19:45 UTC, `ftp-1110065041-r3` was still running, in its first Review,
after 1,742.7 seconds. Preprocess took 1.2 seconds, Parse 514.9 seconds, and Merge 1,066.4
seconds; Review had run for 160.1 seconds. This already misses the 20-minute target, but
it is not a final outcome. The process remains subject to the same 2,400-second deadline.

Merge began with 496 candidate records and 351 entries: 8 same-name conflicts, 328 structural
errors and 15 obligations. At submission, the 15 obligations and 11 generated free-text checks
were resolved and the conflict list was empty. The frozen model has 442 records. Observed
rejections included merge-argument shape, unreceived source batches, stale/sealed-view revisions,
insufficient page budget, removed candidate selectors and selecting one candidate twice.
The smaller Parse time did not translate to a shorter complete run: Merge was the dominant
completed phase. These observations describe the frozen implementation, not a timing test of
the later scoped-diagnostic changes.

Independent checks of frozen revision 1 against the saved RFC text found:

| Source checked | Model evidence | Assessment |
| --- | --- | --- |
| RFC 3659 §7.3.1 (`blk-000216`) | `User-PI sends MLST command` → `Server-PI sends MLST control response` allows a fully qualified equivalent pathname and has no raw-path equality relation | This specific defect from trials 1–2 is absent. It does not establish complete MLST acceptance. |
| RFC 959 §5.2 (`blk-000462`, `blk-000465`) | Separate `Listen/Connect FTP control to server A/B` steps; both connects precede `Third party PASV A` | The missing A/B control setup seen in trial 1 is represented here. This is not acceptance of the whole third-party transfer model. |
| RFC 3659 §7.2 (`blk-000202`–`blk-000203`), RFC 959 PASV §4.1.2 (`blk-000316`) | `Server-DTP sends MLSD listing entry` uses only `FTP data, server initiates`; the MLSD request leads to the active connect. The passive download step explicitly covers only RETR/LIST/NLST | Passive MLSD is omitted; the presence of a generic passive channel does not complete its MLSD setup and data path. |
| RFC 959 §3.2 (`blk-000213`, `blk-000215`–`blk-000216`) | `FTP passive data close by user` requires first seeing the server close; `Passive transfer user to server` leads to the server close | This fails to represent the user-as-sender exception when closing signals EOF. A server-first close cannot substitute for that upload termination. |

Review is ongoing, and these are selected checks of its input, not acceptance of a published
protocol or a prediction that the reviewers will miss the same issues. Final timings, findings,
any later model revision and publication status still need inspection.

### Observed workload and interface costs

The Review assignments retain unique ownership, but their model loads differ considerably:

| Trial / reviewer | Assigned source batches | Owned records | Cross-range context records | Cross-range source batches |
| --- | ---: | ---: | ---: | ---: |
| 1 / reviewer-1 | 7 | 259 | 288 | 10 |
| 1 / reviewer-2 | 12 | 234 | 249 | 12 |
| 1 / reviewer-3 | 10 | 33 | 36 | 7 |
| 2 / reviewer-1 | 7 | 302 | 340 | 15 |
| 2 / reviewer-2 | 12 | 312 | 365 | 14 |
| 2 / reviewer-3 | 10 | 27 | 47 | 11 |

These counts come from the stored Review assignments. Context includes referenced definitions;
it is not an additional ownership claim. Balanced source tokens therefore do not balance the
number of model records to inspect. In trial 2, reviewer-3 occupied 52.0% of the Review phase,
while reviewer-1/2 occupied 96.4% each. In trial 1 those fractions were 80.8%, 96.2% and 91.1%:
even the smallest model assignment was not proportionally faster. Record count alone cannot
explain the elapsed time; source reading, missing-model checks and report retries also matter.

Review delivered 934,949 / 1,558,271 MCP payload tokens in trials 1 / 2, including repeated
delivery. This is separate from the native API token totals above. Trial 1's aggregate agent
tool execution time was 70.9 seconds across concurrent reviewers, against 1,150.9 seconds of
Review wall time. It is not valid to subtract this concurrent sum to obtain exact inference
time, but SQLite/tool execution alone does not account for the long Review critical path.

All observed worker catalogs contained 11 tools (the role union from the 13-tool total).
Review recorded 345 / 319 catalog requests and 2 / 9 argument-schema errors. Catalog requests
are recorded separately from the agent tool-call counts; supervisor status observations are
also separate. Review report calls produced 32 / 30 conclusions, all fresh, with zero carried
conclusions. Neither run reached incremental re-review, so these trials provide no measured
incremental-review speedup. Trial 1 started its first Fix; trial 2 did not reach Fix.

Each published model needs a separate inspection against the
[FTP semantic checklist](../ftp-runtime-stability/semantic-checklist.md), including original
sections, supporting model names, accepted alternatives and actual failures. The runner's
`semantic_audit=pending` is not a pass. All three outcomes and the remaining bottlenecks must
be reported; missing the target does not start an unlimited tuning loop.

## Implementation evidence

- Contiguous chapter-aware partitioning and source coverage: `tests/test_batch_partition.py`
  and the source/evidence tests. Worker counts default to three; one uses the same architecture.
- Worker identity, attempts, independent drafts, idempotence and thirteen tool contracts:
  `tests/test_workflow_store.py`, `tests/test_named_mutations.py` and
  `tests/test_parallel_workflow.py`.
- Scoped duplicate handling, atomic rename/merge, field-reference changes, original report
  associations and explicit deletion provenance: `tests/test_scoped_merge.py` and
  `tests/test_parallel_workflow.py`.
- Independent Fix acceptance after a merge rename/deletion, including unchanged base objects:
  `tests/test_workflow.py`. Resolutions stay immutable and expose their current associations.
- Positive dependencies, missing-object query scopes, carried proof and conservative prose/name/
  field invalidation: `tests/test_incremental_review.py` and end-to-end workflow tests.
- Fair dispatch, serialized initialization, lost responses, exact native restoration and bounded
  recovery: `tests/test_parallel_supervisor.py`, `tests/test_workflow_supervisor.py` and MCP tests.
- Cross-range obligation contexts through rename/deletion, successive Fix rounds and merge
  restart: `tests/test_review_context.py`. Original reports/resolutions remain immutable.
- Independent report errors, compact field diagnostics, precise read hints and atomic retry:
  `tests/test_report_validation.py`. Frozen-model selector normalization before Fix grouping:
  `tests/test_incremental_review.py`.
- Offline mutation replay, source/model delivery, complete query pages, independent decisions,
  HC send-before-claim evidence and publication/export consistency: `workflow/audit_parallel.py`
  and tampering regressions. Programmatic runs without HC artifacts explicitly report that
  HC handoffs were not verified.
- Phase/origin metrics, current output artifacts and owned-process cleanup:
  `tests/test_parallel_metrics.py`, `tests/test_parallel_benchmark.py`. Payload tokens and
  claimed-worker occupancy are distinguished from model API token usage and inference time.
- Current configuration, thirteen named contracts, migration and recovery documentation:
  [README](../../README.md), [pipeline](../../docs/pipeline.md) and `config.example.toml`.

The implementation and tests were checkpointed in `c3f55df7` (274 tests and 78 subtests passed),
then `62964270` with the subsequent diagnostics, shared validator and regression tests.
Subsequent changes improve report diagnostics, normalize Fix selectors and distinguish failed
workflow audits from partial publication in benchmark outcomes. Exact Codex session restoration
has simulated tests and a read-only lookup against a real saved session; its current launch path
still requires a live recovery check. These later fixes are **not** in the three frozen FTP trials.
The requirement-by-requirement completion audit and final real acceptance results remain open.

Current full-suite verification: **289 tests and 78 subtests passed in 50.04 seconds**.
The namespace-race regression verifies that two concurrent campaigns
whose truncated HC names collide cannot both acquire the namespace, and durable ownership
continues to reject the loser after initialization. Documentation now matches the actual
24,000-token default / 30,000-token maximum page budget, without a fixed 50-item page limit.

Structural conflict detail now comes from the shared reference validator, including affected
definitions, value paths, source evidence and reverse references. Scoped Merge validation uses
the bound local objects instead of selecting a global same-name definition by insertion order.
On the **same first-trial initial merge seed**, current diagnostics yield 20 entries (7 same-name
conflicts and 13 obligations), instead of the frozen runner's 436. The 416 removed entries were
repeated global-name/reference fallout; the name conflicts still block freezing. Regression
tests retain real role/field failures and ambiguous foreign-reference blockers. This is an
offline comparison of diagnostics, **not** a new measured Merge runtime.

The recovery probe now waits for a committed model write before interrupting the Parser, and
checks retained history and report counts after replacement. Its simulated test includes a
completed source report. The real check remains pending until the FTP timing trials finish.

## Requirement audit

This maps the plan to inspected code and exercised assertions. Unit/integration evidence proves
the stated engineering behavior, not the quality or duration of an uncompleted LLM trial.

| Plan requirement | Implementation and evidence | Remaining qualification |
| --- | --- | --- |
| Separate Parse/Review phases; defaults 3/3; positive configurable counts and the same path for 1 | `Workflow.create`, `source_assignments`, `Supervisor.step`; workflow/CLI/partition tests | Real single-worker recovery run is queued. |
| Preserve final protocol shape and named agent objects | Existing v8 schema/validator; named mutation contracts, scoped `display` strips storage keys; protocol and named-mutation tests | Original source-integrity metadata is preserved; no new hash-based object identity. |
| Ordered, bounded source batches with exact nonempty coverage, chapter/subsection/Unicode splits | `sources.py`, source/citation tests and `test_batch_partition.py` | Actual FTP inputs frozen once per three-run series. |
| Contiguous token partition: optimal M, integer 110% bound, chapter/document/cut tie-breaks | `partition.py`; exhaustive independent cut enumeration through 8 batches and 4 workers, large-batch/no-empty tests | Token balance does not guarantee equal extraction work; real worker times reported above. |
| Fixed ownership, supplementary reads, independent drafts/revisions, sealed inputs | `Store`, `Worker.read_batches`, pinned sealed views; concurrent-writer, ownership and sealed-page tests | No work-stealing queue or per-source task table. |
| Attempt-bound credentials and receipts; retain drafts/reports after confirmed failure | Store replacement/credential tests, supervisor retry tests and recovery-probe fixture | Current native-session recovery still needs the queued real run. |
| Source-order deterministic assembly, local-first references, ambiguous foreign candidates | `merge.combine`/`bind`; scoped-merge tests, including reversed input order and distinct bindings | Ambiguity stays a blocker. |
| Strict deduplication, upstream changes uncover downstream duplicates, unequal dependencies retained | `merge.automatic`; duplicate/identity/condition regressions | No synonym or condition-equivalence inference by code. |
| Conflict types, differing fields, reference paths, scopes, affected records and evidence | `merge.conflicts`, shared `reference_issues`, named Fix writes; scoped conflict tests | The list is not a complete semantic defect detector. |
| One reused Parser resolves Merge, explicit source reads, no blanket structural waivers | Merge phase permissions and gates; source/evidence tests and `resolve` rejection tests | Actual frozen trials exercised this phase; current detailed diagnostics are later changes. |
| Atomic scoped merge/rename, all typed references and field qualifiers, original report mappings | Merge/mutation transaction paths; rollback, alias, dependency/field and report-immutability tests | Prose stays unchanged until explicit source-backed checks resolve. |
| Freeze only complete, structurally valid merged input | Coordinator/worker gates and `final_records`; unfinished/invalid/tampered-source tests | Structure alone never marks semantic acceptance. |
| Source-based Review plus unique model/check ownership and supplementary global queries | `assign_checks`, `required_context`, `review_gate`; delivery, orphan-conclusion and context tests | Raw Parser explanations remain Merge-only. |
| Cross-range obligations survive rename/deletion and successive Fix; unknown links expand context | `review_links`/name maps; `test_review_context.py`, including two Fix rounds and merge restart | Reviewer must still make its own comparison. |
| Actual comparisons and complete source/model/query delivery, correct revision/attempt | `prepare_report`, `review_errors`, live/offline absence-query checks; report and tampering tests | Read receipts prove delivery, not semantic truth. |
| Group known overlapping Fix objects; independent patches on a frozen base; detect new overlaps | `fix_groups`, `merge_inputs`; selector normalization and `test_parallel_fix.py` | The integration test verifies two simultaneous patches, shared-write conflict, preserved base/drafts and independent re-review. |
| Incremental positive/reverse/source/negative-query dependencies, conservative prose fallback | `invalidated`, `carry_forward`; incremental tests and carried-proof tampering | Reuse requires the new ownership/context to fit the predecessor's proof. |
| Independent resolution acceptance, immutable Fix explanation through later names/deletions | `resolution_associations`, `review_resolutions`; workflow/context tests | Review limits may yield explicit partial publication; missing engineering gates may not. |
| One service/write entrance, normalized state, short transactions, version checks and idempotence | `TaskServer`, `Store`, workflow lock and transaction boundaries; socket/concurrency/restart tests | HC/model inference and response serialization are outside SQL write transactions. |
| Exactly 13 agent tools; coordinator isolation, phase catalogs, notifications and cached-client permissions | `tools.py`, server/stdio bridge; schema/catalog/coordinator/removed-tool tests | Historical compact/old-tool data remains audit-only. |
| Named arrays/records, exact selectors, independent error aggregation and atomic failure | Named mutations, schema branch diagnostics and report preparation; mutation/report regression tests | No silent field insertion, skipped rows or ambiguity selection. |
| HC/native executable, update-check skip, matching display names, serialized ready then fair dispatch | Adapter/supervisor; launch argument, ready-order and fair-dispatch tests; frozen FTP session metadata | New native-ID restoration has simulated proof; live result pending. |
| Durable send-before-claim, no duplicate dispatch on lost response, timeout ≠ death, at most two retries | Supervisor/store; lost launch/send response, observation timeout, retry-limit and audit handoff tests | Private namespace collision/concurrent initialization and cleanup ownership also tested. |
| Phase process release and conversation restoration; service/supervisor restart | Supervisor and CLI locks; saved-ID/name-collision, phase-transition, stale-socket and interrupted-merge tests | Queued native run checks actual restoration after committed progress. |
| Exports, metrics and offline audit share authoritative state | `Workflow.artifacts`, `parallel_metrics`, `audit_parallel`; metrics/export/decision-only/tampering tests | API counters are separate from payload tokens and worker occupancy. |
| Config, tool contracts, migration, recovery and measurements documented | `README.md`, `docs/pipeline.md`, `config.example.toml`, this record; CLI help/config checks | Historical execution uses archived source; no in-place migration. |
| Three fresh RFC959+3659 Luna/high trials, 40-minute cap, complete timing and semantic outcomes | Frozen manifest/source/config, per-run timing records and inspected snapshots/findings | Runs 1–2 failed; run 3 pending. Failed/partial runs do not satisfy the 20-minute target. |

The queued native check is under `rsc/runs/parallel-recovery-20261110T065056Z/` with a
separate source snapshot matching the current runtime files when prepared. `runner-state.json`
records waiting/running/finished state; it starts only after all three FTP outcomes exist and
the original benchmark process has exited. It is not a fourth FTP performance trial.

## Outstanding completion checks

1. Finish all three frozen FTP trials, inspect every outcome and published model, and record
   complete metrics and semantic findings. Reconcile measured snapshot behavior with later fixes.
2. Exercise the current exact native-session restoration with real HC/Codex, including a
   confirmed worker interruption, retained state, new attempt credentials and final cleanup.
3. Reconcile the final runtime results with the requirement audit above. Cross-range contexts,
   successive Fix provenance, merge restart and namespace ownership races have passing tests.
   Parallel Fix integration checks newly overlapping writes before Merge and independent
   re-review. The prepared recovery run and the outstanding FTP result still require inspection;
   a green suite alone does not close the goal.

No raw credentials or Codex session contents belong in this tracked report.
