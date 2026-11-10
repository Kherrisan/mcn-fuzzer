# FTP RSC runtime stability experiments

Status (2026-11-10): the optimization goal was stopped and cleared by the user;
no candidate met the full acceptance criteria. Iteration 23 was stopped by request.
The current workflow removes experimental source planning, reviewer preparation,
source-fact sharing and construction-window hints. Historical artifacts and offline
auditing remain intact; the runner now uses the simplified workflow settings.
Use a run's frozen package and configuration to reproduce its historical behavior.

Goal: `plans/rsc-ftp-runtime-stability.md`. Completion requires the same package version
passing three fresh sequential RFC 959 + RFC 3659 runs, each published in <1200 seconds,
with independent review, full existing gates, and a semantic inspection of FTP behaviors.
A timed-out or structurally valid but semantically deficient run does not pass.

## Baseline

`baseline.json` preserves phase timings and original diagnostics from the two Luna high
runs. The earlier run took 1934.398 seconds. In the later run, parse alone took 1907
seconds instead of 868 seconds; initial review/repair also took longer. The MCP execution
time was approximately 80 seconds per complete run. The remaining wall time includes
agent generation, request transport and scheduling; these records cannot isolate inference.

The historical `workflow_status` events do **not** identify their caller. The supervisor
itself observes workflow state through the parser credential, so parser-labelled status
calls are not sufficient evidence of agent polling. New events record `origin=supervisor`
or `origin=agent`; historical events remain explicitly unclassified.

## Reproducible runner

From the repository root:

```sh
python3 rsc/scripts/benchmark_ftp_runtime.py \
  --output rsc/runs/runtime-stability/iteration-01 --runs 3
```

The default image is pinned to the same image used for the baseline. The runner snapshots
the current `rsc/rsc` Python package and copies it into fresh containers; it verifies the
loaded package fingerprint, copies both fixed RFC inputs, and primes Codex SQLite before
starting RSC. The model and effort are fixed to `gpt-6-luna/high`, source batch limits
remain at 8000/12000, and max review rounds remains 5. Existing fuzz campaigns are untouched.

The 1200-second timer covers CLI startup through publication. SQLite priming is recorded
separately. `script -e` preserves the RSC exit status. On timeout the process group is
terminated, diagnostics are saved and the container ends. Containers and copied artifacts
remain available for inspection. A failed trial ends the sequence for analysis; every code
change requires a new output directory and resets the passing streak.

The default order is RFC 959 then RFC 3659. `--rfc-order extensions-first` selects RFC 3659
then RFC 959 for a controlled comparison; the manifest and actual CLI command record it.
Changing order also starts a new streak. Both files remain required with identical contents.

Each trial produces `timing-result.json`, task reports, logs and workflow SQLite. Reports
include phase timings, gate status, validation errors, unresolved findings, tool rejections,
and available Codex usage counters. Raw Codex session content and auth are not exported.
`performance_and_gates_passed` is preliminary; `semantic_audit=pending` must be resolved by
inspecting the actual protocol and source-backed reviewer outcomes before final acceptance.

## Initial implementation

- Existing 18 tool names remain; completions accept one report or atomic report arrays.
- Reports can arrive out of order and are saved in source order without relaxing receipts.
- Source windows support nonadjacent IDs and copyable batch/block evidence.
- Errors name bad evidence ownership and schema argument paths; mutations return a name/ID ledger.
- Model queries can filter source evidence without satisfying full-review reading gates.
- Each phase gets its own procedure and a shared instruction block. All later review rounds
  still read the full model, complete diff, affected sources and resolutions.
- New audit fields distinguish observer calls and preserve delivery token statistics.

Initial checks: 146 tests and 78 subtests passed; targeted changed-file Ruff checks passed.
Real performance acceptance is still pending.

## Later findings and delivery verification

Iteration-03 reached the 1200-second deadline and failed; it entered first review only
near the deadline. Its parse phase took 1151.3 seconds and generated 57498 output tokens,
including 18042 reasoning tokens. The older fast baseline parse took 868.1 seconds and
generated 42384 output tokens, including 18112 reasoning tokens. Most extra generated
work was therefore visible code/arguments, not increased reasoning-token count. Phase
wall-time output rates were similar (about 49 tokens/s); these are not direct API decode
latency measurements. Different extraction results and input ordering require independent
quality acceptance; this is a workload diagnostic, not an equal-quality speed claim.

Iteration-04 adds compact source responses with full reconstruction, dispatch-scoped
code-mode caches, smaller reading windows, and incremental reference feedback. On the
actual 30 batches, records responses used 124939 tokens and compact responses 79261,
a 36.56% reduction, with exact text/metadata round trips. No source selection was removed.

An optional offline audit in the same benchmark script checks actual model-visible Codex
tool outputs against canonical complete source batches, matching role, dispatch time window
and receipt. It rejects metadata-only outputs, stale-phase outputs and altered source text:

```sh
rsc/.venv/bin/python rsc/scripts/benchmark_ftp_runtime.py \
  --audit-traces RUN/codex-sessions --audit-task RUN/task \
  --audit-output RUN/source-delivery-audit.json
```

Copy Codex session artifacts from the completed container into the ignored trial directory
for this check; do not publish raw sessions. The audit report exports only delivery counts
and missing batch IDs. It also compares each review's model/diff pages exactly against
canonical server response logs, checking role and phase time; metadata-only or modified
pages cannot satisfy delivery. Later rounds require both model and diff logs. Delivery
proof, full workflow gates and the semantic checklist are
separate requirements. Iteration-03 verified delivery of all 30 parse batches and the four
review batches read before timeout; it still failed performance and workflow completion.

## Evidence after the presentation/control trials

Iteration-11 timed out in parse at 1202 seconds with **zero tool rejections**. All 30 source
bodies were verified against actual model-visible outputs; review had not started. Observed
parse output was 61178 tokens including 39103 reasoning tokens, while tool execution for the
whole trial was 25.484 seconds. These unfinished measurements are not equivalent-quality
throughput comparisons. They show that eliminating format retries and changing source tables
to named records are insufficient to meet the deadline. Exact duplicate source text is only
449 of 54691 text tokens, so large duplicated inputs are not the observed primary cost.

Iteration-12 completed parse in 535.318 seconds, but five valid source-backed review findings
led to a repair timeout. All required source/model deliveries were ultimately verified. Its
output-warning event was a broad tool-discovery metadata list; the strict harness still rejects
any warning. Iteration-13 adds brief cited interaction obligations during source reading,
round-specific review routing, precise discovery and grouped repair work. Existing source/model/
diff/resolution and independent-review requirements remain. Prelaunch regression passed 179 tests
and 78 subtests, with focused dispatch checks after final wording edits. No candidate has yet
passed the three-run criterion; inspect iterations.md and live Docker/timing reports.

## Independent source preparation experiment

Iteration-15's serial parser planning timed out in parse, despite complete delivery of all
sources. Iteration-16 disables that serial mode and prepares sources in the existing independent
reviewer session while parsing proceeds. The preparation stage cannot read/edit draft models,
submit findings, complete formal comparison reports or publish. The frozen review still reads
all model pages and compares all sources; later reviews retain full model/diff/source/resolution
requirements. Same-dispatch complete source receipts carry into its first formal review, with
separate preparation-source and frozen-model delivery windows. Overlapping durations are
reported separately; the performance clock remains total CLI-to-exit time under 1200 seconds.
The runner records all workflow settings and its own fingerprint as well as the RSC package,
and rejects edits to either during a three-run sequence. This candidate is not yet a measured
runtime or semantic pass. The success streak remains zero.
