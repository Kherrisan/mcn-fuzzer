# File-based RSC pipeline implementation

Checkpoint: `6a7bdf44` (before redesign). Untracked fuzz analysis outputs,
skills-lock.json and third-party/FuzzAgent remain untouched.

## Contract

- Preprocess sources, estimate parser partitions, run independent parsers.
  C=floor((0.75*context_tokens-fixed_tokens)/2). Start with ceil(T/C), keep
  contiguous chapter-local balanced partitions under C. Defaults: auto,
  max concurrency 4, context 258400, fixed 45000. Manual positive count is
  honored with capacity warnings. Save parse-plan.json.
- Each parser saves one complete JSON checkpoint. Names only; no object IDs,
  hashes, digests, schema versions, fingerprints, revision or submission counters.
  Steps and references use names; dependencies have neither name nor ID.
  Source document/block locators remain. Different parser files can use the
  same names; a file cannot contain different objects with the same kind/name.
- One result bypasses Merge. Multiple results: ONE fresh merge agent reads ALL
  parser results and emits a complete candidate, consulting source as needed.
  No cluster/groups/replacements/deletions/revision table. Preserve evidence.
  Merge budget: fixed + input + max(input,40000) + 20000 <= .75*context.
  Over-budget stops explicitly preserving parse results.
- Deterministic validation precedes independent semantic Review. Reviewer reads
  every source range and the frozen candidate. It submits findings, never edits.
  Final single-parser, Merge and Fix submissions are validated before acceptance;
  deterministic errors return to the same agent turn without spending semantic
  feedback rounds. Parser and Reviewer may save accumulated partial checkpoints;
  completion coverage must grow monotonically, and partial review cannot publish.
  Original parser (single) or merger (multiple) owns Fix. Validate each Fix and
  re-review affected sources; global changes require full review. Default five
  feedback rounds. Unresolved source ambiguity/external references may publish
  complete=false; correctable errors cannot. No publication on hard failure.
- Runtime uses JSON, no SQLite. Parser results, candidate, round-specific review
  snapshots, small state.json and protocol.json. Agent writes work file; service
  validates and atomically saves official file. One coordinator; stale session
  credentials revoked. Content equality handles duplicate completed submission.
  Persist result before phase advancement; restore from accepted files. No
  generic file database. Old SQLite runs require archived implementation.
- Exactly five MCP tools: read_source_batches, query_model, submit_parse_result,
  submit_model_result, submit_review_result. Submission uses complete JSON file.
  No search_source: agents use shell/rg against immutable readable source text.
  No agent-supplied attempt/seq/version/revision. Service owns scheduling.
- Remove app metadata schema_version/input_sha256/document_sha256/source_hash,
  profile fingerprints and comparison logic across ingestion, model and readers.
  NEW FORMAT ONLY, no silent legacy compatibility. Keep transport-required MCP
  versions, package versions and protocol payload version/hash fields.
- Add reviewer profile/CLI selection inheriting parser; remove merge_workers.
  Reuse validation, source handling and HC adapters. Avoid overengineering.

## Verification

Tests: capacity/locality/concurrency, single vs multi path, same-name concepts,
source evidence/references, atomic files/retries/stale sessions/recovery,
full independent review/Fix feedback limit, no SQLite or removed metadata,
protocol downstream consumers. Real RFC959+RFC3659 trials via hc and native
Codex executable, gpt-6-luna/high: auto trial then two repeats on fixed code;
also two parsers if auto selects one. Record phase times, context, calls,
findings, fixes, success. Twenty minutes is a target, not assumed success.
