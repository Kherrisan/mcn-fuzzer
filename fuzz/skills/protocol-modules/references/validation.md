# Fixed generator artifact checks

Use `synthesis.validate_synthesis_draft` when the generation prompt provides it.
It applies the Rust publication checks to the current request's draft, including
manifest and proposal consistency. For profiles without the MCP tool, follow
the prompt's static `validate_generator.py` command. Neither static validator
imports or executes the candidate.

## Common checks (also used by Rust acceptance)

- The completion request_id equals the dispatched request; status is
  `ok`; added_steps is an array of strings. Pass result.draft.json via `--result`
  before publication; the default path is OUTPUT_DIR/result.json for acceptance.
- generator.py parses and compiles without execution. It defines a synchronous
  top-level decorated step handler. Handlers and any decorated initializer
  must accept two positional arguments and may not yield.
- Schema root type is object. The production subset checks string names in
  required, additionalProperties=false when properties is provided, and the
  declared property types string/integer/boolean/array.
- Params root is an object. Field values may be strings, booleans, signed
  64-bit integers, lists or objects. Floats, null and non-JSON numbers are
  rejected. Boolean values do not satisfy integer properties.
- Remote `$ref` values are rejected recursively. Local refs and other unsupported
  keywords are not resolved or enforced. Do not rely on them.

The checker does **not** require every property to appear in required, require
additionalProperties=false to be explicitly present, or enforce enum, pattern,
minimum/maximum, array item schemas or the full JSON Schema vocabulary. The
generation prompt may request a narrower authoring style than this accepted
subset. Use the fixed checker rather than inventing extra acceptance rules.

## Optional agent import probe

When module loading is uncertain, use the campaign interpreter and the paths
from the generation prompt. Set `VALIDATOR` to the artifact validator script
path. Run the probe before promoting the draft to `result.json`.

```sh
"$PYTHON" "$VALIDATOR" --output-dir "$OUTPUT_DIR" --request-id "$REQUEST" \
  --result "$OUTPUT_DIR/result.draft.json" --check-imports \
  --pythonpath "$PYTHONPATH" --shared-lib "$WORKSPACE/lib"
```

The probe loads the module with the same Registry loader used by the runtime and
checks its actual callable signature. It runs under the campaign Python with
the supplied import path, in a temporary cwd, with a default five-second deadline.
It catches top-level import errors, async/generator entrypoints, invalid callable
signatures and ordinary import-time socket, thread, subprocess or file writes.

This is a lifecycle check, not a security sandbox. SDK imports that need services
or OS sockets belong inside generate, as required by the worker contract.
Lazy imports inside generate are not executed by this probe; verify relevant SDK
APIs when choosing a module. The probe never calls generate or starts the SUT.

Generator.py may import reusable modules from `WORKSPACE/lib`; the generator
server adds that directory to `sys.path` when it loads a generator, and the
validator receives it through `--shared-lib` for its import probe. Generator
code does not need a parent-directory bootstrap. Shared helpers must be
import-safe (no sockets, threads, subprocesses or file writes at module scope);
the probe does not validate each helper separately. Reuse existing helper
modules as-is; add a versioned module for changed behavior instead of editing an
existing module in place.

## What remains to check

Review selected step order, party ownership, conditional prerequisites, endpoint
discovery, field reuse, deadlines and cleanup in the handler and helper source.
Only a published generator's actual execution can establish observed interaction.

On success, atomically rename the checked completion draft to result.json. On
failure, fix the reported error before publishing a success signal. Do not import
the validator from generator.py or depend on its files in a published generator.
