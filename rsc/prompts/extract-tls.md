# Task: extract a TLS protocol interaction model from the RFC sources

You are extracting one machine-readable protocol model from RFC text. The model
feeds a network fuzzer's generator, so it must describe the protocol itself, not
the benchmark target and not Python libraries.

## Inputs (read them all)

Sources (HTML RFC snapshots):

```text
/home/user/mcn-fuzzer/rsc/tests/fixtures/profuzzbench-rfc/TLS/rfc8446.html   RFC 8446, TLS 1.3
/home/user/mcn-fuzzer/rsc/tests/fixtures/profuzzbench-rfc/TLS/rfc6066.html   RFC 6066, TLS extensions
/home/user/mcn-fuzzer/rsc/tests/fixtures/profuzzbench-rfc/TLS/rfc7301.html   RFC 7301, ALPN
```

Authoritative JSON Schema (read it before writing any output):

```text
/home/user/mcn-fuzzer/rsc/protocol.schema.json
```

Reference model for shape only (it predates the `states`/`processing` fields):

```text
/home/user/mcn-fuzzer/rsc/runs/tls13-codex-high-20261112c/protocol.json
```

## Outputs

```text
OUT=/home/user/mcn-fuzzer/rsc/runs/direct-codex-20261115T1243/tls
$OUT/protocol.json
$OUT/snapshots/rfc8446.json
$OUT/snapshots/rfc6066.json
$OUT/snapshots/rfc7301.json
$OUT/done.json
```

`protocol.json.meta.documents` must list exactly the three snapshots with
`doc_id` (`rfc8446`, `rfc6066`, `rfc7301`), `rfc` (`RFC 8446`, ...) and `path`
(`snapshots/rfc8446.json`, ...). `meta.protocol` is a one-line description and
`meta.complete` is `true`.

## Commands

```sh
cd /home/user/mcn-fuzzer/rsc
.venv/bin/rsc ingest tests/fixtures/profuzzbench-rfc/TLS/rfc8446.html \
  tests/fixtures/profuzzbench-rfc/TLS/rfc6066.html \
  tests/fixtures/profuzzbench-rfc/TLS/rfc7301.html --out "$OUT/ingest" --force
mkdir -p "$OUT/snapshots"
for id in rfc8446 rfc6066 rfc7301; do
  cp "$OUT/ingest/$id/document.json" "$OUT/snapshots/$id.json"
done
.venv/bin/rsc validate protocol "$OUT/protocol.json"
```

`rsc ingest` accepts one input path per call; run it once per RFC if passing all
three fails. Validation prints `OK <path>: protocol schema and references
verified` and exits 0 only when the model is schema-valid and every reference
resolves.

## Model requirements (current schema)

Top-level object, no unknown keys:

```text
meta                {protocol, complete, documents[]}
parties             [{name, description}]
messages            [{name, associated_fields: [{name}]}]
channels            [{name, description, transport, connection,
                      endpoint_party_names[], initiator_party_name,
                      acceptor_party_name, endpoint_sources[]}]
states              [{name, party_name, source: "processing"|"external"}]   optional
steps               [send_step | listen_step | connect_step | close_step]
step_dependencies   [{prior_step, dependent_step, condition, field_relations[]}]
unresolved          [{description, object_names[]}]
```

`send_step` requires `name`, `action: "send"`, `channel_name`, `condition`,
`message_name`, `sender_name`, `receiver_name`, and may carry:

```text
processing: {
  before_send: [operation],   # sender, before the record leaves
  after_send:  [operation],   # sender, after it left
  on_receive:  [operation]    # receiver, after it arrived
}
operation: {reads: [string], writes: [string], rule: string}
```

Operation references: `state.<name>` for the acting party's own state,
`message.<field>` for an associated field of the current message, and
`message_bytes` for the deframed bytes after send/receive. Only `before_send`
may write `message.<field>`. `reads` and `writes` may be empty arrays but `rule`
must be non-empty and describe the branch/transformation/validation.

Non-send steps require `name`, `action`, `channel_name`, `condition`, and
`party_name`. `action` is one of `send`, `listen`, `connect`, `close`.

## Modeling rules

- Preserve each source-stated mode, direction, and lifecycle with its own state
  checks and comparisons. Distinct selectors with different arguments, state
  changes, or prerequisites stay distinct message types; do not collapse them
  into a generic command envelope.
- Parties are direct communication roles, not software containers. A channel's
  `transport` is the underlying transport named by the source. `connect` uses
  the channel's initiator; `listen` uses its acceptor. Source-supported reversed
  roles use distinct channels.
- A step is a reusable operation, not an occurrence. Reuse one send step for the
  same message, channel, sender, and receiver; keep scenario guards on
  dependency edges. A dependency may cite an earlier occurrence of the same
  step, never the current one.
- `condition` is `""` unless there is an additional guard beyond ordering and
  the already-structured action/message/channel fields. Do not narrate the same
  fact twice.
- Put local computation, checks, persistence, defaults, and negotiated-state
  retention in `processing`. Declare only values needed across steps in
  `states`; use `source: "processing"` when a rule produces it and
  `"external"` when configuration supplies it (trust anchors, PSK policy,
  application identity policy, and similar). Keep intermediate values inside
  `rule`. Never invent transmitted fields or network messages for local state.
- For TLS 1.3 capture key-schedule inputs, transcript boundaries (including the
  HelloRetryRequest transcript replacement), Finished verification, and
  resumption/ticket inputs from the earlier connection. For TLS 1.2 include the
  separate master-secret and ChangeCipherSpec flow when the source defines it.
- Retain defaults alongside optional negotiation; a negotiated value is a
  prerequisite only on the branch that consumes it.
- Preserve error/abort behavior, `close`/`close_notify`, connection reuse, and
  distinct reconnect/resumption lifecycles as their own channels where the
  source requires a new transport.
- Do not invent facts, combinations, or fields. Record source gaps or
  externally-defined policy in `unresolved` instead of guessing.
- Names are unique per kind; state names are unique per party. Descriptions are
  short role/lifecycle phrases. `condition` strings stay concise.

## Completion

1. Write `protocol.json` and the three snapshots.
2. Run the validation command. Fix every reported error and re-run.
3. Only when validation exits 0, write `done.json`:

```json
{"status": "ok", "validated": true, "output": "<OUT>/protocol.json",
 "command": ".venv/bin/rsc validate protocol <OUT>/protocol.json",
 "counts": {"parties": 0, "messages": 0, "channels": 0, "states": 0, "steps": 0, "dependencies": 0}}
```

If a genuine source limitation blocks completion, write `{"status": "error",
"reason": "..."}` instead, but never stop while a validation error is fixable.

Do not read benchmark SUT source code. Do not modify anything outside `$OUT`.
