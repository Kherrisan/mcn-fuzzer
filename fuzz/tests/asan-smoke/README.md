# ASAN smoke campaign

Minimal TCP server used to verify the mcn-fuzzer ASAN path end to end.

- `server.c` reads one line from a loopback TCP connection.
- `"CRASH"` performs a one-byte heap out-of-bounds read.
- The ASAN+PCGUARD build aborts on that read.
- The non-ASAN LLVM-coverage build is used by `mcn-fuzz coverage`.

Build both binaries:

```sh
./build.sh
```

Run with the new campaign-root CLI:

```sh
sudo ./target/release/mcn-fuzz run \
  --campaign tests/asan-smoke/campaign.toml \
  -o /tmp/asan-smoke-runs \
  --iterations 1
```

Expected evidence under `/tmp/asan-smoke-runs/mcn-fuzzer-*/`:

```text
cases.jsonl          # exit_kind "crash" for signal 6
crashes/<case>       # objective OnDiskCorpus
crashes/.*_1.metadata
asan/asanlog.<pid>   # AddressSanitizer heap-buffer-overflow report
coverage/            # llvm-cov replay artifacts
generator.log
coverage-generator.log
```

To run only coverage replay, use `mcn-fuzz coverage -o /tmp/asan-coverage-runs
--campaign tests/asan-smoke/campaign.toml --corpus <run>/corpus --step 1`; it
creates a timestamped campaign root and does not add a build-hash directory.

The ASAN observer parses `asan/asanlog.<pid>` and the objective combines:

```text
NewHashFeedback<AsanBacktraceObserver>
OR CrashFeedback
```

The objective corpus is the campaign's `[campaign].crashes` `OnDiskCorpus`.
