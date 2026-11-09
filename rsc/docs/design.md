# RFC extraction architecture

The current architecture is documented in [pipeline.md](pipeline.md) and [the README](../README.md).

RSC uses a persistent MCP service, source batches grouped by chapter, a parser, an independent reviewer, and a Python supervisor managing both Codex sessions through Herdr Core. The old objects/interactions extraction, link and completion compiler stages have been replaced.
