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
  protocol/         Protocol models, trace selection, receipts
  evaluation/       Standalone synthesis evaluation and its CLI entrypoint
```

## Build

```sh
# AFL++ and the Python venv already live under fuzz/.deps and fuzz/.venv.
bash targets/lightftp/build.sh
cargo build --release
```

`build.sh` copies LightFTP, applies `targets/lightftp/forkserver-session.patch`, and compiles with `afl-clang-fast`, `AFL_LLVM_INSTRUMENT=PCGUARD`, and `AFL_LLVM_THREADSAFE_INST=1`. It removes `AFL_LLVM_ONLY_FSRV` and `AFL_GCC_ONLY_FSRV` from the compiler environment when those variables are present. It does not modify `fuzz/.deps/LightFTP`.

## Run

Build as a normal user, then start the fuzzer with sudo. sudo restores the calling account from `SUDO_UID`/`SUDO_GID`. Pass `--run-as USER` when that account must be named explicitly. The fuzzer does not stay root, and it does not install a file-capability copy under `/usr/local`.

```sh
cargo build --release
sudo ./target/release/mcn-fuzz doctor
sudo ./target/release/mcn-fuzz accept --campaign targets/lightftp/campaign.toml
sudo ./target/release/mcn-fuzz run --campaign targets/lightftp/campaign.toml
sudo ./target/release/mcn-fuzz replay --campaign targets/lightftp/campaign.toml --input targets/lightftp/corpus/<file>
```

When LLVM source coverage is enabled in `[coverage]`, full JSON export is off by default. Add `--source-coverage-json` to save the detailed JSON in snapshots. Without the flag, the stage uses `llvm-cov export --summary-only` for coverage deltas and saves profdata snapshots and metadata. `[source_coverage]` is a configuration error; move that table to `[coverage]`. `analysis_enabled` is separate and requires collection. The private HC executable is `[agents].hc_executable` (default `hc`), not `protocol_generation.hc_executable`. Analysis output lives under `coverage.output/<build>/analysis/`. `targets/lightftp/build-coverage.sh --graphs` emits the forkserver coverage binary, bitcode, callgraph, CFG dots, and the four-field manifest in one build.

Set `[campaign].timeout_secs` to stop the fuzz loop after a wall-clock duration. When present, it takes precedence over `[campaign].iterations`; the CLI `--iterations` option remains an explicit override. The deadline is checked between complete LibAFL iterations, so an iteration already in progress can finish before shutdown.

`setup-capture.sh` checks Linux, pkg-config, and libpcap, then builds the Release binary with the stats feature. It does not call sudo or setcap.

`replay` reads the corpus input file. LibAFL also writes `.metadata` sidecars; those are not generator inputs.

Optional `[protocol_generation]` synthesizes new `generate(ctx, params)` scripts from a schema 7.0.0 protocol model. It is off when the section is omitted. The `run` command is the only entry that starts a campaign-private HC core; replay, accept, and benchmark do not. The core starts when synthesis or coverage analysis is enabled, and both agents share it. Profiles live under `[agents.profiles.<name>]` with `provider` set to `codex`, `claude`, or `pi`. A failed synthesis task is recorded and existing corpus entries keep fuzzing.

Fuzz-stage scheduling uses consecutive ordinary executions without newly occupied
PCGUARD bitmap slots. Configure the positive threshold under `[campaign]`:

```toml
[campaign]
bitmap_stagnation_limit = 256
```

New slots reset the streak; hitcount changes on known slots and the runtime marker
at index 0 do not count as new coverage. Seeds and synthesized inputs update the
bitmap baseline without increasing the streak, and invalid cases are ignored.
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
Seedless campaigns wait for initialization and the first generated input because
ordinary mutation requires a schedulable corpus.

Round-based `interval` settings have been removed from `[protocol_generation]`
and `[coverage]`; configuring them is an error. Initial and final
source-coverage snapshots, and first-generator bootstrap when seeds are
absent, still run as before. With seeds present, round 1 no longer starts an agent
task automatically. Source coverage still requires `[coverage].enabled=true`
and a coverage-built binary; synthesis requires `[protocol_generation].enabled=true`.
The shared agent runtime lives in `src/agents/worker.rs`. `AgentClient<I, T>` owns the command/outcome channels and background HC terminal. `AgentInit` supplies initialization checks and the ready artifact; `AgentTask` supplies request preparation and artifact acceptance. Both agents report `AgentOutcome` values, share session transitions/logging in `agents/session.rs`, and use `agents/artifacts.rs` for JSON reads, identity checks, and sealed tool inputs. Stages apply accepted results to LibAFL state at stage boundaries; the generic worker never accesses fuzz state.

Coverage analysis uses the same `[coverage]` table and does not require synthesis. Both agents queue initialization together before seeds are loaded or source coverage is replayed. Each has a background HC worker, command/outcome channels, a persistent terminal, and at most one request in flight. Coverage source verification and handler extraction run in the background; the stage skips dispatch until initialization is ready. New snapshots replace the queued snapshot while a request is running. Completed snapshots are not dispatched again.

Coverage measurement reads true/false counters from `llvm-cov export`'s `functions[].branches`. `coverage_analysis.cfg` extracts a `cfg/source-map.json` from the same bitcode used to emit the CFG DOTs; the LightFTP bundle builder generates it before hashing the manifest. At coverage-analysis startup, Rust invokes `coverage_analysis.graphs` once to validate the DOT bundle and publish `analysis/graphs/callgraph.json` plus `analysis/graphs/cfg.json`. Rust reads and validates both files, then seals them against later edits. All subsequent requests and agent graph queries read these JSON files; measurement never reparses DOTs or the source-map sidecar. The merged CFG JSON embeds per-function topology, source locations, and call sites. Existing graph bundles can regenerate the source map and manifest with `python3 analysis/lightftp/build_coverage_bundle.py --output targets/lightftp/build-cov --source-dir targets/lightftp/build-cov/LightFTP/Source --manifest-only` from `fuzz/`.

After a coverage snapshot is frozen, the fuzzer main thread measures it in Rust. That step reads `coverage.json`, the sealed graph JSON, and the accepted handler mapping. It does not start Python or `llvm-cov`. Reachable CFG blocks and direct callgraph edges form regions from known-zero branches and basic blocks. Covered or unknown paths break those regions. Optimized branch polarity is used only when successor coverage disambiguates it. Ambiguous conditions, absent CFGs, external calls, and indirect calls are `unknown` entries. `complete: false` means unresolved boundaries remain, even when `zero: true`. `handlers.json` and `regions.json` are frozen before the coverage agent is prompted, and the agent must not edit them.

Coverage results must match the dispatched session, request, build, snapshot, and protocol. Invalid artifacts reject that request and leave the agent ready for the next snapshot. HC failures and timeouts pause that agent. Both agents record the same session phases and HC terminal observations in their own `session.jsonl`; coverage also records request events in `events.jsonl`. Stopping a worker closes only its terminal; the campaign runtime owns the shared HC core.

The synthesizer prompts are Markdown MiniJinja templates in `prompts/`. They are embedded in the fuzzer at build time, so rebuild after editing them.

Protocol, step and trace hash IDs use the first eight lowercase hexadecimal characters of SHA-256 throughout receipts, prompts, generator origins and evaluation artifacts. Trace hashing still uses full step digests before shortening its own output. RSC-generated step and diagnostic IDs likewise use eight hash characters after their kind prefix. Document/source integrity digests retain their full SHA-256 values.

`synthesis-init.md` establishes the campaign background, reference paths, party roles, workflow, and artifact contract. `synthesis-generation.md` supplies each receipt's task with the public Python API, prerequisite handling, SDK/mock guidance, worker lifecycle, and publication rules. Both receive the same read-only campaign context with absolute paths and execution budgets. Missing template variables fail rendering; completion JSON examples are serialized by Rust before injection.

Both prompts also inject the absolute path to [the protocol module skill](skills/protocol-modules/SKILL.md) and require the agent to read it before selecting libraries. It covers all ten ProFuzzBench protocols, with message/packet codecs, upstream/server SDKs, crypto helpers, import names, installed versions and limitations. Keep the skill and its references accessible at runtime. Evaluation fingerprints the skill directory along with the prompt templates, so changing its contents requires preparing a new evaluation task.

Receipts contain the selected trace and relevant definitions, plus an absolute `model_path` for targeted prerequisite queries. They do not embed `model_snapshot`. The skill requires the agent to run [the fixed artifact validator](skills/protocol-modules/scripts/validate_generator.py) on `result.draft.json` before atomically publishing `result.json`. The agent additionally runs its bounded import probe. Fuzzer acceptance rechecks the completion IDs, parameter schema and values, and Python syntax and entrypoint in Rust using the RustPython compiler; it does not launch Python or run the generator or SUT.

The prepared environment is CPython 3.12 on Linux x86_64. Install its pinned dependencies into the campaign interpreter (the supplied campaigns use `.venv/bin/python`):

```sh
.venv/bin/python -m pip install -r python/requirements-protocols.txt
.venv/bin/python -m pip check
.venv/bin/python skills/protocol-modules/scripts/audit.py --network
```

The audit checks 32 cataloged distributions and runs protocol/crypto/codec probes; `--network` additionally exercises temporary loopback DNS, FTP, SMTP, DICOM, SSH and HTTP/DMAP peers. These checks verify the library environment, not interoperability with every ProFuzzBench target. Synthesis agents use installed modules and do not install dependencies themselves.

Coverage-analysis init and task prompts receive `[target].source_dir` as `SOURCE_DIR`.
Synthesizer prompts do not receive that path, even when it is configured.
Both prompts and the module skill prohibit reading SUT source or using SUT feature
support or expected message acceptance to decide whether to generate. Protocol
behavior comes from the receipt and RFCs; public generator API/SDK references and
static artifact validation remain available. Runtime replies, failures and
coverage are evaluated by the fuzzer after publication.

`WORKSPACE` is the agent's fixed working directory (`protocol_generation.output`). Each generation request receives a separate `OUTPUT_DIR` at `WORKSPACE/requests/<request-id>/` for its receipt, `generator.py`, parameter schema, initial params, and completion JSON. Accepted generators are copied to that directory's `published/`. Initialization writes `ready.json` to `WORKSPACE/init/`. Request artifacts are never written directly to the workspace root.

When synthesis is enabled, `[parties]` and `protocol_generation.model` are required. A campaign that includes `[parties]` resolves those selectors against the model before cases start, even when synthesis is disabled. A missing model is an error. `sut` and `generator` are each a non-empty list of protocol party names or `"*"`. `"*"` means every model party not named by the other side. Parties in neither set stay unavailable. The removed `protocol_generation.bindings` table is a configuration error. The LightFTP campaign assigns the server parties to the SUT and the remaining parties to the generator.



Python unit tests:

```sh
PYTHONPATH=python .venv/bin/python -m unittest discover -s python/tests -v
```

Stats lines named `coverage_map_occupancy` count occupied entries in the shared PCGUARD map. They are not source-coverage percentages. The runtime may set map index 0 as a marker; that byte alone is not treated as coverage.

A generator I/O timeout and a Python stall are stored on the generator status. The case is a target hang only when the case deadline or the exit grace runs out while the target is still running. A normal target exit is not a target timeout. A real target SIGSEGV is a crash.

If the target reports a map larger than the campaign allocation, set `map_size` under `[target]` in the campaign file. The default is 65536. `core` under `[campaign]` is optional. When it is omitted, the forkserver is not pinned. Set `core = N` to pin it. Coverage does not depend on that pin.

`target.extra_env` must not set `AFL_LLVM_ONLY_FSRV`, `AFL_GCC_ONLY_FSRV`, `__AFL_SHM_ID`, `__AFL_SHM_ID_SIZE`, or `AFL_MAP_SIZE`. The first two disable PCGUARD counters when present, including a value of `0`. The other three name the shared coverage map, which the fuzzer allocates itself.

## OpenSSL s_server

`targets/openssl/build.sh` downloads OpenSSL 3.5.9, checks SHA256 `603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a`, and compiles `openssl`, `libssl`, and `libcrypto` with `afl-clang-fast`, `AFL_LLVM_INSTRUMENT=PCGUARD`, `AFL_LLVM_THREADSAFE_INST=1`, and `AFL_MAP_SIZE=1048576`. Configure uses `no-shared no-module no-asm no-tests`. The same script writes an unencrypted RSA 2048 certificate into `targets/openssl/build/fixture/`. That directory, the tree, and the private key are gitignored.

A campaign that omits `args` still launches LightFTP with `["@CONFIG@"]` and writes `fftp.conf`. The OpenSSL campaign sets `args` to `s_server -accept 127.0.0.1:@PORT@ -cert @CERT@ -key @KEY@ -tls1_2 -www -naccept 1 -no_cache -no_ticket` and sets `extra_env` to `OPENSSL_CONF=@CONFIG@`. `@CONFIG@` is the `openssl.cnf` written under `workdir` on every case, and `s_server` loads that file. The campaign allocates a 1 MiB map. The handshake still rejects a target that needs a larger map. The generator sends one ClientHello on its own TCP connection. Do not add a readiness connection: `-naccept 1` would treat that probe as the case.

```sh
bash targets/openssl/build.sh
sudo ./target/release/mcn-fuzz run --campaign targets/openssl/campaign.toml
sudo ./target/release/mcn-fuzz accept --campaign targets/openssl/campaign.toml
sudo ./target/release/mcn-fuzz replay --campaign targets/openssl/campaign.toml --input targets/openssl/corpus/<file>
```

`generators/tls12.py` keeps `generate(ctx, params)`. It rebuilds TLS lengths, then applies `version`, `cipher`, `sni`, `extensions_hex`, and `truncate`. The response reader takes the 5-byte record header and then at most 18432 body bytes. A ServerHello is `ok`. An alert is `alert`. A short close is `eof`. Connect failures stay `conn` and read deadlines stay `timeout`.
