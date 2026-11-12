# File-based RSC pipeline verification

Implementation checkpoint before redesign: `6a7bdf44`.
Plan: [file pipeline](../../plans/rsc-file-pipeline.md).

The new runtime uses JSON files, name-based objects, an automatic contiguous
parser plan, one whole-input merger only for multiple parser results, and an
independent reviewer with owner-driven Fix. Its five MCP tools have no source
search or object IDs, hashes, revisions or schema-version arguments.

## Automated verification

- Full RSC suite: **283 passed, 78 subtests passed**.
- Fuzzer Rust library: **142 passed**.
- Fuzzer Python coverage service: **13 passed**.
- Rust/Python coverage MCP integration: **1 passed**.
- Independent review exercised dispatch crash recovery, partial-overlap ambiguity
  retention, source impact of both dependency endpoints, unresolved changes,
  owned-terminal cleanup on failure, and acceptance/crash ordering. All seven
  reproduced findings have regression coverage and are resolved.

Native Codex still uses its own session database. The removed SQLite dependency
is the RSC workflow database. Old protocol metadata is rejected explicitly;
existing campaign protocol files must be regenerated or explicitly converted.

## Live FTP trials

Inputs: RFC 959 and RFC 3659. Model: `gpt-6-luna`, effort `high`, native Codex
binary, updates disabled. Each experiment freezes its code and inputs. Earlier
diagnostics stopped at the first failure; the final matrix uses `--keep-going`
to retain all four outcomes. Deadline is 2,400 seconds; target is 1,200.

- `runs/ftp-file-auto-20261111-impl`: initialization failed after 9.64 seconds.
  HC explicitly reported startup output not settled and nothing typed. Added
  bounded retry for definite nondelivery, without retrying uncertain delivery.
- `runs/ftp-file-trials-20261111-r2`: stopped by the implementation owner during
  Parse to incorporate review fixes before freezing the repeat runs. This is
  not a model failure; the run contains `stopped-reason.json`.
- `runs/ftp-file-trials-20261111-r3`: failed after **1,184.57 seconds** at the
  five-Fix limit. Parse took 252.69 seconds; five independent reviews still
  uncovered an omitted upload-mode path at the last round. No protocol was
  published. This is not an under-20-minute success. The runner stopped before
  repeats. Prompt changes then required a complete scenario audit, consistent
  fixes across supported sibling paths, and distinct semantic command types.
- `runs/ftp-file-trials-20261111-r4`: failed after **1,890.03 seconds**.
  Three semantic reviews ran. At Fix 5, deterministic field-carrier errors
  (`sender_marker` absent from the transmitting message) exhausted the limit.
  No protocol was published. Complete candidate validation was subsequently
  moved before submission acceptance: errors now return to the same agent turn
  without consuming semantic feedback rounds. Regression tests cover rejection
  and successful correction at the final feedback round.
- `runs/ftp-file-trials-20261111-r5`: final frozen matrix completed in the
  sequence auto, auto, auto, two parsers. The results below retain all four runs,
  including timeouts. Current `rsc/` implementation and schemas match this frozen
  snapshot exactly; no production edits were made between the repetitions.

Optional incremental review checkpoints were completed after the r4 snapshot;
their monotonic coverage, restart and non-publication behavior pass unit tests.
The frozen r4 experiment exercises complete review submissions, not this optional
incremental path. R5 includes the incremental API and same-turn deterministic
validation. Frozen experiments are never silently updated with working-tree changes.

The current FTP source estimate is 53,320 tokens. Automatic planning selects
one parser with estimated context 151,640 tokens; manual two-parser planning
splits on the RFC boundary (24,481 and 28,839 source tokens).

### Final frozen matrix

**Publication succeeded in 2/4 trials; the 20-minute target was met in 0/4.**
Automatic planning selected one parser: 1/3 auto runs published. The single
two-parser run published. These samples do not establish a reliable speedup.

