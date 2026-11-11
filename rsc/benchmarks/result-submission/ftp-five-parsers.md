# FTP rerun: five Parsers, five feedback rounds

Requested configuration: RFC959 + RFC3659, `gpt-6-luna/high`, 5 Parser workers, 3 Merge workers, `max_feedback_rounds=5`. Initial execution is round 0; regrouping and local correction both consume feedback rounds. The diagnostic retained the 2,400-second total cap.

Raw output: `rsc/runs/ftp-p5-f5-20261110T105513Z/ftp-1110105513-r1/`.

The run stopped after **1,399.60 seconds (23m 20s)** with exit 2. No protocol was published. This was **not feedback-budget exhaustion**: at feedback round 2, `merge-1` ended its turn without submitting, so the supervisor stopped and the runner cleaned up the owned HC namespace. The task's saved limit was verified as 5. The other two Merge workers were interrupted by cleanup; they did not independently fail.

| Phase (all rounds combined) | Seconds |
| --- | ---: |
| Preprocess | 0.96 |
| Parse | 217.36 |
| Cluster | 279.96 |
| Merge, including the interrupted round | 901.18 |
| Deterministic Review | 0.14 |

Parse completed all 29 source batches with 252 items. Initial clustering produced 111 groups. After regrouping and review feedback, 103 groups remained, of which 54 retained results at interruption. Agent MCP calls: 519; rejected calls: 20.

Native logs show an empty final answer from `merge-1`, no RSC tool call in that turn, and task completion after 9.806 seconds. Its last request used 229,755 input tokens (214,912 cached) and 236 output tokens, including 230 reasoning tokens. Long context is an observed risk, not a proven explanation for the empty answer. No explicit API error was observed for that turn. The supervisor correctly did not treat the empty response as a submitted result.

This run does not establish whether five feedback rounds would converge, since it stopped during round 2. Compared with the preceding three-Parser run, both Parser count and correction budget changed, and neither run published a model; their total wall times are not a successful performance comparison.

The implementation now supports `[workflow].max_feedback_rounds`, stores the setting with the task and preserves it on resume. Existing tasks without the setting retain their old limit of 2. The benchmark accepts `--max-feedback-rounds 5`. Forty-three related tests and the changed-file Ruff checks passed.

[Machine-readable metrics and diagnostics](ftp-five-parsers.json). Original submissions, source/code snapshot, supervisor logs and private native session traces remain in the raw output directory. This single run was not automatically replaced by another fresh trial.
