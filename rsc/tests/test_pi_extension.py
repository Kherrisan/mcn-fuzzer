"""Exercise the packaged extension, including Pi's real native 20 KiB truncation."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_pi_lossless_results_errors_and_abort(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is an optional Pi runtime")
    extension = Path(__file__).resolve().parents[1] / "rsc/agents/pi.mjs"
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "namespace": "rsc_test",
                "bridge": {"command": "unused", "args": []},
                "model": "deepseek/deepseek-flash",
                "effort": "high",
                "events": str(tmp_path / "events.jsonl"),
                "fatal": str(tmp_path / "fatal.json"),
            }
        )
    )
    script = tmp_path / "test.mjs"
    script.write_text("""import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const { default: extension, restoreResult } = await import(pathToFileURL(process.argv[2]));
const hooks = new Map(); let aborted = 0;
const ctx = { model: { provider:'deepseek', id:'deepseek-flash' }, abort: async()=>{aborted++;} };
extension({ registerMcpServer:(name,cfg)=>assert.equal(cfg.exposure,'direct'),
 on:(name,fn)=>hooks.set(name,fn), getThinkingLevel:()=> 'high' });
await hooks.get('before_agent_start')({},ctx);
const text = 'HEAD\\n' + '数据中段\\n'.repeat(6000) + '\\nTAIL';
let event = { toolName:'mcp__rsc_test__read_source_batches',
 content:[{type:'text',text:'truncated'}],
 structuredContent:{ content:[{type:'text',text}], isError:false } };
if (process.argv[3]) {
 const {convertMcpResult} = await import(pathToFileURL(process.argv[3]));
 const native = await convertMcpResult('rsc_test','read_source_batches',event.structuredContent,
   {saveOutput:async()=>'/tmp/not-read-by-extension'});
 assert.notEqual(native.content[0].text,text);
 event = {...native,toolName:event.toolName};
}
assert.equal((await hooks.get('tool_result')(event,ctx)).content[0].text,text);
assert.equal(restoreResult({...event,structuredContent:{content:[{type:'text',text:'denied'}],isError:true}},'rsc_test').isError,true);
assert.equal(restoreResult({toolName:'unrelated'},'rsc_test'),undefined);
const failed = await hooks.get('tool_result')({toolName:event.toolName},ctx);
assert.equal(failed.isError,true); assert.equal(aborted,1);
assert.equal(hooks.get('tool_call')().terminate,true);
ctx.model.id = 'unexpected-model';
await hooks.get('before_agent_start')({},ctx);
assert.equal(aborted,2);
console.log('lossless and abort verified');
""")
    args = [node, str(script), str(extension)]
    pi = shutil.which("pi")
    if pi:
        native = Path(pi).resolve().parents[2] / "dist/extensions/mcp/tools.js"
        if native.exists():
            args.append(str(native))
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "RSC_PI_RUNTIME": str(runtime)},
    )
    assert result.returncode == 0, result.stderr
    assert "lossless and abort verified" in result.stdout
    assert (tmp_path / "fatal.json").exists()
