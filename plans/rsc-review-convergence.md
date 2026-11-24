# RSC Review 收敛优化计划

## 目标

使 document-based review 按现有协议模型的语义及表达边界检查原文，避免因概念理解不同反复要求错误类别承载内容。将审查改为全局阶段开始时的串行逐 batch agentic loop，查询模型、记录 gaps，再逐 batch 交给 Fixer 补充。本计划替代旧的类别阶段并行文档审查流程。

## 第一项：Review 提示词介绍各个实体及关键属性

在 Review 提示词中介绍各类实体是什么、各关键属性表示什么，以及它们如何表达原文中的协议要求。不能只列类别名称，也不能依赖 Reviewer 从输入样本自行推断语义。

这些是模型概念说明，不要求 RFC 使用相同的属性名、hook 名或程序变量标识。Reviewer 应检查原文要求是否在模型中得到适当表达，而不是在原文或模型中寻找预设术语。State 名称可以是对原文明示且需要跨步骤保留的数据的规范命名；processing hook 是对原文计算、验证和更新时机的映射，不要求每条消息都有所有 hook。

### 1. 六类实体

| 实体 | 关键属性 | 需要介绍的含义 |
| --- | --- | --- |
| Party | `name`、`description`、`alias` | 直接参与通信的角色名称、职责和等价角色名称；区分角色、软件容器和运行时绑定。 |
| Channel | `name`、`description`、`transport`、`connection`、`endpoint_party_names`、`initiator_party_name`、`acceptor_party_name`、`endpoint_sources` | 通道名称与生命周期、底层传输、连接类型、端点角色、连接发起/接受角色和端点参数来源。连接发起方不等于每条消息的发送方。 |
| Message | `name`、`associated_fields[].name` | 实际传输的消息类型及其携带的字段，包括构造、解析、计算和验证所需字段。字段名支持稳定嵌套路径；语法节点、抽象分类和局部计算值不自动构成消息或字段。 |
| State | `name`、`party_name`、`source` | 跨步骤使用的数据名称、持有角色和生产来源；`processing` 表示本地处理产生，`external` 表示外部输入。State 不是全协议有限状态机，也不直接存放计算算法。 |
| Step | `name`、`action`、`channel_name`、`condition`；send 的 `message_name`、`sender_name`、`receiver_name`、`processing`；网络动作的 `party_name` | 可重复实例化的动作模板。解释 `send/listen/connect/close` 的含义、执行角色、通信双方、通道和处理过程，区分协议终止消息与实际关闭连接。 |
| Step dependency | `prior_step`、`dependent_step`、`condition`、`field_relations` | 对先前步骤发生及其结果的依赖，不要求相邻；表达前置条件、分支、顺序和跨消息数据关系。不能仅因两个动作存在便推断依赖。 |

### 2. 关键属性的嵌套结构

- **Endpoint source**：解释 `party_name`、`parameter`、`from`，说明哪个角色的哪个端点参数来自消息字段、常量或外部配置。
- **Processing hooks**：`before_send`、`after_send` 属于发送方，`on_receive` 属于接收方。说明构造、发送后保存状态、接收解析与验证分别适用的时机；一个 send 模板可以同时包含双方处理。
- **Processing operation**：`reads` 是具体计算、验证的输入；`writes` 是持久化状态或待发送字段的输出；`rule` 描述计算、条件、校验结果与失败处理。验证操作允许没有写入结果。
- **数据引用**：解释 `state.<name>`、`message.<field>` 和 `message_bytes` 的含义、角色归属与可用时机。历史消息的数据通过先前保存的状态使用；编码字节与语义字段不能相互混淆。
- **Condition**：Step 条件是所有实例共同适用的条件；Dependency 条件描述特定依赖、场景或分支。默认、可选、必须和禁止行为按原文条件表达，不能任意增强为无条件前置关系。
- **Field relation**：解释 `prior_field`、`dependent_field`、`derive`，区分字段直接复用、字段派生与需要 processing 承载的多输入或本地状态计算。
- **Evidence**：解释文档、block、章节及范围定位，`field` 对应被支持的实体属性，`note` 解释证据支持的结论。证据原文、提取说明和实体实际定义是不同内容；证据登记本身不能替代最终模型需要的 processing 或依赖。
- **名称与引用**：解释 Party/Channel/Message/State/Step 之间的名称引用，State 按角色区分；注册表实体 ID 用于管理和定位，不是 RFC 的协议字段。说明注册表 `aliases` 与 Party `alias` 的不同含义。

