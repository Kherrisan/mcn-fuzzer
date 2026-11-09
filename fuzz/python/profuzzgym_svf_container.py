"""Execute a recorded ProFuzzGym analysis build inside its dependency image."""

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

OUT = Path('/out')
plan = json.loads((OUT / 'plan.json').read_text())
spec = plan['spec']
name = plan['name']
source = Path('/opt/pfb/src') / plan['source']['dest']
assets = OUT / 'assets'
assets.mkdir(exist_ok=True)
context = Path('/gym') / Path(plan['spec_path']).parent
for asset in spec.get('assets', []):
    original = context / asset['path']
    destination = assets / Path(asset.get('destination', original.name)).name
    if original.is_dir():
        shutil.copytree(original, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(original, destination)
env = os.environ.copy()
env.update(PATH='/analysis-tools:/analysis-llvm/bin:' + env['PATH'],
           CC='/analysis-tools/wllvm', CXX='/analysis-tools/wllvm++',
           LLVM_COMPILER='clang', LLVM_COMPILER_PATH='/analysis-llvm/bin',
           LLVM_LINK_NAME='llvm-link', LLVM_AR_NAME='llvm-ar',
           LLVM_BITCODE_GENERATION_FLAGS=f'-g -fdebug-prefix-map={source}={plan["host_source"]}',
           FUZZ_SOURCE=str(source), SOURCE_SUT=str(source), WORKDIR=str(assets),
           AFLNET='/analysis-tools', MAKE_JOBS=str(plan['make_jobs']))
env.pop('AFL_USE_ASAN', None)
env.pop('AFL_PATH', None)
if name == 'tinydtls':
    env['CPPFLAGS'] = '-D_DEFAULT_SOURCE'
steps_path = OUT / 'completed-steps.json'
done = json.loads(steps_path.read_text()) if steps_path.exists() else []
commands = []
try:
    # Some dependency images omit `file`. Recover annotations from bitcode
    # already emitted by wllvm before the missing tool was supplied.
    repaired = 0
    os.environ.update(env)
    sys.path.insert(0, '/home/user/mcn-fuzzer/fuzz/.venv/lib/python3.12/site-packages')
    from wllvm.compilers import attachBitcodePathToObject
    for bitcode in source.rglob('.*.bc'):
        obj = bitcode.with_name(bitcode.name[1:-3])
        if not obj.is_file() or obj.suffix not in {'.o', '.lo', '.os'}:
            continue
        sections = subprocess.check_output(['readelf', '-SW', str(obj)], text=True, stderr=subprocess.DEVNULL)
        if '.llvm_bc' not in sections:
            attachBitcodePathToObject(str(bitcode), str(obj))
            repaired += 1
    if repaired:
        print(f'Repaired {repaired} missing wllvm object annotations', flush=True)
        done = [command for command in done if not re.search(r'\bmake\s|cmake --build', command)]
    for command in spec['build']['fuzz']:
        if '${SOURCE_PJSIP}' in command:
            continue
        if command.startswith(('python3 ', 'mkdir ', 'cp ${AFLNET}', 'cp -r ${AFLNET}')):
            continue
        command = command.replace('${AFLNET}/afl-clang-fast++', '/analysis-tools/wllvm++')
        command = command.replace('${AFLNET}/afl-clang-fast', '/analysis-tools/wllvm')
        command = command.replace('AFL_USE_ASAN=1 ', '').replace('-fsanitize=address', '')
        command = command.replace('CC=clang-14', 'CC=/analysis-tools/wllvm')
        # Assembly has no LLVM IR; select OpenSSL's equivalent C implementations.
        if name == 'openssl' and './Configure ' in command:
            command += ' no-asm'
        commands.append(command)
        if command in done:
            print('Reusing step:', command, flush=True)
            continue
        print('RUN:', command, flush=True)
        subprocess.run(['bash', '-c', 'set -euo pipefail\n' + command], env=env, check=True)
        done.append(command)
        steps_path.write_text(json.dumps(done, indent=2) + '\n')
    binary = Path(spec['runtime']['fuzz']['target'][0].replace('${FUZZ_SOURCE}', str(source)))
    if not binary.is_absolute():
        binary = Path(spec['runtime']['fuzz']['cwd']) / binary
    print('Extracting:', binary, flush=True)
    subprocess.run(['/analysis-tools/extract-bc', '-m', '-l', '/analysis-llvm/bin/llvm-link',
                    '-o', str(OUT / 'target.bc'), str(binary)], env=env, check=True)
    subprocess.run(['/analysis-llvm/bin/opt', '-passes=verify', '-disable-output', str(OUT / 'target.bc')], check=True)
    shutil.copy2(binary, OUT / 'target.native')
    (OUT / 'executed-build.json').write_text(json.dumps({
        'commands': commands, 'native_binary': str(binary),
        'scope': 'Linked target executable; external shared libraries/plugins and separately executed helpers are excluded.',
        'openssl_no_asm_for_ir': name == 'openssl',
    }, indent=2) + '\n')
finally:
    subprocess.run(['chown', '-R', f'{plan["host_uid"]}:{plan["host_gid"]}', str(OUT)], check=True)
