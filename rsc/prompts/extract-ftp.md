# Task: extract an FTP protocol interaction model from the RFC sources

You are extracting one machine-readable protocol model from RFC text. The model
feeds a network fuzzer's generator, so it must describe the protocol itself, not
the benchmark target and not Python libraries.

## Inputs (read them both)

```text
/home/user/mcn-fuzzer/rsc/tests/fixtures/profuzzbench-rfc/FTP/rfc959.html    RFC 959, FTP
/home/user/mcn-fuzzer/rsc/tests/fixtures/profuzzbench-rfc/FTP/rfc3659.html   RFC 3659, FTP extensions
```

Authoritative JSON Schema (read it before writing any output):

```text
/home/user/mcn-fuzzer/rsc/protocol.schema.json
```

Reference model for shape only (it predates the `states`/`processing` fields):

```text
/home/user/profuzzgym/profuzzbench/subjects/FTP/LightFTP/mcn-fuzzer/protocol.json
```

## Outputs

```text
OUT=/home/user/mcn-fuzzer/rsc/runs/direct-codex-20261115T1243/ftp
$OUT/protocol.json
$OUT/snapshots/rfc959.json
$OUT/snapshots/rfc3659.json
$OUT/done.json
```

`protocol.json.meta.documents` must list exactly the two snapshots with
`doc_id` (`rfc959`, `rfc3659`), `rfc` (`RFC 959`, `RFC 3659`) and `path`
(`snapshots/rfc959.json`, ...). `meta.protocol` is a one-line description and
`meta.complete` is `true`.

## Commands

```sh
cd /home/user/mcn-fuzzer/rsc
.venv/bin/rsc ingest tests/fixtures/profuzzbench-rfc/FTP/rfc959.html --out "$OUT/ingest" --force
.venv/bin/rsc ingest tests/fixtures/profuzzbench-rfc/FTP/rfc3659.html --out "$OUT/ingest" --force
mkdir -p "$OUT/snapshots"
for id in rfc959 rfc3659; do
  cp "$OUT/ingest/$id/document.json" "$OUT/snapshots/$id.json"
done
.venv/bin/rsc validate protocol "$OUT/protocol.json"
```

Validation prints `OK <path>: protocol schema and references verified` and exits
0 only when the model is schema-valid and every reference resolves.

## Model requirements (current schema)

Top-level object, no unknown keys:

```text
meta                {protocol, complete, documents[]}
parties             [{name, description}]
messages            [{name, associated_fields: [name]}]
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

- Preserve each source-stated command, reply class, transfer direction, and
  mode with its own state checks and comparisons. Distinct commands with
  different arguments or prerequisites stay distinct message types; do not
  collapse them into a generic command/argument envelope.
- Parties are direct communication roles: user PI, server PI, user DTP,
  server DTP, and the third-party roles the source defines for server-to-server
  transfers. Do not merge distinct peers because they share a type. Channels
  name the underlying transport and direction: control, passive data, active
  data, and third-party data get distinct channels.
- Connection ownership: `connect` uses the channel's initiator (for active data
  the server initiates to the user's listening port; for passive data the user
  initiates to the advertised port). `listen` uses the acceptor. Model the
  advertised endpoint values with `endpoint_sources`.
- A step is a reusable operation, not an occurrence. Reuse one send step for the
  same reply/command, channel, sender, and receiver; keep scenario guards on
  dependency edges. A dependency may cite an earlier occurrence of the same
  step, never the current one.
- Include the greeting and login exchange, command/reply pairing, data-transfer
  completion replies (150/226/425/426/451 and similar), abort/error paths, and
  the QUIT/221 and transport close lifecycles the source defines.
- `condition` is `""` unless there is an additional guard beyond ordering and
  the already-structured action/message/channel fields.
- Put local computation, parsing, checks, and persistence in `processing`:
  parsing a reply code, matching a 227/229 endpoint, deriving a data port or
  address, defaulting TYPE/MODE/STRU, and retaining negotiated values. Declare
  only values needed across steps in `states`; use `source: "processing"` when a
  rule produces it and `"external"` when configuration supplies it (account
  credentials, root directory, permissions, TLS trust).
- Retain defaults alongside optional negotiation; a negotiated value is a
  prerequisite only on the branch that consumes it. Trace stored parameters to
  the exchanges that set and consume them.
- Preserve restart/resume support and the parameters it needs, when the source
  defines it.
- Do not invent facts, combinations, or fields. Record source gaps or
  externally-defined policy in `unresolved` instead of guessing.
- Names are unique per kind; state names are unique per party. Descriptions are
  short role/lifecycle phrases. `condition` strings stay concise.

## Completion

1. Write `protocol.json` and the two snapshots.
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
