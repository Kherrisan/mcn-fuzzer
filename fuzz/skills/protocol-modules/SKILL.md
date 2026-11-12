---
name: protocol-modules
description: Choose installed Python modules and validate synthesis artifacts for ProFuzzBench generators (DAAP, DICOM, DNS, DTLS, FTP, RTSP, SIP, SMTP, SSH, TLS). Covers packet/message construction, upstream/server peers, crypto/codecs and the fixed artifact validator used before result publication.
---

# Protocol modules for generators

Use this skill during initialization to identify relevant libraries, and during
generation before implementing wire formats, cryptography or external services.
Use the campaign Python and PYTHONPATH injected by the calling prompt. All paths
below are relative to this SKILL.md, independent of the agent's working directory.

## Choose a library

1. Read the relevant section of [protocols.md](references/protocols.md). Do not
   load every protocol's details for a single receipt.
2. Consult [modules.md](references/modules.md) for pip names, import paths,
   installed versions and cross-protocol codecs. The machine-readable equivalent
   is [modules.json](references/modules.json).
3. Check the relevant import, version and signature with the campaign Python,
   using `importlib.metadata.version`, `inspect.signature`, `help` or installed
   source. Inventory versions describe the prepared `fuzz/.venv`; campaigns can
   configure a different interpreter. Do not install packages during synthesis.
4. Use SDKs for valid prerequisite exchanges, authentication, crypto and assigned
   peers. For targeted wire mutations, build/serialize a valid message and then
   edit the bytes, or use `struct`/`construct` directly. High-level libraries may
   reject malformed fields, repair lengths/checksums, reorder fields or hide
   intermediate steps. Never let that erase the requested behavior.
5. If one SDK cannot express the trace, consider another listed module or a
   bounded native-socket implementation. A missing convenience API alone does
   not establish that a trace is impossible. Derive protocol behavior from the
   receipt and RFC documents; do not read SUT source or assess SUT feature support
   or expected acceptance. Report only intrinsic receipt/RFC conflicts or actual
   generator implementation/validation failures, not predicted SUT rejection.

| Protocol / benchmark target | Message and codec helpers | External peer / upstream choices |
| --- | --- | --- |
| DAAP / forked-daapd | `pyatv.protocols.dmap` tags/parser, `struct` | `httpx` client, `aiohttp.web` HTTP server; optional `zeroconf` |
| DICOM / Dcmtk | `pydicom`, `pynetdicom.pdu` | `pynetdicom.AE` SCU/SCP |
| DNS / Dnsmasq | `dns.message`, `dns.edns`, `dnslib`, Scapy DNS | `dnslib.server` UDP/TCP upstream, `dns.query` client |
| DTLS / TinyDTLS | `mbedtls.tls`, `construct`/`struct` record fields | `python-mbedtls` DTLS client/server, PSK or certificate |
| FTP / BFTPD, LightFTP, ProFTPD, PureFTPD | `ftplib`, explicit CRLF/data-channel framing | `pyftpdlib` FTP/FTPS server, `aioftp` client/server |
| RTSP / Live555 | `aiortsp.rtsp.parser`, `dpkt.rtp`, Scapy RTP | `aiortsp` client; bounded native/asyncio server glue |
| SIP / Kamailio | `twisted.protocols.sip`, native SDP text | Twisted legacy SIP proxy/registrar building blocks or native peers |
| SMTP / Exim | `email`, `smtplib`, `aiosmtplib` | `aiosmtpd` SMTP/LMTP server |
| SSH / OpenSSH | `paramiko`, `asyncssh`, `cryptography` | Paramiko `Transport` + `ServerInterface`, AsyncSSH server |
| TLS / OpenSSL | `tlslite.messages`/extensions, Scapy TLS, `dpkt.ssl` | `ssl`, `OpenSSL.SSL`, `tlslite.api`, `mbedtls.tls` |

## Fit the worker and party contracts

- Implement generator-assigned parties only. An SDK server can satisfy an
  upstream or callback role; it cannot replace the real SUT or fabricate its
  replies. Endpoint choice must agree with configuration or actual discovery.
