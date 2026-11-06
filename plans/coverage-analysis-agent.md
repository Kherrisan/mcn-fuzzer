> **Superseded by [coverage-mcp-proposal.md](coverage-mcp-proposal.md).** Rust exploration graph, priority deltas, region measurement, and receipt selection were removed; the MCP service owns analysis state and emits proposals.

# Coverage Analysis Agent

## 1. 目标与配置归属

Coverage analysis agent 读取 SUT 源码、LLVM coverage 快照和同构建的 callgraph/CFG，提取消息 handler，计算连续未命中区域并分析前缀消息与阻塞条件。它输出 handler 映射、`protocol-refined.json` 和 step 优先级 delta，供后续 synthesis 使用。

全部 coverage 采集与分析配置集中在 `[coverage]`，不新增独立分析配置表。仓库当前实际使用 `[source_coverage]`；实现时将配置读取、campaign 模板及相关文档统一迁移到 `[coverage]`。旧表名给出明确的迁移错误，避免被忽略后静默关闭覆盖率采集。

示例配置（路径均相对 campaign TOML）：

```toml
[coverage]
enabled = true
binary = "build-coverage/fftp"
output = "source-coverage"
llvm_profdata = "llvm-profdata-18"
llvm_cov = "llvm-cov-18"
analysis_enabled = true
analysis_profile = "coverage-analyzer"
analysis_init_timeout_secs = 120
analysis_task_timeout_secs = 600
callgraph_dot = "build-coverage/callgraph.dot"
cfg_dir = "build-coverage/cfg"
build_manifest = "build-coverage/manifest.json"

[agents]
hc_executable = "hc"

[agents.profiles.coverage-analyzer]
provider = "codex"
executable = "codex"
model = "<model>"
```

- `enabled` 控制源码覆盖采集，`analysis_enabled` 单独控制分析；分析默认关闭，开启时必须同时启用采集。
- 使用已有 `target.source_dir` 提供源码目录，使用已有 protocol model 和 party 绑定；analysis profile 引用现有 `[agents.profiles.*]`，可以与 synthesis 选择不同模型。
- 缓存与分析产物默认放在 `coverage.output/<构建指纹>/analysis/`，不重复配置输出根目录。
- HC 可执行文件配置从 `protocol_generation.hc_executable` 提升到共享的 `[agents].hc_executable`，默认仍为 `hc`；同时迁移受仓库管理的配置，旧字段给出迁移提示。
- 分析不依赖 `protocol_generation.enabled`：只启用分析时仍能启动 HC、生成报告和持久化结果。模型路径和 party 绑定仍须配置，供分析和 step ID 关联使用。

## 2. 同构建图数据和启动转换

- 扩展现有 LightFTP wllvm 构建工具，使用覆盖率构建相同的源码、补丁、编译器、宏和参数，在同一次构建中产出 forkserver-only coverage binary、完整应用 bitcode、callgraph DOT、函数 CFG DOT 和 manifest。
- manifest 仅保存四个实际用于读取和校验的字段：`version`（格式版本）、`binary_sha256`（匹配 coverage binary）、`source_sha256`（匹配分析源码并使 handler 缓存失效）、`graphs_sha256`（callgraph/CFG 产物集合的摘要，用于图产物完整性校验）。图集合按相对路径排序后计算统一摘要，无需逐文件记录冗长清单。路径由 TOML 提供，不在 manifest 重复保存；不记录工具版本、编译命令、参数、环境、时间戳或构建过程信息。图生成在离线构建步骤执行；Rust fuzzer 启动时校验并转换已有 DOT 产物。
- Rust fuzzer 启动时调用 campaign Python 和 NetworkX，将 callgraph DOT 转为 `analysis/graphs/callgraph.json`，将全部函数 CFG DOT 合并为独立的 `analysis/graphs/cfg.json`。Rust 读取、校验并冻结这两个 JSON，后续 Rust 和 coverage analysis Python/agent 都只读取 JSON；测量时不再解析原始 DOT。
- 从 DOT 读取函数符号、调用边和 CFG 拓扑；源码位置由同构建 bitcode 提取的 source map 补充。直接调用、补充的间接调用候选及外部函数分别标记，保留补充边依据。
- CFG 工具保留基本块、调用点、分支后继和源码位置，以关联 `llvm-cov` 分支计数。同构建也可能出现优化造成的一对多或缺失映射；这些情况标记为未知，不按零命中计数。
- 缺少必要输入、依赖或构建不匹配时，在初始化阶段明确报错；运行中某个快照分析失败则记录诊断并继续 fuzzing。

