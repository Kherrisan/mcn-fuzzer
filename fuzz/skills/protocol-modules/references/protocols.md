# Protocol recipes and limits

These are building blocks, not complete generators. Put case resources and SDK
service imports inside `generate`. Supply bounded timeouts and explicit cleanup.
Inspect installed source for arguments not shown here.

## DAAP — forked-daapd

DAAP uses HTTP transport with DMAP binary tagged bodies. Use `httpx.Client` or
`aiohttp.ClientSession` for valid HTTP requests, and `aiohttp.web.Application`,
`AppRunner`, `TCPSite` for assigned HTTP upstreams. Keep sessions and revision IDs
from real replies. Use `zeroconf.ServiceInfo`/`Zeroconf` or async equivalents only
when the trace requires DNS-SD; close/unregister them explicitly.

```python
from pyatv.protocols.dmap import tags, parser, tag_definitions
body = tags.container_tag("mlog", tags.uint32_tag("mstt", 200)
                          + tags.uint32_tag("mlid", 1))
decoded = parser.parse(body, tag_definitions.lookup_tag)
assert parser.first(decoded, "mlog", "mlid") == 1
```

`pyatv` is primarily an Apple TV client, not a full forked-daapd-compatible music
server. Reuse its codecs without assuming its discovery/login API implements all
DAAP endpoints. Unknown tags may require a custom lookup or `struct.pack('!4sI',
tag, len(payload)) + payload`. Preserve actual authentication/session behavior.

