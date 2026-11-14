# mcn-fuzz

Forkserver fuzzer. Each case starts a new child for the selected target. AFL++ PCGUARD is the coverage source. The host-side Python generator speaks the target protocol over native sockets. Campaigns are included for LightFTP and OpenSSL `s_server`.

## Source layout

```text
src/
  lib.rs, main.rs    Library and CLI entrypoints
  app/              Campaign configuration, run/replay, acceptance, benchmarks
  engine/           Executor, generator inputs, mutation, stagnation, synthesis
  runtime/          Supervisor, forkserver backend, generator IPC, lifecycle, reports
  coverage/         LLVM source coverage and PCAP capture
  agents/           Agent profiles and HC integration
  protocol/         Protocol models, stable IDs, read-only catalog
  evaluation/       Standalone synthesis evaluation and its CLI entrypoint
```

## Build

```sh
# AFL++ and the Python venv already live under fuzz/.deps and fuzz/.venv.
bash targets/lightftp/build-pcguard-asan.sh   # fuzz target
bash targets/lightftp/build-coverage.sh       # llvm-cov target
bash targets/lightftp/build-graphs.sh         # callgraph + CFG
cargo build --release
```

The three scripts build from `targets/lightftp/source`, the pristine LightFTP copy, so `.deps/LightFTP` and the source copy stay untouched. Each script copies the source, applies `targets/lightftp/forkserver-session.patch`, and writes into its own `build-*` directory. The ASAN variant uses `AFL_LLVM_INSTRUMENT=PCGUARD`, `AFL_LLVM_THREADSAFE_INST=1`, and `AFL_USE_ASAN=1`; the coverage variant uses `AFL_LLVM_ONLY_FSRV=1` plus profile flags; the graph variant uses `clang-18 -flto` with the same profile flags and exports `callgraph.dot` and `cfg/*.dot`.

## Run

Build as a normal user, then start the fuzzer with sudo. sudo restores the calling account from `SUDO_UID`/`SUDO_GID`. Pass `--run-as USER` when that account must be named explicitly. The fuzzer does not stay root, and it does not install a file-capability copy under `/usr/local`.

```sh
cargo build --release
sudo ./target/release/mcn-fuzz doctor
sudo ./target/release/mcn-fuzz accept --campaign targets/lightftp/campaign.toml
sudo ./target/release/mcn-fuzz run --campaign targets/lightftp/campaign.toml
sudo ./target/release/mcn-fuzz run -o /path/to/runs --campaign targets/lightftp/campaign.toml
sudo ./target/release/mcn-fuzz coverage -o /path/to/coverage-runs --campaign targets/lightftp/campaign.toml --corpus targets/lightftp/corpus --step 1
sudo ./target/release/mcn-fuzz replay --campaign targets/lightftp/campaign.toml --input targets/lightftp/corpus/<file>
```

`run -o PARENT` (or `--output PARENT`) creates a fresh
`mcn-fuzzer-YYYYMMDD-HHMM` directory below `PARENT` and uses it as the campaign
root. Without `-o`, the root is created below the current directory. A
same-minute collision gets a `-2`, `-3`, ... suffix. Relative `[campaign]
corpus`, `[campaign] crashes`, `[coverage] output`, and
`[generator_synthesis] output` paths are resolved below that root; absolute
paths are left unchanged. Inputs such as seeds, binaries, models, and documents
stay relative to the campaign file. `coverage -o PARENT` follows the same
timestamped-root rule and writes snapshots/analysis directly below the new
coverage campaign root. It never adds a build-hash directory.

When LLVM source coverage is enabled in `[coverage]`, the fuzzer stores only the merged profile: each snapshot keeps `coverage.profdata.zlib`, metadata, and the SUT logs, never a full JSON export. The analysis MCP service decompresses that profile, runs `llvm-cov export` itself when `load_coverage_json` needs the detailed counters, and removes its temporary JSON and profile once the model is built. Rust still runs a summary-only export to record line/branch totals in `metadata.json` for reporting. A snapshot directory contains only `coverage.profdata.zlib`, `metadata.json`, and the SUT logs: there is no `checkpoint/`, no `progress.json`, and no raw `.profraw`. `metadata.json` carries the snapshot number, cumulative replay count, summary, per-batch delta, case ids, and timings; it no longer stores fingerprints or binary hashes. A restarted campaign discovers progress from the existing snapshot metadata and merges the new raw profiles with `current.profdata`. The `--source-coverage-json` flag is gone. `[source_coverage]` is a configuration error; move that table to `[coverage]`. `analysis_enabled` is separate and requires collection. The private HC executable is `[agents].hc_executable` (default `hc`), not `generator_synthesis.hc_executable`. Analysis output lives under `coverage.output/analysis/`; the coverage root is no longer split by a build fingerprint. `targets/lightftp/build-graphs.sh` emits bitcode, `callgraph.dot`, CFG dots, and `source-map.json`; `targets/lightftp/build-coverage.sh` emits only the coverage binary.

