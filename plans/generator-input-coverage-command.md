# Generator input coverage command

## Summary

Add `mcn-fuzz coverage` to replay generator inputs from a campaign corpus through its LLVM coverage target and emit cumulative line and branch coverage after configurable batches. Reuse the replay, profile accumulation, snapshot publication, and cleanup code used by the existing coverage stage.

## Command and output

```text
mcn-fuzz coverage --campaign <campaign.toml> --output <new-empty-dir> [--corpus <dir>] [--step N]
```

- `--campaign` and `--output` are required. `--corpus` defaults to the campaign corpus. `--step` defaults to 1 and must be positive.
- Reject a non-empty output directory. Do not start the fuzz loop, synthesis agent, or coverage-analysis agent. The command explicitly requests source coverage, so it uses the campaign's coverage binary and LLVM tools even if periodic coverage is disabled for `run`.
- Enumerate regular, non-hidden files, excluding `.metadata` and `.pcap` sidecars. Parse all selected inputs before starting replay; report malformed input paths before any SUT execution.
- Sort by file modification time ascending, then filename ascending. Replay every selected file, including duplicate contents. Resolve generator paths through the existing `GeneratorInput::from_file` behavior.
- Export after every N inputs and after a final partial batch. The CSV `Time` value is the Unix timestamp in seconds of the last input in that batch, matching profuzzgym's input-time convention.
- Write `cov_over_time.csv` with columns `Time,l_per,l_abs,b_per,b_abs,l_total,b_total,inputs`. Percentages are 0–100; use 0 if the total is zero. `inputs` is the cumulative number of successfully replayed files.
- Keep the normal cumulative coverage snapshots, metadata, profdata, and replay logs under the requested output directory. Flush each CSV row when written.

## Shared replay implementation

- Refactor `fuzz/src/coverage/mod.rs` to expose a shared batch replay operation independent of LibAFL corpus/state enumeration. It owns coverage-session setup, fixture restoration, generator/SUT execution, timeout and child cleanup policy, raw profile accumulation/rollback, LLVM merge/export, snapshots, and logs.
- The existing coverage stage continues selecting uncollected corpus/solution inputs and calling the shared batch operation. The new CLI selects directory inputs and divides them into `--step` batches, then calls that same operation.
- Make resident main-fuzz process IDs optional inputs to replay cleanup: the coverage stage supplies its live PIDs; the standalone command supplies none. Do not launch a main fuzz executor for the command.
- Extend `CoverageSummary` with LLVM line and branch totals, parsing both `covered` and `count` from `llvm-cov` totals. Preserve old checkpoint compatibility by defaulting absent totals. Growth comparisons remain based on covered counts.
- Return each published batch's cumulative summary, delta, successful input count, and stop/failure information so the command can write its CSV row. Preserve the stage's existing checkpoint and attribution behavior.
- A fatal replay or export error stops the command with a non-zero exit. If the batch has a valid successful prefix, publish that prefix with its metadata and final CSV row before exiting. Preserve existing timeout/crash classification and cleanup semantics.

## Validation and acceptance

- Cover CLI parsing, defaults, zero step rejection, non-empty output rejection, file filtering, stable mtime ordering, and generator path resolution.
- Cover covered/total extraction, percentages including zero totals, exact-step and partial final batches, and cumulative input counts.
- Use the existing coverage probe to verify cumulative profiles across multiple batches and matching final covered/total counts between the CLI and coverage stage for the same inputs.
- Verify malformed inputs fail before execution; replay failures preserve only a valid prefix and all child processes are reaped.
- Run relevant Rust checks, then replay the completed OpenSSL r3 corpus into a fresh output directory and inspect the CSV and snapshots.
