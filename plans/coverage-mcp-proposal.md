# Coverage MCP 驱动的 proposal → generator 流程

## 1. 目标和已确定的边界

参考 `third-party/FuzzAgent` 的分层覆盖查询与调用序列推理流程，实现：

```text
普通变异 → coverage replay → 冻结 snapshot（profdata + coverage.json）
                              ↓
                coverage agent 调用 MCP 查询、推理
                              ↓
                      proposal + 图修改事务
                              ↓
                Rust 校验、确认、暂存最新 proposal
                              ↓
                synthesis 阶段直接转交 proposal
                              ↓
                  generator 校验、发布、执行
```

- Rust 不计算 uncovered regions，不维护可变 protocol 图或 step priority，不选择、补齐、重排 trace，也不构造 receipt。
- Python MCP 服务维护 coverage、CFG、callgraph、handler 映射和可变 protocol 图；agent 选择探索目标与完整 step 序列。
- Rust 可以保留只读 protocol 索引、ID 工具和 party binding，用于引用校验、evaluation 和运行配置；不借此重新做探索决策。
- coverage analysis 开启时，正常 synthesis 必须等待有效 proposal；关闭时，由 synthesis agent 自主规划并生成。无 seed 的首次 bootstrap 是开启 analysis 时唯一允许自主规划的例外。
- 同时最多一个 analysis request 和一个 synthesis request 在途。各自仅保存一个最新待处理项；新 snapshot/proposal 不取消已开始的任务。
- 不再产出 `regions.json`、`region_summary`、`protocol-refined.json`、`priority-delta.json` 或 `receipt.json`；不兼容这些旧请求产物，但保留已经发布的 generator 和 corpus 使用能力。

## 2. 实施顺序与提交

1. 改动实现前，先检查 diff 并提交当前源码、依赖、配置及 plans，记录 commit ID。排除 `fuzz/analysis` 中的生成数据、`rsc/runs` 中的运行结果和独立 Git 参考仓库；这些目录中的源码应单独辨别。不要使用无差别的 `git add .`，不 push。
2. 先实现 Python 内存数据层、查询和 protocol 图事务，以及 MCP stdio/后台通信；用真实保存的 LightFTP snapshot 验证。
3. 接入服务生命周期、coverage init/task 提示词、proposal 发布和 Rust 接收。
4. 修改 synthesis 和 evaluation 直接消费 proposal；覆盖自主生成及 bootstrap。
5. 确认新链路工作后删除被替代的 measure、priority、selector、receipt 路径与过时配置，更新文档并完成验收。

主要改动集中于 `fuzz/python/coverage_analysis/`、Rust coverage/synthesis/agents 模块以及对应提示词；复用现有 HC worker、artifact 身份校验和日志组件，不再造一套 agent 会话框架。

## 3. 后台服务、配置和生命周期

### 3.1 进程和传输

- 提供 `python -m coverage_analysis.server serve --socket ... --state-dir ...` 和 `python -m coverage_analysis.server stdio --socket ...` 两个入口。前者是真正持有内存状态的服务，后者是 agent 的无状态 MCP 桥接。
- Agent 到桥接使用标准 MCP stdio，通过 Python MCP SDK 实现协议；桥接到服务以及 Rust 控制端到服务使用本地 Unix socket。内部使用按换行分帧的 JSON 请求/响应，以 `id` 关联，支持健康检查、工具调用、请求登记、确认和终止。
- 默认由 supervisor 在进入 SUT namespace 之前，以 campaign 用户身份启动服务，与 HC 启动位置相邻。服务不属于 SUT/testcase 进程组，testcase 清理不会杀死它。
- 一个 campaign 对应一个独立服务和 socket，避免跨 campaign 共享图与覆盖状态。socket 使用短的 runtime 路径，实际位置写入 campaign 的 runtime 记录，避免 Unix socket 路径长度问题。
- `[coverage]` 新增可选 `mcp_socket`：未配置时启动自有实例，配置时连接 supervisor 提供的实例。Python 与 PYTHONPATH 复用现有 campaign 配置；启动/连接就绪等待默认 30 秒。
- 自有服务由 supervisor 持有进程句柄并负责退出回收；外部服务仅断开连接。退出时先停止 agent 请求，再停止自有服务，超时后终止并 wait，避免遗留进程。
- 服务空状态启动，不读取或检查任何 coverage/图/model 文件。健康检查成功只表示服务可连接、tools 可调用。
- 为 coverage agent 生成会话专用 MCP 配置，注册上述 stdio 桥接，不改用户全局配置。provider 无法原生连接 stdio MCP 时明确报配置错误；不能退化为 shell 命令模拟 tool。先以当前 campaign 的 Codex profile 完成真实验收。

