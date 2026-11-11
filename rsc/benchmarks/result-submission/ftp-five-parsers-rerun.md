# Second FTP run: five Parsers, five feedback rounds

RFC959 + RFC3659, native `gpt-6-luna/high`, 5 Parser workers, 3 Merge workers, `max_feedback_rounds=5`. This is a fresh run with frozen inputs, source and config. No implementation changes were made for this trial. Initial execution is round 0; regrouping and local fixes both consume the feedback budget.

Raw output: `rsc/runs/ftp-p5-f5-20261110T122142Z/ftp-1110122143-r1/`.

The process stopped after **2,237.87 seconds (37m 18s)** with exit 2. It completed four correction rounds and stopped during round 5 Merge. No protocol was published. Neither the 2,400-second diagnostic deadline nor feedback-budget exhaustion caused the stop.

`merge-2` was assigned `g16`, but attempted to submit `g153`. The server rejected this with `group belongs to another assignment`. The worker then ended its turn with a nonempty final message reporting that rejection, without completing its assigned work. The supervisor stopped the run with `worker merge-2 ended its turn without submitting`. Unlike the preceding trial, this was not an empty final response. Evidence is in `agents/merge-2/codex-session.jsonl:1307` and the workflow event log. The other workers were stopped during cleanup.

| Phase, all rounds combined | Seconds |
| --- | ---: |
| Preprocess | 0.91 |
| Parse | 342.98 |
| Cluster | 300.62 |
| Merge, including interrupted round 5 | 1,593.03 |
| Deterministic Review | 0.33 |

Parse completed all 29 batches and extracted 290 items. Initial clustering produced 146 groups. Rounds 1 and 2 handled regroup requests. Review after round 2 triggered round 3 fixes; further regroup requests led to round 4. Review after round 4 found same-name conflicts and ambiguous references, including duplicate `FTP control connection` channels, leading to round 5. At interruption, 154 groups existed, with results retained for 99 and 55 unfinished. There were 930 agent MCP calls and 32 rejected calls. The owned HC namespace was cleaned up.

No official `protocol.json` exists. The last integrated candidate, from round 4, was validated in memory using the frozen validator: **98 validation errors**. The integration diagnostic contains **99 checks** (1 reference, 2 same-name, 95 ambiguous-reference, 1 structure); this is a different check pass and should not be confused with the file-model validator count. Details are saved in the raw task's `candidate-validation.json`. The incomplete round 5 has not produced a new integrated candidate. No independent semantic review was performed.

This run does not demonstrate convergence with five corrections, because round 5 did not finish. Merge work and repeated regrouping remain the main measured cost; deterministic Review itself took less than one second. No further trial or implementation tuning was started automatically.

[Machine-readable metrics and diagnostics](ftp-five-parsers-rerun.json).