Set `[campaign].timeout_secs` to stop the fuzz loop after a wall-clock duration. When present, it takes precedence over `[campaign].iterations`; the CLI `--iterations` option remains an explicit override. The deadline is checked between complete LibAFL iterations, so an iteration already in progress can finish before shutdown.

`setup-capture.sh` checks Linux, pkg-config, and libpcap, then builds the Release binary with the stats feature. It does not call sudo or setcap.

`replay` reads the corpus input file. LibAFL also writes `.metadata` sidecars; those are not generator inputs.

Optional `[generator_synthesis]` synthesizes decorated step-handler scripts from an RSC protocol model. It is off when the section is omitted. The `run` command is the only entry that starts a campaign-private HC core; replay, accept, and benchmark do not. The core starts when synthesis or coverage analysis is enabled, and both agents share it. Profiles live under `[agents.profiles.<name>]` with `provider` set to `codex`, `claude`, or `pi`. A failed synthesis task is recorded and existing corpus entries keep fuzzing.

`[generator_synthesis]` replaces the removed `[protocol_generation]` table; the
old name is a configuration error with a migration hint, including when both
tables appear. Optional `extra_files` lists read-only campaign files whose paths
are resolved relative to the campaign TOML. Both synthesis prompts list their
absolute paths and allow deployment facts such as accounts, permissions,
certificates, and roots to come from them; protocol behavior still follows the
proposal and RFCs. Missing, unreadable, or non-regular files fail context
construction with a `generator_synthesis.extra_files[index]` error. The files are
not copied into prompts and generators must not read them at testcase runtime.

Fuzz-stage scheduling uses consecutive ordinary executions without newly occupied
PCGUARD bitmap slots. Configure the positive threshold under `[campaign]`:

```toml
[campaign]
bitmap_stagnation_limit = 256
```

New slots reset the streak; hitcount changes on known slots and the runtime marker
at index 0 do not count as new coverage. Seeds update the bitmap baseline without
increasing the streak, and invalid cases are ignored. A published synthesis
generator is never evaluated as-is, so its initial parameters cannot enter the
corpus.
Once the threshold is reached, the trigger stays set until the current mutational
stage finishes. Source coverage then runs at the end of that round; synthesis is
requested at the start of the next round. While an agent task is in flight,
ordinary mutation continues and updates the bitmap baseline, but the no-growth
streak is paused at zero and no new source-coverage/synthesis trigger is queued.
After the main loop handles the task's result, counting restarts from zero; a
fresh threshold is required for the next request. Synthesis stays asynchronous.
Disabled source coverage skips collection and still releases
the next-round request; source-coverage errors are logged and do the same.
Disabled or paused synthesis consumes the request without launching an agent.
Agent initialization also runs in the background: campaigns with seeds load them
and mutate while the agent prepares. Each synthesis-stage visit checks readiness;
while initialization is pending it skips dispatch and retains the trigger for a
later round. Initialization failures pause synthesis while ordinary fuzzing continues.
Seedless campaigns wait for initialization and the first synthesis input.
Before the ordinary fuzzer loop starts, the same mutation stage is invoked
directly while its FIFO is non-empty. It runs the baseline once through normal
feedback, then applies the ordinary mutation/evaluation loop until a mutated
child enters the corpus; the baseline is kept when the evaluator finds it
interesting. The mutation stage is the only
consumer of the FIFO, and the normal `synthesis → mutation → coverage replay →
coverage analysis` order is unchanged once the corpus is schedulable.

