# Rust 双层变异与 Step Trace 驱动执行

## 状态与目标

本文是 [Protocol name 引用与 proposal mutations](protocol-name-proposal-mutations.md) 的后续实施计划。基础结构已实施（见文末实施记录）；仍未完成的部分在验收记录中明确标出。

Rust fuzzer 负责 step 和 field 两级变异；generator 只按 trace 顺序执行 step instance、填充字段和完成协议计算，不读取或解释变异策略。同一个 generator 可以执行不同顺序、重复次数和字段取值的 trace，减少重新 synthesis 的次数。

当前 `GeneratorMutator` 主要修改 `GeneratorInput.params`，执行端仍以 params 驱动 generator。此次改造将完整 step trace 作为执行输入，并贯通 corpus 和 replay。

| 层 | 职责 |
| --- | --- |
| Coverage proposal | 提供基础 trace、step 能力集合、dependencies、global_context 和字段 mutations |
| Rust fuzzer | 消费 mutations，生成 mutable fields 描述，执行两级变异并保存最终执行 trace |
| Generator synthesis | 根据 step 定义、dependencies、global_context 和 mutable fields 生成可调度 handlers |
| Generator / Ctx | 按实例顺序执行、填充字段、完成协议计算，并按数据依赖保存和读取结果 |

## Proposal：Dependency 与 Global Context

Proposal 保留目标、party/channel 绑定、基础 trace、可实现的 step 集合、dependencies 和 mutations，新增 `global_context`。不为每个 step 另建前序消息上下文，也不单独声明“哪些值需要保存到 ctx”。

- **Dependencies** 统一表达 step 间的时序依赖和数据依赖。数据依赖描述来源输出、目标字段及必要的派生关系；纯时序依赖只约束顺序，不要求保存消息或字段。资源和协议状态要求也通过依赖表达。
- **Global context** 描述不依赖任何 step 的常量、执行环境输入、本地生成值及其派生关系。每个 testcase 初始化一次，需要重放的生成种子随输入保存。依赖 step 输出的数据仍放在 dependency 中，不移入 global_context。
- **Mutations** 提供字段可变异点和候选操作，供 Rust 消费。Step 插入、重复、删除和重排策略由 Rust 配置控制，proposal 不再声明 duplicate 次数等序列变异策略。

Step 能力集合与基础 trace 分开：集合可包含基础 trace 尚未使用的消息，synthesis 必须为集合内全部 step 生成 handler。Rust 首版只能插入已有 handler 支持的 step；扩展能力集合需要重新 synthesis。

Synthesis 根据数据依赖推导 ctx 读写，仅保存后续数据依赖需要的输出。纯时序依赖不产生数据保存要求；跨连接数据的来源和作用域也由 dependency 表达。例如，后续认证消息依赖前序 challenge，dependency 指明来源字段及派生关系，synthesis 据此保存 challenge 并生成消费者读取逻辑，无需额外的保存清单。

自然语言可以解释条件和派生算法，但 Rust 判断顺序所需的依赖与实例绑定必须机器可读。未知条件不能当作已满足，复杂协议计算由 handler 实现。继续用 name 引用定义与实例，不恢复摘要实体 ID。

## Rust 转换与执行接口

Rust 接收 proposal 后生成面向 synthesis 和执行的描述：

1. 解析 mutations，提取各 step 的 mutable fields。
2. 将变异操作、候选值和选择策略保留在 Rust 内部。
3. 剥离交给 synthesis/generator 的 mutations，替换为各 step 的 `mutable_fields`，仅声明可填充字段的路径、类型和默认取值来源。
4. 保留 step 定义、dependencies、global_context 和基础 trace，让 synthesis 实现字段填充与协议执行。

原始 proposal 保留用于交接追溯；剥离发生在 Rust 生成的下游描述中。Synthesis 不接收 mutations，生成的代码也不包含 duplicate 控制或字段变异算法。

执行入口改为：

```python
def generate(ctx, step_trace):
    for instance in step_trace:
        execute_step(ctx, instance)
```

每个 step instance 包含唯一实例名、step 引用、party/channel 绑定、具体数据依赖绑定，以及 Rust 决定的字段取值。字段取值属于实例，同一模型 step 的多个实例可以使用不同取值。Generator 不自行插入、重复或重排步骤。

字段填充规则：