- Keep decorated step handlers and the optional global initializer synchronous. Run async SDKs through a case-local
  `asyncio.run(...)` when appropriate. Bound the entire coroutine with
  `asyncio.wait_for` as well as individual I/O operations.
- The runtime imports generator.py before forking. Import service SDKs inside
  `generate` if they may initialize background services. Create sockets, keys,
  event loops, threads, temporary files and SDK servers inside the worker.
- Use byte-building libraries without raw packet transmission when talking to a
  normal TCP/UDP SUT. Scapy interface discovery can open OS sockets even during
  import; import it inside the worker. Raw injection has extra privilege/routing
  requirements and does not replace an established TCP stream.
- Ctx channels are opaque. SDKs needing a real socket, TLS wrapping or datagram
  boundaries must use their own native sockets with bounded timeouts. Do not
  access private Ctx internals or invent Channel methods.
- Start required listeners before sending the triggering request. A dynamic port
  works only when it can be advertised to the SUT. Readiness and endpoint
  assignment are not supplied by the executor.
- In `finally`, stop servers, close clients/sockets, cancel and await tasks,
  shut down event loops and join threads within the case budget. Do not leave a
  daemon thread or an unbounded `serve_forever()` running. Twisted's global
  reactor is not restartable; prefer message codecs or a case-local integration.
- Only generator.py and params.schema.json are published. Inline service glue
  in generator.py and use installed libraries; do not import this skill's audit
  script or depend on request-directory files at runtime.
- The calling prompt points to `generator_server/template.py` and
  `generator_server/params.schema.json`. The scaffold's imports, helpers and
  per-target handler examples are copy references; adapt only the relevant
  block to the proposal and do not publish the scaffold itself.
- This skill supplements the calling prompt. Party ownership, receipt order,
  parameter representation, lifecycle, verification and result.json rules from
  that prompt still apply. Report checks performed; module imports or codec
  roundtrips do not prove that a target trace succeeded against the SUT.

## Validate before publishing (every successful generation)

Use [validate_generator.py](scripts/validate_generator.py) with the campaign
Python. The generation prompt supplies its absolute path and a fully rendered
command with OUTPUT_DIR, the request id and PYTHONPATH. Execute that
command directly; do not recreate its syntax, schema or parameter checks.

1. Finish generator.py, params.schema.json and params.json.
2. Write the success completion object to OUTPUT_DIR/result.draft.json.
3. Run the supplied validator command with `--result` pointing to that draft,
   `--check-imports` and the campaign `--pythonpath`. It prints a JSON report and
   exits 0 on success, 1 on validation failure. Fix reported errors and rerun.
4. Only after success, atomically rename result.draft.json to result.json. The
   validator neither publishes nor modifies artifacts. A failed synthesis may
   publish its error completion directly without successful-generator checks.

Read [validation.md](references/validation.md) for the exact supported checks and
their limits. Review trace correspondence, prerequisite conditions, resource
cleanup and protocol-specific wire behavior separately. Do not replace actual
runtime evidence with a successful static report. The maintainer audit below is
for the whole installed environment; it is not the per-generator validator.

## Audit and setup (maintainer actions)

The environment is pinned for CPython 3.12 on Linux x86_64. From `fuzz/`:

```sh
.venv/bin/python -m pip install -r python/requirements-protocols.txt
.venv/bin/python -m pip check
.venv/bin/python skills/protocol-modules/scripts/audit.py
# Also exercise temporary loopback DNS/FTP/SMTP/DICOM/SSH/HTTP peers:
.venv/bin/python skills/protocol-modules/scripts/audit.py --network
```

Use the configured campaign Python in place of `.venv/bin/python` when checking
another environment. `audit.py` checks all catalog imports and exact versions,
then performs bounded protocol/crypto/codec probes in separate processes. It
prints JSON and exits nonzero on failure; `--report PATH` also saves it. Network
probes use loopback, ephemeral ports and temporary fixtures, never a benchmark
SUT. An agent normally needs only a targeted import/API check, not a full audit.
