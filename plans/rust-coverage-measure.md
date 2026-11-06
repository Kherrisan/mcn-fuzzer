> **Superseded by [coverage-mcp-proposal.md](coverage-mcp-proposal.md).** Rust exploration graph, priority deltas, region measurement, and receipt selection were removed; the MCP service owns analysis state and emits proposals.

# 将 coverage measure 迁移到 Rust，并验证分析结果应用

## 1. 目标与问题背景

将 coverage measurement 的 Python 实现迁移到 fuzzer Rust 代码，保留 Rust 对 LLVM 工具的调用，验证真实 LightFTP campaign 能完成 regions 生成、agent 分析、内存模型更新和后续 trace selection。

已定位的失败原因：当前后台 Rust worker 启动的 measure Python 继承了 fuzzer 的 SUT network namespace。testcase 结束时，namespace 清理会终止不在常驻 PID 列表中的 measure 和它的 llvm-cov 子进程。同一个 snapshot 在宿主环境中测量成功，在 campaign namespace 中可复现 SIGTERM 终止和部分产物残留。现有错误信息没有记录退出码或信号，因而只留下 `coverage measure failed: `。

此计划补充 [Coverage Analysis Agent](coverage-analysis-agent.md) 的测量与验证部分。HC 用户交互增强已取消，不纳入此次改动。

## 2. 执行顺序与 LLVM 导出

统一采用以下流程：

```text
coverage replay 完成，回收 coverage SUT/forkserver
  → 等待 profraw 写入稳定
  → Rust 调用 llvm-profdata 合并
  → Rust 调用 llvm-cov export，输出 JSON
  → 原子发布冻结 snapshot
  → fuzzer 主线程同步执行 Rust measure，计算并冻结 handlers/regions
  → 派发后台 HC coverage agent，分析阻塞条件、依赖和优先级
  → fuzzer 主线程接收并应用结果
  → 后续 trace selector 使用更新后的探索图
```

- LLVM 导出直接复用现有 coverage replay 末尾的导出步骤，在主线程中同步完成。导出期间不恢复普通 testcase 执行和回收。
- 开启 `coverage.analysis_enabled` 时导出完整 JSON，并从同一份 JSON 提取覆盖率 summary，避免重复运行 summary 和 detailed export。
- 命令使用 `llvm-cov export <binary> -instr-profile=<profdata>`；LLVM 默认输出 JSON，不传入不支持的 `--format=json`。
- 未开启 analysis 时，沿用现有 `--source-coverage-json` 行为。
- snapshot 包含 `coverage.profdata.zlib`、`coverage.json` 和现有 metadata/checkpoint，成功发布后才向 analysis stage 暴露新的 snapshot 编号。
- 原始 profile 写入、合并和导出必须遵守现有 forkserver 回收顺序；不在 coverage forkserver 存活时替换 raw profile。
- Rust measure 放在 coverage analysis stage 的主线程中同步执行，只进行 Rust 计算和文件读写，不再启动 Python 或 LLVM 子进程。LLVM 导出和 measure 完成后才恢复普通变异；HC agent 的初始化、请求执行和结果等待继续在后台完成。

## 3. Rust measure 与结果应用

### 输入、校验和产物

- 新增内部 coverage measurement 模块，替代 Python measure、flow 和 regions。
- 读取冻结 snapshot 的 profdata 和 coverage JSON、启动时生成的 callgraph/CFG JSON、accepted handler 映射、protocol model 和 manifest。
- 校验 binary/source/graph 与 manifest 一致、handler 映射完整、coverage JSON 包含所需函数和分支结构。缺失必要输入或输入损坏时明确拒绝，不生成伪造的零覆盖报告。
- source digest 使用 Rust 实现，并复用于 coverage init 的源码校验，移除该后台初始化步骤对 Python 校验子进程的依赖。摘要规则与现有 manifest 工具保持一致。
- 保持 `handlers.json`、`regions.json` 的现有格式和 profile hash，采用临时文件加 rename 发布；保留现有 artifact seal，避免 agent 修改测量结果。
- snapshot 只读；测量前后确认冻结输入未改变。

### 计算语义

