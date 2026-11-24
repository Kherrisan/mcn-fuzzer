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

Protocol models can declare `states` by party and name for values used across steps.
A message step's `processing` lists ordered operations under `before_send`, `after_send`
or `on_receive`. Each operation declares `reads`, `writes` and a source-backed `rule`.
Rules describe branch applicability, computations, failure behavior and state lifetime.
RSC validates references and checks that internally produced states have writers.
The same coverage rules require channels to have lifecycle steps, messages to be sent,
states to be accessed and steps to appear in a dependency.

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
run concurrently. Pi supports shell reading/writing and the namespaced MCP tools.
Fix reuses the original author's native session; the reviewer has an independent session.

## Files, tools and resume

The MCP tools are `read_source_batches`, `query_model`, `submit_parsed_model`,
`submit_merged_model`, `submit_model_patch`, `validate_model`, and `submit_review`. Agents can
search source files with shell `rg`. Submissions pass a JSON file path. The service validates and
atomically saves the official
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

## Incremental extraction with Pydantic AI

Install the optional dependencies and check that the configured model can call MCP:

```sh
.venv/bin/python -m pip install -e '.[extract]'
.venv/bin/rsc extract --config extract.example.toml --probe
```

Run against a fixed collection of local RFC files:

```sh
.venv/bin/rsc extract --config extract.example.toml --protocol FTP \
  --task-dir runs/extract-ftp \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc959.html \
  --rfc tests/fixtures/profuzzbench-rfc/FTP/rfc3659.html
```

Each scan pass reads the same frozen source batches. Two passes cover the six
categories. The first writes parties, channels and messages together, in that
order: the batch names each participant, the transport lifecycle that carries it,
and the wire message types it exchanges. The second writes one interaction unit:
steps, the states those hooks write, and the step dependencies between exchanges.
The agent keeps its conversation within a pass and shares a registry across
passes. Writing the unit in one pass is what lets a hook declare
`writes: state.<name>` in the same batch that reads it. The first three categories
are corrected together by one Fix, so a batch that is missing a channel endpoint
is repaired in the same pass that wrote the parties; a missing send step is the
interaction unit's work and is recorded as unresolved until that pass.
Every scan batch receives the full list of open unresolved items with their evidence.
The agent calls MCP to add or update entities in batches, read indexed RFC sections,
and validate entities. It submits a scan batch with `complete_batch` and a category
or global Fix with `complete_fix`. The host controls the cursor.
Overview entities are provisional: detailed grammar, layouts and algorithms must
enrich the existing entity after a name or alias lookup, even when its earlier
evidence is outside the current batch. List updates preserve still-valid fields,
operations and branches. Message fields cover construction, parsing, computation
and verification as well as later interactions. Scan and Fix instructions preserve
source-supported participants, defaults, branches, sender and receiver processing,
failure outcomes and lifecycle constraints, with version and extension applicability.
Both submission tools and the Fix prompt include `repairable_categories`. A scan
batch reports its own write scope (one category, or steps with their states and
dependencies); a Fix lists the current category and earlier ones, all six for the
interaction unit's Fix, and all six for a global Fix.
`get_entity(ids=[...])` reads multiple entities in one call. `search_entities(queries=[...])`
returns the paged union of name and alias matches. Both tools return full entity
records with all references and the complete indexed text of every evidence
block, alongside its original citation locator.
After each scan pass, the host checks forward and reverse relationships.
Checks needing a later category run once both categories are ready. Each
connection-oriented channel requires connect and listen actions for its declared
roles and a close action by an endpoint. Repair can update categories already
scanned. Each review and the validation tool return the full list of current
issues. Scan and Fix prompts include all open unresolved items. Category and
global Fix prompts require reviewing and addressing each item with source evidence.
Use `record_unresolved` to record a new issue and `solve_unresolved` to close an
existing one with a reason and source evidence. A linked coverage check remains
in validation until the model passes it.
Fix prompts include the implicated records once per entity ID. An entity update
identifies its target by ID and cites the fields it changes.

