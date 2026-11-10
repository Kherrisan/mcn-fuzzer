# RSC RFC protocol extraction workflow

RSC reads a caller-selected RFC set and publishes a protocol interaction model. One MCP service owns the SQLite state and gates; a Python supervisor uses Herdr Core (`hc`) to dispatch independent Codex or Pi workers. Parse and Review each default to three workers and run in separate phases.

## Install and run

Requires Python 3.12+, `hc`, the selected agent CLI with native authentication, and local RFC TXT/HTML/XML files. Codex must support `--no-daemon`; Pi's requirements are listed below.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/rsc doctor
.venv/bin/rsc config check --config config.example.toml
.venv/bin/rsc run --task-dir work/ftp --protocol 'FTP/RFC959+RFC3659' \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc959.html \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc3659.html \
  --max-review-rounds 5 \
  --config config.example.toml --profile codex-luna-high
```

`tiktoken` downloads its `cl100k_base` data once. For an offline installation, populate its cache beforehand. Default batch target/max sizes are 8,000/12,000 tokens including batch metadata. Inputs are grouped by chapter; oversized blocks are split into exact Unicode text ranges. Every nonempty source range occurs once. Each worker owns a contiguous section of the ordered batch list, balanced by raw source tokens with chapter locality preferred within a 10% tolerance. Supplementary reads do not transfer ownership.

Without a profile override, Parser and Reviewer use `codex-luna-max` (`gpt-6-luna`, `max`). The command above explicitly selects `codex-luna-high`, the FTP benchmark profile. Set `workflow.parse_workers` and `workflow.review_workers` to positive integers; `1` uses the same implementation for serial diagnosis. Configuration is TOML; old JSON and inline role settings are rejected.

Profiles live under `[agents.profiles.NAME]`, with `provider`, `executable`, `model`, `effort`, `args`, and `envs`. Optional `agents.parser_profile` and `agents.reviewer_profile` override `agents.default_profile`. For each role: role CLI → common `--profile` → role config → default profile.

```sh
.venv/bin/rsc doctor --config config.example.toml --profile pi-deepseek-flash
.venv/bin/rsc run --task-dir work/mixed --protocol Example --rfc example.txt \
  --config config.example.toml --profile codex-luna-max \
  --reviewer-profile pi-deepseek-flash