- 有实例显式取值时，直接采用该值。
- 没有显式取值时，按 dependency 或 global_context 取得输入，再完成派生计算。
- 删除字段以显式的字段缺省表示，与“没有提供覆盖值”区分；这是最终字段状态，不是交给 generator 的 mutation 操作。
- 长度、校验值等自动派生字段根据最终输入计算；若 Rust 为这些字段提供显式值，则采用显式值，不能再次覆盖修复。

运行时才可取得的数据仍按 dependency 解析。首版不支持向 generator 下发“对未知响应值执行翻转”等变异指令；Rust 可以为相应目标字段提供独立的替代值。Generator 始终只处理取值、填充和协议计算，不理解变异方式。

Connect/listen/close 等网络 step 复用现有 transport 能力。模型中 SUT 发送的 step，在 generator 一侧对应接收和解析，不因此新增协议 action 类型。Handler 负责组帧，不能把一次 recv 直接视为一条完整消息。

## Ctx、实例绑定与重放

Ctx 以 testcase 为隔离边界，管理 global_context、连接资源，以及数据依赖要求保留的输出。无需为所有 step 保存完整消息历史；执行诊断与依赖所需数据分别处理。

- 输出按唯一实例名和连接生命周期存储。重复执行创建新实例，不覆盖先前结果，下游不能隐式读取“最后一条消息”。
- 重复块内部引用映射到对应副本，块外输入保留原绑定，已有下游引用默认不变。改变绑定必须由 Rust 明确生成并校验。
- 重复默认复用 party/channel，新连接由显式网络 step 建立。关闭或重连后，旧连接级状态失效；跨连接共享数据须由 dependency 明确声明。
- Handler 保存依赖需要的实际输出，记录解析失败、EOF、超时等结果；缺失输出不能被假定为已产生。

Corpus 保存最终 trace、字段取值、依赖绑定和必要的生成种子。Rust 的变异策略记录与 generator 执行输入分离。Replay 使用相同描述重新执行，服务端响应重新获取；实际解析及派生值记录在执行日志中，不承诺动态响应逐字节一致。

## Rust 两级变异与依赖策略

首版支持插入、重复单步或连续块、删除、重排，以及实例字段变异，两级操作可以组合。插入可以选择基础 trace 尚未使用、但已有 handler 的 step；新增实例获得唯一名称及独立字段取值。

Step 级采用可配置的默认策略：

- **90% 保持依赖**：变异后检查时序、数据和资源依赖；插入位置及数据绑定必须满足要求，没有可行候选时跳过该操作。
- **10% 尝试违反依赖**：允许破坏协议顺序或状态前提，不自动修复，并在 Rust 执行元数据中记录被违反的依赖。

比例指策略选择概率，分别统计候选、生成成功和实际执行数量。保持依赖模式只保证已建模的约束，不宣称生成序列必然完全符合协议。

实际执行仍需有效 handler、可用连接及可取得的必需字段。删除数据生产者后，Rust 若为消费者提供显式字段值，可以执行该实例；否则报告依赖缺失，不伪造 ctx 输出，也不能声称已向 SUT 发送非法消息。

重排需考虑共享状态冲突，不能仅检查是否存在直接依赖边。随机拓扑排序仅适用于已具体化且无环的实例依赖，不能直接把含循环和条件边的全协议图当作 DAG。

沿用单次重复上限 32、trace 长度上限 4096，以及现有 testcase 超时和资源清理机制，避免重复造成无界执行或状态积累。

## TLS 场景中的上下文与字段填充

保留 TLS 1.3 PSK resumption 作为复杂数据依赖的设计示例，不作为首版必须实现的协议能力，也不表示现有 OpenSSL campaign 已支持该流程。

```text
bootstrap_conn:
  connect → 完整握手 → recv NewSessionTicket → close

resume_conn:
  connect → send resumption ClientHello → recv server message → 后续处理
```

“完整握手”是说明用缩写，实际 trace 应展开对应消息。恢复 ClientHello 对首次握手秘密值、ticket/nonce 等数据的需求通过 dependencies 表达，synthesis 据此生成保存和读取逻辑。无 step 依赖的配置与本地生成输入放入 global_context，不另建一份前序消息上下文。

| 场景 | 依赖与执行含义 |
| --- | --- |
| 多连接恢复尝试 | 明确 ticket 来源，各连接保持独立状态，允许声明的跨连接数据引用 |
| HRR 后的 ClientHello | 通过 dependency 读取实际 HRR，handler 根据最终字段完成相关派生计算 |
| 同连接重复 ClientHello | Rust 插入新的实例，作为可能违反时序的状态机测试，不因连接存在就判定协议合法 |