## 3. HC 状态管理与生命周期

Coverage analysis agent 通过现有 `HcClient` 管理，使用 campaign 私有 HC core。将当前仅供 generation 使用的 HC 启动请求扩展为 campaign agent runtime：启用 synthesis 或 coverage analysis 任意一项即启动 core，两项都开启时共享同一个 core/session。

- 使用独立的 `coverage-analysis` terminal、工作目录、profile 和后台 I/O worker；synthesis 继续使用自己的 terminal。两个 worker 分别建立 HC 连接，互不阻塞。
- 复用现有 namespace 启动顺序：core 在进入 SUT 网络 namespace 前启动；agent 及其工具留在宿主网络和 campaign PID namespace，避免被 testcase 的 SUT 进程清理误杀。
- 初始化在后台进行，发送专用 initialization prompt，要求原子发布包含 campaign session 的 `ready.json`。有 coverage 快照但尚未 ready 时保留最新待分析快照，普通 fuzzing 继续。
- 状态包含 `initializing`、`ready`、`busy`、`paused`、`exited`，保存 HC terminal 原始 state/status/error 和状态变化原因。worker 用有界 `terminal.get` 观察存活状态；任务完成以关联的产物为准，不单靠终端文本或 HC idle 状态判断。
- 每个请求冻结输入版本，使用独立目录和 request ID。agent 完成所有输出后最后原子发布 `result.json`，包含 session、request ID、构建指纹、coverage 快照、输入模型版本、成功/失败状态和诊断信息。
- 通过 HC 的 send/get/read/interrupt/kill 管理派发、观察、诊断和终止。初始化超时、任务超时、连接丢失或异常退出时保存原因和经脱敏的 terminal 信息，暂停该 agent 的新派发；不自动重启或盲目重发。
- 单个 worker 结束时只关闭自己的 terminal。将现有 synthesis worker 中关闭整个 session/core 的职责移交 campaign runtime；campaign 退出时统一停止 worker、关闭 terminal 并回收私有 core，PID namespace 退出提供最终回收保证。

## 4. Handler、覆盖区域与依赖分析

### Handler 映射

初始化任务由 coverage agent 阅读源码和 protocol model，根据实际 dispatch 和调用关系提取 handler，建立 `message_name → handler 函数集合`，保存为 `init/handlers.json`，在完整映射通过校验后才接受 `ready.json`，冻结为 `handlers.accepted.json` 供所有快照复用。工具不按函数名推断 handler，也不在快照测量时重新提取。每项记录关联 step ID、函数标识、源码位置、dispatch 依据及不确定性。支持一个消息对应多个 handler，以及多个消息共享同一 handler；未找到映射时记录原因，不将其解释为协议消息不合法。

映射随源码/构建和基础模型版本保存，后续快照复用；源码或模型不匹配时重新提取。

### 连续未命中区域

- agent 调用确定性的分析工具计算图与计数，不依靠语言模型估算规模。工具读取已发布的不可变 coverage 快照；可从其中的 profdata 调用 `llvm-cov export`，生成紧凑的函数、分支与命中索引，不要求打开全量 coverage JSON 的 CLI 开关。
- 在每个 handler 下游可达的 CFG 中寻找零命中分支边的最大连通块：非分支路径连接相邻分支，已执行分支边作为边界，零命中调用点可连接到已解析的 callee 入口。连通块使用忽略方向后的连通性分组，可达性仍按有向控制流计算。
- 分支数量按不同的分支方向计数，函数数量按函数标识去重；输出每个区域的两项计数、分支位置、关联函数和 handler 内去重后的汇总。循环和递归通过 visited 集合避免重复计数，不枚举无限路径。
- 外部库、未解析间接调用和不明确的 coverage/CFG 映射列为未知边界。共享区域可以出现在多个 handler 报告中，不能将各 handler 的计数直接相加作为全程序规模。

