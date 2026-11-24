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
  protocol/         Protocol models and party bindings
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
stay relative to the campaign file.

`coverage -o DIR` writes `cov_over_time.csv` and its snapshots directly into
`DIR`, which must be empty; it never appends a timestamped child. `run` keeps
the timestamped root because several campaigns can share one parent. Neither
command adds a build-hash directory.

When LLVM source coverage is enabled in `[coverage]`, the fuzzer stores only the merged profile: each snapshot keeps `coverage.profdata.zlib`, metadata, and the SUT logs, never a full JSON export. The analysis MCP service decompresses that profile, runs `llvm-cov export` itself when `load_coverage_json` needs the detailed counters, and removes its temporary JSON and profile once the model is built. Rust still runs a summary-only export to record line/branch totals in `metadata.json` for reporting. A snapshot directory contains only `coverage.profdata.zlib`, `metadata.json`, and the SUT logs: there is no `checkpoint/`, no `progress.json`, and no raw `.profraw`. `metadata.json` carries the snapshot number, cumulative replay count, summary, per-batch delta, case ids, and timings; it no longer stores fingerprints or binary hashes. A restarted campaign discovers progress from the existing snapshot metadata and merges the new raw profiles with `current.profdata`. The `--source-coverage-json` flag is gone. `[source_coverage]` is a configuration error; move that table to `[coverage]`. `analysis_enabled` is separate and requires collection. The private HC executable is `[agents].hc_executable` (default `hc`), not `generator_synthesis.hc_executable`. Analysis output lives under `coverage.output/analysis/`; the coverage root is no longer split by a build fingerprint. `targets/lightftp/build-graphs.sh` emits bitcode, `callgraph.dot`, CFG dots, and `source-map.json`; `targets/lightftp/build-coverage.sh` emits only the coverage binary.

### PCGUARD index to source location

A PCGUARD map index is the guard value: AFL++ assigns IDs sequentially from 5 in
`__sancov_guards` order. When the binary is built with
`AFL_LLVM_INSTRUMENT=LLVMNATIVE` (native `trace-pc-guard,pc-table`), it also
carries `__sancov_pcs`, whose entries are index-aligned with the guards. That is
enough to precompute the mapping without running the target:

```sh
mcn-fuzz symbol-map --binary build-pcguard-asan/fftp \
  --index-map build-pcguard-asan/index-map.z \
  --location-map build-pcguard-asan/location-map.z

mcn-fuzz symbol-map --index-map build-pcguard-asan/index-map.z \
  --location-map build-pcguard-asan/location-map.z --lookup-index 147
mcn-fuzz symbol-map --index-map build-pcguard-asan/index-map.z \
  --location-map build-pcguard-asan/location-map.z --lookup-location ftpserv.c:310
```

Both files are zlib-compressed varint tables. `index-map.z` stores
`map index -> outermost frame` (the function that contains the block, not an
inlined callee); `location-map.z` stores `file:line -> map indexes` and is
one-to-many because inlining, optimization and macros can attribute several
blocks to one line. `--lookup-location` accepts an absolute path or a path
suffix such as `ftpserv.c:310`.

Campaigns point at the generated tables with optional `[coverage]` keys:

```toml
[coverage]
index_map = "build-pcguard-asan/index-map.z"
location_map = "build-pcguard-asan/location-map.z"
```

The maps are build-specific: regenerate them after every rebuild. `-O1 -g` is
used for both the PCGUARD and llvm-cov builds so line attribution stays accurate
and consistent.

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
A new campaign needs usable seeds or a previously populated corpus. Pending
synthesis candidates from an earlier run may also supply the first corpus entry.

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

Coverage MCP tools use Pydantic input and output models. The stdio bridge
publishes both schemas and returns successful results as JSON text and MCP
structured content. The Unix socket and MCP bridge share validation and the
existing structured error codes. `mcp` and `pydantic` are direct Python
dependencies of the coverage service.