### 3.2 状态、并发和恢复

- 分别保存已加载的 coverage、CFG、callgraph、handlers、protocol，以及它们的内容摘要、build/source 身份、加载版本；用 `get_loaded_state` 查询。
- 大文件解析在后台工作线程完成，成功后原子替换；解析期间仍能响应健康检查和读取旧的有效数据。相同内容重复加载直接复用。
- 每个 analysis request 登记不可变的 request/session/build/snapshot/protocol 身份。Agent 的请求级工具调用携带 request ID，读取该请求固定的数据版本；显式加载后来的 snapshot 不改变在途查询。
- 同一请求的数据必须属于兼容的 build/source/model。新静态数据使旧索引失效时重建索引；无法配套的查询返回 `incompatible_data`，不混用。
- Agent/桥接重连复用后台内存；失败请求的图修改被丢弃。后台服务重启后仍为空，由 agent 显式 load 静态输入、所需 snapshot 和最近一次已提交的 protocol 图。
- 服务中断使当前 analysis request 明确失败，不提交半成品，不影响已有 generator 的普通变异。自有服务由 supervisor 恢复；重新就绪后，下次 analysis request 重载所需数据。

## 4. MCP tools 与数据语义

### 4.1 显式加载

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| `get_loaded_state` | 无 | 返回各数据集是否加载、身份与版本；不触发加载 |
| `load_profdata` | `request_id, snapshot_dir, manifest_path` | 读取冻结 snapshot 的 metadata、profdata 和 coverage JSON，绑定到该请求 |
| `load_cfg` | `cfg_dir, manifest_path` | 解析 CFG DOT 和现有 source-map，建立块、边、指令位置索引 |
| `load_callgraph` | `callgraph_dot, manifest_path` | 解析调用图，建立正反向邻接索引 |
| `load_protocol` | `protocol_path, catalog_path, checkpoint_path?` | 加载原始 protocol 与稳定 ID 表；可显式叠加已提交图快照 |
| `load_handlers` | `handlers_path` | 加载并校验初始化得到的入站 message ↔ handler 多对多映射 |

- snapshot 目前保存 `coverage.profdata.zlib`；也允许测试输入提供未压缩 `coverage.profdata`。profdata 是 LLVM 二进制 profile，不自行用 Python 解码其计数；在内存中解压/校验后，解析同一冻结 snapshot 中由 Rust 在 replay 结束后调用 llvm-cov 导出的 `coverage.json`。
- 保留 Rust replay/export；缺少 coverage JSON 时 load 报错，不重新 replay，也不悄悄启动另一次 LLVM 导出。保存并校验 JSON 摘要与 profile 摘要，确保二者属于同一次 snapshot 导出。
- CFG/callgraph 的 DOT 解析移动到显式 load 阶段，不再由 Rust 启动时强制转换。可复用现有 Python parser；保留后继端口/序号等原始边信息，后续工具不再需要猜边序号。
- 文件不存在、解析失败、身份不符时，返回具体错误并保留之前成功加载的状态。
- 查询依赖未加载时，MCP tool 返回 `isError=true`，内容包含 `code: not_loaded`、`missing` 和可调用的 load 工具；不返回空列表或零覆盖代替错误。

### 4.2 覆盖和可达查询

