# File pipeline

The service owns scheduling and official files. Coding agents read immutable source text,
write work files, and submit paths. The workflow is Preprocess → Parse → optional Merge →
validation → independent Review → publication, with Fix returning to the original author.

## Partitioning and budgets

Preprocessing preserves every nonempty source range exactly once in bounded chapter batches.
Searchable `sources/text/source-NNN.txt` files contain document/block locators; agents can
use shell tools such as `rg` without a search MCP tool.

The default parser count is `auto`, with at most four active parser agents. Capacity is
`floor((0.75 * parse_context_tokens - parse_fixed_tokens) / 2)`, using defaults 258400
and 45000. Planning starts at `ceil(total_source_tokens / capacity)` and chooses contiguous,
balanced partitions under capacity while preferring chapter boundaries. Manual positive
counts are honored as far as nonempty batches permit, with explicit capacity warnings.
`parse-plan.json` records settings, partitions, estimates, warnings and concurrency.

One completed parser bypasses Merge. Multiple parser files go to exactly one fresh merger,
which reads every input and produces one complete candidate. It resolves synonyms and
same-name distinct concepts, rewrites references and preserves evidence. There are no
clusters, group workers, object IDs, replacement tables or revisions.

The merge gate checks:

```
fixed + input + max(input, 40000) + 20000 <= 0.75 * context
```

`merge_context_tokens` and `merge_fixed_tokens` default to 258400 and 45000. Input estimates
cover every accepted parser file. `merge-budget.json` records the decision. Over-budget
runs fail explicitly, preserve parser results, and do not start a merger or publish.

## Five MCP tools

| Tool | Purpose |
| --- | --- |
| `read_source_batches(batch_ids, max_tokens)` | Read complete source ranges; follow `remaining`. |
| `query_model(view, kind, name, references, cursor, max_tokens)` | Read complete or indexed input records; follow `next_cursor`. Names are scoped by `input_file`. |
| `submit_parse_result(path)` | Save the parser's whole accumulated checkpoint. |
| `submit_model_result(path)` | Save a complete merged or fixed candidate. |
| `submit_review_result(path)` | Save independent review findings and declared source coverage. |

A stable five-tool catalog supports native clients that cache schemas. Calls still enforce
assignment, role and phase. Coordinator operations use a separate private credential and
are not agent tools. Default/maximum read budgets are 24000/30000 tokens. Responses keep
complete records and source batches; an oversized record produces an explicit budget error.
No cursor uses a revision or object ID. MCP JSON delivery is compact. For shell reads of
pretty files, agents can print `json.dumps(json.load(open(path)), separators=(",", ":"))`.

Agents write under `work/<worker>/` and submit an absolute or task-relative path. Checkpoints
use protocol record shapes:

```json
{
  "items": [
    {
      "kind": "parties",
      "value": {"name": "client", "description": "request sender"},
      "evidence": [{"batch_id": "batch-00001", "block_id": "source block locator"}]
    }
  ],
  "unresolved": []
}
```

Names must be unique within each kind in a file. Different parser files can reuse names.
Steps and references use names; dependencies have neither names nor object IDs. Each item
requires actual source batch/block evidence. Unresolved entries use
`{description, object_names}`. Protocol payload fields that themselves represent a version
or digest remain ordinary source-supported protocol fields.

Parser progress adds `complete:false` and `completed_batches:[...]`. Every submission
contains the whole accumulated checkpoint and replaces the same official parser file.
Completed source coverage can only grow and earlier source evidence is preserved. A final
checkpoint sets `complete:true` and covers all assigned batches; omitting both progress
fields means a final submission. Structural rejection includes actionable diagnostics and
leaves the official file unchanged. The agent corrects its work file and retries.

A successful response includes `stop_turn`. Partial parsing returns the remaining batch
names and keeps the same turn active. Final accepted results return `stop_turn:true`.
Identical completed content can be retried even from a different work filename. Different
content cannot replace a completed result.

## Review and Fix

Schema, evidence, names, references, endpoint roles and field relations are checked before
semantic review. Deterministic errors in a complete single-parser, Merge or Fix submission
are returned immediately for correction in the same turn, without consuming feedback rounds.
Semantic findings become Fix assignments. The single parser,
or the merger for multi-parser runs, owns Fix and resumes its native session. Fix always
submits a complete candidate; it cannot close reviewer findings itself.