### 阻塞条件与模型更新

- agent 结合源码、区域入口和现有模型分析触发 handler 的条件，包括必要前缀消息序列、认证/会话状态、参数或字段约束，以及无法仅靠消息满足的环境条件，并保存源码依据。
- 输出有序的前缀 step 序列；替代序列分别记录，条件性依赖不得直接变成所有 trace 必须满足的全局依赖。不确定条件保留为说明，不自动作为强制前缀。
- `protocol-refined.json` 保持现有 protocol schema：保留原始 step 定义及其 ID，新增必要的 dependency 边，并在 dependency 的 `condition` 中补充可供 generator 使用的文字说明。原始 protocol model 保持为基础输入，报告单独保存源码证据和分析元数据。
- refined model 的内容摘要生成新的 protocol ID；仅更新 dependency 时既有 step ID 保持稳定。迁移模型输出路径时正确解析 RFC/document 路径，避免相对引用失效。

## 5. 异步调度、结果发布与探索分数

每次 source coverage 发布新快照后提交分析，包含初始 seed replay 及正常退出前生成的新快照；没有新快照不重复派发。分析期间普通变异和现有 synthesis 调度继续运行。

- 每次最多一个 analysis request 在途，另外只保留一个最新待分析快照；新快照覆盖待派发项，不中断当前分析。分析任务不占用 synthesis 的在途标记，也不重置 bitmap stagnation 计数。
- 后台 worker 只做 HC I/O、工具操作和产物准备，不修改 LibAFL state。fuzzer 在主线程 case/stage 边界接收结果、校验并更新状态；synthesis 下一次选择 trace 时使用最新已接受版本。
- 结果须匹配 session、request、构建、快照和输入模型版本，完成 schema、step 引用、文件及新增前缀依赖检查后才发布。拒绝新增强制依赖形成的环；原模型中的自依赖或重复交互不能直接当作必须递归补齐的前缀。
- 同一结果的 refined model 和 priority delta 作为一组接受；按 request/snapshot 记录应用账本，防止重复加分或重启后再次应用。快照顺序只向前推进；失败、过期或不匹配的结果保留诊断并继续使用最近有效版本。
- agent 读取当前探索分数和上一轮报告，输出 `priority-delta.json` 中的 `step_id` 与有限浮点 `delta`，附简短理由。Rust 按现有 8 位 step ID 更新 `score = max(0, score + delta)`；初始分数为 0，抽样权重为 `1 + score`，低分 step 仍有被选中的机会。
- Rust 在常驻 petgraph 的 step 节点上维护分数，持久化节点分数、依赖边、已应用结果标识和有效模型版本；恢复时校验基础模型与构建，避免将旧 campaign 状态套到其他目标。
- selector 按下述“优先级采样 → 最少补点连接 → 最高均值分量”流程生成有序 receipt；使用已有 trace 去重和有界采样限制。
- 已派发的 synthesis request 保留自己的 receipt 和不可变模型快照；新请求才使用更新模型和分数。生成 agent 可以读取 receipt 中的 refined dependency 条件文字，同时继续遵守禁止阅读 SUT 源码的要求；不向它提供源码目录、handler 表或完整 coverage 报告。
- 退出时不等待新分析长期完成；通过 HC 停止在途任务、标记未完成状态并统一回收 runtime。已发布快照保留，后续启动可以继续分析尚未完成的最新快照。

### Rust 常驻 step dependency 图

