# TOML profiles and agent adapters: acceptance

Verified on 2026-11-08. All changes are confined to RSC.

## Configuration and runtime

- User settings are TOML, parsed by `tomllib`; `config.example.toml` contains Codex and Pi profiles. Old JSON and inline role settings are rejected.
- CLI common/role selection, immutable role snapshots, same-profile retry, explicit-config resume, environment validation/overrides/isolation, and redacted summaries have automated coverage.
- Codex and Pi use adapter-generated launch specifications. HC receives only the Python launcher and a private file path; explicit environment values stay in 0600 settings. Ambient credentials are inherited, not serialized.
- Pi 1.5.0 is installed at `/home/user/.local/bin/pi`, using the authorized user-local npm prefix because the system prefix is not writable. Node is 24.21.0.
- Pi runs interactively with native MCP and OSC 7501. It uses offline **startup** to avoid automatic downloads/catalog refreshes; explicit model API requests work normally.
- Pi initially reports native idle before initialization finishes. The first trial at `runs/providers-1108` exposed this: the submitted task was not claimed. Scheduling stopped without blind resend, and that trial's private agents/core were cleaned. The adapter now waits for its `session_start` readiness marker before sending; an automated regression test covers that gate.
- The packaged extension restores original MCP content, preserves tool errors, and aborts/blocks subsequent tools on restoration or model-selection failure. Tests invoke Pi's installed native `convertMcpResult()` to produce an actual truncated result above 20 KiB, then verify the complete restored content. A built wheel contains the identical extension source.

## Real API and workflow runs

Artifacts: `runs/providers-1108b/`. The opt-in runner is `scripts/verify_agent_providers.py`.
The key was entered through a non-echoing prompt and forwarded only in process environment.
No key value is stored in configuration, argv, logs, or the acceptance report.

The initial probe uses the official `https://api.deepseek.com/chat/completions`,
model `deepseek-flash`, thinking `high`. It completes a real tool call and a
follow-up request retaining the required reasoning/tool history; `api-probe.json`
contains success, elapsed time and token usage, with no prompts, reasoning text or credentials.

Each workflow extracts the same small fictional RFC: TCP port 9000, HELLO(token),
WELCOME(the same token), CRLF framing and the ordered/correlated dependency.

| Combination | Parser | Reviewer | Seconds | Reviews | Server tool calls | Rejected calls |
|---|---|---|---:|---:|---:|---:|
| Pi/Pi | DeepSeek Flash/high | DeepSeek Flash/high | 112.48 | 2 | 58 | 1 |
| Codex/Codex | GPT-6-Luna/max | GPT-6-Luna/max | 300.37 | 2 | 65 | 1 |
| Codex/Pi | GPT-6-Luna/max | DeepSeek Flash/high | 124.54 | 1 | 39 | 0 |

All three publish `complete=true`: 2 parties, 2 messages, 1 channel, 2 steps,
1 dependency, 0 unresolved. The two homogeneous runs exercise real findings,
repair to the original parser, and re-review by the original reviewer; no seeded
model was used. The mixed run passes its first review.

The original `t1`/`t2` and their native foreground PIDs remain stable through
handoffs. Every run passes `rsc audit`; `verification.json` and `results.json`
record complete evidence and metrics. Private profile/launch settings have mode
0600; all three report cleanup complete, their workflow sockets are removed,
and the runner verifies that the provided key was not persisted anywhere in the
test artifact tree. The failed initial trial was separately cleaned and checked.

Server call totals include supervisor status checks; they are not LLM request
counts. Timings include startup, workflow, audit and cleanup and are a small
functional smoke, not a quality or performance benchmark. No FTP full run was started.

## Automated checks

Run `python -m pytest -q tests` from the RSC project directory. Tests exercise
TOML parsing/migration, profile precedence/fixing, environment handling, native
Pi result restoration, startup readiness and failure blocking, alongside the
existing source batching, workflow gate, socket recovery and protocol tests.
Ruff, formatting and whitespace checks cover the changed implementation/tests;
wheel verification checks that the Pi extension is shipped.

Final regression: **133 tests and 74 subtests passed**. Ruff and formatting checks passed for all changed integration modules, adapters, scripts and tests; `git diff --check` passed.
