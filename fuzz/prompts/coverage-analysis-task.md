# Coverage analysis request

REQUEST={{ request_id }}
SESSION={{ session_id }}
OUTPUT_DIR={{ output_dir }}
INPUT_FILE={{ output_dir }}/input.json
SNAPSHOT={{ snapshot }}
SNAPSHOT_DIR={{ snapshot_dir }}
BUILD={{ build }}
CALLGRAPH_JSON={{ callgraph_json }}
CFG_JSON={{ cfg_json }}
MANIFEST={{ manifest }}
BINARY={{ binary }}
MODEL={{ model }}
SOURCE_DIR={{ source_dir }}

The fuzzer converted the callgraph and all CFG DOTs once at startup. Do not parse the original DOTs, CALLGRAPH_JSON, or CFG_JSON. region_summary already lists the missed locations.

The fuzzer already measured this frozen snapshot in-process. Do not run a measurement command. handlers.json and regions.json are frozen; do not open regions.json and do not edit either file. INPUT_FILE.region_summary is the coverage summary: each row is one handler with missed branch directions, function count, unknown count, zero, complete, and a few source locations. zero=true with complete=false is not full coverage. unknown boundaries are not misses.

INPUT_FILE.steps is the only step table. `id` is the fuzzer step id. `name` is the only string allowed as prior_step or dependent_step. Do not invent step names or ids.

Do not search or list SOURCE_DIR. Open a source file only when a region_summary location names it, and only to confirm a dependency you will write. Use the dispatch evidence already in handlers.json. Then write the two JSON files and stop. Do not keep searching after those decisions are made.

Write OUTPUT_DIR/protocol-refined.json exactly as JSON:
{"step_dependencies":[{"prior_step":"exact step name","dependent_step":"exact step name","condition":"text","field_relations":[]}]}
`step_dependencies` is required. Use [] when there is no dependency update.

Write OUTPUT_DIR/priority-delta.json exactly as JSON:
{"steps":[{"step_id":"exact id from INPUT_FILE.steps","delta":1.0,"reason":"text"}]}
`steps` is required. Use [] when there is no priority update. delta must be finite.

Copy session, request, build, snapshot, and input_protocol from INPUT_FILE into result.json. As soon as the two JSON files are written, publish result.json atomically with status "ok", or status "error" and an error string. That publication is the end of the task.
Do not turn an uncertain condition into a required prefix.
