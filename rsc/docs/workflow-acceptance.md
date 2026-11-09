# MCP workflow acceptance

Acceptance date: 2026-11-08. Live runs use private HC cores and interactive
`gpt-6-sol` / `high` parser and reviewer sessions. Generated run directories are
local artifacts under `rsc/runs/` and are ignored by Git.

## Requirement evidence

| Requirement | Implementation and verification |
|---|---|
| Persistent resident service, stdio MCP, phase-specific tools | `workflow/server.py`, `tools.py`, `state.py`; real socket/stdio tests and live sessions. The 17 tools are authenticated and checked on every call. Codex uses a stable role catalog because it caches schemas; future-phase calls still fail. |
| Chapter batches and immutable source coverage | `workflow/sources.py`; exact Unicode ranges, chapter/document boundaries, tables, appendices, oversized blocks and missing/overlapping ranges tested. The live FTP run covered 1,135 blocks across 29 batches. |
| Parser CRUD, evidence, revision and operation IDs | Atomic transaction, rollback, stale revision, idempotency, source and missing/ambiguous reference tests in `test_workflow.py`. Published schema is unchanged. |
| Independent full initial review and affected-source re-review | Frozen model delivery, diff delivery and source receipts enforced by `check_gate`; full FTP run has three completed reviews. |
| Reviewer owns finding closure | Parser resolutions cannot close findings; rejection histories survive repairs. Three-round and explicit partial-publication tests exercise both paths. |
| Bounded rounds and no hard-gate bypass | Tests cover first-review success, two-round correction, one-round partial publication, three-round partial publication, final hard-gate failure and interrupted review. |
| HC handoff and recovery | `test_workflow_supervisor.py` tests busy/blocked/exited work, uncertain delivery, no blind resend, observation timeouts, same-terminal handoff, explicit retry and owned cleanup. Service crash/restart is tested over real sockets. |
| Actual parser/reviewer reuse and replacement | Live repair run returns work to parser `t1`, then reviewer `t2`; live recovery replaces parser `t1` with `t3`, retains reviewer `t2` and preserves assignment/round. |
| Durable artifacts and timings | SQLite is authoritative; publication exports sources, batches, coverage, changes, findings, tool audit and phase timing reports. `rsc audit` replays frozen revisions and verifies delivery/publication invariants. |
| Migration | Old provider/compiler/link/completion implementations and private SDK dependencies removed. `rsc compile` prints migration guidance. Ingest, index, schema validation and stats remain and are covered by the full test suite. |

## FTP full run

Task: `runs/workflow-ftp-chapters-20261108/task`, ID `998bd150`.
Inputs: RFC 959 and RFC 3659 fixture HTML. Parser `t1` and reviewer `t2` remain
the same throughout all seven handoffs. No replacement occurred. Resuming the
published task using the installed console from the repository parent, with
`PYTHONPATH` unset, successfully finalized cleanup without launching new agents.

| Review | Frozen revision | Model records | Required/reviewed source batches |
|---|---:|---:|---:|
| 1 | 18 | 295 | 29/29 |
| 2 | 24 | 397 | 6/6 |
| 3 | 27 | 453 | 5/5 |

The published model has 8 parties, 62 messages, 6 channels, 136 steps,
238 dependencies and 4 unresolved entries. Nine findings were raised; eight
were accepted by the reviewer and one remains open. The configured three-round
limit was reached, so this is an intentional partial publication:
`meta.complete=false`, `review_limit_reached=true`. The open finding remains
open in `review.json` and is carried into the model's unresolved entries.
Schema, references, source hashes, reading receipts and review coverage pass.

This live service predates the record-ID pagination change and recorded fixed
50-item page offsets. The audit verifies every historical offset and reports
`legacy_offsets` instead of treating it as current record-ID tracking. The largest
actual model response was 9,589 tokens, diff response 15,069 tokens and source
response 9,621 tokens, below the configured client output limits. Current code
tracks each delivered record/change ID, uses token-bounded pages and counts the
same JSON representation delivered to MCP clients.
Rebuilding those 1,135 source blocks with the final whitespace-aware token counter
produces 30 batches, at most 8,111 tokens each, in 3.52 seconds. Exact range-coverage
validation passes; the published run keeps its original immutable 29-batch manifest.

| Phase | Round | Observed seconds | MCP calls |
|---|---:|---:|---:|
| Preprocess | 0 | 22.57 | 10 |
| Parse | 0 | 480.70 | 141 |
| Review | 1 | 176.79 | 78 |
| Repair | 1 | 177.93 | 46 |
| Review | 2 | 105.90 | 32 |
| Repair | 2 | 94.70 | 20 |
| Review | 3 | 79.20 | 31 |

These phase durations start at the first observed MCP call in each assignment;
they exclude agent startup and gaps between assignments. Their sum is 1,137.80
seconds, and the first observed tool call to final publication spans 1,153.59
seconds. They are labelled `first_observed_tool` in `phases.json`. New tasks
record assignment creation time. MCP call counts are not model API request,
token or cost measurements.

For context, the existing direct-Codex extraction at
`runs/ftp-gpt-6-sol-high-20261104T031756Z` used the same RFC fixtures and
`gpt-6-sol/high`; its recorded outer process ran 1,228.33 seconds. The timing
boundaries and review orchestration differ, so this is not a controlled speedup
claim. It produced 147 steps, 563 dependencies and 3 unresolved issues; object
counts alone do not establish semantic quality or equivalence.

## Small live runs and recovery

- `runs/workflow-final-smoke/task`, ID `2f496d01`: final implementation launched
  with the installed console and `PYTHONPATH` unset. Two real `gpt-6-sol/high`
  sessions, both `--no-daemon` and with native subagents disabled, completed
  preprocessing, parsing and review. Published 8 records at revision 4 with
  `complete=true`; `rsc audit` verifies record-ID delivery and 2/2 source batches.
  Assignment-based phase timings were 19.95, 61.29 and 33.47 seconds. Task cleanup
  completed, with no new agents started during publication recovery.
- `runs/workflow-smoke/task5`: actual independent extraction/review and complete
  publication with 8 records.
- `runs/workflow-repair-smoke/task`: deliberately incomplete, explicitly labelled
  seed fixture (not parser-produced output); real reviewer raised six findings,
  the original parser repaired them, and the original reviewer approved round 2.
- `runs/workflow-recovery-smoke/task/recovery-verification.json`: actual parser
  loss/replacement; reviewer, assignment ID and round are retained; publication
  succeeds. `scripts/verify_hc_recovery.py` reproduces this opt-in test.

The early small runs predate some audit metadata. The seeded repair run also
contains intentionally code-driven setup phases. They prove the particular live
handoff/recovery behavior above, rather than satisfying every check of the newer
`rsc audit` command. FTP has complete auditable phase and review metadata.

## Reproduce local checks

From the `rsc` project directory after installation:

```sh
python -m pytest -q tests
rsc doctor
rsc config check --config config.example.toml
rsc validate protocol runs/workflow-ftp-chapters-20261108/task/protocol.json
rsc audit --task-dir runs/workflow-ftp-chapters-20261108/task
```

The full suite passed with **98 tests and 74 subtests**. Ruff checks for the new
workflow and changed integration modules, formatting checks and `git diff --check`
also pass. Socket tests use real local sockets; normal tests never start paid agents.

An audit proves structural validity, source/model delivery and workflow
invariants. Neither receipts nor independent review mathematically guarantee
that every RFC semantic requirement was extracted correctly.