Rust fuzzer 持续维护 `ProtocolExplorationGraph`，内部使用 `petgraph::stable_graph::StableDiGraph<StepNode, StepDependency>`。它是整个 campaign 内 step 探索状态和依赖关系的运行时图，selector 直接查询这张图；不在每次采样时重新从 JSON 构建依赖图。Python NetworkX 负责源码 callgraph/CFG 的解析和覆盖分析，Rust petgraph 负责 protocol step dependency，两者节点含义不同。

- **节点：** 每个 protocol step 对应一个节点，保存 8 位 step ID、step 定义/模型顺序和当前 priority score。原始模型全部 step 都进入图；party 绑定在查询时筛选 eligible 视图，不从原图删除不可选节点。维护 `StepId → NodeIndex` 和 `step name → NodeIndex` 索引，分别支持 delta 应用与模型依赖引用解析。
- **边：** 每条 step dependency 对应 `prior_step → dependent_step` 的有向边，保存 condition 和 field relations。精确重复的依赖去重；同一对 step 上条件或字段关系不同的依赖保留为不同边，不能覆盖丢失信息。连通性计算使用该图去掉自环后的无向视图，排序仍使用有向依赖。
- **初始化：** 加载基础模型时创建节点和边；有有效 checkpoint 时恢复最近接受的 refined model、priority 和应用账本。普通变异持续期间图常驻内存，即使暂时没有 synthesis request 也不销毁。
- **模型更新：** analysis 结果校验后，以当前图版本为基础准备新增/变更依赖及 priority 更新，在 fuzzer 主线程的 case/stage 边界统一提交。节点定义不变时保留节点和既有分数，只更新相关边与分数；任何校验失败都不改变有效图。更新模型、图、priority、结果账本和版本引用作为同一次状态提交，对下一次 selector 调用同时可见。
- **分数更新：** `priority-delta.json` 通过 step ID 定位到节点并更新 node weight 中的 score；该节点分数是采样、分量均值和 priority 日志的唯一运行时来源，避免另外维护一份可能失同步的分数表。权重按 `1 + score` 查询时计算。
- **版本与索引：** 每次接受状态更新产生新的 exploration state revision，并分别标明模型版本和 priority 版本。一次 selector 调用固定读取一个 revision；在途 synthesis request 保留其 receipt/模型版本。`NodeIndex` 只在当前进程的图内部使用，日志、agent 输出和持久化关联使用 step ID；恢复时重建索引，不依赖数值索引保持不变。
- **持久化与日志：** checkpoint 保存版本化的 step/score、dependency 和应用账本，恢复时重建 petgraph 及索引。记录图初始化/恢复、每次节点或边的变化、priority 变化、更新前后节点/边数量和 revision；selector 日志携带所读 revision，便于核对图状态与分量结果。

### Trace selector：优先级采样与最少补点

当前代码中的 selector 仍为均匀随机采样、按模型顺序排列；以下为本计划要实现的新规则。

1. **按 step 优先级采样。** 在 party 绑定允许的 eligible step 中，按 `1 + score` 加权无放回采样，得到目标 step 集合。采样数量沿用 `trace_len_min/trace_len_max` 范围，桥接 step 也必须来自 eligible 集合。
2. **补最少的连接 step。** 直接使用常驻 petgraph 中当前 revision 的 eligible 视图；连通性使用去掉自环的无向投影，即弱连通，不要求所有 step 两两直接相邻。保持已采样节点，寻找能使其连通、且新增不同 step 数最少的补点集合；多个连接路径共享的中间 step 只计一次。最终总长度不能超过 `trace_len_max`。
3. **不能完全连接时尽量合并。** 原图中不存在路径，或长度预算不足以连接所有已采样分量时，先在预算内尽可能减少分量数，再以新增 step 数最少作为次级目标。不能只因它们在原图同一个分量里，就把缺少中间节点的已选集合视为连通。
4. **保留平均优先级最高的分量。** 在补点后的已选 step 诱导子图上重新计算连通分量。若仍有多个分量，计算 `mean_priority(C) = sum(score(step)) / |C|`，仅保留均值最高的一个。均值包含桥接 step，使用累计优先级分数；不按总分或节点数选。均值相同时，按分量内最小的模型 step index 决定，确保可复现。
5. **生成 trace 并校验。** 在选定分量内按依赖方向排序，同级节点使用原模型顺序；输出一个弱连通的有序 trace。若不满足最小长度、party 角色要求、顺序约束或 trace 去重要求，则在 `sample_attempts` 内重新采样；耗尽后跳过本轮。丢弃分量后不再加入无关节点凑长度。

