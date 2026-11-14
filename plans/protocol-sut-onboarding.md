# 协议被测程序接入步骤

把一个网络协议服务端接进 `mcn-fuzzer`，需要同时准备**可执行目标、生成器、覆盖率和图、campaign 配置**四部分。本文按执行顺序列出步骤，并以 OpenSSL `s_server` 为例给出具体命令。命令默认在 `fuzz/` 目录下执行。

整体流程：

```text
固定源码 ──> 构建三个变体 ──> 定义运行契约 ──> generator + seeds
                                              │
                     图与 manifest ──> campaign.toml ──> 冒烟验证 ──> 正式运行
```

## 0. 目录约定

```text
fuzz/
├── .deps/<name>/                 原始源码、tarball（gitignored）
├── targets/<name>/
│   ├── build-pcguard-asan/       fuzz 目标（PCGUARD + ASAN）
│   ├── build-llvm-cov/           覆盖率目标（llvm-cov，不带 ASAN）
│   ├── build-graphs/             callgraph/CFG 专用构建（无插桩）
│   ├── fixture/                  证书等运行期素材
│   ├── seeds/                    generator 输入
│   ├── generators/               Python generator
│   ├── cleanup.sh                每个 case 前重置 workdir
│   ├── campaign.toml             正式配置
│   └── campaign-asan-smoke.toml  短时间冒烟配置
└── runs/                         运行输出（通常由 `-o` 指定）
```

`targets/` 默认被仓库忽略；需要版本化的文件（如 `cleanup.sh`）单独 force-add。

## 1. 固定源码版本

1. 选一个确定的 release，下载官方 tarball。
2. 把 SHA256 写进构建脚本，构建前校验。
3. 解压出的目录保持 **pristine**：不 configure、不打 patch、不编译。

```text
.deps/<name>/<name>-<version>.tar.gz       # 固定输入
.deps/<name>/<name>-<version>/             # pristine 副本
.deps/<name>/<name>-<version>-<variant>/   # 从 pristine 复制出的变体
```

不要把构建产物或 patch 直接写进 pristine；需要裁剪时复制一份再改。

## 2. 构建三个变体

同一版本、同一 Configure 选项，构建三次：

| 变体 | 用途 | 关键开关 |
|---|---|---|
| `build-pcguard-asan` | fuzz 目标，捕获 ASAN crash | `AFL_LLVM_INSTRUMENT=PCGUARD`、`AFL_USE_ASAN=1`、`-fsanitize=address` |
| `build-llvm-cov` | coverage replay，不能带 ASAN | `AFL_LLVM_ONLY_FSRV=1`、`-fprofile-instr-generate -fcoverage-mapping` |
| `build-graphs` | 导出 callgraph/CFG | `clang -flto` + 同样的 profile 插桩 flags，无 AFL/ASAN |

以 OpenSSL 4.0.2 为例：

```sh
tar -xzf .deps/openssl/openssl-4.0.2.tar.gz -C .deps/openssl
# 每个脚本内部：校验 tarball → 复制/确认源码 → Configure → make
bash targets/openssl/build-pcguard-asan.sh
bash targets/openssl/build-coverage.sh
bash targets/openssl/build-graphs.sh
```

### 2.1 如果 CLI 包含大量无关子命令

协议服务端通常只需要一个子命令（例如 `s_server`），其余 CLI 只会把 callgraph/CFG 撑大。做法：

1. 复制 pristine 到 `<name>-<variant>`。
2. 修改 `apps/build.info` 的源文件列表，只保留目标子命令及其 support 文件。
3. 删除命令表中引用已移除文件的条目（如 `progs.pl` 的 digest/cipher 列表）。
4. 删除主程序中指向已移除命令的回退分支（例如 `openssl.c` 的 `EVP_MD_fetch` 回退）。
5. 把需要的共享符号（常量、从其他命令文件抽出的函数）放进一个新的 support `.c`。
6. 把上述改动导出成 patch，构建时从 pristine 复制后再 apply。

OpenSSL 4.0.2 的实例：

```text
targets/openssl/s_server_prune-4.0.2.patch
targets/openssl/rebuild-4.0.2.sh
```

注意 Configure 会读取 `apps/build.info`。因此顺序必须是：

```text
Configure → apply patch → make（首次重生成 configdata.pm 并退出）→ make
```

或者更简单：先 apply patch，再 Configure，再 make。两种都可以，但**不要**在 Configure 之后只 make 一次。

### 2.2 构建后自检

