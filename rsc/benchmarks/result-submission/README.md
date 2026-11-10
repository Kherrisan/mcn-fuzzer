# Result-submission pipeline validation

Implementation follows [the result-submission plan](../../../plans/rsc-result-submission-pipeline.md). This replaces the previous LLM Parse/Review/Fix experiments; those historical results do not measure this architecture.

## Automated and native checks

- 159 pytest tests and 78 subtests passed; changed runtime modules pass Ruff and `git diff --check`.
- The suite covers atomic/concurrent submissions, same-payload replay, rejected changed/stale attempts, completed-batch recovery, group partition checks, canonical name/reference rewriting, ambiguous references, qualified fields, dependency ownership/deduplication, explicit prose conflicts, bounded correction, immutable group reads, real socket/stdio catalogs, fair HC dispatch and native-session restoration.
- A real Luna/high greeting-protocol run used two Parse and two Merge workers, published 15 items from 10 groups, and passed submission-replay audit. Its Parse/Cluster/Merge/Review times were 66.78/30.14/93.39/0.02 seconds. These phase figures exclude 216.66 seconds of preprocessing/setup, which included an HC namespace collision and a too-long Unix socket path before resume. It is an integration smoke, not an FTP performance claim.
- Smoke output: `rsc/runs/result-smoke-20261110T091928Z/result-smoke-20261110T091928Z/`; `native-verification.json` contains the audit, phase metrics and native token counters.
- Real smoke exposed ambiguous enum descriptions and incorrectly shaped dependency updates; tool descriptions now distinguish scalar enum alternatives and `{id,value,evidence}` updates.

## Frozen FTP comparison

Both runs use RFC959 + RFC3659 in that order, Luna/high, the same frozen source, 8,000/12,000 token batches and three Merge workers. Parser count is 1 then 3. Each run has a 2,400-second cap. Source/code/config and original logs are retained under:

`rsc/runs/results-ftp-20261110T094019Z/`

An earlier comparison at `results-ftp-20261110T093005Z` was stopped during Parse after discovering the native output-budget configuration still named the removed `read_batches` tool. It is excluded from the comparison. The corrected adapter derives output limits from the current read-tool set, with a regression test. The replacement snapshot also includes qualified-field and frozen-group-read fixes; both comparison runs use this same corrected snapshot.

Both runs finished below the 40-minute diagnostic cap, but **neither published a protocol** and neither met the 20-minute target. Both exhausted the two allowed correction rounds. Owned HC namespaces were cleaned up, native token counters were captured, and native logs showed zero truncated tool outputs after the budget fix.

| Stage (all rounds combined) | 1 Parser | 3 Parsers |
| --- | ---: | ---: |
| Preprocess | 0.95 s | 0.95 s |
| Parse | 828.08 s | 282.81 s |
| Cluster | 131.48 s | 321.63 s |
| Merge | 936.94 s | 787.60 s |
| Deterministic Review | 0.28 s | 0.15 s |
| Complete process wall time | 1,898.76 s (31m 39s) | 1,394.36 s (23m 14s) |
| Agent MCP calls / rejected calls | 619 / 19 | 537 / 16 |
| Published | no | no |

The single-Parser run ended with 18 field-carrier errors. For example, its RNTO message defined a long field label describing pathname semantics, while the dependent relation was corrected to the shorter `pathname`; these still did not match. Independent group work plus bounded after-the-fact correction did not converge on consistent field names.

The three-Parser run ended on further regroup requests for three groups: distinct server PI/FTP roles were still clustered together, an MLSD data channel duplicated an existing transport channel, and a reply step combined different server peers. The initial 63 groups grew to 85 after splitting over-broad command/reply groups, then became 78 after another correction. The final request required a third correction, so the server failed rather than bypassing the configured bound.

In this pair of runs Parse was about 2.93 times faster with three workers, and deterministic Review was negligible. This does **not** establish successful full-pipeline acceleration: neither run passed, extracted item counts differed (208 versus 234), and one sample per configuration is insufficient for a stability claim. Clustering quality, stable field/reference naming and correction convergence remain unresolved acceptance limitations. There was no independent semantic review.

[results.json](results.json) records complete phase metrics, final errors and native token totals without private session traces. Per-run raw `timing-result.json`, `workflow.sqlite`, original submissions and logs remain in the ignored run directory. No additional tuning runs were automatically started.