The independent reviewer reads the frozen candidate and every assigned source range. Shell
reads and MCP reads are both valid. Review coverage is a declaration, not a tool receipt or
proof that an agent understood the text. Review files have this shape:

```json
{
  "reviewed_batches": ["batch-00001"],
  "findings": [{
    "category": "correctable",
    "description": "Explain the source-backed issue and correction needed",
    "batch_ids": ["batch-00001"],
    "global": false
  }]
}
```

To checkpoint an unfinished review, add `"complete": false` and list the ranges
reviewed so far together with all accumulated findings. The same review file is
replaced atomically; reviewed coverage cannot shrink. Partial reviews survive
restart and never authorize publication. Final submission covers all assigned
ranges and omits `complete` or sets it to `true`.

Categories are `correctable`, `source_ambiguity` and `external_reference`. Correctable errors
prevent publication. Genuine source ambiguity or unavailable external references can produce
`meta.complete:false`; unresolved descriptions retain document/block citations, and immutable
review files retain structured batch evidence. Empty findings allow complete publication
only when the candidate has no unclassified unresolved issues.

Initial review covers the entire source. After Fix, local changes include their evidence,
original finding ranges and reverse references through steps and dependencies. Role/channel
definition changes, renames and global findings require full review. Unaffected ambiguity
classifications outside a scoped review remain accepted. Uncertain associations conservatively
fall back to full review. Every accepted Fix undergoes validation and a new independent review.
The default limit is five feedback rounds; exhaustion with correctable errors fails without
publication.

## Persistence and recovery

The workflow runtime uses JSON files, with no database:

- `state.json`: small scheduling state, private credentials and assignment histories.
- `sources.json`, `sources/text/`, `parse-plan.json`: immutable source preparation and planning.
- `parsers/parser-N.json`: accumulated parser checkpoints, frozen once complete.
- `merge.json`, `candidate.json`: accepted whole-input merge and current candidate.
- `reviews/round-N/`: frozen candidate, review findings and any Fix input/result for that round.
- `protocol.json`, `snapshots/`, `report.json`: publication and source document snapshots.
- `events.jsonl`, `phases.json`: diagnostic events, timing and tool usage.
- `supervisor.json`, `supervisor-events.jsonl`, `agents/`: HC/native session and launch diagnostics.

A file is fully written and flushed before atomic installation. Accepted output is persisted
before recording service acceptance and advancing scheduling. A stray official filename
alone cannot complete an assignment. A crash after installation but before acceptance requires
the original agent to resubmit its work file; content equality does not bypass acceptance.
Restart reconciles accepted files, completing transitions
without asking an agent to repeat accepted work. JSON rejects duplicate keys and nonfinite
numbers. No hashes, schema versions, profile fingerprints or submission counters identify
workflow artifacts. Old SQLite runs require their archived implementation for execution
and audit; they are not silently migrated.

Official-file recovery assumes trusted local coding agents obey the write boundary. Native
agents and the service run as the same OS user, so file modes and MCP credentials do not
prevent an agent's shell from replacing official files or reading another role's token.
The audit checks file consistency and declared ownership, not cryptographic authorship.
Enforcing isolation against a malicious same-user agent requires a separate OS identity or
sandbox outside this workflow; the five-tool API alone does not supply that isolation.

One service and one supervisor own task-local locks. HC launch intent is saved before launch,
so recovery looks up the existing named terminal before starting another. Startup and native
session discovery are serialized; ready parser workers run up to the planned concurrency.
Codex uses its native executable with `--no-daemon`; terminal and native session names remain
stable. Native Codex's own session database is separate from workflow persistence.

Confirmed terminal death permits two replacements, revoking stale credentials first. A live
terminal ending before complete submission receives at most two precise continuation prompts
in the same session. Observation timeouts do not prove death and do not allow another live
agent. Completed workers are retired between phases, and Fix restores the original author's
native context. Only owned terminals are cleaned up; the user's HC core is not stopped.

`rsc audit` validates source coverage, accepted-file provenance, the single/multi merge path,
reviewer independence, review coverage and publication equality. It does not establish
semantic completeness independently of the recorded review. Metrics measure complete phase
wall time, tool calls/rejections, delivered token estimates, retries and correction rounds;
worker occupancy is not model inference utilization.