Round-based `interval` settings have been removed from `[generator_synthesis]`
and `[coverage]`; configuring them is an error. Initial and final
source-coverage snapshots, and first-generator bootstrap when seeds are
absent, still run as before. With seeds present, round 1 no longer starts an agent
task automatically. Source coverage still requires `[coverage].enabled=true`
and a coverage-built binary; synthesis requires `[generator_synthesis].enabled=true`.
The shared agent runtime lives in `src/agents/worker.rs`. `AgentClient<I, T>` owns the command/outcome channels and background HC terminal. `AgentInit` supplies initialization checks and the ready artifact; `AgentTask` supplies request preparation and artifact acceptance. Both agents report `AgentOutcome` values, share session transitions/logging in `agents/session.rs`, and use `agents/artifacts.rs` for JSON reads, identity checks, and sealed tool inputs. Stages apply accepted results to LibAFL state at stage boundaries; the generic worker never accesses fuzz state.

Coverage analysis uses the same `[coverage]` table and does not require synthesis. Both agents queue initialization together before seeds are loaded or source coverage is replayed. Each has a background HC worker, command/outcome channels, a persistent terminal, and at most one request in flight. Coverage source verification and handler extraction run in the background; the stage skips dispatch until initialization is ready. New snapshots replace the queued snapshot while a request is running. Completed snapshots are not dispatched again.

Coverage analysis is driven by a campaign-private MCP service. The supervisor
starts `python -m coverage_analysis.server serve` before it enters the SUT network
namespace, next to the private HC core. The service owns all parsed coverage,
CFG, callgraph, handler, and protocol-graph state in memory; it starts empty and
never reads campaign files on its own. Each coverage agent session gets
`-c mcp_servers.coverage.*` overrides that launch the stateless
`coverage_analysis.server stdio` bridge, so the user's Codex configuration is not
modified. Set `[coverage].mcp_socket` to connect to a supervisor-provided service
instead; `mcp_ready_timeout_secs` defaults to 30. PID 1 creates
`coverage.output` and `generator_synthesis.output` with campaign ownership before
either service starts, so the MCP working directory and HC state stay writable
after privileges drop. The `run -o` campaign root and its configured output
directories are created before this hand-off. The analysis profile must use `provider = "codex"`; a
wrapper executable that ignores its argv cannot register the bridge, so the stage
rejects non-Codex providers instead of simulating tools with shell commands.
A reusable analysis profile looks like this (the executable must forward the
extra `-c` arguments; a wrapper that ignores its argv will not work):

```toml
[coverage]
analysis_enabled = true
analysis_profile = "codex-mcp"

[agents.profiles.codex-mcp]
provider = "codex"
executable = "/home/user/.nvm/current/bin/codex"
model = "gpt-6-luna"
args = ["-c", "model_reasoning_effort=max"]
```

Parsed data, the committed graph checkpoint, `service.jsonl` call records, and
`service.log` live under `coverage.output/mcp-state/`; only the transient MCP
socket lives under `/tmp/mcn-mcp-*`. `service.jsonl` is the audit trail for
agent tool calls.

The service tool set is explicit: `load_coverage_json`, `load_cfg`, `load_callgraph`,
`load_protocol`, `load_handlers`, `get_loaded_state`; five coverage views
(`show_overview/module/file/handler/func_code_coverage`); `get_reachable_functions`,
`get_reaching_handlers`, and `show_source_coverage`; and protocol-graph tools
(`get_protocol_steps`, `get_step_dependencies`, `update_step_annotations`,
`upsert_dependency`, `remove_dependency`, `begin_analysis`, `submit_proposal`).
Unloaded data returns `not_loaded` with the load tool to call; mismatched builds
return `incompatible_data`. Coverage counts come from the frozen
`coverage.json` true/false source branches, not from reconstructed IR edges.

After a coverage snapshot is frozen, the coverage agent loads that request's
inputs, explores the handler and reachable-function summaries, and writes a
proposal in a graph transaction. `submit_proposal` validates step names, handler
associations, named instances, and dependencies, then freezes `proposal.json`.
Rust rechecks the proposal and party binding, commits the transaction through
the service, and places the proposal in a one-slot mailbox. `regions.json`, `priority-delta.json`,
`protocol-refined.json`, the Rust exploration graph, and trace selection are no
longer produced or maintained by Rust.

