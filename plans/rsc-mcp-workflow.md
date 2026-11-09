# RSC：MCP 工作流控制与 Herdr Core 双 Agent 提取方案

## 1. 目标与架构

替换现有 RSC 逐 block 提取、合并和补全流水线，改为按章节批次提取，由 MCP server 强制管理阶段、阅读覆盖和核验 gate。

复用现有 RFC 解析器、章节索引、protocol Schema 和基础校验。最终 `protocol.json` 保持现有结构；来源依据、阅读记录、核验问题单独保存。

| 组件 | 职责 |
|---|---|
| 常驻 MCP server | 维护任务、批次、模型 revision、核验轮次和问题；提供工具并执行 gate |
| Python supervisor | 启动服务，通过 Herdr Core 管理两个 Codex 会话并转发任务 |
| Parser Codex | 调用预处理工具、提取协议模型、修订核验问题 |
| Reviewer Codex | 独立核验原文与模型，复查 parser 的修订 |

```text
Parser：预处理 → 解析
                  ↓
Reviewer：      第1轮核验
                  ↓ 有问题
Parser：          修订
                  ↓
Reviewer：      第2轮核验
                  ↓ 有问题
Parser：          修订
                  ↓
Reviewer：      第3轮核验
                  ↓
Server：完整发布 / 部分发布 / 保留失败草稿
```

默认最多 **3 轮核验**，首轮计入上限，因此最多进行两次修订及复核。允许提前通过并结束。

## 2. 工作流与 gate

### 预处理

Parser 调用工具，由 server 执行确定性的预处理：

- 固定 RFC 输入集合，解析章节、block 和原始位置，保存内容快照与哈希。
- 按章节组织批次，默认目标 **8,000 tokens**、上限 **12,000 tokens**，可配置。使用 `cl100k_base` 统一计数，包含正文和附带上下文。
- 超长章节按 block 拆分，超长 block 按文本范围继续拆分。重复提供的标题上下文不重复计入正文覆盖。
- 全部非空正文进入批次，不按关键词提前排除章节或附录。

**Gate：** 所有输入解析成功，快照哈希一致，正文覆盖无缺口、无非上下文性质的重复，批次长度合规。通过后冻结原文和批次清单。

### 解析

Parser 按固定批次顺序阅读，同时提取对象、交互和依赖，允许额外跨章节检索。

- Server 记录实际交付的批次正文、哈希和读取凭据；每批提交处理报告或“没有相关信息”的理由。
- 模型支持查询、添加、修改、删除和原子批量操作。使用内部稳定记录编号定位对象，不扩展发布 Schema。
- 每个保留的模型记录关联来源 block 或文本范围。合并、删除时更新相关记录和处理报告。
- 草稿允许暂时缺失引用；每次写入检查字段结构、来源合法性和 step ID。
- 模型修改必须携带预期 revision 和幂等操作 ID。

**Gate：** 全部批次完成，模型记录有有效来源，Schema、step ID、名称引用及角色、信道结构校验通过。悬空或歧义引用阻止推进。

阅读凭据证明原文已交付，不能单独证明 agent 已正确理解或提取全部语义。

### 核验、修订与轮次限制

- 首轮核验冻结模型 revision。独立 reviewer 全量对照原文和模型，检查遗漏、重复概念、消息方向、条件、跨章节依赖和通道协商。
- Reviewer 不继承 parser 聊天记录，也不能直接修改模型。
- Finding 保存稳定编号、发现轮次、模型 revision、相关记录、来源、问题描述、建议及处理历史。
- Reviewer 完成本轮后，server 才安排 parser 修订，避免核验期间模型被修改。
- Parser 逐项提交 `applied / not_applicable / unresolved`、理由和相关修改。Parser 无权自行关闭 finding。
- 修订后，reviewer 检查完整差异、问题处理记录和受影响来源批次，确认当前 revision；未变化内容保留首次全量审核记录。
- 复核发现的新问题纳入同一问题清单，参与后续修订或最终剩余问题汇总。
- `max_review_rounds` 为正整数，默认 3。轮次及其 assignment 持久化；消息重发、执行重试和断线恢复不增加轮次，agent 无权修改上限。

### 发布规则

由 server 自动判断发布结果，不提供可绕过 gate 的发布工具：

| 情况 | 处理 |
|---|---|
| 核验通过，且无 unresolved | 发布 `protocol.json`，`meta.complete=true` |
| 核验通过，但存在明确保留的真实 RFC 歧义 | 发布部分模型，`meta.complete=false` |
| 达到轮次上限，仍有未解决的语义问题 | 最后一轮核验及全部硬性校验完成后，发布部分模型 |
| Schema、引用、来源或阅读覆盖不合格，或核验尚未完成 | 保留草稿和报告，不发布 `protocol.json` |

达到上限的部分发布必须将剩余问题写入 `unresolved`，独立报告标记 `review_limit_reached`，保留尚未获得 reviewer 认可的问题状态，不伪装成核验全部通过。

发布后任务只读；更改原文或继续修改模型需建立新任务。

## 3. MCP 工具与状态接口

每个 server 管理一个任务，通过 Unix socket 常驻，agent 使用 stdio 桥接访问。SQLite 保存任务状态、模型、revision、批次、assignment、轮次和 findings。

工具列表按角色和 phase 展示；每次调用仍执行服务端权限检查，不能通过直接调用隐藏工具越过阶段。

