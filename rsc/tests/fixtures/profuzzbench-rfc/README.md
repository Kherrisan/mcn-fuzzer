# ProFuzzBench protocol RFC corpus

RFC documents used to exercise RSC against the protocols in
[ProFuzzBench](https://github.com/profuzzbench/profuzzbench).

- ProFuzzBench commit: `8573ec81d68f2c1ffc8f5e605dfdd9c61fbeb074`
- Retrieved: 2026-10-18T13:02:47.173890+00:00
- Source: <https://www.rfc-editor.org/>
- Formats: HTML (RFC Editor) and RFCXML for RFC 9147.
- Files: 28 (27 HTML + 1 RFCXML), about 4.8 MB.
- DAAP and DICOM have no IETF RFC and are marked `no_rfc` in the manifest.

| Protocol | Target(s) | RFCs |
|---|---|---|
| DAAP | forked-daapd | — (no RFC) |
| DICOM | Dcmtk | — (no RFC) |
| DNS | Dnsmasq | RFC 1034, RFC 1035, RFC 2181, RFC 2308, RFC 6891 |
| DTLS | TinyDTLS | RFC 6347, RFC 9147 (HTML + XML) |
| FTP | BFTPD, LightFTP, ProFTPD, PureFTPD | RFC 959, RFC 3659 |
| RTSP | Live555 | RFC 2326, RFC 7826 |
| SIP | Kamailio | RFC 3261, RFC 3262, RFC 3264, RFC 3311, RFC 3326 |
| SMTP | Exim | RFC 5321, RFC 5322, RFC 1869, RFC 4954, RFC 3207 |
| SSH | OpenSSH | RFC 4251, RFC 4252, RFC 4253, RFC 4254 |
| TLS | OpenSSL | RFC 5246, RFC 8446 |

## Notes

- Old RFC HTML is the RFC Editor `rfc2html` form: the document is a single
  `<pre>` block with `<span class="h1">`/`h2`/`h3` headings. RSC must handle
  this layout, not only modern HTML with `<section>`/`<p>`.
- RFC 9147 has both HTML and RFCXML; the XML file is kept for the XML adapter.
- The manifest records URL, size and SHA-256 for every file.
- Re-run `python fetch.py` from this directory to refresh the files; use
  `python fetch.py --check` to verify the checked-in files against the manifest.