The service tool set is explicit: `load_coverage_json`, `load_cfg`, `load_callgraph`,
`load_protocol`, `load_handlers`, `get_loaded_state`; five coverage views
(`show_overview/module/file/handler/func_code_coverage`); `get_reachable_functions`,
`get_reaching_handlers`, and `show_source_coverage`; and protocol-graph tools
(`get_protocol_steps`, `get_step_dependencies`, `update_step_annotations`,
`upsert_dependency`, `remove_dependency`, `begin_analysis`, `submit_proposal`).
Unloaded data returns `not_loaded` with the load tool to call; mismatched builds
return `incompatible_data`. Coverage counts come from the frozen
`coverage.json` true/false source branches, not from reconstructed IR edges.

Function source windows (`show_function_source`, `show_source_coverage` with a
function, and `describe_handler`) use Tree-sitter C/C++ definition boundaries.
The returned `range` describes the whole function; `start`/`end` and `remaining`
describe the bounded window. Branch coordinates remain coverage annotations and
do not truncate the function. Parsing is cached by resolved path, modification
time, and file size. Missing or ambiguous definitions return
`source_range_unavailable`; use an explicit file window to inspect unsupported
or macro-generated definitions. Install the parser dependencies from
`python/pyproject.toml` or `python/requirements-protocols.txt`.

After a coverage snapshot is frozen, the coverage agent loads that request's
inputs, explores the handler and reachable-function summaries, and writes a
proposal in a graph transaction. `submit_proposal` is the single validator: it
checks step names, handler associations, party/channel binding, dependency
order, mutation kinds with their defaults, and that every mutation lands on a
generator-filled step. It also derives each instance's `mutable_fields` and
rejects a proposal that cannot project onto a generator action. Only then does
it freeze `proposal.json`. Rust trusts that verdict: it commits the transaction
through the service and places the proposal in a one-slot mailbox. `regions.json`, `priority-delta.json`,
`protocol-refined.json`, the Rust exploration graph, and trace selection are no
longer produced or maintained by Rust.

Accepted generators are put into a FIFO in LibAFL state metadata instead of
being evaluated. The ordinary mutation stage checks that FIFO before asking the
scheduler: a non-empty queue supplies the input for this round, while an empty
queue uses the normal scheduler/corpus testcase. For a queued synthesis input,
the same mutation stage first runs its original initial parameters once through
normal feedback, then applies the existing `GeneratorMutator` and ordinary
mutation iterations/evaluation/feedback. A candidate receives
`clamp(64 * mutable_fields_total, 512, 2048)` evaluations, where the field total
sums the per-instance `params` field counts; ordinary corpus rounds keep the `1 + rand(0..127)` schedule.
When the generator's `mutable_fields.json` declares both groups, the candidate
budget halves: analyzed fields are offered first, the odd evaluation goes to
analyzed, and adhoc fields follow. A generator with only one group keeps the full
budget for it, and a pool that runs dry falls back to every field. Ordinary
corpus rounds may pick either group. Each mutation event records the field's
`origin`.
Skipped or unchanged mutations do not consume the candidate budget. The baseline
enters the corpus only
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

The campaign root has one unified `events.jsonl`. Every line is
`{"time_ms": ..., "kind": ..., "body": ...}`: case outcomes use `kind: "case"`,
and mutation-stage selections use `kind: "corpus_selected"` or
`"candidate_selected"`. `corpus_selected` carries the scheduler case id
(`corpus:<id>`) and current corpus count; `candidate_selected` carries the
synthesis request id and corpus count. Row order is the event order, so case
events and scheduler choices share one timeline.

The synthesis stage copies the accepted proposal into the request directory
with the validated `mutable_fields` and a trace projected
onto generator-owned actions: generator sends keep `receiver` and drop
`sender`, SUT sends become `recv` with `sender` and no `receiver`, SUT
listen/connect/close steps are omitted, and generator network actions remain.
The prompt points at that downstream description instead of a receipt. Synthesis requires an accepted coverage proposal. The agent writes
`generator.py`, `params.schema.json`, and `params.json`, optionally with
`mutable_fields.json` when it exposes fields the proposal did not declare, then
signals completion with `result.json`. `mutable_fields.json` is keyed by model step, because one handler serves every
instance of that step and Rust later creates, renames, duplicates, and drops
instances. Each step lists its fields with `origin` `analyzed` or `adhoc`, the
declared kind and encoding, the default and override rules, whether omission is
supported, a sample value, and the agent's own `verified` capability check, plus
a required `reason` recording what the agent audited: candidates it rejected and
why, or why there were none. Analyzed fields must equal the proposal's set for
that step, every instance of the step must carry exactly the declared fields, and
adhoc fields must be new and verified. Rust
validates a staged copy before publishing the accepted files with one directory
rename, so a rejected manifest leaves no `published/` directory or staging
residue. The proposal copy handed to
synthesis carries the validated `mutable_fields` and
its trace projected to the generator perspective; dependencies stay in the proposal and
are filtered if a dropped SUT setup step was an endpoint. Failed or expired
proposals are aborted and leave existing corpus entries untouched.

