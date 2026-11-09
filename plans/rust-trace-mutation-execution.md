# Rust trace 变异与可调度的协议生成器

## 状态与目标

本文记录讨论中的设计方向，作为 [Protocol name 引用与 proposal mutations](protocol-name-proposal-mutations.md) 的后续方案。接口示例用于表达职责，尚不是已经实现或冻结的 schema；待确定事项列在文末。

目标是让 synthesis 根据 proposal 生成可复用的 step 实现，Rust 在其支持的范围内变异执行序列和字段参数。一个 generator 可以执行多个 trace，减少每次序列变化都重新 synthesis 的成本。

最近 LightFTP 一小时运行中，7 次完成的 synthesis 从 proposal 复制到生成器发布，按产物时间戳估算平均耗时约 4 分 20 秒，中位数约 3 分 58 秒，范围约 2 分 30 秒至 7 分 15 秒。序列变异的预期收益是复用这些生成结果；是否提高探索效率仍需对照实验验证。

## 当前限制

- Rust 的 `GeneratorMutator` 目前主要修改 `GeneratorInput.params`；执行输入主要是 generator 路径和参数。仅修改 proposal trace，不会使已生成 Python 代码自动改序执行。
- Protocol dependency 保存前后 step、自然语言 condition 和 field relations。它还不能直接成为 Rust 可执行的完整约束，尤其是条件依赖、循环和重复 step 的实例绑定。
- 当前 `Ctx` 管理连接、监听器、收发缓冲及字节计数；`recv()` 返回的数据没有自动登记为带来源的协议消息。
- 现有 generator 可以自行维护协议状态，但这些状态和操作尚没有统一的可调度接口。

## 分工与执行方式

| 层 | 职责 |
| --- | --- |
| Coverage analysis | 选择探索目标、基础 trace、必要依赖和允许的字段/序列变异，给出源码与覆盖率依据 |
| Synthesis | 实现消息构造、编码、接收、组帧、解析和状态更新，生成可按 trace 调用的 step handlers |
| Rust | 变异执行输入的 trace 和 params，检查可机器判断的约束，执行后依据反馈保留 corpus |
| 通用执行器 / Ctx | 调度实例、管理资源与消息历史，向 handlers 提供当前连接和协议状态 |

概念接口如下，具体签名和传输方式待定：

```python
def generate(ctx, params, trace):
    for instance in trace:
        execute_step(ctx, instance, params)
```

`execute_step` 是分派入口，各种消息由协议专用 handler 实现；connect/listen/close 等操作可复用已有 transport 能力。模型中 SUT 发送的 step，在 generator 一侧对应接收、解析和记录，不要求为了这一执行视角新增 protocol action 类型。

Rust 只能调度 generator 已实现的操作及其支持的 party/channel 绑定。首版建议限定在一个 proposal 的能力集合内；引入新消息或新连接行为时，再扩展或重新生成 generator。无需一开始就要求每个 generator 覆盖整个协议。

被变异的是执行输入，而不是冻结的 proposal。变异后的 trace 和参数需要随 corpus 保存并通过运行接口传入 generator，确保重放使用相同的变异描述；接收数据等动态结果在重放时重新获取。

## Proposal 应表达的内容

| 字段 | 作用 |
| --- | --- |
| `goal` | 目标 handler、路径及源码/覆盖率依据 |
| `parties` | 参与者实例及模型 party 引用 |
| `channels` | 通道实例、端点和连接角色 |
| `steps` | generator 必须实现的操作集合，以及输入、输出、状态要求 |
| `trace` | 基础有序 step instance 序列 |
| `dependencies` | 实例之间的资源、数据和状态依赖 |
| `mutations` | 字段变异与允许的序列变异范围 |

继续以 name 引用定义和实例，不恢复实体摘要 ID。`steps` 描述能力，`trace` 描述某次执行；实例名和其引用的模型 step name 分别对应不同对象。