若 Rust 选择让 supported_groups 缺省，trace 表达最终字段状态，handler 按该状态构造消息并计算未被显式覆盖的长度和 binder。若 Rust 同时提供 binder 的显式错误取值，handler 直接采用，不再修复。删除、重复及字段值选择均发生在 Rust，generator 无需知道这些值来自哪种变异。

服务端的 HRR、ServerHello、alert、断连或超时需关联到实际实例与连接。协议拒绝与 generator 实现失败应分别记录。

## 实施阶段与验收

实施顺序：

1. 更新 proposal 类型和 Python/Rust 校验，以 dependencies 表达 step 上下文，增加 global_context，并调整 coverage 提示词。
2. 实现 Rust 对 mutations 的消费和剥离，生成包含 mutable fields 的 synthesis 描述。
3. 更新 synthesis 提示词、Ctx 和产物校验，实现依赖驱动的字段保存、读取及填充，并为能力集合生成全部 handlers。
4. 贯通 GeneratorInput、执行请求、Python server、corpus、replay 和 evaluation 的 step trace 输入。
5. 将重复控制迁入 Rust，加入其余 step 操作和实例字段变异，更新示例与验证用例。

新格式显式版本化。旧 params-only generator 和 corpus 不自动推断迁移，加载时明确提示重新 synthesis；环境配置继续由执行环境提供。

验收重点：

- 数据依赖正确导出、保存和读取字段；纯时序依赖不产生消息缓存要求。
- Global context 正确提供常量、环境和不依赖 step 的生成值，testcase 间状态隔离。
- Synthesis/generator 输入不包含 mutations 或变异策略，只包含 mutable fields 描述及执行所需信息。
- Generator 按实例顺序执行，采用 Rust 提供的字段值；字段缺省不被补回，显式派生字段值不被重算覆盖。
- 插入、重复和字段变异正确组合，重复块绑定及跨连接作用域正确。
- 保持依赖与违反依赖两类策略可区分，无法执行的缺失依赖有明确结果。
- Corpus replay 保持顺序、字段值、生成种子和依赖绑定，动态响应重新获取。
- 短程 LightFTP campaign 跑通 proposal → synthesis → 双层变异 → 执行 → corpus replay，确认序列变化无需重新 synthesis。

记录新覆盖率、实际执行比例、吞吐、超时/依赖缺失/generator 失败比例和 synthesis 次数，避免仅因生成更多无法执行的序列而误判收益。本计划不自动启动一小时 campaign，也不扩大先前清理摘要实体 ID 的范围。


## 实施记录（2026-11-10）

以检查点 `bf3feb3a` 为基线完成以下范围：

- Proposal：新增 `global_context`、实例级 `dependencies`、trace step 的 `mutable_fields`；Python coverage proposal 构建/校验和 Rust `Proposal` 校验均支持。`Proposal::synthesis_copy` 剥离 `mutations` 并生成下游 `mutable_fields`。
- Rust mutation：`GeneratorInput` 升级为 `version: 3`，新增 `TraceInstance`、`step_trace`、`global_context`、`DependencyInstance`；`GeneratorMutator` 支持实例字段变异、字段显式缺省、插入/重复、删除、重排和基础依赖检查；90% 保持依赖、10% 允许破坏并记录 `broken_dependencies`。
- 执行：`generate(ctx, step_trace)`；step instance 携带 Rust 决定的 `fields`、`omitted_fields`、dependency `inputs` 和 provenance；`GeneratorInput::run_request`、Python server/standalone、evaluation replay 贯通 `step_trace`。
- Ctx：`ctx.field()` 按显式值、显式缺省、dependency、global_context 顺序解析；`ctx.save_state()` / `ctx.load_state()` 按实例和字段保存 states；缺省不补回，缺失依赖报错。
- Corpus/replay：输入 JSON、fingerprint、evaluation input、standalone 请求都保存/重放 trace、字段值、global context 和依赖绑定；旧 params-only JSON 在 `GeneratorInput::from_file` 明确报错，要求重新 synthesis。
- 兼容说明：旧 params-only generator 的 Python server/artifact 入口仍可用于本地直调测试，但 Rust 执行输入必须是 version 3 TraceInstance。

