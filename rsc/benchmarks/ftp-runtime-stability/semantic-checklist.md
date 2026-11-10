# Semantic acceptance for the FTP runtime trials

Evaluate each published protocol against the saved RFC documents and reviewer findings.
Use record/step names and source evidence, not fixed model sizes or literal name matching.
This inspection is additional to schema/reference, receipt and complete-review gates.
The benchmark's `semantic_audit=pending` is not an accepted semantic result.

| Area | Evidence to inspect | Passing behavior |
| --- | --- | --- |
| Control lifecycle and commands | RFC 959 control connection and command/reply sections | Distinct user/server PI roles, control listen/connect and source-supported termination; command prerequisites and reply conditions preserved. |
| Active data setup | RFC 959 sections 3.2 and PORT command | User-side data listener, server-side connect; PORT provides the appropriate remote endpoint. |
| Passive data setup | RFC 959 sections 3.2 and PASV command | Server-side data listener, user-side connect; PASV reply supplies the selected endpoint. |
| Transfer direction and EOF | RFC 959 sections 3.2, 3.4, STOR and APPE | Upload and download sender/receiver roles correct; in modes where close signals EOF the sender initiates close. Server's connection-maintenance exception for User-DTP sending data is preserved. |
| Completion versus connection reuse | RFC 959 reply 226/250 and connection-management prose | 226 after connection close and 250 with data connection remaining open are not conflated into an unconditional close. Genuine underspecification remains documented. |
| Machine-readable listing | RFC 3659 MLST/MLSD sections and RFC 959 data setup | MLST control reply distinguished from MLSD data transfer; source-supported active/passive setup and transfer-completion dependencies retained. |
| Restart/resumption | RFC 959 REST, RFC 3659 REST STREAM | Restart state and subsequent transfer connected by source-supported dependencies/field relationships; resumed connections not silently placed on a closed channel; active/passive roles not omitted without source justification. |
| Base restart markers | RFC 959 section 3.5 and restart text | Block/Compressed mode's sender/receiver marker mapping and exchanged receiver marker are retained alongside the later STREAM numeric offset mechanism. |
| Resumption safety | RFC 3659 MDTM and REST STREAM usage | RETR safety retains the original modification time, later comparison and unsynchronized-clock caveat; resumed STOR retains the partial destination/source checks specified by the source. |
| Abort outcomes | RFC 959 ABOR and transfer-error descriptions | Completed transfer and transfer-in-progress outcomes remain distinct, including the source-prescribed reply sequence and data cleanup; this is not represented as only a generic command/reply. |
| Third-party transfer | RFC 959 third-party/control/data connection description | Two server PI/DTP roles distinguished, user controls setup, direct data connection represented with correct endpoint derivation and direction. |
| Message and field scope | Cross-check extracted definitions with source text | Actual exchanged units rather than grammar/category labels; relevant cross-message fields retained without invented wire detail or arbitrary command/mode Cartesian products. |
| Repair quality | `review.json`, `changes.json`, `coverage.json` and source evidence | Each submitted resolution independently reviewed; no open or rejected finding hidden by publication at review limit. Unresolved source ambiguity is explicit and evidence-backed. |

Record a per-run assessment containing the source sections checked, supporting model names,
accepted alternatives/unresolved interpretations and any failures. A structural keyword match
is only a candidate locator; it cannot by itself satisfy this checklist.