每个 step 实现契约需要说明输入来自 params、环境还是前序输出，哪些输出写入 ctx，以及执行前后状态如何变化。报文编解码与密码学实现由 synthesis 完成，不要求 coverage analysis 提供代码。

自然语言 condition 可以作为说明，但不能成为 Rust 判断合法性的唯一依据。Coverage/synthesis 需要将本次选定分支的必要依赖具体化；未知条件不应被当成已满足。

## Ctx 的消息与状态管理

建议在现有 transport 能力上增加以下概念，不必立即将其固定为具体类结构：

```text
ctx
 ├─ parties      参与者实例与状态
 ├─ channels     通道实例、连接资源和协议状态
 ├─ executions   每次 step 执行及其输入、输出和结果
 └─ messages     收发消息的原始字节、解析字段和来源
```

- 每次执行单独记录。重复 step 不能覆盖先前结果；下游必须能够定位所依赖的 channel、step instance 和具体执行轮次。
- 一次 recv 不等于一条消息。协议 handler 负责组帧和解析，再登记消息；同时保留部分数据、EOF、超时或解析失败的实际结果。
- 数据绑定与字段变异有明确先后关系：先解析前序输出并构造消息，再应用变异，按声明更新或故意破坏长度、校验和等关联字段。
- 通道状态包括连接是否有效，以及协议需要的认证状态、密钥、transcript、序号等。关闭或重连后，不能将旧连接的状态无条件继承到新连接。
- 状态以 testcase 为边界隔离；需要有消息总字节、执行次数和资源数量限制，不能让重复变异造成无限积累。
- 一个通道应明确读写所有权。若 handlers 使用并发辅助服务，执行器需要与其协调，不能仅靠共享字典保证正确性。

## 依赖与变异规则

需要区分两类约束：

1. **执行所需的依赖**：连接存在、必需的数据已经取得、引用的输出属于正确连接/轮次。这类依赖保证生成器有能力执行该操作。
2. **协议合法性约束**：消息允许的时序、扩展配对、字段一致性等。默认可保持，但 proposal 可以明确指定其中某些规则为本次有意违反的目标。

例如前面发生过 login，并不代表另一条新连接已经认证。重复模型 step 后，依赖必须落到实际实例与执行轮次；仅检查名称的前后位置不足以保证状态正确。

首版候选操作：

- 交换：调整确认独立的步骤；除了没有依赖路径，还需排除共享状态冲突。
- 重复：复用已有数组/区间语义。`["s2", "s4"]` 分别原地重复；`"[s2:s4]"` 整体重复包含端点的连续区间。
- 删除：仅删除声明为可选、且删除后仍满足保留步骤依赖的部分。

重复副本默认复用 party/channel。新建连接必须通过显式实例和网络操作表达，不由 duplicate 隐式克隆。重复后如何绑定前序消息和状态，需要按操作语义确定，不能一律引用“最后收到的消息”。

随机拓扑排序只适用于已经具体化且无环的实例依赖。不能将包含循环、条件边的全协议图直接当 DAG，也不能通过自动修复把本次有意制造的非法字段或序列恢复成合法输入。

## TLS 1.3 PSK resumption 示例

本节以 session ticket 恢复为例，展示设计需求。它不是对现有 OpenSSL campaign 已支持 TLS 1.3 恢复的声明。

### 基础交互与执行状态

```text
bootstrap_conn:
  connect → 完整握手 → recv NewSessionTicket → close

resume_conn:
  connect → send resumption ClientHello → recv server message → 后续处理
```

“完整握手”是说明用缩写，实际 trace 应展开对应消息。服务端在两次连接间必须保留可用的 ticket 状态；单次连接后退出、禁用 ticket 或仅启用 TLS 1.2 的配置不能直接承担此场景。

