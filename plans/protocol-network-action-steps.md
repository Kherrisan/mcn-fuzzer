# 将网络操作纳入协议 Step，并打通生成链路

## 1. 目标与建模约定

让 RSC agent 在阅读 RFC、提取 interaction 时，同时识别消息交互和网络操作，建立它们之间有来源依据的依赖关系，并贯通 coverage → proposal → synthesis。

- Step 支持四种 `action`：`send`、`listen`、`connect`、`close`。
- 网络操作节点以 **`(party, channel, action)`** 去重。不同 channel 分别建节点；同一 party 在同一 channel 上，同一种网络 action 只保留一个节点。
- 节点表示操作类型，而不是某一次运行实例。同一节点可在有序 trace 中重复出现，以表达重连或重复数据传输。
- `listen` 表示开始监听并可接受连接；`connect` 表示主动建立连接；`close` 表示该方关闭传输连接。不新增独立的 `accept` Step。
- QUIT、TLS close_notify 等协议终止消息仍是 `send`；RFC 描述了后续传输关闭时，另建 `close` 节点与依赖。
- 根据 RFC 的语义提取，不要求原文出现系统调用名称；没有相关描述时，不自动为每个 party 或 channel 补齐三种网络 action。

## 2. Schema 与 RSC 工作流

### 2.1 数据结构

协议 schema 升级为 **8.0.0**，不兼容读取旧版。Step 改为以 `action` 区分的联合类型：

| 类型 | 公共字段 | 类型专属字段 |
| --- | --- | --- |
| `send` | `id`、`name`、`action`、`channel_name`、`condition` | `message_name`、`sender_name`、`receiver_name` |
| `listen` / `connect` / `close` | `id`、`name`、`action`、`channel_name`、`condition` | `party_name` |

- 网络 action 禁止填充消息或收发双方字段，不创建虚构的 message 来承载网络操作。
- 保留现有 dependency 结构。涉及网络 action 的边通过 `condition` 表达模式、端点来源和发生条件，`field_relations` 必须为空。
- 消息之间的依赖继续支持现有字段关联；协商端点继续记录在 channel 的 `endpoint_sources` 中。
- action 节点保存共通条件；不同交互上下文的条件放在相应依赖边和 proposal trace occurrence 的约束中，避免为条件变化复制同一个网络 action 节点。

### 2.2 提取、核验与发布

- 更新 Python 类型、发布 schema、MCP 修改工具和引用校验，确保工具暴露的输入 schema 正确处理 Step 联合类型。
- 服务端继续分配 Step ID，并校验网络 action 节点唯一性、party/channel 引用及端点归属。
- 更新 parser 提示词及 supervisor 内嵌角色指令：逐批检查监听准备、连接建立、端点协商、关闭与消息之间的先后约束；跨章节查询并复用已有节点；节点和边均保留来源证据。
- 更新 reviewer 提示词：检查网络操作的遗漏、重复、角色方向、channel 归属、条件和依赖依据，区分协议终止消息与传输关闭。
- 同一 channel 若存在互不兼容的连接角色，按现有 channel 区分规则拆分；不因命令数量机械复制 channel，不凭空制造操作或时序。
- 依赖表达前置条件，不要求相邻执行。不强制将整个协议图变成 DAG，以免排除重连等重复交互。
- 更新仓库内模型和测试样例的格式。旧消息 Step 显式标记为 `send`，不得在机械转换时推测添加网络操作。
- 历史运行产物保留；恢复旧 schema 的任务时明确报错，不隐式升级旧任务或篡改历史审计记录。

## 3. Coverage → Proposal → Synthesis

### 3.1 Rust 读取与身份校验