| 工具 | 用途与权限 |
|---|---|
| `workflow_status()` | 两个角色查询当前阶段、轮次、revision、待办、允许工具及阻塞项 |
| `claim_assignment(dispatch_id)` | 领取当前角色的有效任务，重复领取幂等 |
| `check_gate()` | 检查提交条件，返回阻塞项及预计下一步，不改变阶段 |
| `advance_phase()` | 提交当前阶段；server 校验并决定下一阶段，调用者不能指定目标 |
| `prepare_sources()` | Parser 在预处理阶段解析 RFC、分批并生成统计 |
| `list_batches(filter, cursor)` | 查询批次、章节、长度、解析和核验进度 |
| `read_batch(batch_id)` | 返回完整批次正文和来源，生成当前 assignment 下的读取凭据 |
| `search_source(query, filters, cursor)` | 检索相关章节和片段，不替代必读批次的完整读取 |
| `query_model(kind, filters, record_ids, detail, cursor)` | 查询模型摘要、完整记录及来源依据 |
| `apply_changes(expected_revision, operation_id, changes)` | Parser 在解析或修订阶段原子执行增删改 |
| `complete_batch(batch_id, receipt_id, report)` | Parser 提交批次处理结果及关联记录 |
| `list_findings(filters, cursor)` | 查询问题、状态及处理历史 |
| `get_changes(from_revision, to_revision, cursor)` | 查询完整模型差异，记录 reviewer 的差异交付进度 |
| `submit_findings(reviewed_revision, findings)` | Reviewer 提交核验问题 |
| `complete_review_batch(batch_id, receipt_id, report)` | Reviewer 提交批次核验结果，包括未发现问题的明确结论 |
| `submit_resolutions(expected_revision, resolutions)` | Parser 提交问题处理说明，不直接关闭 finding |
| `review_resolutions(reviewed_revision, decisions)` | Reviewer 接受或拒绝处理结果，并说明原因 |

不提供 `set_phase`、`set_round` 或直接 `publish` 工具。来源、批次、任务、角色和 revision 检查统一在 server 执行。

## 4. Supervisor 与 Herdr Core 交接

新增 `rsc run` 作为完整入口，配置 RFC 输入、协议名称、任务目录、分批参数、核验轮次以及两个角色的模型设置；另提供独立 server 和 stdio 桥接入口。

- Supervisor 启动任务专用 HC 实例，使用独立 `HC_SESSION` 和状态目录，不接管用户已有会话。
- 启动两个交互式 Codex，名称为 `rsc-parser-<task8>`、`rsc-reviewer-<task8>`，记录实际 terminal ID。
- 使用 `hc run --bg`，Codex 携带 `--no-daemon`，禁用启动自动更新。模型与环境可配置，默认两个角色使用 `gpt-6-sol/high`。
- 两个角色配置不同的 MCP 凭据，禁止自行启动更多 agent；凭据不写入任务消息和审计日志。
- Supervisor 使用 Python subprocess 参数数组调用 HC，通过 `hc wait` 等待，通过 `hc get/read` 诊断；只向可接收输入的会话发任务。
- Server 完成阶段交接时持久化 assignment，包含角色、phase、revision、review round 和唯一 dispatch ID。
- Supervisor 通过 `hc send` 发送简短通知，agent 再通过 MCP 领取任务、读取 findings 或差异。正常交接保留原进程和会话，不使用 `codex exec resume`。
- 消息发送成功只代表已提交；MCP 的领取记录确认任务已开始。结果不确定时先核对状态，禁止盲目重发。
- HC 的 `done/idle` 不能替代 gate。Agent 停止工作但任务未提交时，暂停并报告，不自动推进。
- Supervisor 重启后根据持久化状态接管已有 terminal。Agent 意外退出或阻塞时保留任务，显式重试可创建新角色会话，从 server 状态继续。
- 完成后清理本任务创建的进程，保留产物和日志，不影响其他 HC 会话。

## 5. 迁移、日志与验收

- 清理旧 compile 的模型调用编排及仅供其使用的配置、依赖；保留 ingest、索引、validate、stats 等能力。旧 compile 明确提示迁移，更新 CLI 文档和角色提示词。
- 保存原文快照、批次清单、来源映射、模型修改历史、各轮 findings 和 resolutions、发布报告，以及阶段和工具耗时。
- 调度日志记录 task、角色、phase、round、revision、dispatch ID、HC terminal ID、消息交付与领取状态，支持关联排查。
- 测试章节分批、超长 block、表格、附录、完整交付与覆盖计算；验证缺失来源和批次不能通过 gate。
- 测试越阶段调用、角色越权、过期 revision、重复操作、悬空和歧义引用，以及服务重启恢复。
- 使用模拟 HC 测试任务路由、领取确认、重复事件去重、忙碌时不重复派发、supervisor 接管及 HC done 不绕过 gate。
- 覆盖首轮通过、第二轮修订成功、三轮后部分发布、上限一轮、末轮硬性校验失败、核验中断及重试不增加轮次。
- 先用小型 RFC 和两个真实 Codex 会话验证完整交接，确认修订任务返回原 parser terminal；再用 FTP 两份 RFC 做全量验收。
- 性能比较固定模型和推理设置，统计各阶段、各轮耗时及调用量，不以速度收益放宽阅读和核验要求。
