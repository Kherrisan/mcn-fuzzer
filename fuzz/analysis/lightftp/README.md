# LightFTP LLVM call graph

From the repository root:

```sh
fuzz/.venv/bin/python -m pip install wllvm==1.3.1
python3 fuzz/analysis/lightftp/build_callgraph.py
```

Requires wllvm (installed under `fuzz/.venv` by default), LLVM 18
(`clang-18`, `llvm-link-18`, `llvm-dis-18`, `opt-18`),
`pkg-config`, and GnuTLS development headers/libraries. Graphviz `dot` is optional;
when installed, the script also renders SVG files.

The default input is the patched source used by the LightFTP campaign, under
`fuzz/targets/lightftp/build/LightFTP/Source`. Override it with `--source-dir`.
The default output directory is `fuzz/targets/lightftp/build-callgraph-wllvm`;
override it with `--output`. Existing analysis outputs there are overwritten.

The script compiles the same four Linux translation units as the campaign
(`main.c`, `ftpserv.c`, `cfgparse.c`, `x_malloc.c`) with `wllvm -O0 -g`, then links
their object files into a native executable using wllvm. It runs
`extract-bc --linker llvm-link-18 --manifest` on that executable to obtain the
whole application module. It verifies the extracted module without running the SUT.
There is no AFL or LLVM coverage instrumentation in this analysis build.

Outputs:

- `lightftp.bc`: the combined application module.
- `lightftp.ll`: readable LLVM IR, with debug locations.
- `callgraph.dot`: original `opt-18 -passes=dot-callgraph` output.
- `callgraph.completed.dot`: the original graph plus labeled dashed edges for
  FTP command-table dispatch, pthread start functions, and cleanup handlers.
- `callgraph.application.dot`: application functions and edges only, including
  those supplemental edges; excludes external library nodes for readability.
- `callgraph.json`: functions, LLVM edges, supplemental edges with evidence, and
  indirect call sites. These records distinguish LLVM results from annotations.
- `manifest.json`: input source hashes, flags, tool versions, commands, and counts.
- `source/`, `ir/`, `fftp`: source snapshot, wllvm object files and hidden bitcode,
  and the native executable. The extract-bc manifest lists the linked bitcode files.

Corresponding `.svg` files are produced for the three DOT graphs when `dot`
is available.

The module contains the complete LightFTP Linux application, including its
static functions. GnuTLS, libc, and pthread implementation bodies are not linked
as bitcode, so calls into those libraries end at external declaration nodes.
The raw LLVM graph does not resolve the FTP function-pointer table or model
pthread callbacks. Supplemental edges are conservative candidates derived from
this source/IR; they do not prove runtime reachability or provide a general
points-to analysis. `-O0` preserves source-level functions, so this graph differs
from the campaign's optimized and instrumented executable.