“最少补点”指全局新增节点数最少，不能把逐对最短路径贪心合并直接当成最优解。实现采用采样终端子集的动态规划/连接搜索，节点代价为已采样节点 0、其他节点 1，并允许共享桥接节点；多个等价最优解按模型 step index 稳定选择。为避免大模型或过大采样配置阻塞主循环，搜索设置显式工作预算；预算耗尽时记录未证明最优，使用当前找到的补点结果执行分量筛选。

此流程只补连通所需节点，不对所有前驱做无条件依赖闭包。refined model 中必要的会话前缀仍作为条件提供给 generator；receipt 保留不可变 refined model 引用，generator 可以查询未被选入目标 trace 的前置 step，并按现有 `added_steps` 机制记录额外执行步骤。

## 6. 关键分析与决策日志

以下信息必须写入持久化日志，不能仅保存在 agent 终端或结果 JSON 中。默认在 `coverage.output/<构建指纹>/analysis/events.jsonl` 追加结构化事件，同时通过现有 Rust `log` 通道输出 INFO 级摘要，便于从 campaign 当前收集的运行日志直接观察进展。详细事件不依赖 DEBUG 开关，不能只记录产物路径或只输出排名靠前的条目。

每条事件包含时间、事件类型、事件序号、campaign session、构建指纹，以及适用的 analysis request ID、coverage snapshot、输入/输出 protocol ID、模型版本、selection ID、采样 attempt 和 trace ID。尚未产生的标识允许为空。使用已有 ID 长度约定；持久化状态记录对应日志事件序号，方便关联恢复前后的行为。

### 必须记录的信息与时机

| 信息 | 输出时机 | 必须包含的内容 |
| --- | --- | --- |
| Handler 与 message 映射 | 首次提取完成、映射更新或从缓存恢复时 | message 名称、关联 step ID/名称、handler 函数标识/符号/源码位置、映射依据与不确定性；记录每一项映射及未匹配项，更新时记录新增/删除/变更 |
| Handler 连续未命中区域 | 每个快照分析完成时 | 每个 handler 的区域列表、每个区域的分支方向数/去重函数数、分支标识与位置、函数列表、handler 内去重汇总、未知边界/无法映射项及原因；无未命中区域时显式记录零结果 |
| 阻塞条件与前缀 | 条件分析完成时 | handler/step 关联、有序前缀 step 序列、替代序列、字段/状态/环境条件、分析依据，以及条件是否被确认为强制前缀 |
| 模型更新内容 | agent 提出更新，以及 Rust 接受或拒绝更新时 | 原/新 protocol ID、具体 dependency 的 prior/dependent step、condition 的旧值与新值、新增/变更内容和原因、产物路径；分别标明 proposed/accepted/rejected，拒绝时附校验原因 |
| 连通分量计算 | 每次 selector 采样及补点后，包括失败 attempt | 采样 step 及各自分数/权重、补点前后的分量及其成员、连接所用 dependency 边/桥接路径、新增 step、搜索预算与是否证明最优、每个分量的 priority 总和/节点数/均值、最终保留和丢弃的分量及原因 |
| 当前 priority 数值 | 初始化/恢复、每次 delta 应用后、每次采样前 | 初始化/恢复及更新后记录全部 eligible step 的完整分数与权重快照，包括零分和未变化项；每次 delta 逐项记录 step ID/名称、before/delta/after、实际权重和更新理由；每次采样记录所用 priority 快照版本，并关联该版本的完整分数表 |
| 最终 trace | 每次选择成功或本轮跳过时 | 最终有序 step、采样节点与补入节点的区分、保留分量的均值、trace ID；重采样/跳过时记录长度、party、依赖顺序、去重或预算等具体原因 |

