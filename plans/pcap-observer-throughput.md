# Per-case PCAP Observer and Throughput Report

> Superseded for future implementation by [nix isolation, persistent libpcap capture, and process reaping](nix-libpcap-process-reaping.md). This document preserves the historical ptcpdump design and measurements; its helper, PID filtering, and separate Python template are no longer the planned architecture.

## Goal and coordination

Implement the observer in `fuzz/` and measure its effect on real campaigns. Grok should implement this plan through the current Herdr session, then report the measured results and limitations. Codex will review the implementation and report after the work is available in the workspace.

The observer captures the TCP/UDP traffic attributed to the Python generator and SUT process trees for one case, stores the capture as zlib-compressed testcase metadata, and exports a `.pcap` sidecar when LibAFL saves that testcase. Do not change `generate(ctx, params)`, wrap `OnDiskCorpus`, validate PCAP contents after capture, or add a validation pass after decompression.

## Capture backend and lifecycle

- Reuse the pinned `ptcpdump` v0.37.0 CLI. Keep capture opt-in through `[capture]`, with an executable path and a 64 MiB per-case file limit. Enable it in the LightFTP and OpenSSL campaign examples.
- The current targets run on the same host and communicate through loopback. Capture `lo` ingress with `--pid` for the forkserver and Python server/template process tree, plus `--follow-forks`; this includes both application directions and dynamic FTP data ports without a port filter.
- Add a case observer that owns the helper process, temporary PCAP, compressed result, capture status, and phase timings. The executor provides the per-case PID roots and calls the observer to begin and finish capture. A future backend can provide its own capture-root source without coupling `GeneratorInput` to forkserver details.
- For readiness, start a dedicated stderr reader immediately after spawning ptcpdump. Recognize the pinned version's `capturing on ...` line, which is printed after tracing and capture consumers start. Send readiness to the executor over a channel while continuing to drain stderr for the helper's lifetime. In parallel, watch for early process exit. Gate SUT start and generator submission on readiness; fail on a 5-second deadline and reap the helper on every error path.
- At fuzzer startup, check the configured executable and version, then perform a real attach/readiness/stop preflight before processing cases. Missing tools, insufficient capabilities, unsupported kernel/BTF, helper exit, and readiness timeout are startup errors. A per-case startup failure after successful preflight is a fatal capture infrastructure error; never silently disable capture.
- Begin each case capture before requesting the SUT child. End it after generator and SUT cleanup, wait 100 ms for trailing packets, then use SIGUSR1 counters to wait up to 1 second for two stable snapshots. Send SIGINT and wait up to 2 seconds for exit. Do not include capture startup time in the case execution deadline.
- Read and compress the captured bytes with zlib level 1; record packet/drop counters and capture errors reported by the helper. If the file limit, helper error, incomplete shutdown, or a known drop occurs, retain the available bytes and mark that case's capture status incomplete. Capture status does not change the target `ExitKind`.

## LibAFL metadata and file persistence

- Add serializable `PcapMetadata` containing the zlib bytes, uncompressed length, capture status, relevant process IDs, helper packet/drop counts, and capture timing fields. Add a `PcapToMetadataFeedback` that always returns false and appends this metadata to newly saved testcases. Combine it with both coverage feedback and the crash objective so both corpus and solution cases receive captures.
- Extend `GeneratorInput` with a runtime-only `#[serde(skip)]` pending-capture attachment. It is excluded from the generator/params JSON, hash, equality, and mutation. `PcapToMetadataFeedback::append_metadata` sets it on the new testcase's cloned input; it never modifies the scheduled parent input.
- Implement export in `GeneratorInput::to_file`: write the existing input JSON and stream-decompress the pending capture into a hidden sibling named `.<input-filename>.pcap`. Do not parse or validate the resulting PCAP. Loading an input starts with no pending attachment.
- Assign every testcase that carries a capture a unique filename derived from its input hash and a per-execution capture ID. This prevents LibAFL's duplicate-input path from skipping `to_file` and losing the later capture; the input hash remains based only on generator and parameters.
- Continue using LibAFL's standard `OnDiskCorpus` and current `JsonPretty` metadata. Its input loading skips hidden files, keeping PCAP sidecars out of the seed set. The current `QueueScheduler` does not remove or replace corpus entries; if corpus pruning is added later, its removal path must clean the matching sidecar.

## Reproducible overhead benchmark

Add `mcn-fuzz bench-capture --campaign <path> --output <directory>`. It must call the same release executor, observers, feedback, and testcase-save path as a campaign, using fixed inputs rather than mutation generations so each paired run executes the same cases.

