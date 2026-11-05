# Installed module inventory

Versions below are the prepared campaign environment. Verify the configured Python before use.
`modules.json` is the machine-readable inventory used by `../scripts/audit.py`.

| pip distribution | Version | Python imports | Use / limits |
| --- | --- | --- | --- |
| scapy | 2.6.1 | `scapy.layers.inet`, `scapy.layers.dns`, `scapy.layers.tls.all`, `scapy.layers.rtp` | Packet builders/parsers: IP/TCP/UDP/DNS/TLS/RTP; raw send is usually unnecessary. |
| dpkt | 1.9.8 | `dpkt`, `dpkt.dns`, `dpkt.ssl`, `dpkt.rtp` | Lightweight packets, TLS records, DNS, RTP, PCAP; no dpkt.rtsp module. |
| construct | 2.10.70 | `construct` | Declarative binary fields, lengths, arrays and TLVs. |
| bitstring | 5.0.0 | `bitstring` | Bit-level integer/flag packing and parsing. |
| pyasn1 | 0.6.5 | `pyasn1.codec.der.encoder`, `pyasn1.codec.der.decoder` | ASN.1 DER/BER encoding and decoding. |
| pyasn1-modules | 0.4.2 | `pyasn1_modules.rfc5280` | ASN.1 certificate and related RFC schemas. |
| asn1crypto | 1.5.1 | `asn1crypto.x509`, `asn1crypto.keys`, `asn1crypto.cms` | X.509, PKCS and CMS structure parsing/building. |
| cryptography | 50.0.2 | `cryptography.x509`, `cryptography.hazmat.primitives.ciphers.aead` | X.509, key exchange, signatures, KDFs and authenticated encryption. |
| pycryptodome | 4.3.0 | `Crypto.Cipher.AES`, `Crypto.Cipher.ChaCha20_Poly1305`, `Crypto.PublicKey.RSA` | Additional crypto primitives; import Crypto, not Cryptodome. |
| pyOpenSSL | 26.4.0 | `OpenSSL.SSL`, `OpenSSL.crypto` | OpenSSL TLS contexts/connections and certificate integration. |
| tlslite-ng | 0.8.2 | `tlslite.api`, `tlslite.messages`, `tlslite.extensions` | TLS client/server, handshake messages and extensions; not DTLS. |
| python-mbedtls | 2.10.1 | `mbedtls.tls`, `mbedtls.pk`, `mbedtls.x509` | DTLS 1.0/1.2 and TLS peers, PSKs, certificates; not DTLS 1.3. |
| dnspython | 2.9.2 | `dns.message`, `dns.query`, `dns.edns`, `dns.dnssec` | DNS messages/queries, EDNS, TSIG and DNSSEC helpers. |
| dnslib | 0.9.26 | `dnslib`, `dnslib.server` | DNS wire messages and configurable UDP/TCP upstream servers. |
| pydicom | 3.0.2 | `pydicom`, `pydicom.filewriter` | DICOM datasets, UIDs and transfer-syntax codecs; not network associations. |
| pynetdicom | 3.0.4 | `pynetdicom`, `pynetdicom.pdu` | DICOM SCU/SCP association, DIMSE and PDU support. |
| pyftpdlib | 2.2.0 | `pyftpdlib.handlers`, `pyftpdlib.authorizers`, `pyftpdlib.servers` | FTP/FTPS servers, authentication and active/passive data endpoints. |
| aioftp | 0.28.3 | `aioftp` | Async FTP clients and servers; not a drop-in FTPS client. |
| aiosmtpd | 1.4.6 | `aiosmtpd.smtp`, `aiosmtpd.controller` | SMTP/LMTP servers, AUTH and STARTTLS; explicit lifecycle required. |
| aiosmtplib | 5.1.3 | `aiosmtplib` | Async SMTP client and STARTTLS. |
| paramiko | 5.1.0 | `paramiko` | Synchronous SSH transport, client/server, host keys and SFTP. |
| asyncssh | 2.24.1 | `asyncssh` | Async SSH client/server, channels, SCP/SFTP. |
| pyatv | 0.18.0 | `pyatv.protocols.dmap.tags`, `pyatv.protocols.dmap.parser`, `pyatv.protocols.dmap.tag_definitions` | DAAP/DMAP tag codecs; Apple TV client, not a complete DAAP music server. |
| aiohttp | 3.14.5 | `aiohttp`, `aiohttp.web` | HTTP client/server for DAAP transport and HTTP upstream roles. |
| httpx | 0.28.1 | `httpx` | Synchronous/async HTTP clients for DAAP; not RTSP. |
| aiortsp | 1.4.0 | `aiortsp.rtsp.parser`, `aiortsp.rtsp.connection`, `aiortsp.rtcp.parser` | RTSP client and incremental request/response/interleaved RTP parser; no full media server. |
| Twisted | 26.10.0 | `twisted.protocols.sip`, `twisted.internet.asyncioreactor` | SIP messages/parser and legacy proxy/registrar building blocks; reactor lifecycle matters. |
| PyNaCl | 1.6.2 | `nacl.public`, `nacl.signing`, `nacl.bindings` | libsodium keys, signatures and encryption, also used by SSH. |
| zeroconf | 0.153.0 | `zeroconf`, `zeroconf.asyncio` | DNS-SD/mDNS registration/discovery when DAAP setup actually requires it. |
| h11 | 0.16.0 | `h11` | HTTP/1.1 event/framing codec for custom HTTP peers; not RTSP. |
| cbor2 | 5.6.5 | `cbor2` | CBOR encoding/decoding; also the existing generator IPC dependency. |
| PyYAML | 6.0.3 | `yaml` | Configuration fixture parsing; use safe_load. Not a network protocol engine. |

## Standard library (no pip install)

- Transport and lifecycle: `socket`, `socketserver`, `asyncio`, `selectors`, `threading`, `contextlib`, `tempfile`.
- FTP/SMTP/HTTP clients and MIME: `ftplib`, `smtplib`, `email`, `http.client`, `http.server`, `urllib.parse`, `urllib.request`.
- TLS: `ssl.SSLContext`, `ssl.MemoryBIO`, `ssl.SSLObject`. The linked OpenSSL controls available versions/ciphers; stdlib `ssl` does not implement DTLS.
- Wire codecs: `struct`, `array`, `base64`, `binascii`, `codecs`, `json`, `plistlib`, `uuid`, `ipaddress`.
- Compression: `zlib`, `gzip`, `bz2`, `lzma`.
- Hash/auth helpers: `hashlib`, `hmac`, `secrets`. Use installed crypto packages for ciphers, signatures and key exchange.

## Sources

- [ProFuzzBench protocol/target directory](https://github.com/profuzzbench/profuzzbench/tree/master/subjects); matches `rsc/tests/fixtures/profuzzbench-rfc/manifest.json`.
- The import paths and APIs in this skill were checked against installed source, not inferred from distribution names.
- Runtime dependencies, including transitive packages, are pinned in `../../../python/requirements-protocols.txt`.
- The package catalog is curated for the benchmark; it does not imply every extension/feature in each protocol is implemented.
