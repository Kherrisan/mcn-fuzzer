# File pipeline verification

This document describes the new file workflow. Earlier SQLite-era trials use different
contracts and cannot establish acceptance of this implementation. Run them with their
archived source package; the current runtime and audit intentionally reject old databases.

## Automated checks

| Area | Coverage |
| --- | --- |
| Planning | Capacity formula, automatic/manual count, contiguous chapter locality, capacity-bound source splitting, complete coverage and concurrency limits. |
| File submissions | Whole checkpoints, progressive overwrite, strict shape/evidence validation, unique names, completed-range monotonicity and content-equal retries. |
| Phase paths | Single parser bypasses Merge; multiple parsers feed exactly one whole-input merger; merge budget fit/failure preserves parser outputs. |
| Validation | References, roles, dependencies and fields validated before review; correctable whole-model errors produce Fix assignments. |
| Review/Fix | Independent reviewer, declared full/scoped coverage, original author ownership, reverse-reference scope, global fallback, retained ambiguity and bounded feedback. |
| Publication | Correctable errors prohibit publication; source ambiguity preserves evidence and publishes incomplete; audit detects changed candidates, exports and source text. |
| Recovery | Accepted output survives interrupted state advancement, stale sessions are revoked, launch intent recovered by terminal name, native author sessions resume. |
| HC dispatch | Serialized startup/rename, concurrent fair dispatch within cap, confirmed-death replacement, timeout restraint and bounded same-session continuation. |
| MCP | Real Unix socket and stdio JSON-RPC, five-tool catalog, phase authorization, compact complete result/error delivery and bounded pagination. |
| Format boundary | No workflow database, object IDs, revision table, application hash/version metadata, or legacy mutation/receipt tools. |

Normal tests use local fixtures and simulated HC observations; they do not start paid agents.
Native Codex session lookup remains exercised separately because its internal storage is
not RSC workflow storage.

```sh
.venv/bin/pytest -q
.venv/bin/ruff check rsc tests
.venv/bin/rsc config check --config config.example.toml
```

## Live acceptance

The opt-in benchmark freezes source code and RFC959/RFC3659, then runs native Codex with
`gpt-6-luna/high` through HC. Required evidence is an automatic-planning trial, two repeats on
fixed code, and a two-parser trial if automatic planning chooses one. Each report should
record phase wall times, context/token usage where available, calls, findings, fixes,
publication result and audit outcome. An interrupted or failed run remains a recorded failure.

The main integration runner owns those trials and their generated reports. This document
does not claim live success or a twenty-minute result before those reports exist.

Declared review coverage records what the independent reviewer reports checking. Shell and
MCP reads are both valid; there are no mandatory read receipts. File audit verifies the
recorded workflow and source/protocol structure, not the semantic truth of every finding.