```

`envs = [["SSL_CERT_FILE", "/path/to/ca.pem"]]` adds literal overrides to the agent and MCP bridge. Duplicate names and overrides of RSC/HC task identity variables are rejected. Clients retain native provider configuration and authentication. For DeepSeek, inherit `DEEPSEEK_API_KEY` in the launching environment. `config check` prints profile/model/effort and environment names only; HC receives a private launch path, not environment values in argv.

Pi requires Node 22.19+ and Pi 1.5.0+ with native MCP and OSC 7501. The acceptance baseline is `npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.0`. Use `--prefix "$HOME/.local"` if the system prefix is not writable. The packaged extension registers a task/role namespace, restricts tools to RSC, verifies model/thinking selection, and restores complete results that native Pi otherwise truncates at 20 KiB. Restoration failure aborts the turn and leaves a fatal diagnostic that stops scheduling.

Tasks freeze role selections and definitions in `agent-profiles.json` (0600). A plain resume retains its saved selections and validates definitions. A changed profile requires a new task, including on `--retry-worker`. Tasks without a profile snapshot cannot restart agents; their artifacts remain available for inspection.

The old `rsc compile` pipeline and provider API settings have been removed. Migrate to the `workflow` and `agents` config in the example. `ingest`, `index`, `validate`, `stats` and schema inspection remain available.

## Workflow and review limits

The server enforces `preprocess → parse → merge → review → fix → merge → review … → published`. Preprocessing is deterministic coordinator work. Each Parser edits its own draft and reports its assigned batches in order. One existing Parser resolves merge conflicts; the coordinator then freezes the global revision. Reviewers independently compare source requirements and associated model closures. They cannot edit the model. Sources, batches, worker counts and review limits are fixed for the task.

`--max-review-rounds N` counts the first Review and overrides the config when creating a task. The default is **5**, allowing up to four Fix rounds. A resume retains the saved limit; an explicitly different limit requires a new directory. A transport retry does not consume a review round. Initial Review assigns every source batch and model record an owner. Later reviews reuse verified unaffected conclusions and recheck invalidated source/model contexts and every submitted resolution. Positive references, source scopes and negative query scopes govern invalidation; uncertain prose changes expand review conservatively.

Every record needs source evidence; every required batch needs complete delivery and a report. Schemas, source integrity, unique names, structured references, connection roles and field carriers are hard gates. Merge keeps scoped local names and updates references and source-report associations atomically. Reading receipts prove delivery, not semantic correctness. Only an independent Reviewer can accept a Fix resolution.

If all findings are accepted and no genuine unresolved questions remain, publication sets `meta.complete=true`. Otherwise a structurally valid partial model has `meta.complete=false` and explicit `unresolved` entries. At the round limit, the last review must have actually completed and every hard gate must pass; the report marks `review_limit_reached`. Incomplete review, missing sources, invalid references or schema errors cannot produce a published model.

## Sessions, recovery and inspection

Each task has a private HC session. Workers such as `parser-1` and `reviewer-2` have separate credentials, namespaces and `agents/<worker>/` directories. HC terminal and Codex thread names match. Startup and ready checks run serially to avoid shared initialization races, then all ready assignments are dispatched before waiting on one worker. Codex must resolve to a native executable; update checks are disabled. Launchers only execute the prepared command.

Agents end their turn after submitting an assignment. The supervisor releases processes between phases and restores saved sessions when needed for Merge, Fix or another Review. HC terminals and Codex threads retain matching display names; Codex restoration uses the saved native session ID, verified against this task's directory and name. Ambiguous name-only matches are rejected. Send intent is persisted before `hc send`; the MCP claim acknowledges receipt. A lost response never authorizes a duplicate dispatch.

Run the same `rsc run --task-dir ...` to reconnect. Supervisor restart retains assignments, drafts, receipts and rounds. Confirmed process exits permit at most two replacement attempts per assignment; the old attempt is revoked first. A blocked, errored or prematurely idle agent stops scheduling for diagnosis. Observation timeouts preserve the process and never prove death. The private HC environment is saved in `supervisor.json` under `hc_env`; use those exact variables to inspect it. Never stop another task's or the user's core.

After diagnosis, `rsc run --task-dir work/ftp --retry-worker parser-1` permits retry only after the recorded terminal is confirmed dead; it refuses a live worker and does not bypass the retry limit. Replacement retains the local draft and completed batch reports; new reads need the replacement attempt's receipts. Task-local service/supervisor locks reject competing instances. A stale socket is removed only after ownership and connection-refusal checks. Resuming a published task completes pending cleanup without starting agents.

The MCP bridge normally exposes phase-specific tools and sends catalog-change notifications. For Codex, `--catalog role` registers the role's tool union for cached clients. Assignment responses include `allowed_tools`; every call still checks phase, worker and attempt. Coordinator-only status and gate operations are absent from agent catalogs.

## Standalone service and MCP client

`rsc run` creates the task. For programmatic initialization without agents:

```python
from rsc.workflow import Workflow

workflow = Workflow.create('work/example', ['rfc.txt'], 'Example protocol', max_review_rounds=5)
workflow.db.close()
```

Then run the service. A coordinator prepares sources and advances to Parse before worker credentials exist. Attach a bridge with an assigned worker's credential file:

```sh
rsc serve --task-dir work/example
rsc mcp --socket "$PWD/work/example/workflow.sock" \
  --token-file "$PWD/work/example/parser-1.token"