```sh
"$BIN" s_server -help          # 被裁剪时只剩 help/s_server
nm -a "$BIN" | grep __asan_init          # ASAN 变体
nm -a "$BIN" | grep __llvm_profile      # coverage 变体
AFL_DUMP_MAP_SIZE=1 "$BIN" s_server -help   # 必须 <= campaign target.map_size
```

ASAN 下 `s_server` 要能正常握手（用系统 `openssl s_client` 验证一次），避免 sanitizer 误报或启动过慢。

## 3. 定义 SUT 运行契约

campaign 的 `[target]` 要描述“怎么启动一个监听 socket 的进程”：

```toml
[target]
binary = "build-pcguard-asan/tree/apps/openssl"
source_dir = "../../.deps/openssl/openssl-4.0.2"
workdir = "work"
cleanup_script = "cleanup.sh"
host = "127.0.0.1"
port = 0
map_size = 1048576
args = [
  "s_server",
  "-accept", "127.0.0.1:@PORT@",
  "-cert", "certs/server.crt",
  "-key", "certs/server.key",
  "-tls1_3",
  "-www",
  "-naccept", "1",
  "-no_cache",
  "-no_ticket",
]
```

要点：

- `@PORT@` 由 fuzzer 选择端口后替换；`@ROOT@` 可指向 workdir。
- 每个 case 一个连接；服务端必须处理完即退出，否则会影响进程回收。
- 不要加 readiness 探测连接；`-naccept 1` 会把探测当成唯一 case。
- `cleanup.sh` 在每个 case 前运行，负责把证书、配置等复制进 workdir。它拿到 `MCN_FUZZ_WORKDIR`、`MCN_FUZZ_HOST`、`MCN_FUZZ_PORT`。
- `workdir` 是输入路径，相对 TOML 解析。并发跑多个 campaign 时用不同 workdir，避免互相覆盖。
- `map_size` 至少覆盖 PCGUARD 实际 map；先构建后测，再写进 campaign。

## 4. generator 与 seeds

generator 是 Python 文件，用 `@step_handler("step name")` 注册每个 step：

```python
from generator_server import InteractionError, step_handler

@step_handler("TLS connect")
def connect(ctx, params):
    ctx.connect(ctx.host, ctx.port, proto="tcp")

@step_handler("ClientHello send")
def send(ctx, params):
    ctx.send(build_hello(params), channel="...")
```

seed 是一个 JSON，描述 generator、step trace 和参数：

```json
{
  "generator": "../generators/tls12.py",
  "step_trace": [
    {"name": "send_1", "step": "send_client_hello", "action": "send", "channel": "control"}
  ],
  "params": {"version": 771, "cipher": 4865}
}
```

要求：

- 至少有一个**真正能完成协议握手**的 seed；否则初始 corpus 全是 alert/timeout，要等 bitmap stagnation 才触发 synthesis。
- seed 的协议版本必须与 target 参数一致（例如 target 是 `-tls1_3`，就不要只放 TLS 1.2 ClientHello）。
- generator 里的长度字段要自己重算，不能依赖 fuzzer 修正。
- 用 `replay` 单独验证 seed：

  ```sh
  sudo ./target/release/mcn-fuzz replay \
    --campaign targets/openssl/campaign.toml \
    --input targets/openssl/seeds/tls13.json
  ```

  期望输出 `generator ok`，而不是 `alert`/`timeout`。

## 5. 图与 manifest

coverage analysis 需要三份数据：

```text
callgraph.dot
cfg/*.dot
```

### 5.1 导出

```sh
# linked bitcode
llvm-link-18 $(find build-graphs/tree -name '*.o') -o build-graphs/tree/target.bc

# callgraph
opt-18 -passes=dot-callgraph -disable-output build-graphs/tree/target.bc

# per-function CFG：按 object 并行，比整模块快约 100 倍
```

`dot-cfg-only` 在单个 linked module 上极慢（约 1 函数/秒）；应拆到每个 bitcode object 上并行导出，再合并到 `cfg/`。

### 5.2 manifest

用 `python/coverage_analysis/manifest.py` 的 `write_manifest` 生成：

```python
write_manifest(
    build_llvm_cov / "tree/apps/openssl",
    pristine_source,
    graph_dir,
    ["callgraph.dot"] + [...cfg/*.dot],
)
```

campaign 里对应：

```toml
callgraph_dot = "build-graphs/graphs/callgraph.dot"
cfg_dir = "build-graphs/graphs/cfg"
```

### 5.3 校验

- manifest 的 `binary_sha256` 必须等于 coverage 二进制。
- `source_sha256` 是分析所用源码树。
- `graphs_sha256` 覆盖 callgraph + cfg（+ source-map，如有）。
- 用 `coverage_analysis.state.State.load_cfg/load_callgraph` 实际加载一次，确认解析和 hash 校验都通过。

