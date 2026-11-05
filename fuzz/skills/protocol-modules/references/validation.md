# Fixed generator artifact checks

Run `../scripts/validate_generator.py` using the campaign interpreter and the
exact invocation rendered in the generation prompt. No third-party package is
needed by the validator itself. It reads files and prints one JSON report.

## Common checks (also used by Rust acceptance)

- Completion IDs equal the dispatched request, protocol and trace IDs; status is
  `ok`; added_steps is an array of strings. Pass result.draft.json via `--result`
  before publication; the default path is OUTPUT_DIR/result.json for acceptance.
- generator.py parses and compiles without execution. It defines a synchronous
  top-level generate function callable with two positional arguments, without
  an unfulfilled required argument or yield-based generator entrypoint.
- Schema root type is object. The production subset checks string names in
  required, additionalProperties=false when properties is provided, and the
  declared property types string/integer/boolean/array.
- Params root is an object. Every value recursively is a string, boolean, signed
  64-bit integer or list of these. Objects, floats, null and non-JSON numbers are
  rejected. Boolean values do not satisfy integer properties.
- Remote `$ref` values are rejected recursively. Local refs and other unsupported
  keywords are not resolved or enforced. Do not rely on them.

The checker does **not** require every property to appear in required, require
additionalProperties=false to be explicitly present, or enforce enum, pattern,
minimum/maximum, array item schemas or the full JSON Schema vocabulary. The
generation prompt may request a narrower authoring style than this accepted
subset. Use the fixed checker rather than inventing extra acceptance rules.

## Isolated import probe (`--check-imports`)

The probe loads the module with the same Registry loader used by the runtime and
checks its actual callable signature. It runs under the campaign Python with
the supplied import path, in a temporary cwd, with a default five-second deadline.
It catches top-level import errors, async/generator entrypoints, invalid callable
signatures and ordinary import-time socket, thread, subprocess or file writes.

This is a lifecycle check, not a security sandbox. SDK imports that need services
or OS sockets belong inside generate, as required by the worker contract.
Lazy imports inside generate are not executed by this probe; verify relevant SDK
APIs when choosing a module. The probe never calls generate or starts the SUT.

## What remains to check

Review selected step order, party ownership, conditional prerequisites, endpoint
discovery, actual field reuse, deadlines and cleanup. Small checks of pure
protocol encoding/parsing helpers can be useful; do not duplicate the common
checks or guess target fixture paths. Only a real execution can establish the
observed interaction, and evaluator success is not automatic semantic coverage.

On success, atomically rename the checked completion draft to result.json. On
failure, fix the reported error before publishing a success signal. Do not import
the validator from generator.py or depend on its files in a published generator.
