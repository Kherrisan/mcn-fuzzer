# RSC agent instructions

Runtime instructions are generated from `rsc/workflow/instructions.py` and the six contracts in `rsc/workflow/tools.py`. See [docs/pipeline.md](docs/pipeline.md) for the current Parse → Cluster → Merge → deterministic Review flow.

The former singleton prompt, LLM Review/Fix loop, reading receipts and explicit phase-advancement instructions are obsolete. Historical tasks must use the prompt/code snapshot stored with that run.
