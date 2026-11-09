# RSC RFC protocol extraction workflow

RSC reads a caller-selected RFC set and publishes a protocol interaction model. A persistent MCP service owns task state and gates; a Python supervisor uses Herdr Core to keep separate parser and reviewer Codex or Pi sessions alive and forward assignments between them.

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
  --config config.example.toml
```

`tiktoken` downloads its `cl100k_base` data once. For an offline installation, populate its cache beforehand. Default batch target/max sizes are 8,000/12,000 tokens including batch metadata. Inputs are grouped by chapter; oversized blocks are split into exact Unicode text ranges. Every nonempty source range must occur once.

Parser and reviewer default to the named `codex-luna-max` profile (`gpt-6-luna`, `max`). User configuration is TOML, parsed with `tomllib`. JSON configuration and old inline role settings are rejected. Protocol/MCP data and internal task metadata remain JSON.

Profiles live under `[agents.profiles.NAME]`, with `provider`, `executable`, `model`, `effort`, `args`, and `envs`. Optional `agents.parser_profile` and `agents.reviewer_profile` override `agents.default_profile`. For each role: role CLI → common `--profile` → role config → default profile.

```sh
.venv/bin/rsc doctor --config config.example.toml --profile pi-deepseek-flash
.venv/bin/rsc run --task-dir work/mixed --protocol Example --rfc example.txt \
  --config config.example.toml --profile codex-luna-max \
  --reviewer-profile pi-deepseek-flash
```

`envs = [["SSL_CERT_FILE", "/path/to/ca.pem"]]` adds literal overrides to the agent and MCP bridge. Duplicate names and overrides of RSC/HC task identity variables are rejected. Clients retain native provider configuration and authentication. For DeepSeek, inherit `DEEPSEEK_API_KEY` in the launching environment. `config check` prints profile/model/effort and environment names only; HC receives a private launch path, not environment values in argv.

Pi requires Node 22.19+ and Pi 1.5.0+ with native MCP and OSC 7501. The acceptance baseline is `npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.0`. Use `--prefix "$HOME/.local"` if the system prefix is not writable. The packaged extension registers a task/role namespace, restricts tools to RSC, verifies model/thinking selection, and restores complete results that native Pi otherwise truncates at 20 KiB. Restoration failure aborts the turn and leaves a fatal diagnostic that stops scheduling.

Tasks freeze role selections and definitions in `agent-profiles.json` (0600). A plain resume retains its saved selections and validates definitions. A changed profile requires a new task, including on `--retry-role`. Tasks without a profile snapshot cannot restart agents; their existing artifacts remain readable by `stats`, `validate` and `audit`.

The old `rsc compile` pipeline and provider API settings have been removed. Migrate to the `workflow` and `agents` config in the example. `ingest`, `index`, `validate`, `stats` and schema inspection remain available.

## Workflow and review limits

The server enforces `preprocess → parse → review → repair → review … → published` and binds each assignment to a role. Only the parser may edit the model. Only the reviewer may accept finding resolutions. Sources, batches and max review rounds are fixed for the task.

`--max-review-rounds N` sets the maximum number of reviews, counting the first review. It overrides `workflow.max_review_rounds` in the config; the default is **5**, allowing up to four repairs and re-reviews. The limit is fixed when a task is created: a plain resume keeps its saved limit, and an explicitly different CLI value requires a new task directory. Resending a message or retrying a tool does not consume another round. The first review reads the entire source set and complete model. Subsequent reviews inspect the complete current model, revision diff, affected source batches and every submitted resolution.

Every record needs valid source evidence; all parsing batches and assigned review batches need reading receipts and reports. Schema, source hashes, unique step IDs, name references, connection roles and field carriers are hard gates. Reading receipts prove delivery, not semantic correctness.

If all findings are accepted and no genuine unresolved questions remain, publication sets `meta.complete=true`. Otherwise a structurally valid partial model has `meta.complete=false` and explicit `unresolved` entries. At the round limit, the last review must have actually completed and every hard gate must pass; the report marks `review_limit_reached`. Incomplete review, missing sources, invalid references or schema errors cannot produce a published model.

## Sessions, recovery and inspection

Each task has its own HC session with parser/reviewer terminal IDs recorded in `supervisor.json`. Normal handoffs use `hc send` to the original process, not `codex exec resume`. Agents finish their turn after each phase transition; the supervisor dispatches the next assignment only when the target is available. MCP assignment claims acknowledge delivery. An uncertain send is never blindly repeated.

Run the same `rsc run --task-dir ...` to reconnect to an existing task and terminals. A blocked, exited or prematurely idle agent stops scheduling with a diagnostic; task data is preserved for inspection. Supervisor restart does not reset the phase or review round. The task's HC environment is isolated under its state/config directories and `/tmp/rsc-hc-<task-id>`. Use those same `HC_SESSION`, `HC_SOCKET`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME` and `XDG_CONFIG_HOME` values to attach with `hc`. Do not stop another task's or the user's core.