- 分别解析 LLVM branches 的 true/false 计数，区分 hit、miss、unknown；源码 code regions 不能直接当作 branch 计数。
- 按函数和源码位置匹配 CFG。只有 successor 覆盖能可靠消除歧义时才判断分支方向，不能按优化后的 successor 顺序猜测 true/false。
- 从 handler 沿静态可达 CFG 和 callgraph 确认的直接调用展开，调用连接限定到记录的 call site 和 callee entry。
- 用 petgraph 计算由已确认未命中分支和基本块组成的连通区域；命中或未知路径截断连接。
- 对递归和共享 callee 去重，保留函数数、分支方向数及位置。缺失 CFG、间接调用、外部函数和模糊映射记入 unknown。
- 保持 `complete`、`zero` 的含义：`zero=true` 且 `complete=false` 不能解释成已完全覆盖。

### 调度、日志和模型更新

- 继续维持一个在途任务和一个最新待处理 snapshot。初始化期间到来的 request 按现有策略跳过，不增加初始化 timeout 计数。
- 在主线程创建 analysis request 并执行 Rust measure；完成并冻结产物后，将 request、产物 seal 和提示词交给后台 worker。worker 不重复测量，只负责 HC 调用和结果接收、校验。准备失败时记录错误并跳过该 request，不向 agent 发送提示词。
- LLVM 工具失败时记录工具、snapshot、退出码或信号、stderr 和耗时；不得发布残缺 snapshot，也不得提前更新覆盖进度。
- 测量日志记录 request、snapshot、耗时、handler 数、region 数、未命中分支数、函数数和 unknown 数。
- 明确提示词中的 refined model 和 priority delta 格式，严格解析输出，避免把缺失或错误字段静默当成空更新。
- 在主线程校验结果身份、依赖有效性、环、step ID 和 delta；通过后整体应用到 synthesis 与 coverage 共用的 `ProtocolExplorationGraph`。失败不得留下部分模型更新，重复结果不得重复应用。
- 日志记录实际新增依赖、分数变化和更新前后 revision；后续 selector 必须从同一个共享图读取更新。
- “更新 fuzzer model”指更新运行中的依赖图和探索分数。原始 protocol JSON 保留为基础模型，agent 的 `protocol-refined.json` 保留为分析产物。

## 4. Python 和文档清理

- 删除被替代的 `coverage_analysis/measure.py`、`flow.py`、`regions.py`，以及仅用于 measure 的 Python 命令入口。
- 将覆盖计算相关测试迁到 Rust；混合测试文件只删除或迁移被替代的测量测试，保留仍有效的构建与图转换测试。
- 保留 Python 中仍用于构建 manifest、源码位置提取和启动时 DOT 转 JSON 的代码及测试。
- 更新 coverage task prompt、Rust prompt 测试和 README，删除调用 Python measure 的说明，说明测量文件由 fuzzer 内部 Rust 逻辑生成且不可编辑。
- 不新增配置表，继续使用当前 `[coverage]`、agent profile 和输出目录配置。

## 5. 测试与实际验收

### 自动验证

- 使用现有真实 LightFTP snapshot，在删除 Python 实现前保存基准结果；Rust 迁移后忽略集合顺序，对比 region 分组、分支数、函数数和 unknown 边界。
- Rust 测试覆盖 true/false 独立计数、方向反转、模糊映射、连续 miss 合并、hit/unknown 截断、递归和共享函数去重、外部和间接调用。
- 验证 snapshot 不被修改、无效输入被拒绝，以及重复或非法 agent 结果不会造成重复或部分模型更新。
- 验证 LLVM 导出发生在 replay 收尾之后，Rust measure 在主线程完成，二者完成前不恢复普通变异；验证后台 worker 不再执行 measurement，Rust measure 本身不启动子进程。
- 运行相关 Rust/Python 测试与 Release 构建。

### LightFTP 真实运行

- 若旧 campaign 仍占用同一工作目录，先停止；使用新的独立 synthesis/coverage 输出目录启动，沿用当前 1h timeout、agent 配置和 256 次停滞阈值。
- 确认至少一个真实 request 生成有效 `regions.json`，随后 agent 返回有效分析结果。
- 核对 fuzzer 接收结果后的真实依赖或 priority 变化，并从后续 selector 日志确认其使用了更新后的 revision 和数值。
- 单纯生成文件、返回 `status: ok` 或记录 `model_accepted` 不作为完整通过；合法空更新记为 no-op，继续观察后续请求，不伪造变化。
- 若链路失败，定位并修复后重新验证；链路通过后，让 campaign 按配置继续运行。
- 最终报告给出运行日志、成功 request、regions 和 refined model 的位置，以及模型应用和后续 selection 的具体证据。