| 工具 | 查询粒度及必要输入 |
| --- | --- |
| `show_overview_code_coverage` | 项目整体以及各模块摘要 |
| `show_module_code_coverage` | 按源码相对目录划分模块，列子目录/文件，可按路径过滤 |
| `show_file_code_coverage` | 指定文件或过滤文件列表，并列出函数统计 |
| `show_handler_code_coverage` | handler 自身与其可达函数集合的累计统计，可过滤 handler |
| `show_func_code_coverage` | 指定函数或过滤函数列表，返回自身与可达累计统计 |
| `get_reachable_functions` | 给定函数，返回去重后继可达函数及距离；可包含自身 |
| `get_reaching_handlers` | 给定函数，返回能到达它的 handler 及各一条最短调用路径 |
| `show_source_coverage` | 指定函数/文件和行范围，返回有限源码片段、源码区域和 true/false 计数 |

- 覆盖工具统一返回 `total_branches, covered_branches, missed_branches, branch_coverage_percent`。这里的 branch 是源码条件的方向：true/false 分别统计；零分支时 percent 为 `null`。
- 直接使用 coverage JSON 的源码分支计数，按文件、函数和行列范围建立稳定记录；不把普通 code region 当成分支，不要求映射到每条 IR CFG 边。
- 函数标识保留 LLVM 原始符号，并关联源码文件；对 `ftpserv.c:cleanup_handler` 等 coverage 名称建立可验证的别名，不用简单删前缀或仅凭同名合并。
- 累计统计包含根函数；同一函数及分支在一个可达集合中仅计一次，递归和环不重复展开。不同 handler 的累计统计可能重叠，overview 独立去重，不能累加 handler 数字得到整体覆盖。
- coverage 列表中已有且 count 为 0 的函数是未命中；图中存在但 coverage 中没有匹配记录的函数是 missing data，不按 0 处理。外部库、间接调用未解析、缺失 profile 等分开列诊断；不恢复旧的统一 unknown 列表。
- 自身统计只依赖 coverage；累计统计额外依赖 callgraph，handler 查询还依赖 handlers。代码片段依赖明确配置的 source root；不需要 CFG 的查询不强制要求加载 CFG。
- 列表默认每页 20 条，最大 100 条；源码默认最多 80 行、最大 200 行。稳定排序，返回总条数与分页信息，支持按未命中数量排序。Agent 从摘要下钻，不一次性获取整个 coverage JSON、CFG 或所有调用路径。

### 4.3 Protocol 图与事务

- 图节点是 step，边是 dependency，采用 NetworkX 有向图。加载时保留现有协议依赖语义，包括条件依赖和重复交互；不再将整图必须为 DAG 作为校验规则。
- 提供 `get_protocol_steps`、`get_step_dependencies`、`update_step_annotations`、`upsert_dependency`、`remove_dependency`。节点可更新分析注释和探索约束；base step 的消息、party、channel 和原始定义保持稳定，不能静默改变已有 step ID。
- Rust 启动时可从只读 model 导出一次稳定 ID catalog，包括 protocol ID、step ID 与 party binding；这是标识表，不包含 priority 或选择算法。MCP 和 agent 使用该表中的 ID，不自行猜测名称或 ID。
- 保持现有 8 位 ID 约定；检测重复/冲突的短 ID 并报错。handler 使用以符号和源码文件区分的稳定 handler ID，映射可由多个 message 共享。
- `begin_analysis` 为已登记请求创建图事务；所有写工具只改该事务。`submit_proposal` 校验 proposal、补充选中定义、计算 trace ID，并冻结 proposal 和 graph checkpoint，写入该 request 的输出目录。
- `commit_analysis` 和 `abort_analysis` 仅供 fuzzer 控制端调用。Rust 校验 proposal 成功后确认提交；服务原子更新已提交图指针，并返回确认，然后 fuzzer 将 proposal 标记为待 synthesis。确认可按 request/proposal 摘要幂等重试。
- 失败、超时、无效产物调用 abort。已冻结事务禁止继续修改；成功快照持久化，未确认事务不作为恢复源。重启可根据已提交记录恢复尚未消费的最新 proposal。

## 5. Proposal、提示词和 synthesis 交接

### 5.1 Proposal v1

以 `proposal.json` 作为唯一生成指导文件；保存最小必要的选中定义，不嵌入完整 model。结构如下：

