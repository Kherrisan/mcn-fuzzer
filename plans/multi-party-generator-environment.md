# 多方 Party 配置与 Generator 合成提示词

## 摘要

把 campaign 的角色定义从两方 `bindings` 改为 SUT party 集合和 generator party 集合。Synthesizer 根据完整 trace receipt，为 generator 分配其中涉及的所有 generator party 行为；这些 party 共同构成 SUT 的外部环境。

## 配置与角色解析

新增可选的 campaign 顶层配置：

```toml
[parties]
sut = ["server-PI", "server-DTP", "server-PI A", "server-PI B", "server-DTP A", "server-DTP B"]
generator = "*"
```

两个字段都接受非空 party 名称列表或 `"*"`。`"*"` 表示协议模型中除另一方已明确分配角色外的所有 party；双方都为 `"*"`、角色重叠、列表重复、未知 party 名称或展开后任一方为空时，在启动 case 前报错。未分配的 party 保持 `Unavailable`。协议生成启用时必须配置双方；未启用时可以省略 `[parties]`。

从 `ProtocolGenerationConfig.bindings` 移除旧配置，并让旧 `bindings` 明确报配置迁移错误。完成 TOML 示例和配置测试的迁移。解析后的分配仍映射到内部 `Binding`，trace 只有在所有 sender、receiver 以及 channel endpoint、initiator、acceptor 都有可执行角色时才可选。

选择 trace 时，所选 steps 的 sender/receiver 必须同时覆盖至少一个 SUT party 和至少一个 generator party。按整条 trace 校验，允许其中包含仅涉及 SUT 或仅涉及 generator 的辅助步骤；不要求每个 step 都跨越双方。不能只凭 channel 的其他端点、未选中的依赖步骤或完整模型快照判定已覆盖双方。不满足这一条件的采样在现有预算内重新选择；预算耗尽后返回无可用 trace，跳过本轮，不放宽角色覆盖条件。

## Synthesizer 提示词

初始化提示词提供全局 party 名称、描述和 SUT/generator 角色，说明 generator 可以承担多个身份，包括 server、upstream 和 user；此阶段仍只初始化 agent，不生成脚本。

每次生成提示词提供 `receipt.json` 的路径、request/protocol/trace ID 和已解析的角色集合，要求 agent 阅读 receipt 中的 steps、channels、dependencies 和完整模型快照，必要时参考其中的 RFC 文档。脚本必须实现 trace 中 generator 承担的全部 party 行为；SUT 承担的行为由目标程序执行。遇到必须补充的前置交互时，将其写入 `added_steps`，保持 trace ID 不变。

提示词说明 `generate(ctx, params)` 的现有 I/O API，包括默认 SUT 连接、`ctx.connect`、`ctx.accept`、`Listener.take`、`ctx.send/recv` 和通道参数。需要并发扮演多个身份时，可用线程驱动 SDK server 与 client，返回前要停止后台服务并关闭资源。告知 agent 使用 campaign 指定的 Python 解释器检查 SDK 是否已安装；可以使用可用 SDK（例如 pyftpdlib），缺失时用现有 socket API 实现，不在生成过程中安装依赖。任何 SUT 需要连接的 listener 必须使用 SUT 配置或参数中可知的地址和端口。

本次不增加每 case 的 listener-ready 握手或端点编排；generator 在触发 SUT 交互前自行准备 trace 所需 listener。

## 验收

- TOML 测试覆盖 party 列表、`"*"` 互补展开、两方 wildcard 冲突、重叠、未知名、重复名、空结果、缺配置及旧 `bindings` 拒绝。
- 模型测试确认多方 channel 只有在所有端点都由 SUT 或 generator 承担时才可进入 trace。
- Trace 选择测试拒绝只涉及 SUT 或只涉及 generator 的序列，接受整体覆盖双方且含单边辅助步骤的序列；覆盖长度为 1、采样预算耗尽以及没有满足条件的 trace 时跳过的情况。
- Prompt 测试确认初始化和单次生成提示分别包含全局角色、receipt 路径、所需 party 行为、现有 I/O API、SDK 可用性规则及 `added_steps` 要求。
- 保留模拟 HC 的初始化、生成和产物验收流程；不要求真实 LLM 输出测试。

## 默认约定

显式列表以协议 `party.name` 精确匹配；不在任一集合中的 party 不执行。Wildcard 只扩展为另一方之外的 party。外部 listener 使用 SUT 可获知的 endpoint；SUT 启动时自行连接上游的场景需要目标配置或目标程序具备重试能力。