The synthesizer prompts are Markdown MiniJinja templates in `prompts/`. They are embedded in the fuzzer at build time, so rebuild after editing them. Both prompts point the agent at two read-only references: [`python/generator_server/template.py`](python/generator_server/template.py), a detailed scaffold with common imports, `ctx` helpers and one commented step-handler example for each ProfuzzGym target (DAAP, DICOM, DNS, DTLS, BFTPD, LightFTP, ProFTPD, PureFTPD, RTSP, SIP, SMTP, SSH, TLS), and [`python/generator_server/params.schema.json`](python/generator_server/params.schema.json), the empty production-compatible parameter-schema shape. The scaffold is a copy reference, not a runnable generator; the request's generator, schema, and initial parameters are published together.

RSC protocol objects and references use names. `protocol.json` metadata contains
only `protocol`, `complete`, and `documents`; each source document has `doc_id`,
`rfc`, and `path`. RSC application schema versions and source hashes are absent.
Source document/block locators and protocol payload fields such as version
numbers or cryptographic hashes remain valid. The readers no longer branch on
legacy schema/version markers; malformed or old shapes fail during JSON/serde
parsing.

`synthesis-init.md` establishes the campaign background, reference paths, party roles, workflow, and artifact contract. `synthesis-generation.md` supplies each proposal's task with the public Python API, prerequisite handling, SDK/mock guidance, worker lifecycle, and publication rules. Both receive the same read-only campaign context with absolute paths and execution budgets. Missing template variables fail rendering; completion JSON examples are serialized by Rust before injection.

Both prompts also inject the absolute path to [the protocol module skill](skills/protocol-modules/SKILL.md) and require the agent to read it before selecting libraries. It covers all 13 ProfuzzGym targets (four FTP variants), with message/packet codecs, upstream/server SDKs, crypto helpers, import names, installed versions and limitations. Keep the skill and its references accessible at runtime. Evaluation fingerprints the skill directory along with the prompt templates, so changing its contents requires preparing a new evaluation task.

Proposals contain the ordered trace with per-step reasons, constraints, and the model step's `processing` hooks, plus the selected definitions, handler references, and dependencies. Rust instantiates the proposal as a `TraceInstance` containing concrete steps and dependencies. Each trace instance declares its own `mutable_fields`; a field may declare a
`kind` (`string`, `bytes`, `integer`, `boolean`, `list`, `map`) and, for `bytes`,
an `encoding` (`hex` or `base64`). Execution input carries the generator path,
`step_trace`, dependencies, and per-instance `params` keyed by step instance
name; no execution-format version or proposal request ID is embedded. Each step instance carries explicit `omitted_fields`, dependency `inputs`, and broken-dependency metadata. Mutable values for an instance live only in `params[instance_name]`. At import the runtime builds a mapping from `@step_handler("model step name")` declarations; during execution it calls an optional `@global_init` once, then dispatches each instance in order to its handler with `(ctx, params)`. Params and `ctx.step_instance` are read-only; `ctx.step_name` is the current instance name. The handler resolves its instance's params entry, omissions and dependency inputs, performs the step action, and uses `ctx.save_state(field, value)` and `ctx.load_state(field)` for the current instance, and `ctx.load_depending_state(field)` for bound prior values; `ctx.states` lives until the generator exits. They do not embed the full model. Codex synthesis agents check `result.draft.json` with `synthesis.validate_synthesis_draft` before atomically publishing `result.json`. Other profiles can use [the static artifact validator](skills/protocol-modules/scripts/validate_generator.py). The MCP tool and fuzzer acceptance share Rust checks for the completion metadata, schema and parameters, field manifest, and Python syntax and entrypoint. The fuzzer applies these to the staged copies before publishing. Agents may optionally run the bounded import probe to diagnose module loading.

