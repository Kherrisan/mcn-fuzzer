# mcn-fuzzer

LibAFL forkserver fuzzing with Python protocol generators, native socket interaction, PCGUARD coverage, and optional per-case libpcap capture.

- [`fuzz/`](fuzz/README.md): Rust fuzzer and Python generator runtime.
- [`rsc/`](rsc/README.md): RFC ingestion and protocol compilation tools.
- [`plans/`](plans/): design and implementation plans.
- `third-party/LibAFLstar/`: reference source, excluding upstream benchmark results and target case studies.

SUT directories named `targets/`, downloaded dependencies, build products, runtime corpora, packet captures, logs, and test/benchmark results stay local and are excluded from Git. The local Fizzle reference checkout is also excluded; its URL and revision are recorded in [`plans/fizzle-entropy-only.md`](plans/fizzle-entropy-only.md).

See the component READMEs for dependencies and runtime details. Target-specific examples in the fuzzing documentation refer to local target files that are not distributed in this repository.
