# Protocol trace、常驻 coding agent 与 generator 合成 stage

## 1. 总体结构与已确定行为

新增可选的协议驱动 generator 合成功能，分为三个模块：

| 模块 | 职责 |
|---|---|
| `fuzz/src/protocol/` | 读取模型、建立引用索引、计算 ID、选择 trace、生成 receipt |
| `fuzz/src/agents/` | agent 配置、启动参数适配、HC 通信与会话状态 |
| `fuzz/src/engine/synthesis/mod.rs` | LibAFL stage、生成任务、产物接收与 input 评估 |

`agents` 不依赖协议模型；选择策略不依赖 agent；stage 负责连接各组件。

采用已确认的行为：

- trace 是需要完成的目标步骤序列，agent 可以补充连接、登录等前置步骤。
- generator 异步生成，同一时刻最多一个任务在途。
- schema 校验初始参数，后续沿用现有参数 mutator。
- campaign 明确绑定 party；第一版继续使用单 SUT executor。
- 生成失败记录原因，已有 corpus 继续 fuzz。
- 新 input 使用现有覆盖反馈决定是否进入 corpus，不强制入队。

配置未启用时不读取 protocol、不启动 HC、不创建 agent。功能首先接入 `run`；普通 replay、accept 和 benchmark 保持原有运行流程。

## 2. 模型、稳定 ID 与 receipt

### 模型读取

以当前输出的 schema `7.0.0` 为依据，建立 Rust 类型及 name 索引，保留原始 step `id` 作为 `source_id`。

启动时读取并保存一份模型快照，整个 campaign 使用该快照。检查名称唯一性，以及 step、channel、party、message、dependency 的引用完整性；错误在执行 case 前报告。允许 `meta.complete=false` 和已有 unresolved 项。

依赖按模型中的 **step name** 解析。依赖可能包含环和自依赖，第一版不把它强行转换成 DAG。

### Hash

使用共享的 SHA-256 实现：

- `StepId`：对 step 的 `name、message_name、channel_name、sender_name、receiver_name、condition` 按固定字段顺序、长度前缀编码后计算 hash；加入 `mcn.step.v1` 域标识。
- 排除原始顺序编号 `source_id`，避免仅重排编号就改变 ID。字符串按原始 UTF-8 编码处理。
- `TraceId`：对 `mcn.trace.v1 + step数量 + 按顺序排列的完整step hash` 计算 hash，再截短输出。步骤顺序和重复次数均参与计算。
- 单独计算完整模型的 `ProtocolId`，用于区分 message/channel 等定义变化；JSON 对象键排序后编码，数组保持原序。

`ProtocolId`、`StepId` 和 `TraceId` 对外使用 SHA-256 十六进制字符串的前 8 位；不使用 Rust `DefaultHasher`。

### 选择策略

提供 `TraceSelector` 接口，输入包括模型索引、LibAFL RNG 和只读的选择上下文，输出有序 `StepTrace`。

选择上下文可访问依赖索引、已尝试 trace 和选择统计，未来可以接入实际 step/channel 覆盖数据。第一版只记录选择及生成结果，不把“选中”记作“已经执行覆盖”。

初始随机策略：

- 从可执行的 steps 中无放回随机选取，默认长度 `1..=8`，受可用数量限制。
- 保持选中 steps 在模型中的相对顺序。
- trace 类型允许未来表达重复步骤。
- campaign 将 party 绑定为 `sut`、`generator` 或 `unavailable`；仅选择参与角色及 channel 端点均可承载的步骤。
- 每个 trace 在同一 campaign 内最多派发一次；寻找新 trace 的采样次数有界，找不到时本轮跳过。

### receipt

每次派发写出独立 `receipt.json`，包含：

- `ProtocolId`、`TraceId`、有序 steps 及其 `StepId/source_id`。
- 引用的 message 定义。
- 引用的 channel，以及 step 和 channel 涉及的完整 party 集合。
- 两端均属于目标 trace 的依赖，以及相关 unresolved 信息。
- party 绑定、完整模型的 `model_path` 引用及 RFC/source 文档的位置；不内嵌完整模型。

定义集合按名称排序，steps 保持 trace 顺序。receipt 明确表示“目标 trace”；agent 补充的步骤记录在生成结果中，不改变目标 trace ID，也不宣称它就是实际运行时 trace。

## 3. Agent 配置、HC 封装与生命周期

### 配置和操作解耦

`AgentProfile` 包含：

- `provider`：`codex | claude | pi`。
- `executable`、可选 `model/effort`、附加 `args` 和 `env`。

三个小型启动适配器将 profile 转为统一的 `argv + env + cwd`。API 后端通过对应工具原生参数或环境配置，不增加统一后端抽象。Codex 使用交互模式并带 `--no-daemon`；各工具均不采用一次性 print/exec 模式。

`HcClient` 统一封装启动、派发 prompt、inspect、读取输出、interrupt 和关闭操作，业务任务不感知 CLI 品牌。