Accepted generators are put into a FIFO in LibAFL state metadata instead of
being evaluated. The ordinary mutation stage checks that FIFO before asking the
scheduler: a non-empty queue supplies the input for this round, while an empty
queue uses the normal scheduler/corpus testcase. For a queued synthesis input,
the same mutation stage first runs its original initial parameters once through
normal feedback, then applies the existing `GeneratorMutator` and ordinary
mutation iterations/evaluation/feedback. The baseline enters the corpus only
when feedback keeps it. Only mutated inputs that pass normal feedback enter the corpus,
where subsequent mutation rounds continue to evolve them. There is no separate
candidate stage, no bypass mutation stage, and no temporary corpus insertion.

The FIFO is persisted to `generator_synthesis.output/candidates.json` and
restored on restart. It contains only pending inputs: taking one hands it to the
mutation stage and immediately removes it from the file, so a restart cannot run
its baseline twice. An old candidate-stage file is migrated when loaded: pending
queue entries and an unbaselined active entry are preserved, while an already
baselined active entry is dropped because its partial mutation state cannot be
reconstructed. Novel children receive the proposal's `TraceOrigin` metadata
before they are written back to the on-disk corpus.

The synthesis stage copies the accepted proposal into the request directory
after stripping `mutations` and adding `mutable_fields`, then points the prompt
at that downstream description instead of a receipt. Coverage
mode never generates without a proposal. Autonomous mode (analysis disabled, or
seedless bootstrap) asks the agent to write `proposal.draft.json`, run
`python -m coverage_analysis.server proposal` to validate named instances, and
then generate. Both modes publish `generator.py`, `params.schema.json`, `params.json`, and
`result.json`; Rust validates the proposal, code, and initial mutable-field
defaults in-process before publishing the generator. The proposal copy handed to
synthesis has `mutations` stripped and replaced by per-instance
`mutable_fields`; dependencies stay in the proposal. Failed or expired
proposals are aborted and leave existing corpus entries untouched.

The synthesizer prompts are Markdown MiniJinja templates in `prompts/`. They are embedded in the fuzzer at build time, so rebuild after editing them. Both prompts point the agent at two read-only references: [`python/generator_server/template.py`](python/generator_server/template.py), a detailed scaffold with common imports, `ctx` helpers and one commented step-handler example for each ProfuzzGym target (DAAP, DICOM, DNS, DTLS, BFTPD, LightFTP, ProFTPD, PureFTPD, RTSP, SIP, SMTP, SSH, TLS), and [`python/generator_server/params.schema.json`](python/generator_server/params.schema.json), the empty production-compatible parameter-schema shape. The scaffold is a copy reference, not a runnable generator; only the request's `generator.py` and extended `params.schema.json` are published.

RSC protocol objects and references use names. `protocol.json` metadata contains
only `protocol`, `complete`, and `documents`; each source document has `doc_id`,
`rfc`, and `path`. RSC application schema versions and source hashes are absent.
Source document/block locators and protocol payload fields such as version
numbers or cryptographic hashes remain valid. The readers no longer branch on
legacy schema/version markers; malformed or old shapes fail during JSON/serde
parsing.

`synthesis-init.md` establishes the campaign background, reference paths, party roles, workflow, and artifact contract. `synthesis-generation.md` supplies each proposal's task with the public Python API, prerequisite handling, SDK/mock guidance, worker lifecycle, and publication rules. Both receive the same read-only campaign context with absolute paths and execution budgets. Missing template variables fail rendering; completion JSON examples are serialized by Rust before injection.

Both prompts also inject the absolute path to [the protocol module skill](skills/protocol-modules/SKILL.md) and require the agent to read it before selecting libraries. It covers all 13 ProfuzzGym targets (four FTP variants), with message/packet codecs, upstream/server SDKs, crypto helpers, import names, installed versions and limitations. Keep the skill and its references accessible at runtime. Evaluation fingerprints the skill directory along with the prompt templates, so changing its contents requires preparing a new evaluation task.

