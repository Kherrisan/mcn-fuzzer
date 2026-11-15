# Protocol models

Copied from the latest profuzzgym RSC outputs on 2026-11-14.

| file | source |
|---|---|
| `lightftp-20261114.json` | `/home/user/profuzzgym/profuzzbench/subjects/FTP/LightFTP/mcn-fuzzer/protocol.json` |
| `openssl-20261114.json` | `/home/user/profuzzgym/profuzzbench/subjects/TLS/OpenSSL/mcn-fuzzer/protocol.json` |

SHA-256:

```text
6d3f875c9d6e09d83a7951570102fdf5d95bb169b0f673db74c0761bfecb237c  lightftp-20261114.json
eda87dd49b095c0f5ee03f3d942053b5508b3af4975fa134fd8b3066016b0c15  openssl-20261114.json
```

Both models report `meta.complete = true`:

- LightFTP: 10 parties, 164 steps, 8 channels, 56 messages
- OpenSSL: 3 parties, 87 steps, 5 channels, 27 messages

These files are now wired into both campaigns:

- `fuzz/targets/lightftp/protocol.json` (copy of `lightftp-20261114.json`)
- `fuzz/targets/openssl/protocol.json` (copy of `openssl-20261114.json`)

The older models are kept here for rollback:

- `lightftp-20261111.json`
- `openssl-20261112.json`

The new models use a different natural-language step vocabulary:

- LightFTP: 0/147 old step names survive; party names changed from
  `server-PI` to `Server-PI` and third-party A/B roles were restructured.
- OpenSSL: 1/121 old step names survive; party names are unchanged.

Example LightFTP party assignment for the new model (third-party peers stay on
the generator side):

```toml
[parties]
sut = ["Server-PI", "Server-DTP"]
generator = "*"
```

OpenSSL uses the same party names, with the `Certificate URL host` party moved
to the generator side:

```toml
[parties]
sut = ["TLS server"]
generator = ["TLS client", "Certificate URL host"]
```

Seed generators still use the previous step names; they remain valid for seed
execution, but newly synthesized generators follow the new model vocabulary.