After both scan passes complete and their deterministic category fixes pass,
the host reviews every frozen batch once. Reviewers run in parallel
(`review_concurrency`, default 3), each with its own workspace and a read-only
toolset: `list_entities`, `search_entities`, `get_entity`, `read_source`,
`validate_entities`, `record_gap` and `complete_review`. No mutation tool is
registered, so a reviewer cannot write an entity even by mistake. The batch text
arrives as block-id-prefixed lines (`[blk-000101] §4.2.6 ...`), and the reviewer
records a gap with `record_gap(kind, name, block_id, evidence_text=None,
missing_attributes=None)`. Only `block_id` locates the evidence: the host checks
that the block belongs to the current batch and derives the document and section
from it. An optional `evidence_text` quote is stored as given, trimmed but never
matched against the source. `complete_review()` finishes one batch; zero gaps is
valid. Each finished batch is written to the checkpoint on its own, so one failed
batch stops the run without discarding its siblings, and resume re-reviews only
the failed batches.

Each validation row returned by `validate_entities`, a rejected `complete_fix`, and
the Fix prompt carries a `suggestion` derived from the same rules the prompts use.
Step processing guidance lists the typical events of each hook, and a state whose
`source` is `processing` must be written by some step operation while an `external`
state needs no producer. `validate_entities` separates blocking rows from `advisory`
rows: a send step with no processing is reported with `advisory_count` and the
`advisory` list, but it never blocks a scan, a Fix or publication, so the reviewer
decides whether the source really prescribes a bare transmission. A global Fix records `repair_start_count`, and a Fix that
changes nothing and reduces nothing is rejected once so the agent retries with a
real change.

After review, the recorded gaps are grouped by source document, `gap_group_max`
(default 8) at a time, and each group becomes one Fixer assignment
(`gap-rfc8446-1`) with a stable gap id per gap (`rfc8446-g1`). A group may read any
block of its document with `read_source`, so it sees one theme whole even when the
review found it in several batches. Groups run one at a time: a Fixer overwrites
the whole registry from its workspace, so parallel groups would drop updates and
collide on entity ids. The prompt lists each gap's block id and its unverified
quote, and the group must re-read the cited block before editing; a gap the source
does not support is reported in `unresolved_gap_ids`. `complete_gaps(summary,
unresolved_gap_ids)` records the outcome, and unresolved gaps stay in
`review.residual_gaps` where the global Fix can still pick them up. They are not
written to `unresolved.json`. After the last group the normal deterministic global
validation and any global fix run; there is no second document review. Reviewer
calls share one request throttle (`review_requests_per_second`, default 5 per
second, provider retries included); the parallel reviewers stop waiting on each
other's thinking time but do not exceed that rate.

Set `review_enabled = false` to skip the review and gap stage.
If a category reaches its repair limit, scanning proceeds and remaining
issues are checked again during global validation. The initial global validation
counts toward the default limit of six checks. That limit and the number of
attempts already used persist on resume. If initial global validation passes
with open items, it runs one global Fix
before publication when a validation attempt remains.

The task directory publishes `protocol.json` and `unresolved.json` after validation.
It also appends model exchanges to one JSONL file per scan batch or Fix round
under `llm/`, for example `llm/category/steps/validate-steps-2.jsonl`. The files
contain the full model messages, settings, tool definitions, responses, and model
errors at the Pydantic AI adapter boundary. Internal retries append to the same
file. Pair a request with its response or error by `run_id` and `request_number`.
`rsc validate protocol` checks the model schema, internal references and coverage
(channel lifecycles, sent messages, accessed states, linked steps) from
`protocol.json` alone; pass `--no-coverage` to check schema and references only.
Extraction applies the same rules after each scan stage. The checkpoint also holds the registry, citations,
per-assignment timing, model usage, and the current conversation. Resume with
`rsc extract --task-dir runs/extract-ftp`. Configuration and inputs are fixed
for a task directory. Existing `rsc run` tasks still use their original entry point.