Proposals contain the ordered trace with per-step reasons and constraints, the selected definitions, handler references, and dependencies. Rust instantiates the proposal as a `TraceInstance` containing concrete steps and dependencies. Proposal mutations are field-only (`op: "field"`); each may declare a `kind`
(`string`, `bytes`, `integer`, `boolean`, `list`) and, for `bytes`, an
`encoding` (`hex` or `base64`). Execution input carries the generator path,
`step_trace`, dependencies, and per-instance `params` keyed by step instance
name; no execution-format version or proposal request ID is embedded. Each step instance carries explicit `omitted_fields`, dependency `inputs`, and broken-dependency metadata. Mutable values for an instance live only in `params[instance_name]`. At import the runtime builds a mapping from `@step_handler("model step name")` declarations; during execution it calls an optional `@global_init` once, then dispatches each instance in order to its handler with `(ctx, params)`. Params and `ctx.step_instance` are read-only; `ctx.step_name` is the current instance name. The handler resolves its instance's params entry, omissions and dependency inputs, performs the step action, and uses `ctx.save_state(field, value)` and `ctx.load_state(field)` for the current instance, and `ctx.load_depending_state(field)` for bound prior values; `ctx.states` lives until the generator exits. They do not embed the full model. The skill requires the agent to run [the fixed artifact validator](skills/protocol-modules/scripts/validate_generator.py) on `result.draft.json` before atomically publishing `result.json`. The agent additionally runs its bounded import probe. Fuzzer acceptance rechecks the completion IDs, parameter schema and values, and Python syntax and entrypoint in Rust using the RustPython compiler; it does not launch Python or run the generator or SUT.

The prepared environment is CPython 3.12 on Linux x86_64. Install its pinned dependencies into the campaign interpreter (the supplied campaigns use `.venv/bin/python`):

```sh
.venv/bin/python -m pip install -r python/requirements-protocols.txt
.venv/bin/python -m pip check
.venv/bin/python skills/protocol-modules/scripts/audit.py --network
```

The audit checks 32 cataloged distributions and runs protocol/crypto/codec probes; `--network` additionally exercises temporary loopback DNS, FTP, SMTP, DICOM, SSH and HTTP/DMAP peers. These checks verify the library environment, not interoperability with every ProFuzzBench target. Synthesis agents use installed modules and do not install dependencies themselves.

Coverage-analysis init and task prompts receive `[target].source_dir` as `SOURCE_DIR`,
plus `[target].configure_options` and `[target].cli_args` as `CONFIGURE_OPTIONS` and
`CLI_ARGS`. The prompts tell the agent to combine those switches with other build
material (`configdata.pm`, `CMakeCache.txt`, generated headers, build logs) to
decide which protocol features are compiled in, to avoid goals for disabled
features, and to prioritize enabled features in proposals and trace selection.
Synthesizer prompts do not receive that path, even when it is configured.
Both prompts and the module skill prohibit reading SUT source or using SUT feature
support or expected message acceptance to decide whether to generate. Protocol
behavior comes from the proposal and RFCs; public generator API/SDK references
and static artifact validation remain available. Runtime replies, failures and
coverage are evaluated by the fuzzer after publication.

`WORKSPACE` is the agent's fixed working directory (`generator_synthesis.output`). Each generation request receives a separate `OUTPUT_DIR` at `WORKSPACE/requests/<request-id>/` for its proposal (mutations stripped, mutable fields added), `generator.py`, initial field-default schema, and completion JSON. Accepted generators are copied to that directory's `published/`. Initialization writes `ready.json` to `WORKSPACE/init/`. Request artifacts are never written directly to the workspace root.

When synthesis is enabled, `[parties]` and `generator_synthesis.model` are required. A campaign that includes `[parties]` resolves those selectors against the model before cases start, even when synthesis is disabled. A missing model is an error. `sut` and `generator` are each a non-empty list of protocol party names or `"*"`. `"*"` means every model party not named by the other side. Parties in neither set stay unavailable. The removed `generator_synthesis.bindings` table is a configuration error. The LightFTP campaign assigns the server parties to the SUT and the remaining parties to the generator.



Python unit tests:

```sh
PYTHONPATH=python .venv/bin/python -m unittest discover -s python/tests -v
```

Stats lines named `coverage_map_occupancy` count occupied entries in the shared PCGUARD map. They are not source-coverage percentages. The runtime may set map index 0 as a marker; that byte alone is not treated as coverage.

A generator I/O timeout and a Python stall are stored on the generator status. The case is a target hang only when the case deadline or the exit grace runs out while the target is still running. A normal target exit is not a target timeout. A real target SIGSEGV is a crash.

If the target reports a map larger than the campaign allocation, set `map_size` under `[target]` in the campaign file. The default is 65536. `core` under `[campaign]` is optional. When it is omitted, the forkserver is not pinned. Set `core = N` to pin it. Coverage does not depend on that pin.