### 3. 明确审查边界

- 审查发生在全部六类提取完成后，向 Reviewer 明确当前 batch 检查全部六类实体。
- State 核验状态概念、归属、来源以及当前结构可承载的信息；状态计算、更新、校验的执行规则应落在 Steps 的 processing，不要求在 State 中添加不存在的属性。
- Step 检查处理规则、读写和计算输入输出；Step dependency 检查先前交换、条件与字段关系，避免将同一原文要求错误地强加给其他类别。
- 对于先前类别中已存在的实体，检查是否实际遗漏或错误，不能只因概念的细节不位于预期类别就报告缺失。
- 明确默认值、条件及约束在现有模型中的承载位置，不引入 `states.scope` 或未经确认的实体字段。

## 实现约定

- 第一项修改 Review 提示词及其必要的输出说明；复用已有概念定义，与 Scan/Fix 保持一致。流程和工具改造见第二项。
- 复用 `rsc/rsc/protocol/rules.py` 和现有模型字段说明，接入 `rsc/rsc/extract/review.py` 的实际 Agent instructions；不能只增加未被调用的文档或常量。
- 同步输出类别的 schema description，使其与提示词范围及含义一致。若修改共享模型字段的 description，同步发布 schema，保持结构约束一致。
- 说明使用通用协议概念，不使用特定协议或消息作为示范，不扩展协议实体 schema。

## 第二项：串行 Agentic Review 与逐 Batch Gap Fix

### 1. 调度流程

```text
六类 Scan + 类别确定性 Fix
  → 全局阶段开始
  → Review batch 1 → Fix 当前 batch gaps → complete_gaps()
  → Review batch 2 → Fix 当前 batch gaps → complete_gaps()
  → ……全部 batch 完成
  → 全局确定性校验及必要的全局 Fix → 输出
```

- 移除后三个类别中原有的并行文档审查；按冻结的 batch 顺序审查一次全部文档。
- Reviewer 改为可调用工具的 agentic loop，每个 batch 内可以多次查询、回查和记录 gap，读完当前 batch 后提交审查完成。
- 同一时刻只运行 Reviewer 或 Fixer 中的一个，不并行启动多个 batch 的 Review，也不将全部 batch 的 gaps 累积后一次性交给 Fixer。
- Reviewer 和 Fixer 使用独立上下文；切换到下一 batch 时重建上下文，查询最新注册表，避免沿用修改前的实体快照。
- Fixer 完成后直接推进下一 batch，不让 Reviewer 重新审查当前 batch，也不在最终确定性校验后再次发起文档审查。
- 模型/provider/effort 继承现有 Scan/Fix 配置。Review API 调用保留代码限速，默认 5 次/s，包括重试。

### 2. Review 工具与上下文

- 初始上下文包含实体及关键属性概念说明、当前 batch 完整原文和定位、当前 batch 已记录的 gaps；实体通过工具按需查询。
- 扩展现有 `list_entities` 的 batch 筛选能力，复用 evidence 与 batch 的关联逻辑查询 entries；支持按类别分页，返回关联实体的完整定义和 fields。
- 复用 `get_entity`、`search_entities` 跨 batch 查询已有实体，复用 `read_source` 回查章节。Reviewer 在判断缺失前应先检索已有定义，不能只根据局部列表下结论。
- 审查所用实体信息保留 evidence 的定位、属性路径和 note，不自动重复展开证据原文，不附带操作历史、完整引用边或无关 unresolved；额外原文按需查询。
- 新增 `record_gap(kind, name, evidence_text, missing_attributes=None)`：记录实体类别、对象或关系名称、支持缺口的证据文本，以及可选的未覆盖关键属性及说明。
- 宿主附加当前 document/batch 及匹配证据定位，核验证据文本来自当前 batch；相同内容重复提交不重复新增。不要求 Agent 填写额外全局 gap ID。
- 新增 `complete_review()`：表示 Reviewer 已完成当前 batch 的审查；允许零 gaps 完成。宿主保存结果后，无 gaps 直接推进，有 gaps 切换至 Fixer。
- Reviewer 只能查询实体和来源、记录 gaps、完成审查，不能调用实体修改或 Fix 完成工具；接口入口也要检查当前角色和阶段。