模型 diff 和 priority 日志必须反映 Rust 实际接受的状态。不能将 agent 建议值或被拒绝的 delta 记录成已生效值；重复/过期结果被忽略时记录原因，分数不得重复变化。采样前的完整分数快照引用必须在同一日志中可追溯，不能仅引用可能被覆盖的外部状态文件。

### 状态、体积与写入规则

- 同时记录 HC 状态迁移、分析排队/快照合并、派发/完成/超时、缓存命中/失效、结果接受/拒绝以及耗时，使用同一套关联标识连接分析结果与后续 synthesis request。
- 日志事件由 Rust 统一组织和串行追加；agent/分析工具输出结构化结果，worker 将事件交给日志写入端，不让多个进程直接竞争写同一个 JSONL 文件。
- 大型映射、区域或 priority 表按条目拆分成关联事件，记录总条目数及完成标记，保证可确认是否完整。INFO 摘要显示总数、关键计数与版本，JSONL 保留全部要求的明细；不反复打印整份源码、callgraph、模型或全量 LLVM coverage JSON。
- 日志追加失败时在现有运行日志明确报告错误，不静默丢弃；不因日志错误误发布尚未校验的结果。日志内容不包含 agent 认证环境变量。

## 7. 产物与验收

分析目录保存`handlers.json`、每次 request 的输入描述/报告/`protocol-refined.json`/`priority-delta.json`/`result.json`、已接受版本及状态与应用账本。输入及已发布版本保持不可变，通过原子更新的最新版本引用供查询；失败结果不覆盖有效版本。

- 配置：验证 `[coverage]` 的采集与分析开关、profile/路径校验，以及旧配置名的迁移提示。
- 图与指标：验证精简 manifest 的格式版本及三个摘要的实际校验、启动转换、两个 JSON 与原始图一致、后续无 DOT 读取及 JSON 篡改检测、构建不匹配、分支方向计数、命中边切分、递归/循环去重、共享区域、间接调用及未知映射处理。
- 模型与分数：验证 handler 多映射、依赖前缀顺序和替代序列、step ID 稳定、RFC 路径、非法依赖拒绝、delta 幂等应用、恢复与加权选择。
- Petgraph：验证每个 step 的节点及索引、依赖方向及不同条件的平行边、refined model 的边更新与分数保留、无效结果不改变图、图/模型/priority 的版本一致性、checkpoint 恢复后的索引重建，以及 selector 连续调用复用同一常驻图。
- Selector：验证单个分量无需补点、两个目标间补最短桥接链、多个目标共享桥接节点的全局最小补点、无路径/预算不足时的分量筛选，以及平均分优先于总分。另测桥接节点参与均值、弱连通与依赖排序、确定性平局处理、长度/party/去重失败重采样、搜索预算耗尽后的明确降级。
- HC：用模拟 core 验证异步初始化、迟到结果、超时、异常退出、状态记录；验证只开 coverage analysis 时也启动 core，两种 agent 同时运行时关闭一个 terminal 不影响另一个。
- 集成：LightFTP 使用同构建 coverage/graph 产物完成一次分析，核对 handler 位置和区域统计；验证分析未完成时 executions 继续增长，新结果用于后续 synthesis，在途 request 保持原版本。
- 回收：验证 testcase 清理不终止分析 agent 的工具进程，campaign 结束后两个 terminal 和私有 core 均被回收。
- 日志：核对 handler 映射、每个区域的分支/函数统计、dependency 条件 diff、所有连通分量及其均值、priority 完整快照与 before/delta/after 均有对应事件；能从最终 trace 反查采样分数、桥接步骤和模型更新来源。另测零结果、缓存恢复、拒绝/重复结果、失败 attempt、明细拆分完整性和日志写入失败诊断。
