# RFC fixture sources

## Synthetic fixtures

- `sample-rfc.txt` — hand-written RFC 9999-style text covering page headers/footers,
  TOC, a paragraph split across a page boundary, list lead-ins, a table, ABNF and
  an ASCII diagram.
- `sample-rfc.html` — hand-written HTML with headings, paragraphs, a list, a
  grammar block and a table.
- `sample-rfc.xml` — hand-written RFCXML with nested sections, paragraphs, a list
  and an artwork block.

## Real RFC fixture

- `rfc8446.txt` — *The Transport Layer Security (TLS) Protocol Version 1.3*.

Source: <https://www.rfc-editor.org/rfc/rfc8446.txt>

Downloaded: 2026-10-17 (UTC) from the RFC Editor.

SHA-256:

```text
47871bc8820a2c3b6ea89f061055577058862cf543686b82d10131239702b3bd
```

This file is included only as an ingest/parser regression input. It is not
modified; `tests/test_ingest.py::Tls13FixtureTests` asserts its metadata,
section tree, the Key Update section and page-artifact removal.
