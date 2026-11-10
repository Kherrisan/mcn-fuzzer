# RSC RFC protocol extraction workflow

RSC extracts a protocol model from RFC documents. A task-local MCP server owns state and scheduling. A Python supervisor uses Herdr Core (`hc`) to run native Codex or Pi sessions. The pipeline is **parallel Parse → concept clustering → parallel group Merge → deterministic Review → publication**. There is no LLM Reviewer or Fix phase.

## Install and run

Requires Python 3.12+, `hc`, an authenticated agent CLI, and local RFC TXT/HTML/XML files. Run commands from this directory.

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

Task directory basenames must be unique because HC namespaces use them. `tiktoken` downloads its `cl100k_base` data once; populate the cache before offline use. Default batch target/maximum sizes are 8,000/12,000 tokens. Every nonempty source range belongs to one batch. Parsers receive contiguous, approximately balanced sections of the batch list, with chapter locality preferred within a 10% tolerance. They can read outside their assignments to resolve dependencies.

Set `workflow.parse_workers` and `workflow.merge_workers` to positive integers (both default to 3). A value of 1 follows the same code path. Each Parser submits actual extracted items per batch, then one Parser clusters the complete name index. Merge workers process successive groups within the same session. The server integrates results and checks schemas, names, references, connection roles and field carriers. `workflow.max_feedback_rounds` controls corrections after the initial pass (default 2, including regrouping); unresolved structural errors fail without publishing.

`meta.complete=true` means this extraction workflow and its deterministic checks completed. It **does not certify semantic completeness**. `report.json`, `phases.json` and the audit explicitly record `semantic_review=false`.

## Agent profiles

Profiles under `[agents.profiles.NAME]` contain `provider`, `executable`, `model`, `effort`, `args` and `envs`. The default profile is `codex-luna-max`; the example also defines `codex-luna-high` and `pi-deepseek-flash`. Optional `agents.parser_profile` and `agents.merge_profile` select roles. Precedence is role CLI option → common `--profile` → role config → default.

```sh
.venv/bin/rsc run --task-dir work/mixed-unique --protocol Example --rfc example.txt \
  --config config.example.toml --parser-profile codex-luna-high \
  --merge-profile pi-deepseek-flash
```

Codex must support `--no-daemon` and resolve to a native executable; update checks are disabled. HC terminal and native thread names match. Initialization is serialized to avoid shared SQLite startup races; ready workers then receive work concurrently. Pi requires Node 22.19+ and Pi 1.5.0+ with native MCP and OSC 7501. Its extension restricts tools to the task namespace and restores complete MCP output when the client truncates it.

`envs = [["SSL_CERT_FILE", "/path/to/ca.pem"]]` adds literal environment overrides. Duplicate names and RSC/HC identity overrides are rejected. Native authentication is inherited; DeepSeek uses `DEEPSEEK_API_KEY`. Config inspection prints environment names only. Saved `agent-profiles.json` freezes selections and definitions for resume; changed profiles require a new task.

## Tools and persistence

The six agent tools are `read_source_batches`, `search_source`, `query_model`, `submit_parse_result`, `submit_clustering_result`, and `submit_merge_result`. There is no claim, apply-changes or phase-advance tool. Successful submissions tell the agent what remains. Scheduling and transitions happen on the server. See [pipeline.md](docs/pipeline.md) for contracts and conflict handling.

`workflow.sqlite` (storage version 3) is authoritative. Items have simple internal IDs; all except dependencies have names. Exported `protocol.json` remains the version 8 name-based model. Source evidence identifies actual batch/block pairs; no reading receipt or extra completion report is required.

Run the same `rsc run --task-dir ...` to resume. Completed batch/group submissions survive restarts. Identical retries return their saved acknowledgment; changed retries and stale attempts are rejected. Confirmed process death allows two replacements per assignment. Send intent is persisted before dispatch; the first valid tool call acknowledges receipt. A timeout is not proof of death and does not permit duplicate dispatch. `--retry-worker parser-1` also requires a confirmed dead terminal.

Task-local locks reject competing services/supervisors. Each task has a private HC environment saved under `hc_env` in `supervisor.json`. Inspect or clean up only that namespace. Tokens and launch settings are private files; permissions do not isolate processes sharing the same OS user.

Old task databases are not migrated. Resume them with their archived code/config. `rsc audit` retains read-only historical support; version 2 audit requires the adjacent archived `source` package. Removed review config and tool names are rejected.

## Outputs and verification

- `protocol.json` and `snapshots/`: exported model and immutable source documents.
- `revision-table.json`: old-to-canonical IDs, deletions, exact before/after edits and group attribution.
- `checks.json`, `assignments.json`, `report.json`: deterministic checks, ownership and outcome.
- `phases.json`: phase durations, MCP calls/rejections, delivery counts/tokens, repeated reads and worker occupancy. Occupancy is not inference utilization; MCP calls are not API calls.
- `supervisor.json`, `supervisor-events.jsonl`, `agents/`: session and dispatch diagnostics. Detailed tool events and immutable submissions are in SQLite.

```sh
.venv/bin/rsc validate protocol work/ftp-unique-name/protocol.json
.venv/bin/rsc audit --task-dir work/ftp-unique-name
.venv/bin/pytest -q
```

An audit replays persisted submissions and deterministic integration and checks exported artifacts. It does not perform semantic review. The opt-in paid benchmark freezes source code and both FTP RFCs, runs 1 and 3 Parsers with 3 Merge workers and Luna/high, and limits each run to 40 minutes:

```sh
.venv/bin/python scripts/benchmark_parallel_rsc.py --out runs/result-comparison-unique
```

It reports complete pipeline time, per-phase work, failures and available native token counters. The 20-minute timing flag is separate from structural audit and manual semantic assessment. There is no automatic unlimited tuning loop.