Sources: [DMAP format](https://pyatv.dev/documentation/protocols/),
[aiohttp web server](https://docs.aiohttp.org/en/stable/web_quickstart.html).

## DICOM — Dcmtk

`pydicom.Dataset` represents datasets; transfer syntax, explicit/implicit VR and
endianness determine encoding. `pynetdicom.AE` handles association and DIMSE:

```python
from pynetdicom import AE
from pynetdicom.sop_class import Verification
ae = AE(ae_title="GENERATOR")
ae.add_requested_context(Verification)
ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 1.0
# Inside the case: assoc = ae.associate(ctx.host, ctx.port)
# Require assoc.is_established; status = assoc.send_c_echo(); assoc.release().
# Always assoc.abort() if an association remains established after an error.
```

For an assigned SCP: add supported contexts, use `AE.start_server((host, port),
block=False, evt_handlers=...)`, then `shutdown()` and `server_close()` in cleanup.
Use `pynetdicom.pdu.A_ASSOCIATE_RQ` and related `.encode()`/`.decode()` for lower
level PDUs. Dataset codecs alone do not implement network associations. Pixel
compression codecs are optional extras and are not part of this environment.

Sources: [pynetdicom](https://pynetdicom.readthedocs.io/en/stable/),
[pydicom](https://pydicom.github.io/pydicom/stable/).

## DNS — Dnsmasq

```python
import dns.message
query = dns.message.make_query("example.test.", "A", use_edns=True)
wire = query.to_wire()
reply = dns.message.from_wire(wire)
```

Use `dns.query.udp`/`tcp` with explicit destination, port and timeout, not an
ambient recursive resolver. `dns.edns`, `dns.tsig`, `dns.dnssec` supply extension
and authentication helpers. Use `dnslib.DNSRecord` for editable wire records;
`DNSRecord.question(...).reply()` preserves query ID/question correctly.

For a controlled upstream subclass `dnslib.server.BaseResolver.resolve`, return
`request.reply()` with the needed `RR`s, and run `DNSServer` with `tcp=False` or
`True` as required. Start and stop it within the case. Do not construct an
unrelated reply without copying the request ID. UDP truncation may require TCP
fallback. Use native datagram sockets if exact datagram boundaries matter.

Sources: [dnspython](https://dnspython.readthedocs.io/en/stable/),
[dnslib](https://nhairs.github.io/dnslib/latest/reference/dnslib/server/).

## DTLS — TinyDTLS

Use `mbedtls.tls.DTLSConfiguration`, `ClientContext`/`ServerContext`, and native
UDP sockets or memory buffers. The installed version supports DTLS 1.0 and 1.2,
not DTLS 1.3. Python `ssl` and `tlslite-ng` are not DTLS replacements.

```python
from mbedtls import tls
config = tls.DTLSConfiguration(
    validate_certificates=False,  # Only for a deliberately unverified test peer.
    pre_shared_key=("identity", b"0123456789abcdef"),
    lowest_supported_version=tls.DTLSVersion.DTLSv1_2,
    highest_supported_version=tls.DTLSVersion.DTLSv1_2,
    handshake_timeout_min=0.1, handshake_timeout_max=1.0,
)
client_context = tls.ClientContext(config)
# client_context.wrap_socket(native_udp_socket, server_hostname=None)
# server uses pre_shared_key_store={"identity": key} and ServerContext(config).
```

Use credentials/ciphers actually accepted by TinyDTLS. Select `ciphers` explicitly
when required (for example inspect `tls.ciphers_available()` for PSK/CCM suites).
Handle cookie exchange, retransmission timers, epochs, sequence numbers and
datagram boundaries. `wrap_buffers` exposes `receive_from_network`,
`peek_outgoing`, `consume_outgoing`, `do_handshake`, `write`, `read` for controlled
transport; it does not manage your network loop. The audit contains a bounded
DTLS 1.2 PSK handshake and encrypted-data roundtrip using these APIs.

Sources: [python-mbedtls examples and API](https://pypi.org/project/python-mbedtls/).

## FTP — BFTPD, LightFTP, ProFTPD, PureFTPD

`ftplib.FTP`/`FTP_TLS` cover valid login, multiline replies and active/passive
transfers. Set `timeout` explicitly; use `FTP_TLS.prot_p()` when the data channel
must be protected. For command mutation, use bounded control sockets and actual
PASV/EPSV/PORT/EPRT replies/addresses rather than hard-coded data endpoints.

Assigned servers: `pyftpdlib.authorizers.DummyAuthorizer`,
`pyftpdlib.handlers.FTPHandler`/`TLS_FTPHandler`,
`pyftpdlib.servers.FTPServer`. Use a fresh handler subclass and authorizer per case;
do not mutate global handler state shared across imports. `FTPServer` can bind
port 0; inspect its bound address. `serve_forever` needs a bounded loop/thread
and `close_all()` plus a thread join in cleanup. Use a temporary filesystem root.

`aioftp.Client` and `aioftp.Server` are async alternatives. Do not assume aioftp
implements explicit/implicit FTPS. High-level clients may hide intermediate
150/226 or authentication replies required by the trace.

Sources: [pyftpdlib](https://pyftpdlib.readthedocs.io/en/latest/tutorial.html),
[aioftp](https://aioftp.aio-libs.org/).

## RTSP — Live555

```python
from aiortsp.rtsp.parser import RTSPParser
parser = RTSPParser()
messages = list(parser.parse(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n"))
assert messages[0].cseq == 1
```

Use `RTSPConnection(host, port, ..., timeout=...)` as an async context manager;
`await connection.send_request(method, url, headers=..., timeout=..., body=...)`
handles valid client requests. Preserve CSeq, Session and Transport fields, RTP
port negotiation and SDP URLs from replies. `aiortsp.rtsp.parser.RTSPParser.parse`
accepts partial chunks and yields requests, responses and `$` interleaved frames.

For payloads use `dpkt.rtp.RTP`, Scapy `scapy.layers.rtp.RTP`, and
`aiortsp.rtcp.parser` (inspect its installed classes). SDP is small text with
CRLF framing and explicit media attributes. This environment does not include a
full RTSP media server or audio/video decoding stack; write bounded
`asyncio.start_server`/native glue for an assigned server. HTTP clients are not
RTSP clients, and **there is no `dpkt.rtsp` module**.

Source: [aiortsp](https://github.com/marssai/aiortsp).

## SIP — Kamailio

```python
from twisted.protocols import sip
request = sip.Request("OPTIONS", "sip:user@example.test")
request.addHeader("cseq", "1 OPTIONS")
request.addHeader("content-length", "0")
wire = request.toString().encode("ascii")
```

`sip.Request`/`Response`, `parseURL`, `parseViaHeader` and `MessagesParser` are
available. In this installed version, message serialization and
`MessagesParser.dataReceived` use **str**; encode/decode at the socket boundary.
Add Via/branch, From/To tags, Call-ID, CSeq and routing/authentication fields
required by the trace; a minimal message above is not a complete transaction.
Preserve transaction/dialog identifiers and handle UDP retransmissions.

`sip.Base`, `Proxy`, `RegisterProxy`, `InMemoryRegistry` are legacy building
blocks documented against RFC 2543. They do not establish complete RFC 3261
or extension support. For exact bytes or SIP/TCP framing use native sockets and
the codec/helpers; build SDP bodies explicitly and compute Content-Length from
encoded bytes. Digest authentication can use `hashlib`/`hmac` with the negotiated
algorithm. Do not install/import the unrelated PyQt `sip` package.

Source: [Twisted SIP APIs](https://docs.twisted.org/en/stable/api/twisted.protocols.sip.html).

## SMTP — Exim

`smtplib.SMTP`/`SMTP_SSL` or `aiosmtplib.SMTP` handle valid client exchanges;
`email.message.EmailMessage` and `email.policy.SMTP` serialize MIME with SMTP
line endings. Use real EHLO capabilities and AUTH/STARTTLS responses. After
STARTTLS, repeat EHLO when required. SDK DATA methods may dot-stuff/normalize
messages, so use explicit wire encoding for malformed DATA cases.

Assigned SMTP/LMTP servers: `aiosmtpd.smtp.SMTP`, `aiosmtpd.lmtp.LMTP` and a custom
handler with `async handle_DATA(self, server, session, envelope) -> str`.
`aiosmtpd.controller.Controller.start()` creates a thread and readiness connection;
always `.stop()` in cleanup. Controller(port=0) is problematic because its
readiness connection can use the configured zero port; for dynamic ports use a
case-local loop's `create_server(lambda: SMTP(handler), host, 0)`, inspect
`server.sockets[0].getsockname()`, then `close()`/`await wait_closed()`.

Sources: [aiosmtpd](https://aiosmtpd.aio-libs.org/en/latest/),
[aiosmtplib](https://aiosmtplib.readthedocs.io/en/stable/).

## SSH — OpenSSH

Use `paramiko.SSHClient` or a socket-backed `paramiko.Transport` for valid peers.
Generate/load host keys with `RSAKey`/`Ed25519Key` or `asyncssh.generate_private_key`.
A server needs a `ServerInterface` subclass, `Transport.add_server_key` and
`start_server(server=...)` with bounded waits. Authentication callbacks must
implement the requested behavior. SSH handshake/authentication, channel setup
and SFTP are distinct operations; do not assume opening a transport runs them all.

`asyncssh.connect` and `asyncssh.create_server` provide async peers, forwarding
and SFTP support. Use explicit connect/login timeouts; close connections/servers
and await `wait_closed`. Manage host-key trust deliberately for test fixtures.
Crypto packet mutation after key exchange must respect SSH framing, negotiated
compression, encryption, sequence numbers and MAC/AEAD; high-level clients
normally hide those packets. `cryptography`/`nacl` provide primitives, not SSH
state machines.

Sources: [Paramiko](https://docs.paramiko.org/en/stable/),
[AsyncSSH](https://asyncssh.readthedocs.io/en/latest/).

## TLS — OpenSSL

For valid sessions use `ssl.SSLContext.wrap_socket`, `ssl.MemoryBIO`/`SSLObject`,
`OpenSSL.SSL.Context`/`Connection`, `tlslite.api.TLSConnection` or `mbedtls.tls`.
Set protocol bounds, SNI, certificate trust, ciphers, ALPN and I/O deadlines to
match the target. Certificate/private-key fixtures use `cryptography.x509` and
key serialization; create transient files inside the case if a socket API
requires filenames.

For handshake/record messages use `tlslite.messages.ClientHello`,
`tlslite.extensions`, Scapy TLS (`scapy.layers.tls.all`) or `dpkt.ssl.TLSRecord`.
Example handshake payload, **without a TLS record header**:

```python
from tlslite.messages import ClientHello
hello = ClientHello().create((3, 3), bytearray(range(32)), bytearray(), [0x002F])
handshake_bytes = bytes(hello.write())
```

Use `struct`/`construct` to wrap or mutate records. `dpkt.ssl` parses records,
but is not a TLS handshake/encryption engine. High-level TLS libraries manage
transcripts, key derivation, Finished and record sequence numbers; they may not
expose malformed extensions or arbitrary record ordering. Reuse their valid
setup or lower-level message/crypto helpers rather than faking Finished.

For encryption/decryption: `cryptography` AEAD (AESGCM, AESCCM,
ChaCha20Poly1305), HKDF, HMAC, RSA/ECDSA/Ed25519, ECDH/X25519;
`Crypto.Cipher.AES`/`ChaCha20_Poly1305`; `nacl` for libsodium operations.
Check negotiated algorithm, nonce, AAD, tag and padding rules. Primitives alone
do not supply TLS state. ASN.1 helpers: `pyasn1` DER/BER, `pyasn1_modules.rfc5280`,
`asn1crypto.x509`/`keys`/`cms`.

Sources: [tlslite-ng](https://github.com/tlsfuzzer/tlslite-ng),
[cryptography](https://cryptography.io/en/latest/),
[PyCryptodome](https://www.pycryptodome.org/),
[Scapy TLS](https://scapy.readthedocs.io/en/latest/api/scapy.layers.tls.html).