- LightFTP workload: sort and cycle the six existing seeds for 60 measured cases. OpenSSL workload: sort and cycle the three existing seeds for 60 measured cases. Run one full seed cycle as warm-up before each measured condition.
- Run five paired capture-off/capture-on trials per target, alternating which condition runs first. Use a fresh session and scratch directories for each trial, identical timeout and logging settings, serial execution, and the same Release fuzzer and instrumented target builds.
- Report setup and preflight separately from steady-state case throughput. For every case, measure target setup, capture spawn-to-ready, generator/SUT interaction through cleanup, tail wait/drain, stop-signal-to-reap, PCAP read, zlib compression, and full `evaluate` wall time. Instrument the sidecar decompression/write path so persistence cost is visible when a case is saved.
- Produce a Markdown summary and JSONL raw results. Include per-target and per-condition case count, wall-clock cases/s, mean/p50/p95 case latency, paired throughput loss and latency increase; per-phase total/mean/p50/p95 and share of full case time; input/compressed sizes and ratio; saved testcase count; per-seed generator/target statuses, packet/drop counts; and preflight/startup cost.
- Record tool and target versions, seed-list hash, capture settings, compression level, case timeouts, build profile, and kernel version. Interpret OpenSSL results against its paired capture-off run; its timeout seed mix is not directly comparable with earlier randomized fuzz throughput.

## Acceptance and deliverables

- Verify startup preflight failures for missing executable, denied capture permissions, unsupported environment, helper early exit, and readiness timeout. Verify readiness handling for split stderr lines and stderr draining while the capture runs.
- Verify ordinary coverage-retained and crashing testcases both contain metadata and a `.pcap` sidecar; identical inputs saved on different executions get distinct sidecars; old inputs load with no attachment.
- Run LightFTP sessions containing LIST/RETR to exercise dynamic data connections, and OpenSSL valid, alert, and timeout seeds. Confirm both loopback directions are present and unrelated processes are excluded.
- Run at least 100 consecutive cases and check helper reaping and file-descriptor stability. Run the five paired benchmark trials and commit the Markdown comparison report plus machine-readable raw results.
- Grok reports the implementation, verification, and measured capture overhead. Codex reviews the resulting changes and independently checks the benchmark report before reporting completion.

## Execution status (2026-11-03)

Grok implemented the observer, metadata attachment and sidecar export, fixed-input benchmark, and LightFTP/OpenSSL campaign fixtures. `cargo test --offline --lib` passed 31 tests. The LightFTP LIST/RETR benchmark paths also passed. Release capture-off baselines completed five 60-case trials per target: LightFTP averaged 11.809 cases/s; OpenSSL averaged 2.358 cases/s.

Capture-on preflight was attempted in all five paired trials for each target. Every attempt failed before the first measured case because `ptcpdump` returned `operation not permitted` for uid 1005. No privilege escalation was applied. Capture overhead, packet/drop/compression metrics, bidirectional PCAP contents, and the 100-case capture stability check therefore remain unverified. The reports preserve this result rather than estimating capture costs:

- [`fuzz/capture-bench/lightftp-r3/report.md`](../fuzz/capture-bench/lightftp-r3/report.md) and its `results.jsonl` contain the LightFTP measurements.
- [`fuzz/capture-bench/openssl-r3/report.md`](../fuzz/capture-bench/openssl-r3/report.md) and its `results.jsonl` contain the OpenSSL measurements.

### Subsequent permission verification (2026-11-03)

With user approval, the original v0.37.0 binary was installed as `root:root`, mode `0755`, at `/usr/local/libexec/ptcpdump`. Both campaign examples now use that path. The executable has `cap_dac_read_search,cap_net_admin,cap_sys_admin,cap_perfmon,cap_bpf=ep`. `CAP_SYS_ADMIN` resolves its namespace-switching failure; `CAP_DAC_READ_SEARCH` resolves the subsequent `/proc/self/mem` read failure during eBPF kernel-version detection. These capabilities must be allowed by the launching environment; the Codex sandbox still prevents their use.

A smoke test outside the sandbox, running as uid 1005, successfully reached readiness, returned SIGUSR1 statistics, exited with status 0 on SIGINT, and wrote a capture owned by uid 1005. When filtering by the diagnostic process's host PID, a controlled loopback request/response produced 10 packets, 0 drops, and an 897-byte capture.

There is a separate PID-namespace limitation: this container has its own PID namespace, while ptcpdump v0.37.0 compares `--pid` directly against the kernel's host TGID. The diagnostic process had container PID 3604952 and host PID 3096567. Using a container PID produced no packets; using its host PID captured the exchange. The current executor supplies container-local PID roots, and a LightFTP LIST replay completed successfully but captured 0 packets. End-to-end case capture therefore still requires PID translation or execution in the host PID namespace before the capture-on benchmark can produce meaningful results. The historical r3 reports above have not been rerun.