| Run | Parsers | Elapsed | Completed reviews | Accepted Fixes | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 1 | 40m 00s | 4 | 4 | timeout |
| 2 | 1 | 31m 18s | 6 | 5 | published |
| 3 | 1 | 40m 00s | 3 | 3 | timeout |
| 4 | 2 | 32m 39s | 5 | 4 | published |

Phase totals in seconds (Review includes the unfinished final review for
timeouts). Elapsed times for timeouts are censored at the deadline,
not successful completion times.

| Run | Parse | Merge | Review | Fix | Service tool handlers |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 314.65 | 0.00 | 1107.89 | 976.44 | 74.98 |
| 2 | 213.26 | 0.00 | 739.38 | 922.11 | 43.66 |
| 3 | 367.49 | 0.00 | 1092.31 | 939.00 | 55.87 |
| 4 | 277.61 | 194.49 | 946.01 | 537.63 | 57.18 |

Peak native input context / compactions; each context window is 258,400 tokens.
These peaks cover the worker session, including later Fix/review turns.

| Run | Worker | Peak input tokens | Compactions |
| --- | --- | ---: | ---: |
| 1 | parser-1 | 244,735 | 1 |
| 1 | reviewer-1 | 235,557 | 1 |
| 2 | parser-1 | 240,864 | 1 |
| 2 | reviewer-1 | 244,697 | 1 |
| 3 | parser-1 | 221,103 | 0 |
| 3 | reviewer-1 | 244,202 | 2 |
| 4 | parser-1 | 100,966 | 0 |
| 4 | parser-2 | 76,343 | 0 |
| 4 | merge-1 | 160,862 | 0 |
| 4 | reviewer-1 | 242,962 | 1 |

Both published artifacts have `meta.complete=true`, no unresolved entries,
and pass independent current-tree protocol/source validation, workflow audit,
the fuzzer Python reader, and the actual Rust library `load_model` function.
Run 2 contains 631 items / 109 steps; run 4 contains 607 items / 108 steps.

Raw results and per-run `timing-result.json`, `independent-validation.json`,
`phases.json`, review files and native session logs are under
[the final experiment](../runs/ftp-file-trials-20261111-r5/results.json).
The runner exits 1 because two runs timed out; that outcome is retained.

Two-parser Parse + Merge takes 472.10 seconds, versus 213.26–367.49 seconds
for the single-parser initial candidates. Review/Fix still takes 1,483.64
seconds in the two-parser run. The measured service handlers account for
43.66–74.98 seconds per run; this excludes model and tool round-trip latency.
The main remaining problem is semantic convergence, with repeated omissions,
candidate expansion and contradictory feedback, rather than runtime storage.

### Observed convergence costs

R5's first trial parses in 314.65 seconds, but its first four reviews report
successive omissions and inconsistent fixes. The candidate grows from 227 items
(105 dependencies) at initial review to 824 items (628 dependencies) at review 3.
This repeatedly increases the material both agents must inspect and rewrite.
The first reviewer also incorrectly requests an OPTS TVFS command; RFC 3659
section 6.4 explicitly defines no such command, and review 1 corrects that
premise. Independent review therefore needs source verification itself; its
findings cannot be counted as automatically correct. These are model convergence
and representation costs, not evidence that adding more MCP tools would help.

Run 2 review 1 also calls the ABOR `426` followed by `226` sequence unsupported.
RFC 959 section 4.1.3 (`blk-000361` in the frozen source) explicitly specifies
that sequence when aborting an in-progress service. This finding is incorrect;
raw finding counts measure reviewer output, not independently verified defects.
The following review (round 2) requests restoring exactly that ABOR sequence
after Fix removed it. This is observed back-and-forth caused by contradictory
review feedback, not merely an unverified possibility.

Reading/completion declarations and deterministic validation do not prove all
RFC semantics are correct. Review remains independent; source ambiguity or
missing external RFCs may result in a published model with `complete=false`.