```json
{
  "version": 1,
  "proposal_id": "8位内容ID",
  "origin": "coverage",
  "session": "会话ID",
  "request_id": "analysis请求ID",
  "build": "构建ID",
  "snapshot": "000011",
  "protocol_id": "8位base协议ID",
  "graph_revision": 3,
  "trace_id": "8位trace ID，顺序及重复step参与计算",
  "goal": {
    "handler_ids": ["handler ID"],
    "reason": "要补齐的handler或内部路径，以及工具给出的覆盖证据"
  },
  "trace": [
    {
      "step_id": "8位step ID",
      "handler_ids": ["该step对应的handler ID"],
      "reason": "目标探索或前缀/回复作用",
      "constraints": ["字段条件、状态条件或场景要求"]
    }
  ],
  "context": {
    "steps": [],
    "messages": [],
    "channels": [],
    "parties": [],
    "handlers": [],
    "dependencies": [],
    "documents": []
  }
}
```

- `context` 由 MCP 从加载的 protocol、binding、handler 映射和当前图事务中提取，填充 trace 涉及的定义与必要的 RFC 路径/文档身份；dependencies 使用本事务的有效依赖。目标可有多个，但须是一个连贯探索任务。
- 保留 trace 顺序和重复 step。一个 step 可以关联多个 handler；纯前缀、环境操作或回复 step 可无 handler，但必须有理由。目标 handler 必须在 trace 对应的入站 message 映射中出现。
- 同一 handler 可对应多个 message，因此不能只凭 handler 推导唯一 step。MCP 校验 step → message → handler 的关联；Rust按该关联检查提案，不能自行替换为另一个同 message 的 step。
- Rust 不生成或重新拼装 receipt/context；只校验 request 身份、引用、party binding、必要字段及摘要，原样复制/转交冻结 proposal。
- 共用 Python proposal schema/ID 工具；MCP submit 和自主 synthesis 都使用相同规则。trace ID 兼容现有有序 step 哈希，proposal ID 绑定整个提案内容；proposals 不因相同 trace 而丢失不同 constraints。

### 5.2 Coverage agent

- Init 保留入站 handler 定义和源码检查流程，输出 handlers 文件；输入无需在服务启动时存在。Agent 调用 `get_loaded_state`，显式加载可用的 protocol/图和初始化产物，最后发布 ready。
- Task 输入仅含 request 身份、路径、catalog/handler/最近成功 checkpoint 引用和预算；不再包含 region_summary。按需 load，确认已加载 snapshot 与请求一致，再开始事务。
- 先看 overview/handler，优先分析尚未进入且可达复杂度较高的 handler；需要时下钻 func/source，利用前驱 handler、protocol dependency 和源码条件构造完整 trace。以实际工具数据说明理由，不硬编码固定 90% 阈值。
- 通过 MCP 更新图、调用 submit_proposal，然后原子发布含身份与 proposal 摘要的 result。状态为 `ok`、`no_proposal` 或 `error`；后两者必须给出原因，并终止事务。
- 无新 proposal 时保留之前已接受但未消费的 proposal；analysis 失败不会转为自主 synthesis。继续普通变异，下次按调度条件重试。

### 5.3 Synthesis agent 与产物校验

- coverage 模式：Rust 到 synthesis stage 时，只有 agent ready、无任务在途且已有未消费 proposal，才发送任务。request 目录保存 proposal 原样副本和独立的 synthesis request 身份；提示词只指向 proposal，不再要求 receipt。
- synthesis 按 proposal 中的 trace、constraints、消息/通道定义和 RFC 生成 generator；保留现有禁止读取 SUT 源码的约束。提案可提供已推理出的条件，不注入 source root 或要求 synthesis 重新调查 SUT。
- 正常自主模式及 seedless bootstrap：synthesis 接收 model/catalog/binding 和 RFC 引用，自行选 trace，利用共用 Python 工具导出 `origin: autonomous` 的 proposal 后生成 generator；不先调用 Rust selector 或请求 Rust 生成 receipt。该模式允许 goal.handler_ids 为空，build/snapshot/graph_revision 可为空，并须说明自主目标。
- 保留 `generator.py`、params/schema 和 `result.json` 的产物协议。result 关联 synthesis request ID、proposal ID、protocol ID、trace ID；Rust 继续在进程内校验代码、参数与身份并发布 generator，不启动 Python 校验子进程。
- Python validator/skill 增加读取 proposal 的入口，所有提示词和 evaluation 改用 proposal。coverage 任务预先绑定提案身份；自主任务在验收 generator 前先验证 agent 生成的 proposal。TraceOrigin 从 proposal 读取 step ID，不再读 receipt。
- proposal 在成功派发时标记已消费；同一 proposal 不自动重复派发。失败记录原因，等待下一份 proposal。覆盖分析进行期间普通变异继续，不新增无界任务队列。