`target.extra_env` must not set `AFL_LLVM_ONLY_FSRV`, `AFL_GCC_ONLY_FSRV`, `__AFL_SHM_ID`, `__AFL_SHM_ID_SIZE`, or `AFL_MAP_SIZE`. The first two disable PCGUARD counters when present, including a value of `0`. The other three name the shared coverage map, which the fuzzer allocates itself.

## OpenSSL s_server

OpenSSL 4.0.2 is built two ways, one directory per instrumentation variant (plus a graph-only build). All use the same tarball (SHA256 `736b467530f916737b7031310ccb21d8218c6229e61e8e160cd1d3458cd543a8`), the same source under `.deps/openssl/openssl-4.0.2`, and match the 4.0.2 LLVM graphs under `targets/openssl/llvm-compatible`.

| script | directory | instrumentation |
|---|---|---|
| `build-pcguard-asan.sh` | `build-pcguard-asan/tree/apps/openssl` | `AFL_LLVM_INSTRUMENT=PCGUARD`, `AFL_LLVM_THREADSAFE_INST=1`, `AFL_MAP_SIZE=1048576`, `AFL_USE_ASAN=1`, `-fsanitize=address` |
| `build-coverage.sh` | `build-llvm-cov/tree/apps/openssl` | forkserver-only, llvm-cov source counters |

Both variants Configure with `no-shared no-module no-asm no-tests`, then compile `openssl`, `libssl`, and `libcrypto` with `afl-clang-fast`. The ASAN build requires `__asan_init`, `__sanitizer_cov_trace_pc_guard`, and `__afl_area_ptr`; the coverage build requires llvm profile counters. Each script checks `AFL_DUMP_MAP_SIZE` against the 1 MiB campaign allocation. `gen_certs.sh` writes one unencrypted RSA 2048 certificate into `targets/openssl/fixture/`; that directory, the build trees, and the private key are gitignored. `cleanup.sh` copies the certificate and key from there.

The ASAN build is the fuzzing target for sanitizer campaigns: the fuzzer sets `ASAN_OPTIONS` with `abort_on_error=1`, so an ASAN report ends the child with `SIGABRT` and is recorded as a crash. The forkserver also parses `asan/asanlog.<pid>` through `AsanBacktraceObserver`. The coverage build stays non-ASAN so source counters are not distorted.

A campaign can set `target.cleanup_script` to a shell script that prepares or resets `workdir` before startup and each case. The script runs with `workdir` as its current directory and receives `MCN_FUZZ_WORKDIR`, `MCN_FUZZ_HOST`, and `MCN_FUZZ_PORT`. OpenSSL's script copies its certificate and key. OpenSSL args use `@PORT@` for the chosen listener port. The campaign allocates a 1 MiB map; the handshake rejects a target that needs a larger one. The generator sends one ClientHello on its own TCP connection. Do not add a readiness connection: `-naccept 1` would treat that probe as the case.

```sh
bash targets/openssl/build-pcguard-asan.sh
bash targets/openssl/build-coverage.sh
sudo ./target/release/mcn-fuzz run --campaign targets/openssl/campaign.toml
sudo ./target/release/mcn-fuzz accept --campaign targets/openssl/campaign.toml
sudo ./target/release/mcn-fuzz replay --campaign targets/openssl/campaign.toml --input targets/openssl/corpus/<file>
```

All campaigns use the ASAN target; only the coverage binary is non-ASAN. `build-graphs.sh` is a third build used solely to export `callgraph.dot` and `cfg/*.dot`; it keeps the same profile-instrumentation flags so inlining matches the coverage binary. Campaigns read the graphs from `build-graphs/graphs/`. `campaign-asan-smoke.toml` is a short ASAN plus coverage smoke run.

`build-graphs/graphs` is exported from the pruned 4.0.2 source: `opt -passes=dot-callgraph` on the linked bitcode, and `dot-cfg-only` per object in parallel (the linked-module pass is ~100x slower). The old `llvm-compatible/` SVF bundle stays for history but is no longer referenced by campaigns. The alias join still reports inline/header function records that have no standalone symbol as unmatched instead of guessing.

`generators/tls12.py` is a params-only example; new synthesis generators use decorated step handlers. It reads the current instance's entry from `params`, rebuilds TLS lengths, then applies `version`, `cipher`, `sni`, `extensions_hex`, and `truncate`. The response reader takes the 5-byte record header and then at most 18432 body bytes. A ServerHello is `ok`. An alert is `alert`. A short close is `eof`. Connect failures stay `conn` and read deadlines stay `timeout`.
