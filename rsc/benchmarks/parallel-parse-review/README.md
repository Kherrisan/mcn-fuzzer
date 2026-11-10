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

The first trial, `ftp-1110053040-r1`, reached Review at approximately 23 minutes and therefore
already missed the 20-minute target. It had not published when this observation was recorded.
The following Parse measurements come from its actual batches, drafts and attempt timestamps:

| Worker | Raw source tokens | Batches | Claimed-to-submitted seconds | Local model records |
| --- | ---: | ---: | ---: | ---: |
| parser-1 | 15,953 | 7 | 269.1 | 236 |
| parser-2 | 17,583 | 12 | 797.9 | 272 |
| parser-3 | 19,784 | 10 | 182.5 | 36 |

Balanced raw text does not guarantee balanced extraction work. These measurements identify a
Parse long tail and a nontrivial subsequent Merge; they do not establish semantic quality or
justify changing the agreed contiguous chapter partition algorithm. Merge also rejected some
operations for missing source delivery in the new assignment and invalid operation fields.
Final counts, complete phase timings and subsequent Review/Fix behavior remain to be collected.

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
- Fair dispatch, serialized initialization, lost responses, named restoration and bounded
  recovery: `tests/test_parallel_supervisor.py`, `tests/test_workflow_supervisor.py` and MCP tests.
- Offline mutation replay, source/model delivery, complete query pages, independent decisions,
  HC send-before-claim evidence and publication/export consistency: `workflow/audit_parallel.py`
  and tampering regressions. Programmatic runs without HC artifacts explicitly report that
  HC handoffs were not verified.
- Phase/origin metrics, current output artifacts and owned-process cleanup:
  `tests/test_parallel_metrics.py`, `tests/test_parallel_benchmark.py`. Payload tokens and
  claimed-worker occupancy are distinguished from model API token usage and inference time.
- Current configuration, thirteen named contracts, migration and recovery documentation:
  [README](../../README.md), [pipeline](../../docs/pipeline.md) and `config.example.toml`.

The full suite passed **268 tests and 78 subtests in 48.56 seconds**, including the additional
incremental-review regressions. Ruff passed for the changed workflow, scripts and tests;
`git diff --check` passed. Both live-script help commands and the Luna/high example config
were checked without launching additional paid agents. The requirement-by-requirement
completion audit and all real semantic acceptance results are still pending.

## Outstanding completion checks

1. Finish all three frozen FTP trials, inspect every outcome and published model, and record
   complete metrics and semantic findings. Reconcile measured snapshot behavior with later fixes.
2. Verify that declared cross-range extraction obligations feed Review's source/model context,
   including associations through rename/deletion; reference closure alone is not enough proof.
3. Recheck session-name collision handling for long campaign names and exact restoration,
   including the interaction between terminal-name truncation and private HC namespaces.
4. Audit the remaining plan requirements against implementation and executable evidence,
   including interruption during merge, recovery with retained reports, delivery bounds and
   provenance across successive Fix rounds. A green suite alone does not close these items.

No raw credentials or Codex session contents belong in this tracked report.