The prepared environment is CPython 3.12 on Linux x86_64. Install its pinned dependencies into the campaign interpreter (the supplied campaigns use `.venv/bin/python`):

```sh
.venv/bin/python -m pip install -r python/requirements-protocols.txt
.venv/bin/python -m pip check
.venv/bin/python skills/protocol-modules/scripts/audit.py --network
```

The audit checks 32 cataloged distributions and runs protocol/crypto/codec probes; `--network` additionally exercises temporary loopback DNS, FTP, SMTP, DICOM, SSH and HTTP/DMAP peers. These checks verify the library environment, not interoperability with every ProFuzzBench target. Synthesis agents use installed modules and do not install dependencies themselves.

Coverage-analysis init and task prompts receive `[target].source_dir` as `SOURCE_DIR`,
plus `[target].configure_options` and `[target].cli_args` as `CONFIGURE_OPTIONS` and
`CLI_ARGS`. Set `source_dir` to the source tree actually used by `coverage.binary`,
including applied patches: LightFTP uses `build-llvm-cov/LightFTP`; the OpenSSL
4.0.2 out-of-tree build uses `../../.deps/openssl/openssl-4.0.2-s-server-only`,
as recorded by its `configdata.pm`. The MCP service receives the same root.
Absolute llvm-cov source paths are read exactly; relative paths are resolved
against this root. Missing compiled files are errors, with no fallback to an
upstream checkout or another file with the same name. Existing sessions and
accepted handler mappings must be regenerated in a new campaign to change their
source evidence.
The prompts tell the agent to combine those switches with other build
material (`configdata.pm`, `CMakeCache.txt`, generated headers, build logs) to
decide which protocol features are compiled in, to avoid goals for disabled
features, and to prioritize enabled features in proposals and trace selection.
Synthesizer prompts do not receive that path, even when it is configured.
Both prompts and the module skill prohibit reading SUT source or using SUT feature
support or expected message acceptance to decide whether to generate. Protocol
behavior comes from the proposal and RFCs; public generator API/SDK references
and static artifact validation remain available. Runtime replies, failures and
coverage are evaluated by the fuzzer after publication.

`WORKSPACE` is the agent's fixed working directory (`generator_synthesis.output`). Each generation request receives a separate `OUTPUT_DIR` at `WORKSPACE/requests/<request-id>/` for its proposal (validated mutable fields, projected trace), `generator.py`, initial field-default schema, and completion JSON. Accepted generators are copied to that directory's `published/`. Initialization writes `ready.json` to `WORKSPACE/init/`. Request artifacts are never written directly to the workspace root. Synthesis-generated generic helpers may live in `WORKSPACE/lib` and remain campaign-scoped runtime imports for accepted generators; the generator server adds that directory to `sys.path` when loading a generator. Task-specific handlers and protocol state stay in `generator.py`.

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

### Evaluate one recorded agent request

Run a single analysis or synthesis turn from the point just before its original
prompt with the stage's Agent profile from the supplied campaign TOML.

```sh
mcn-fuzz eval-request --campaign campaign.toml --run-dir path/to/run \
  --stage analysis --request-id 000002-example --out path/to/new-eval-dir
```

Use `--prompt FILE` for a complete replacement or `--replace-from FILE
--replace-with FILE` for an exact single substitution. Pass a different campaign
TOML to compare Agent profile settings. Both stages require a Codex profile with
the app-server's bounded thread fork support and access to the original local
session recording. The output directory must be new.

The evaluation starts private Coverage and Campaign Introspection MCP services,
forks the session before the original request, and sends one turn. The output
includes the exact prompt, restored MCP state, turn timing, tool call metadata,
and artifact validation results. For older campaigns the evaluator reconstructs
the service state from events and records the missing historical statistics in
`mcp-initial-state.json`.