After inspecting a failed agent, `rsc run --task-dir work/ftp --retry-role parser` explicitly replaces the active parser (or use `reviewer` when that role is active). It keeps the same assignment and round and retains the other agent. Repeated normal starts never silently replace an agent or resend an uncertain message. Task-local supervisor/service locks reject competing instances. A service restart removes a stale socket only when ownership matches and connecting is refused; an observation timeout is not evidence of process death. Resuming an already published task finishes any pending cleanup without starting new agents.

The MCP bridge normally exposes tools for the current phase and notifies clients of catalog changes. The supervisor selects `--catalog role` for Codex, which currently caches tool catalogs: this registers all schemas for the authenticated role up front. `workflow_status.allowed_tools` and per-call server checks still enforce the phase; registering a schema never authorizes its execution.

## Standalone service and MCP client

`rsc run` creates the task. For programmatic initialization without agents:

```python
from rsc.workflow import Workflow

workflow = Workflow.create('work/example', ['rfc.txt'], 'Example protocol', max_review_rounds=5)
workflow.db.close()
```

Then run the service and attach a bridge with a role's credential file:

```sh
rsc serve --task-dir work/example
rsc mcp --socket "$PWD/work/example/workflow.sock" \
  --token-file "$PWD/work/example/parser.token"
```

Role tokens are task-local files with mode 0600. Treat task directories as trusted local data: role enforcement applies to MCP calls, not arbitrary filesystem access by another process running as the same OS user. Agents are instructed to use MCP for source reads and all edits.

The 17 tools, phase gates and assignment flow are documented in [pipeline.md](docs/pipeline.md).

## Artifacts and checks

The authoritative state is `workflow.sqlite`: current task/model, change history, findings, review decisions, assignments, and timed tool events. Source snapshots are frozen under `snapshots/`; the service validates them before transitions. Final outputs include:

- `protocol.json`: the existing version 7 protocol model; no internal record IDs or audit metadata added.
- `sources.json`, `batches.json`, `coverage.json`: provenance and parsing/review records.
- `review.json`, `changes.json`, `report.json`: findings and resolution history, model revisions, completed rounds and publication outcome.
- `events.jsonl`, `phases.json`: tool calls (including rejected calls), call durations, phase durations and per-tool counts. Older task timings reconstructed from audit events are labelled `first_observed_tool`; new tasks use assignment creation time. These are MCP call counts, not model API request/token/cost measurements.
- `supervisor.json`, `supervisor-events.jsonl`, service logs and task-specific HC state: process and dispatch diagnostics.

```sh
rsc validate protocol work/ftp/protocol.json
rsc stats work/ftp --validate --json
rsc audit --task-dir work/ftp
pytest
```

`audit` independently reconstructs frozen revisions and verifies publication, source receipts, complete model/diff delivery, review coverage, reviewer decisions and HC handoffs. It reports delivery and workflow evidence; semantic completeness still depends on extraction and independent review. Server tool-call totals include supervisor status checks and do not measure LLM requests or cost. Tool result pages target 8,000 tokens, contain at most 50 items and reject single results above 24,000 tokens; model records are capped at 8,000 tokens so large revisions can be reviewed across pages.

Live HC/Codex acceptance results and their limitations are recorded in [workflow-acceptance.md](docs/workflow-acceptance.md). Ordinary pytest runs do not start paid model sessions. The opt-in `scripts/verify_hc_recovery.py` exercises real agent replacement with a small RFC and a separate task directory.

## Adapter acceptance

`agents/<role>/launch.json` and `mcp-launch.json` are private execution settings (0600). They contain explicit profile overrides, never copies of ambient API credentials. Pi logs delivery byte counts/hashes and model checks in `pi-events.jsonl`.

Ordinary tests never start paid agents. The opt-in `PYTHONPATH=. .venv/bin/python scripts/verify_agent_providers.py --out runs/new-smoke --config config.example.toml` probes DeepSeek tool continuity, then runs Pi/Pi, Codex/Codex and Codex/Pi on a small RFC. It audits artifacts, checks credential persistence, and cleans only its private HC sessions. It reads the key from `DEEPSEEK_API_KEY` or a non-echoing prompt, never a key argument, and does not start FTP extraction.

Provider installation and live matrix results: [agent-profiles-acceptance.md](docs/agent-profiles-acceptance.md).