### 3. Fix 与无参数完成工具

- Fixer 获得当前 batch 原文、全部 gaps、相关实体和证据说明，允许通过现有查询及修改工具补充全部六类实体。复用实体关联索引组装上下文，不靠 description 全文匹配定位。
- Fix 提示词要求尽力完成当前 batch 的全部 gaps，并保存有效的原有内容；不要求逐条提交处理回执。
- 新增 **`complete_gaps()`，无任何参数**。含义是 Fixer 认为当前 batch 的补充已经完成。
- 宿主等待正在进行的修改结束，保存注册表及当前 batch 的补充完成状态，然后推进下一 batch。不要求传递 summary、gap 列表、实体引用或逐条解决说明。
- `complete_gaps()` 不是独立语义验收，也不把 Agent 的判断标记为经过 Reviewer 复审。无需强制所有确定性问题当场清空；全部 batch 完成后统一做确定性校验。
- 正常类别和全局确定性 Fix 继续使用现有 `complete_fix`；`complete_gaps` 仅供逐 batch 补充阶段使用，禁止互相替代。
- 超时、预算耗尽或 Agent 未调用完成工具时，保留未完成状态和已提交进度；不得沿用部分结果自动封存并跳过当前 batch 的处理。

### 4. 命名、恢复及日志

- 当前实现中的 `uncovered` 类型、变量、提示词、工具返回、统计和新检查点字段统一改为 gap/gaps；历史运行产物不批量改写。
- 保存 batch 游标、当前 Review/Gap Fix 阶段、已记录 gaps、注册表和完成状态。恢复时继续当前阶段，不重复跳过未完成 batch，不因完成工具重试推进两次。
- 完整记录 Review 和 Gap Fix 的 LLM request/response、工具调用及返回。记录各 batch 审查/补充耗时、请求数、token 和 gap 数量。
- 统一复用原有配置、JSON 检查点、来源索引、模型日志和查询/修改实现，不引入数据库、hash、实体 revision 或额外工作流框架。
- 最后沿用现有全局确定性校验次数限制和发布规则，最终产物仍为 `protocol.json`、`unresolved.json`。

## 验证

- 检查实际记录的 Review request，确认包含六类实体及上述关键属性、嵌套结构和当前阶段边界的说明，而不仅是类别名称。
- 确认提示词不要求 RFC 出现相同的 hook 名或 State 变量标识，不把缺少预设术语或空 hook 本身判为遗漏。
- 检查 Scan/Fix/Review 以及工具 schema 的语义一致性，尤其是 State 与 processing、Message 与 send、Channel 与网络动作的区别。
- 使用真实 FTP/TLS 的既有重复 gap 问题做针对性复核，检查不再要求 States 直接表达算法；保留对真实缺失实体的报告能力。
- 验证 Review 可多次调用查询和 record_gap，能跨 batch 找到已有对象，证据定位正确，零 gaps 的 batch 可以直接推进。
- 验证所有 batch 严格串行、Fixer 完成当前 batch 后才审查下一 batch、每个 batch 均有完成记录；不恢复旧的每轮全量重复审查。
- 验证 `complete_gaps()` 无参数，无需逐条回执，重复调用不会重复推进；工具角色限制、超时、失败和中断恢复正确。
- 验证全部 batch 完成后统一执行确定性校验，仍有结构或引用问题时进入现有全局 Fix，不执行额外文档审查。
- 运行相关现有测试。报告提示词改动和复核结果，不将提示词已更新等同于完整提取或收敛效果已得到证明。
