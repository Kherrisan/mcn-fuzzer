# RSC RFC protocol extraction workflow

RSC turns local RFC documents into a protocol interaction model. A task-local MCP service
owns JSON files and scheduling; Herdr Core (`hc`) runs native coding-agent sessions.
The workflow is **Parse → optional whole-input Merge → validation → independent Review**.
Correctable findings return to the original parser or merger for Fix.

## Install and run

Requires Python 3.12+, `hc`, an authenticated native agent CLI, and RFC TXT/HTML/XML files.
Run commands from this directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/rsc doctor
.venv/bin/rsc config check --config config.example.toml
.venv/bin/rsc run --task-dir work/ftp-unique-name --protocol 'FTP/RFC959+RFC3659' \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc959.html \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc3659.html \
  --config config.example.toml --profile codex-luna-high
```

Use a unique task-directory basename for its HC namespace. `tiktoken` downloads its token
encoding once; populate its cache before offline use. Batches default to 8000 target and
12000 maximum tokens. Searchable immutable source text preserves document/block locators.

`parse_workers="auto"` plans contiguous chapter-local partitions, using a 258400-token context
and 45000-token fixed reserve. `parse_max_workers=4` limits concurrency independently of the
number of partitions. Positive manual parser counts are supported with capacity warnings.
See `parse-plan.json` for actual assignments and estimates.

One parser skips Merge. Multiple results go to one fresh merger that reads every parser
file. An explicit whole-input context gate stops oversized merges while preserving results.
There is no `merge_workers` setting. Independent review reads all source ranges initially,
then affected ranges after local fixes; global changes require a full review. The default
feedback limit is five rounds. Correctable errors never publish. Source ambiguity or missing
external references may publish with `meta.complete=false` and cited unresolved descriptions.

## Agent profiles

Profiles under `[agents.profiles.NAME]` specify `provider`, `executable`, `model`, `effort`,
`args` and `envs`. Parser, merge and reviewer profiles can be selected separately:

```sh
.venv/bin/rsc run --task-dir work/mixed-unique --protocol Example --rfc example.txt \
  --config config.example.toml --parser-profile codex-luna-high \
  --merge-profile pi-deepseek-flash --reviewer-profile codex-luna-high
```

Merge and reviewer inherit the parser selection unless overridden. `agent-profiles.json`
saves names and full profile definitions for resume; changed selections require a new task.
`envs` contains literal `[name,value]` pairs. Duplicate names and managed identity overrides
are rejected; inspection prints environment names only. Native authentication is inherited.

Codex must support `--no-daemon` and resolve to a native executable. HC terminal and native
session names match. Initialization and native session lookup are serialized, then parsers
run concurrently. Pi supports shell reading/writing and the five namespaced MCP tools.
Fix reuses the original author's native session; the reviewer has an independent session.

## Files, tools and resume

Exactly five tools are exposed: `read_source_batches`, `query_model`, `submit_parse_result`,
`submit_model_result`, and `submit_review_result`. Agents can search source files with shell
`rg`. Submissions pass a JSON file path; the service validates and atomically saves the official
file before advancing. Names and source locators replace object IDs. There are no artifact
hashes, schema versions, revisions, submission counters or workflow SQLite database.

Parser submissions can save whole accumulated progress with `complete=false` and
`completed_batches`; a final checkpoint covers every assigned batch. Reviewer submissions
contain findings and declared reviewed source ranges. See [pipeline.md](docs/pipeline.md)
for complete file shapes, validation rules, budgets and recovery behavior.

Run the same `rsc run --task-dir ...` to resume. Accepted files restore progress after a crash;
identical completed submissions are idempotent. Confirmed worker death revokes its credential
before replacement. An early-ended turn gets bounded continuation in the same session.
Timeouts do not permit duplicate live agents. Task-local locks enforce one service/supervisor.
Private tokens do not isolate processes sharing the same OS user.

Old SQLite runs require their archived code for both resume and audit. New runs use:

- `state.json`, `sources.json`, `parse-plan.json` and searchable `sources/text/`.
- `parsers/`, optional `merge.json`, `candidate.json`, and round-specific `reviews/`.
- `protocol.json`, immutable `snapshots/` and `report.json` after accepted review.
- `phases.json`, `events.jsonl`, `supervisor.json` and `agents/` for timings and diagnostics.

```sh
.venv/bin/rsc validate protocol work/ftp-unique-name/protocol.json
.venv/bin/rsc audit --task-dir work/ftp-unique-name
.venv/bin/pytest -q
```

An audit checks source coverage, file provenance, deterministic validity and independent
review records. Review improves semantic checking but does not mathematically guarantee
complete extraction. The opt-in FTP benchmark runs native Luna/high agents on RFC959 and
RFC3659, records phase/context/call metrics, and compares automatic planning with two parsers:

```sh
.venv/bin/python scripts/benchmark_parallel_rsc.py --out runs/file-pipeline-unique
```

Twenty minutes is a measurement target, not an assumed result. See
[workflow-acceptance.md](docs/workflow-acceptance.md) for the current verification boundary.
