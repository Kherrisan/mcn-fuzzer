# Result-submission pipeline

## Phases and ownership

1. **Preprocess (server):** ingest immutable source documents, form source batches and assign contiguous, token-balanced batch ranges with chapter locality. A batch is the Parse work unit; there is no separate task graph to claim.
2. **Parse (parallel):** each Parser submits `parties`, `messages`, `channels`, `steps` and `step_dependencies` for each assigned batch in order. The server allocates IDs such as `i1`; all kinds except dependencies have a `name`. A Parser can update its own earlier items by ID. Supplemental source reads can cross assignment boundaries. Structured references bind to explicit IDs or unique local names and retain ambiguity rather than choosing a random match.
3. **Cluster (one Parser session):** freeze all Parse outputs. Read the full name index, consult definitions/references/RFC text as needed, and submit a complete partition of named items. Each group contains one concept and one kind; every named ID appears once. Dependencies are excluded. Singleton groups are valid.
4. **Merge (parallel pool):** workers read frozen groups, submit one canonical value per group, and continue with the next group returned by the server. The smallest numeric member ID survives. Original inputs stay immutable. The pool reuses a session across groups rather than starting an agent per group.
5. **Review (server only):** integrate all results, rewrite references through the complete replacement map, deduplicate identical dependencies and run structural checks. Publish only after all checks pass.

Dependencies belong to the group containing their dependent step. That group's worker may submit complete dependency updates. Different conditions remain separate; exact duplicates combine evidence. Changed message names also update qualified field carriers. Prose is never globally search-and-replaced: cross-group text edits require an exact item/path/before/after proposal with source evidence. A collision with another proposal or canonical definition returns an explicit conflict.

## Six agent tools

| Tool | Purpose |
| --- | --- |
| `read_source_batches(batch_ids, max_tokens)` | Read complete batches; follow `remaining`. |
| `search_source(query, batch_ids, cursor, revision, max_tokens)` | Locate supplementary RFC context. |
| `query_model(view, ids, references, kind, name, cursor, revision, max_tokens)` | Read own Parse items, the frozen index/items, group membership or checked candidate. |
| `submit_parse_result(batch_id, items)` | Atomically store actual extraction and acknowledge assigned IDs. |
| `submit_clustering_result(groups)` | Submit all groups as arrays of item IDs. |
| `submit_merge_result(group_name, value, evidence, dependencies, text_updates)` | Submit one canonical group and optional owned dependency/prose proposals. Alternatively supply `regroup_reason`. |

Item form: `{id?, kind, value, evidence:[{batch_id,block_id}]}`. Omit `id` for a new item; supply an owned ID for a complete replacement. Existing evidence is retained. Values follow the existing protocol schema; tool descriptions provide the shapes. No extra report, receipt, finding or resolution is submitted.

A merge dependency update is `{id,value,evidence}`. A prose update is `{id,path:[...],before,after,evidence}` and must address a prose field (`description`, `condition`, `from`). Invalid ownership/paths are rejected before committing. Responses include `stop_turn`; a Merge response also supplies `next_group` and membership. Continue in the same turn until `stop_turn=true`.

Read pages use `next_cursor` and a pinned `revision`. Default/max budgets are 24,000/30,000 tokens. Parser revisions are local; other Parsers do not invalidate them. Source reads use `remaining` rather than a cursor. The source catalog arrives with the assignment.

Tools are authorized by role, phase, assignment and attempt. Codex can register a stable role catalog (Parser has reads plus Parse/Cluster submissions; Merge has reads plus Merge submission). Calls still enforce the current assignment. Internal `tick`, `record_send`, `replace_attempt` and status operations have a separate coordinator credential and are not agent tools.

## Integration and correction

The revision table contains `replacements`, `deletions` and `updates`. Each update has an item ID, path, before/after values and responsible group. Canonical references are resolved only after all groups submit. Ambiguous references may resolve when all candidates collapse to the same group; otherwise they fail. Shared protocol validation checks schemas, name uniqueness, references, connection initiator/acceptor roles and field inventories.

Errors carry affected groups. Local errors reopen only those groups; name collisions or an explicit regroup request return to clustering. Unchanged group membership preserves accepted results. `workflow.max_feedback_rounds` limits correction rounds after the initial pass (default 2). Regrouping and local Merge correction share this limit. The saved value is retained on resume; older tasks without this setting retain their two-round limit. An unresolved hard error fails the task without `protocol.json`; there is no partial success or LLM Review/Fix loop.

`meta.complete=true` indicates completed extraction and deterministic gates. It is not an independent semantic judgment. Run reports and audit state `semantic_review=false`.

## Persistence and recovery

Storage version 3 separates documents/batches, assignments/attempts, credentials, items, immutable submissions, groups, snapshots and events. Short SQL transactions commit one result atomically. LLM work, HC calls and source preprocessing do not run inside write transactions. All references and writes share the same integration/validation functions used by audit.

Each submission is keyed by assignment, attempt and batch/group. Exact retries return the original acknowledgment even after a phase transition; changed payloads are rejected. Submission and completion state commit together. Restart runs the same automatic scheduler without duplicating accepted work. Credentials fence replaced attempts. The first valid call acknowledges a durable send intent; uncertain transport responses are never blindly resent.

HC initialization is serialized, then ready workers are dispatched fairly. Confirmed terminal exit/missing state allows at most two replacement attempts; completed items and submissions remain. A live blocked/idle-incomplete process requires diagnosis. Observation timeout never permits replacement. Processes are released between phases; native session IDs restore context when reused. Task-local service/supervisor locks and private HC namespace ownership prevent competing writers and cross-task cleanup.

Historical state is not migrated. Execute old tasks using their frozen package; read-only historical audit remains separate. Current tools never execute old claim/mutation/advance contracts.

## Verification

Unit/integration tests cover concurrent submissions, partition completeness, reference rewriting, dependency deduplication, conflicting edits, bounded correction, exact retries, stale attempts, restart, real socket/stdio MCP contracts and simulated HC dispatch/replacement. Audit reconstructs the frozen extraction from submissions, integrates group results again and compares publication plus revision table.

The paid FTP comparison freezes code and RFC959/RFC3659 once, then runs `parse_workers=1` and `3`, with `merge_workers=3` and Luna/high for both. Each run has a 40-minute cap. Time includes startup, all merge groups and correction work. Phase metrics distinguish agent/supervisor calls and record rejected calls, delivered tokens, repeated deliveries and worker occupancy; native token counters are reported separately when available. Structural success, timing and semantic assessment are separate results.