验证：

- `cargo test --lib`：137 passed；`cargo test --test coverage_mcp`：1 passed。
- Python unittest：53 passed。
- `cargo check --all-targets`、`cargo build --release` 通过。
- 单元测试覆盖 proposal synthesis copy、global/dependencies 校验、step_trace round-trip/legacy 拒绝、字段/step mutation、artifact `mutable_fields` 默认值、Ctx field/dependency 解析、server step_trace。
- 短程 LightFTP `test-results/trace2`：synthesis 发布了 `generate(ctx, step_trace)` generator；autonomous candidate 执行了 baseline，step 级变异生成 6 个实例并进入 corpus，coverage replay 运行。首次 coverage analysis 因多个 message 复用同一 `ftpserv.c::ftp_client_thread` handler 被误判为重复名而失败；已修复 `handlers.py` 按符号/位置允许同一 handler 被多个 message 引用，并对真正不同身份的冲突继续报错。

未完成/限制：

- `global_context` 的 `generated` 值目前由 proposal 提供具体值/seed，Rust 尚未生成派生值工具。
- 依赖策略是首版基础检查；条件边、循环和跨连接作用域尚未完整求解。
- 未运行一小时 campaign；短程 coverage 阶段被上述既有 handler mapping 重复名阻塞。


## 实施记录补充（TraceInstance 版）

- Rust 执行输入升级为 `version = 3`，新增 `TraceInstance`：
  - `PartyInstance { name, type }`
  - `ChannelInstance { name, type, endpoints[] }`
  - `StepInstance`（引用 channel 和 party instance，不保存 connection）
  - `DependencyInstance`（prior/dependent step instance、kind、condition、field bindings、origin）
- `TraceInstance` 与 model 的绑定关系保持同构：Channel.start/endpoints 对应 party instances；Step 的 channel/party 字段对应 channel/party instance；Dependency 的 prior/dependent 对应 step instances。
- proposal dependency 在 `GeneratorRequest::accept` 中实例化为 `DependencyInstance`，字段 binding 同时写入 `StepInstance.inputs`。
- mutator 支持连续 block duplicate：唯一重命名、内部 dependencies/inputs 重映射、外部引用保留；复制后可在同 channel 的兼容 prior 上新增 `origin = "mutation"` 的 `DependencyInstance`。
- `Ctx.outputs` 改名 `Ctx.states`，`save_output/load_output` 改名 `save_state/load_state`；states 从出现保留到 generator 结束，关闭连接不清理。
- `available_steps` 已删除；不构造基础 trace 未出现的 model step。
- 当前验证：Rust `137 passed`；Python `53 passed`；`cargo check --all-targets`、`cargo build --release` 通过。
- 未完成：完整条件依赖求值、循环拓扑排序、复杂跨连接策略；未跑一小时 campaign。


## 60 分钟 LightFTP 验证（trace61）

- 命令：`sudo -n -E ./target/release/mcn-fuzz run --campaign test-results/trace61/campaign.toml`
- 实际运行：1h 1m 11s；`executions = 12522`，`corpus = 103`，`crashes = 0`。
- coverage map occupancy：`158/960`（16%）；最终 source coverage：`209/720` branches，`626/1790` lines。
- synthesis：6 个请求（1 autonomous + 5 coverage proposal），全部 `result.status = ok`；coverage analysis 5 个 proposal 被接受，7 个 snapshot。
- trace instance：corpus 103 个输入全部 `version = 3`；step trace 平均 35.6，范围 7..154；dependency 共 1913，其中 `origin = mutation` 1476，`origin = proposal` 437。
- 本 run 的所有 dependency 都是 `order`；没有产生 `data` dependency，因此没有在真实 SUT 上验证 field binding 路径（单元测试覆盖 `Ctx.field`/`inputs`）。
- generator status：`ok = 10030`、`timeout = 1862`、`generator = 528`、`dependency = 102`。`dependency` 来自显式 `omitted_fields` 的实例，符合“缺省不补回”的语义。
- 首个 synthesis candidate baseline 在 15:21:48 执行一次且未进入 corpus；corpus 首个 trace 已经是带 mutation dependency 的副本。
- 本轮真实运行验证了 block duplicate、唯一实例名、mutation-origin order dependency、TraceInstance v3 持久化和 coverage replay；未验证 data dependency 的真实 SUT 路径。
