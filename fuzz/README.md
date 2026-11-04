# mcn-fuzz

Forkserver fuzzer. Each case starts a new child for the selected target. AFL++ PCGUARD is the coverage source. The host-side Python generator speaks the target protocol over native sockets. Campaigns are included for LightFTP and OpenSSL `s_server`.

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

When LLVM source coverage is enabled in the campaign, full JSON export is off by default. Add `--source-coverage-json` to save the detailed JSON in snapshots. Without the flag, the stage uses `llvm-cov export --summary-only` for coverage deltas and saves profdata snapshots and metadata.

`setup-capture.sh` checks Linux, pkg-config, and libpcap, then builds the Release binary with the stats feature. It does not call sudo or setcap.

`replay` reads the corpus input file. LibAFL also writes `.metadata` sidecars; those are not generator inputs.

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
sudo ./target/release/mcn-fuzz run --campaign targets/openssl/campaign.toml --iterations 100
sudo ./target/release/mcn-fuzz accept --campaign targets/openssl/campaign.toml
sudo ./target/release/mcn-fuzz replay --campaign targets/openssl/campaign.toml --input targets/openssl/corpus/<file>
```

`generators/tls12.py` keeps `generate(ctx, params)`. It rebuilds TLS lengths, then applies `version`, `cipher`, `sni`, `extensions_hex`, and `truncate`. The response reader takes the 5-byte record header and then at most 18432 body bytes. A ServerHello is `ok`. An alert is `alert`. A short close is `eof`. Connect failures stay `conn` and read deadlines stay `timeout`.