- 使用明确的 Step 枚举，更新模型读取、party binding、catalog、evaluation 和提示词上下文，不使用空字符串模拟网络 action。
- `send` 按发送方、接收方及 channel 参与方检查绑定；网络 action 按执行方及 channel 参与方检查绑定。SUT/generator 角色判断和上下文收集覆盖两种结构。
- catalog 升级为新版，并同步修改 Rust 输出与 Python MCP 读取规则。
- Python 与 Rust 同步更新 Step/Trace 哈希规则，纳入 action 及对应类型字段；公开 ID 仍使用前八位。顺序和重复 occurrence 均参与 Trace ID 计算。
- 旧协议 schema 和旧 catalog 明确拒绝，错误信息包含实际版本及所需版本。

### 3.2 Coverage MCP 与 proposal

- MCP 的 Step 查询、party 过滤、图编辑、上下文收集及 proposal context 完整保留四种 Step。
- 只有 `send` 参与 message → handler 映射；trace step 不携带 `handler_ids`，handler 关联由服务根据已加载的映射推导。
- Coverage agent 仍以 handler 为探索目标，将必要的监听、连接、关闭及消息步骤纳入同一 trace；网络 action 需说明其前置、配套或收尾作用。
- Proposal 保留现有有序 trace，允许重复引用同一 Step，每次出现分别携带理由和约束。不得在序列传递中去重或重新排序。
- Rust 验收 proposal 时按 Step 类型检查引用与绑定，并保留网络 action 的完整上下文。现有 proposal 外层格式可继续使用，不为网络操作构造 receipt。

### 3.3 Synthesis 与执行边界

- 更新 synthesis init/generation 提示词及自主 proposal 生成路径，明确四种 Step 的含义、重复引用和网络操作执行方。
- Generator 负责其绑定 party 的操作，使用现有网络 API 生成对应交互。`listen` 是监听就绪，不要求等待远端连接后才执行后续协议操作。
- SUT 的操作由真实 SUT 执行，generator 通过配套交互等待或观察，不代替 SUT 建立连接、监听或关闭连接。
- 沿用现有运行生命周期和网络 API，不新增通用 Step 执行器，不扩大 UDP 或其他传输的运行时能力。无法由现有运行生命周期承载的要求应明确报告，不能通过省略 Step 伪装完成。

## 4. 实施顺序

1. 修改协议类型、schema、MCP 输入结构和校验，同步更新 RSC 提示词、工作流测试与样例。
2. 修改 Rust 模型、catalog、稳定 ID 和绑定检查，更新 evaluation 与仓库内 campaign 模型格式。
3. 修改 coverage MCP 的图、查询、proposal 校验和上下文，确保 Python/Rust 身份计算一致。
4. 修改 coverage/synthesis 提示词及自主生成路径，完成 proposal 到 generator 的交接。
5. 执行回归和小型真实链路验收，更新相关文档，记录结果与限制。

## 5. 验证与验收

- **Schema 与引用**：四种合法 Step、非法字段组合、重复网络节点、同 party 不同 channel 的合法节点、未知 party/channel、非端点 party、涉及网络 action 的非法字段关联，以及旧版拒绝。
- **RSC 工作流**：MCP 添加/更新联合类型、服务端 ID 分配、来源证据、核验修订、gate 和最终发布均正常。
- **RFC 语义**：使用 FTP 主动/被动数据连接片段验证监听与连接方、端点协商和连接的先后关系、数据消息与关闭的关联，以及 QUIT 与传输关闭的分离。
- **重复与条件**：同一通道多次连接和关闭、条件分支、包含环的类型依赖图，以及没有网络操作描述的输入；不机械补节点、不强制全图拓扑排序。
- **跨语言交接**：Python/Rust ID 一致，重复 occurrence 改变 Trace ID；catalog、proposal 和 synthesis context 保持 action、party、channel、顺序与约束；网络 action 不进入 handler 映射。
- **角色执行**：generator 侧网络动作由 generator 实现，SUT 侧动作仅由真实 SUT 执行；不通过替代 SUT 或丢弃 Step 满足校验。
- **回归与真实验收**：完成 RSC 回归及相关 Rust/Python 测试，再用小型 RFC 跑通提取、核验、proposal、generator 生成和短程 replay，记录最终节点/边数量与链路结果。本次不自动启动一小时 campaign。