## 6. campaign TOML

最小结构：

```toml
[target]        # 可执行、workdir、args、map_size
[timeouts]      # connect / io / case / exit grace
[campaign]      # seeds, corpus, crashes, python, pythonpath, timeout_secs
[capture]       # 可选 pcap
[coverage]      # enabled, binary, output, analysis_enabled, agent profile, graphs
[generator_synthesis]  # enabled, model, output, profile
[parties]       # sut / generator 对模型 party 的映射
[agents.profiles.<name>]  # provider/model/effort/env
[agents]        # hc_executable
```

路径语义：

- **输入**（seeds、binary、workdir、cleanup、model、source_dir、coverage binary、图）相对 campaign TOML。
- **输出**（corpus、crashes、coverage.output、synthesis.output）相对 CLI `-o` 创建的 campaign root；不传 `-o` 用当前目录。
- root 名为 `mcn-fuzzer-YYYYMMDD-HHMM`，同分钟冲突加 `-2`。

运行：

```sh
sudo ./target/release/mcn-fuzz run \
  --campaign targets/openssl/campaign.toml \
  -o targets/openssl/runs
```

## 7. 启动前检查清单

```text
[ ] tarball SHA256 已固定
[ ] 三个变体都构建成功，版本一致
[ ] ASAN 目标能正常握手
[ ] coverage 二进制不带 ASAN
[ ] map_size >= AFL_DUMP_MAP_SIZE
[ ] callgraph/cfg/manifest 三者匹配，State.load_* 通过
[ ] 至少一个 seed replay 为 generator ok
[ ] 所有 TOML 路径存在，parties/profile/model 对得上
[ ] cleanup.sh 能独立跑一次
```

建议的冒烟顺序：

```sh
# 1. 单 case
mcn-fuzz replay --campaign ... --input seeds/<valid>

# 2. 覆盖率单批
mcn-fuzz coverage -o /tmp/cov --campaign ... --corpus <corpus> --step 1

# 3. 短 campaign
mcn-fuzz run -o /tmp/smoke --campaign targets/openssl/campaign-asan-smoke.toml --iterations 2
```

## 8. 正式运行与产物

```sh
sudo ./target/release/mcn-fuzz run -o runs --campaign targets/openssl/campaign.toml
```

每个 campaign root 包含：

```text
cases.jsonl                 每个 case 的状态/耗时/edges
corpus/  crashes/          输入与 objective
synthesis/                 requests、published generator、candidates
source-coverage/           snapshots、analysis/
generator.log               Python generator 输出
asan/asanlog.<pid>         ASAN 报告（如果触发）
```

跑完后用仓库里的 campaign summary 脚本汇总：

```sh
python3 .agents/skills/mcn-campaign-summary/scripts/summarize_campaign.py <campaign-root>
```

## 9. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `OPENSSL_API_COMPAT expresses an impossible API compatibility level` | out-of-tree 构建没先生成头文件，编译器用了系统 OpenSSL | 先 `make build_generated` |
| patch apply 后行为没变 | configure 之后才 patch，且只 make 了一次 | patch 后重跑 Configure，或 make 两次 |
| `map size` 握手失败 | `target.map_size` 小于实际 | 用 `AFL_DUMP_MAP_SIZE=1` 测，改 TOML |
| ASAN 目标不产生 crash | ASAN 没有 abort，或 `ASAN_OPTIONS` 被覆盖 | 保留 `abort_on_error=1`，检查 `asan/` 日志 |
| 初始 seed 全是 alert | seed 协议版本与 target 参数不一致 | 加一个匹配版本的 seed，先 replay 验证 |
| coverage analysis 找不到 callgraph | manifest/graph 路径旧或 hash 不匹配 | 重新生成 manifest，State.load_* 验证 |
| alias 大量 unmatched | 图与二进制版本/构建 scope 不一致 | 用同一版本、同一 prune 源码重新导出 |
| 并发 campaign 互相干扰 | `workdir` 共用 | 每个 campaign 用独立 workdir |

## 10. 参考

- `plans/protocol-coverage-synthesis-mutation-flow.md`：模型、coverage、synthesis、mutation 全流程
- `plans/coverage-mcp-proposal.md`：coverage MCP 工具与数据语义
- `plans/openssl-server-fuzzing.md`：早期 OpenSSL 3.5.9 接入记录（历史）
- `fuzz/README.md`：CLI、campaign 字段和 OpenSSL 构建说明