## 6. Rust 移除范围、调度与日志

- 删除 Rust measure 和 region 产物 seal 路径、petgraph protocol exploration、priority delta 接收、随机/优先级 trace selector，以及 receipt 构造。仅为展示/验证需要的有序 trace 数据类型可保留或移到 proposal 类型中。
- `coverage/graphs.rs` 的启动转换路径移除；图由 MCP load 显式解析。保留 snapshot replay、profdata 合并/压缩、coverage JSON 导出、manifest 校验及底层执行隔离。
- coverage stage 向共享的有界 proposal mailbox 写入；synthesis stage 在下一次进入时读取。旧 proposal 被替换、派发、失败和完成均留下关联记录。
- bitmap 停滞触发机制和 agent 后台初始化继续使用现有逻辑；达到阈值后先 coverage replay/analysis，synthesis 不阻塞等待。analysis 在途时仅更新待分析的最新 snapshot，synthesis 在途时继续遵守现有抑制重复派发规则。
- 保留初始化单次 5min、连续三次初始化超时退出 campaign 的行为；初始化尚未结束的一次 stage 跳过不计超时。Task 沿用配置预算，服务错误和任务超时有明确分类。
- 删除失去意义的 sample_attempts、trace_len_min/max 等 Rust 选择配置及相应校验，更新 campaign/evaluation 模板。Evaluation 中需要固定 trace 的测试用例直接准备 proposal fixture，不再通过 receipt 适配。
- 日志覆盖：服务启动/连接/恢复/退出、load 版本及耗时、工具名称与统计摘要、handler-message 映射、dependency 更新、事务提交/回滚、proposal 目标与有序步骤理由、mailbox 替换、synthesis 关联 ID 与结果。保留 stdout/stderr 和 HC 会话观察；不把完整 coverage 或图写入 agent 上下文，不记录凭证。

## 7. 测试与验收

1. **数据加载**：文件尚不存在时可空启动；未加载/部分加载工具报 not_loaded；压缩及普通 profdata、原子换 snapshot、重复加载、失败保留旧状态、构建不符和在途快照固定均有测试。
2. **覆盖与图查询**：用有已知 true/false 计数的小 fixture 核对五级统计；验证静态函数别名、同名函数、多文件、零分支、missing profile、图外调用、可达去重、递归、共享 callee、反向 handler 和分页。
3. **事务与 proposal**：验证合法及错误引用、空前缀 handler、重复 step、多 message 共用 handler、条件 dependency 和环、不完整 artifact、冻结后修改、幂等确认、失败回滚、服务重启和未消费 proposal 恢复。
4. **调度与隔离**：验证最新待处理项替换、analysis/synthesis 在途互不阻塞、无 proposal 跳过、analysis 关闭自主生成、无 seed bootstrap、初始化三次连续超时；testcase 回收不影响服务，campaign 退出不遗留自有进程。
5. **协议与生成**：真实 stdio MCP initialize/list_tools/call_tool；Python 工具查询与内存数据层结果一致。Rust/Python ID fixture 一致，generator 验收直接使用 proposal，evaluation 无 receipt 依赖。
6. **LightFTP 真实链路**：先用已有 profdata/coverage JSON 和图跑离线工具验证，再开启 coverage analysis 与 synthesis 运行 campaign。需看到 agent 的真实 MCP 调用、proposal 已接受及图提交、proposal 原样交给 synthesis、generator 发布并执行；记录耗时和关联 ID。分析完成但 generator 尚未发布不能作为全链路通过。

完成标准：新流程通过相关 Rust/Python 测试与上述真实链路验收，Rust 不再调用 regions/selector/receipt 路径，agent 能在不读取完整 coverage JSON 的情况下完成查询、推理和生成。