Rust 使用 HC 官方的 Unix socket、逐行 JSON-RPC 接口，直接传递 argv/env，并设置有界 I/O；HC CLI 仅用于首次启动私有 core。连接失败不会隐式启动替代 core。[HC 官方 API](https://github.com/herdrdev/herdr-core/blob/master/schema/README.md)

会话状态区分初始化、就绪、任务执行、暂停和退出，并保留 HC 原始状态。部分 CLI 不上报 agent 状态，因此不能仅凭 `idle/done` 或终端文本判定任务完成。

### 常驻与 namespace

沿用外层监督进程和 Fuzzer PID 1：

1. PID 1 先创建 mount namespace，挂载私有 `/proc` 和 IPC 文件系统。
2. 在仍使用宿主网络时，通过短命、已降权的 bootstrap 启动 campaign 私有 HC core。
3. HC core/holder/agent 保留宿主网络，并留在 campaign PID namespace。
4. PID 1 再创建 SUT 网络 namespace，完成现有降权，通过共享文件系统上的 Unix socket 控制 HC。
5. generator agent 在启动阶段收到初始化 prompt；此时不创建 receipt、不生成 generator。

私有 HC 使用唯一 session、socket 和独立 XDG 路径。必须使用自定义 `XDG_RUNTIME_DIR`，避开 HC 的 systemd user service 启动路径，防止进程逃出 campaign PID namespace。[HC 官方启动实现](https://github.com/herdrdev/herdr-core/blob/master/crates/herdr-core/src/platform/systemd.rs)

启动环境设置为目标用户的 HOME、USER、LOGNAME，保证 sudo 启动后使用调用者的认证配置。环境变量值不写入任务日志或 state metadata。

初始化 prompt 提供 generator ABI、目标配置、party 绑定和资料路径，并要求写出带 session 标识的 `ready.json`。初始化在后台进行；已有 seed 时不等待初始化即可执行 case。进入 synthesis stage 时若尚未就绪，则保留触发并跳过派发，下轮再检查；初始化失败或超时暂停合成，普通变异继续。无 seed 时仍等待初始化及首份可调度产物，失败则结束启动。

### 清理

调整每个 case 的清理范围：清理 SUT 网络 namespace 中的非 resident 进程，保留宿主网络中的 HC 和 agent 工具进程；继续收养和回收孤儿 zombie，保留 forkserver/Python server 的 wait 所有权。

campaign 退出时关闭自己创建的 terminal 和私有 core；PID 1 退出提供最终回收保证。不会操作用户共享的 HC core。

## 4. 异步合成 stage 与产物

`GeneratorSynthesisStage` 放在 mutation stage 之前，负责接收完成结果、按节奏选择新 trace、派发任务。

使用一个后台 I/O 线程处理有界 HC 调用和结果等待；该线程在 namespace、forkserver、Python server 启动完成后创建，不修改 LibAFL state。stage 在 case 边界接收事件、校验产物并调用现有 evaluator。

按连续普通变异有效执行无新增 bitmap 槽位计数，默认阈值为 **256 次**，由 `[campaign].bitmap_stagnation_limit` 配置。达到阈值后，在当前变异 stage 结束时进入 source coverage，下一轮进入 synthesis。任务在途时继续普通变异并更新 bitmap 基线，但暂停停滞计数，不生成新的待派发请求；主循环处理完成结果后从 0 重新累计。已有种子时首轮不自动派发，无种子时仍阻塞生成首个 generator。初始化超时默认 **120 秒**，单个生成任务默认 **600 秒**，均可配置。

每个请求使用独立目录和 request ID。agent 输出：

- `generator.py`：实现现有 `generate(ctx, params)` ABI。
- `params.schema.json`：初始参数的 JSON Schema。
- `params.json`：具体初始参数。
- `result.json`：包含 request/protocol/trace 标识、成功或失败状态，以及补充步骤说明；最后原子发布。

初始化使用 `ready.json`，生成任务使用关联的 `result.json` 判定完成。旧请求、超时请求的迟到结果不得用于其他 trace。

接收成功结果后：

1. 校验关联 ID、文件和 JSON Schema；禁用 schema 的远程引用。
2. 将初始参数转换为现有 `ParamValue`，拒绝当前 input 无法表达的值。
3. 在 case 边界通过有界 Python 子进程检查语法和 `generate` 入口。
4. 将通过校验的产物发布到不可覆盖的新路径，再创建现有 `GeneratorInput { generator, params }`。
5. 调用现有 evaluator，由正常覆盖/崩溃反馈决定是否保存。

现有 Python registry 可以延迟加载新路径，无需重启 server。每个发布版本使用独立路径，避免命中旧模块缓存。

stage 的 state metadata 保存已尝试 trace、任务状态及产物映射；被保留的 testcase 附加 trace 来源 metadata。参数 schema 留在产物目录，暂不参与后续 mutator。

失败保存诊断并继续已有 fuzzing。超时、阻塞或连接丢失时暂停新派发，不自动重启 agent，也不盲目重复发送 prompt。

允许空 seeds 启动：初始化后使用同一 stage 的 bootstrap 流程等待首份产物，并按正常反馈评估；若不能得到可调度的 corpus，则明确结束启动。

配置集中使用可选 `[protocol_generation]` 和独立 `[agents.profiles.<name>]`；前者引用 profile 名称，生成任务不包含具体模型或启动参数。

## 5. 验收

- **稳定性**：JSON 键重排、source ID 重编号不改变 StepId；step 内容变化改变 ID；trace 调序或重复步骤改变 TraceId；固定 RNG seed 可重现选择。
- **模型与 receipt**：使用当前 FTP 输出验证读取、角色筛选及 message/channel/party 闭包；重复名称、悬空引用明确失败；不完整模型可正常加载。
- **异步与产物**：用模拟 HC 服务和常驻测试 agent 验证初始化、延迟结果、失败、超时、旧 manifest、无状态上报及 schema 错误；等待生成时已有 fuzzing 持续推进。
- **实际执行**：LightFTP 流程验证新脚本无需重启 Python server 即可执行；初始 seed 按现有反馈决定入队，后续仍能进行参数变异。
- **进程与抓包**：验证 core/holder/agent 的普通 UID、PID namespace 和宿主网络；连续 case 清理不杀 agent 工具；TERM、PID 1 退出和初始化失败均无残留，agent API 流量不进入 SUT pcap。
- **兼容性**：关闭该功能时，现有静态 generator、源码覆盖 stage、replay、accept 和 benchmark 按原流程运行。