```

Worker and coordinator tokens are task-local files with mode 0600. Worker tokens rotate on reassignment. Treat task directories as trusted local data: MCP permissions do not isolate processes sharing an OS user. Agents use MCP for source reads and all edits.

The **13 agent tools**, named mutation format, phase gates and recovery protocol are documented in [pipeline.md](docs/pipeline.md). Older singleton tasks require their archived source package for execution; `rsc audit` retains historical read-only support. New tasks use storage version 2; there is no in-place migration or old-tool execution compatibility.

## Artifacts and checks

The authoritative state is `workflow.sqlite`: current task/model, change history, findings, review decisions, assignments, and timed tool events. Source snapshots are frozen under `snapshots/`; the service validates them before transitions. Final outputs include:

- `protocol.json`: the existing version 8 protocol model; no worker scopes or internal record keys added.
- `sources.json`, `batches.json`, `coverage.json`: source metadata, fixed source batches and review conclusions, including explicit carried-conclusion provenance.
- `assignments.json`, `source-reports.json`: worker ownership and immutable extraction reports with final-name/deletion associations.
- `review.json`, `changes.json`, `report.json`: findings and resolution history, model revisions, completed rounds and publication outcome.
- `events.jsonl`, `phases.json`: tool calls (including rejected calls), call durations, phase durations and per-tool counts. Older task timings reconstructed from audit events are labelled `first_observed_tool`; new tasks use assignment creation time. These are MCP call counts, not model API request/token/cost measurements.
- `supervisor.json`, `supervisor-events.jsonl`, service logs and task-specific HC state: process and dispatch diagnostics.

```sh
rsc validate protocol work/ftp/protocol.json
rsc stats work/ftp --validate --json
rsc audit --task-dir work/ftp
pytest
```

`audit` replays named mutations and merges from sealed inputs, verifies source receipts, owned model-context delivery, review coverage, independent decisions and exported artifacts. Carried conclusions retain their original proof rather than generating fresh receipts. This verifies workflow evidence, not semantic completeness. Tool calls are grouped by agent, supervisor and health origin. MCP payload tokens are not model API token usage. Pages default to 24,000 tokens, with requested budgets up to 30,000; item counts follow the token budget. Single ordinary mutation records have an 8,000-token limit.

Historical HC/Codex acceptance results and their limitations are recorded in [workflow-acceptance.md](docs/workflow-acceptance.md). Ordinary pytest runs do not start paid model sessions. From this directory, the opt-in `PYTHONPATH=. .venv/bin/python scripts/verify_hc_recovery.py --source example.txt --out runs/new-recovery --config config.example.toml` interrupts one claimed Parser, checks its fenced replacement and audits the result. It uses a fresh directory and defaults to a 20-minute diagnostic limit.

## Adapter acceptance

`agents/<worker>/launch.json` and `mcp-launch.json` are private execution settings (0600). They contain explicit profile overrides, never copies of ambient API credentials. Pi logs delivery byte counts/hashes and model checks in `pi-events.jsonl`.

Ordinary tests never start paid agents. The opt-in `PYTHONPATH=. .venv/bin/python scripts/verify_agent_providers.py --out runs/new-smoke --config config.example.toml` probes DeepSeek tool continuity, then runs Pi/Pi, Codex/Codex and Codex/Pi on a small RFC. It audits artifacts, checks credential persistence, and cleans only its private HC sessions. It reads the key from `DEEPSEEK_API_KEY` or a non-echoing prompt, never a key argument, and does not start FTP extraction.

Provider installation and live matrix results: [agent-profiles-acceptance.md](docs/agent-profiles-acceptance.md).

## Parallel FTP measurement

From this directory, run `.venv/bin/python scripts/benchmark_parallel_rsc.py --out runs/new-parallel-ftp` to freeze the code and RFC 959/3659 inputs, then run three fresh sequential trials with three Parse/Review workers and `gpt-6-luna/high`. This opt-in command starts paid agents; pytest does not. Each trial has a 40-minute diagnostic limit and records phase timings, tool outcomes, merge conflicts, review reuse and available Codex token counts. The 20-minute target requires complete publication and separate [semantic inspection](benchmarks/ftp-runtime-stability/semantic-checklist.md). Timeouts, partial publication and failed semantic inspection are failures; the runner never treats `semantic_audit=pending` as success.

Current evidence and outstanding checks: [parallel acceptance record](benchmarks/parallel-parse-review/README.md).
