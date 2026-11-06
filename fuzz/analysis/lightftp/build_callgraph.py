#!/usr/bin/env python3
"""Build LightFTP with wllvm, extract its bitcode, and run LLVM dot-callgraph.

Uses the campaign's patched source tree by default. Dependencies such as libc
and GnuTLS remain external declarations. No SUT execution is performed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def run(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"command failed: {shlex.join(argv)}\n{result.stdout}{result.stderr}")
    return result.stdout


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    fuzz = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path,
                        default=fuzz / "targets/lightftp/build/LightFTP/Source")
    parser.add_argument("--output", type=Path,
                        default=fuzz / "targets/lightftp/build-callgraph-wllvm")
    parser.add_argument("--llvm-version", default="18")
    parser.add_argument("--wllvm", default=str(fuzz / ".venv/bin/wllvm"))
    parser.add_argument("--extract-bc", default=str(fuzz / ".venv/bin/extract-bc"))
    args = parser.parse_args()
    source = args.source_dir.resolve()
    output = args.output.resolve()
    if source == output or source in output.parents or output in source.parents:
        parser.error("source and output directories must not overlap")
    units = ["main.c", "ftpserv.c", "cfgparse.c", "x_malloc.c"]
    for name in units:
        if not (source / name).is_file():
            parser.error(f"missing translation unit: {source / name}")
    tools = {name: f"{name}-{args.llvm_version}"
             for name in ["clang", "llvm-link", "llvm-dis", "opt"]}
    for tool in [*tools.values(), "pkg-config", args.wllvm, args.extract_bc]:
        if not shutil.which(tool):
            parser.error(f"missing tool: {tool}")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "source"
    snapshot.mkdir(exist_ok=True)
    inputs = units + sorted(p.name for p in source.glob("*.h"))
    for name in inputs:
        shutil.copyfile(source / name, snapshot / name)
    ir = output / "ir"
    ir.mkdir(exist_ok=True)
    commands = []
    compiler_env = {
        "LLVM_COMPILER": "clang",
        "LLVM_CC_NAME": tools["clang"],
        "LLVM_CXX_NAME": f"clang++-{args.llvm_version}",
        "LLVM_LINK_NAME": tools["llvm-link"],
        "LLVM_AR_NAME": f"llvm-ar-{args.llvm_version}",
    }
    env = os.environ.copy()
    for key in ["WLLVM_CONFIGURE_ONLY", "LLVM_COMPILER_PATH", "LLVM_BITCODE_GENERATION_FLAGS",
                "LLVM_BITCODE_STORE", "LLVM_LINK_FLAGS"]:
        env.pop(key, None)
    env.update(compiler_env)

    def execute(argv, *, cwd=None):
        commands.append(argv)
        return run(argv, cwd=cwd, env=env)

    cflags = shlex.split(run(["pkg-config", "--cflags", "gnutls"]))
    libs = shlex.split(run(["pkg-config", "--libs", "gnutls"]))
    flags = ["-std=c99", "-O0", "-g", "-fno-discard-value-names", "-fno-builtin",
             "-I", str(snapshot), *cflags]
    objects = []
    for name in units:
        obj = ir / Path(name).with_suffix(".o")
        execute([args.wllvm, *flags, "-c", str(snapshot / name), "-o", str(obj)])
        objects.append(str(obj))
    executable = output / "fftp"
    execute([args.wllvm, *objects, "-o", str(executable), "-lpthread", *libs])
    bitcode = output / "lightftp.bc"
    execute([args.extract_bc, "--linker", tools["llvm-link"], "--manifest",
             "--output", str(bitcode), str(executable)])
    extracted_modules = [line.strip() for line in
                         executable.with_suffix(".llvm.manifest").read_text().splitlines()
                         if line.strip()]
    expected_modules = {str(ir / f".{Path(name).stem}.o.bc") for name in units}
    if set(extracted_modules) != expected_modules or not all(Path(p).is_file() for p in extracted_modules):
        raise RuntimeError(f"extract-bc did not include exactly the four translation units: {extracted_modules}")
    execute([tools["opt"], "-passes=verify", "-disable-output", str(bitcode)])
    execute([tools["llvm-dis"], str(bitcode), "-o", str(output / "lightftp.ll")])
    execute([tools["opt"], "-passes=dot-callgraph", "-disable-output",
             "-callgraph-dot-filename-prefix=lightftp", str(bitcode)], cwd=output)
    raw = output / "lightftp.callgraph.dot"
    raw.replace(output / "callgraph.dot")
    dot = (output / "callgraph.dot").read_text()
    ll = (output / "lightftp.ll").read_text()
    names = dict(re.findall(r'(Node\w+)\s*\[.*?label="\{([^"{}]*)\}"', dot))
    by_name = {name: node for node, name in names.items()}
    direct_edges = {(names[a], names[b]) for a, b in re.findall(r'(Node\w+)\s*->\s*(Node\w+)', dot)
                    if a in names and b in names}
    definitions = set(re.findall(r'^define\b[^\n]*?@([\w.$]+)\(', ll, re.M))
    # Preserve unknown indirect calls in JSON. LLVM's basic call graph does not
    # perform points-to analysis or model library callbacks.
    current = None
    indirect = []
    callbacks = []
    cleanup_callbacks = []
    for line in ll.splitlines():
        match = re.match(r'^define\b[^\n]*?@([\w.$]+)\(', line)
        if match:
            current = match[1]
        if re.search(r'\bcall\b.*?%[\w.]+\(', line):
            indirect.append({"caller": current, "ir": line.strip()})
        if "call" in line and "@pthread_create(" in line:
            targets = re.findall(r'@([\w.$]+)', line)
            callbacks.extend({"caller": current, "callee": target,
                              "kind": "pthread_start", "evidence": line.strip()}
                             for target in targets if target in definitions)
        cleanup = re.search(r'store ptr @([\w.$]+), ptr %__cancel_routine\b', line)
        if cleanup and cleanup[1] in definitions:
            cleanup_callbacks.append({"caller": current, "callee": cleanup[1],
                                      "kind": "pthread_cleanup_callback",
                                      "evidence": line.strip()})
    ftp_source = (snapshot / "ftpserv.c").read_text()
    table = re.search(r'ftpprocs\[MAX_CMDS\]\s*=\s*\{(.*?)\};', ftp_source, re.S)
    dispatch = []
    if table:
        for target in re.findall(r'\bftp\w+\b', table[1]):
            if target not in definitions:
                raise RuntimeError(f"dispatch target missing from module: {target}")
            dispatch.append({"caller": "ftp_client_thread", "callee": target,
                             "kind": "command_dispatch_candidate", "evidence": "ftpprocs[MAX_CMDS]"})
    supplemental = callbacks + cleanup_callbacks + dispatch
    completed = dot.rstrip().removesuffix("}")
    for edge in supplemental:
        caller, callee = edge["caller"], edge["callee"]
        if caller not in by_name or callee not in by_name:
            raise RuntimeError(f"missing DOT node for supplemental edge: {caller} -> {callee}")
        completed += (f'\n\t{by_name[caller]} -> {by_name[callee]} '
                      f'[style=dashed,color=blue,label="{edge["kind"]}"];')
    completed += "\n}\n"
    (output / "callgraph.completed.dot").write_text(completed)
    application = ['digraph LightFTP {', '  rankdir=LR;', '  node [shape=box];']
    application.extend(f'  {json.dumps(name)};' for name in sorted(definitions))
    application.extend(f'  {json.dumps(a)} -> {json.dumps(b)};'
                       for a, b in sorted(direct_edges) if a in definitions and b in definitions)
    for edge in supplemental:
        application.append(f'  {json.dumps(edge["caller"])} -> {json.dumps(edge["callee"])} '
                           f'[style=dashed,color=blue,label="{edge["kind"]}"];')
    application.append('}')
    (output / "callgraph.application.dot").write_text('\n'.join(application) + '\n')
    write_json(output / "callgraph.json", {
        "defined_functions": sorted(definitions),
        "external_functions": sorted(set(names.values()) - definitions),
        "llvm_edges": [{"caller": a, "callee": b} for a, b in sorted(direct_edges)],
        "supplemental_edges": supplemental,
        "indirect_call_sites": indirect,
    })
    if shutil.which("dot"):
        for stem in ["callgraph", "callgraph.completed", "callgraph.application"]:
            execute(["dot", "-Tsvg", str(output / f"{stem}.dot"), "-o", str(output / f"{stem}.svg")])
    manifest = {
        "pipeline": "wllvm -> native executable -> extract-bc -> opt dot-callgraph",
        "wllvm": args.wllvm, "extract_bc": args.extract_bc, "compiler_env": compiler_env,
        "extracted_bitcode_inputs": extracted_modules,
        "source_dir": str(source), "output_dir": str(output), "translation_units": units,
        "source_sha256": {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in inputs},
        "tools": {name: run([path, "--version"]).splitlines()[0] for name, path in tools.items()},
        "flags": flags, "commands": commands,
        "defined_functions": len(definitions), "external_functions": len(set(names.values()) - definitions),
        "llvm_edges": len(direct_edges), "supplemental_edges": len(supplemental),
        "indirect_call_sites": len(indirect),
        "scope": "All four Linux LightFTP translation units; shared-library bodies and AFL runtime excluded.",
        "notes": ["O0 retains application functions without inlining.",
                  "callgraph.dot is the original LLVM dot-callgraph output.",
                  "Dashed edges in completed DOT are source/IR-derived candidate dispatch, pthread start, and pthread cleanup callback edges, not LLVM points-to results.",
                  "Neither graph guarantees runtime reachability or resolves arbitrary library callbacks."],
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({key: manifest[key] for key in ["output_dir", "defined_functions", "external_functions", "llvm_edges", "supplemental_edges", "indirect_call_sites"]}, indent=2))


if __name__ == "__main__":
    main()