Ctx 需要保存首次握手的秘密值及收到的 ticket/nonce 等信息，以获得恢复 PSK；不能只复制 ticket 字节。恢复 ClientHello handler 从这些状态取输入，完成编码、binder 计算并记录实际发送内容。参见 [RFC 8446 §4.6.1](https://www.rfc-editor.org/rfc/rfc8446.html#section-4.6.1)。

### 多个 ClientHello 的不同含义

| 场景 | Trace 概念 | 执行要求 |
| --- | --- | --- |
| 多条连接上的尝试 | conn1: CH₁ → response；conn2: CH₂ → response | 每条连接独立维护 transcript/密钥，明确 ticket 来源与连接实例 |
| HRR 流程 | CH₁ → HRR → CH₂ → response | CH₂ 根据实际 HRR 构造，处理指定 key share 和 binder |
| 同连接额外重复 | CH₁ → CH 副本 → CH 副本 → response | 作为有意重复消息的状态机测试，不因满足 connect 前置条件就宣称合法 |

HRR 后发送的 CH₂ 不是普通字节副本；其消息和 binder 需满足对应的更新规则。参见 [RFC 8446 §4.1.2](https://www.rfc-editor.org/rfc/rfc8446.html#section-4.1.2) 和 [§4.2.11.2](https://www.rfc-editor.org/rfc/rfc8446.html#section-4.2.11.2)。

### 缺少 group 的不同变异

| ClientHello 变化 | 应区分的行为 |
| --- | --- |
| `psk_ke`，同时省略 supported_groups/key_share | 可以是纯 PSK 模式，接受与否取决于服务端 |
| `psk_dhe_ke`，有 supported_groups，key_share 列表为空 | 可以触发 HRR 选择 group |
| 保留 key_share，删除 supported_groups | 有意破坏扩展配对要求 |
| 保留 supported_groups，删除整个 key_share 扩展 | 不等同于空 key share 列表，同样破坏配对要求 |

参见 [RFC 8446 §4.2.8](https://www.rfc-editor.org/rfc/rfc8446.html#section-4.2.8)、[§4.2.9](https://www.rfc-editor.org/rfc/rfc8446.html#section-4.2.9) 和 [§9.2](https://www.rfc-editor.org/rfc/rfc8446.html#section-9.2)。

如果探索目标是删除 supported_groups，建议保持其他关联项正确：先构造正常 ClientHello，再删除扩展、更新长度、重算 binder，最后发送。否则会同时引入 binder 错误，难以解释命中了哪条路径。错误 binder 本身可以作为另一项明确变异。

记录服务端实际返回的 HRR、ServerHello、alert、断连或超时，并保留它们所属的连接与执行轮次。协议拒绝不等于生成器实现失败，也不应为了保证握手成功自动补回被删除的扩展。

## 实施建议、验证与待定事项

建议先建立 proposal 能力集合、step 输入/输出契约和执行输入中的 trace，再把已有 duplicate 迁入 Rust 控制；随后评估受约束的交换、删除和更广的序列生成。

验证应包括：实例/轮次引用正确、组帧结果保存、跨连接状态隔离、重复块的数据绑定、缺失依赖处理、重放一致性、有限资源清理，以及 TLS 的正常恢复/HRR/故意非法消息三类场景。

收益用相同墙钟时间的对照实验判断：新覆盖率、目标步骤实际执行比例、吞吐、超时/生成器失败比例，以及 synthesis 调用次数。保留字段变异基线，避免仅因更多序列被拒绝而误判效率。

正式实施前还需确定：

- trace 从 Rust 传入 Python 的接口、corpus 存储格式及旧输入兼容策略。
- step 能力描述、可执行条件和依赖绑定的机器可读格式。
- 重复执行、跨连接操作的输出引用和状态作用域。
- 字段变异、依赖重算、密码学计算与故意破坏的执行顺序表达。
- 首版开放的序列操作、资源上限，以及合法性约束的可违反范围。

本文件不扩大先前清理摘要实体 ID 的范围；TLS transcript hash、binder 和密钥派生是协议执行所必需的密码学计算。
