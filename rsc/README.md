# RSC — RFC 协议交互模型提取器

输入调用者选择的同一协议版本的 RFC 集合，输出协议交互模型 protocol.json，供后续 generator 使用。根目录项目与 third-party 副本独立。详细流程、输入输出和验收边界见 [pipeline.md](docs/pipeline.md)。

~~~bash
cd rsc
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate

rsc ingest /path/to/selected-rfcs --recursive --out work/input
rsc compile work/input --protocol 'FTP/RFC959' --out work/model --config config.json \
  --objects-workers 8 --interactions-workers 4 --block-lists 8 --json
rsc validate protocol work/model/protocol.json
rsc stats work/model --validate --json
~~~

调用者负责选择协议版本，编译器不会按 RFC 编号覆盖冲突描述。单份 RFC 是批次的特例。当前不生成 grammar、状态机或消息编解码器。

## Agent 编译流程

所有 Agent 阶段直接返回 JSON，由 PydanticAI 和 RSC 程序校验并反馈错误。输出 schema 在指令中提供一份；工具列表仅保留 7 个检索工具，不提供候选预检或最终结果工具。

parse（ingest）→ objects → objects_link → objects_complete → interactions → interactions_link → interactions_complete → finalize。

准备阶段校验完整 batch、建立结构索引及固定 block list。每个列表有持续会话，从 objects 延续到 interactions；每个 block 的新增输入只有焦点正文和定位信息，工具调用、返回和结构化输出继续追加到历史。Agent 按需查询关键词、正文、父章节、邻居及已有对象，没有自动窗口或 inventory 注入。objects、interactions 同时返回关键词，不再单独调用关键词模型。

全局 link、complete 共用一个协调会话，调用串行；列表抽取仍并行。阶段间通过最新模型快照交接，不混合列表私有聊天历史。上下文接近容量时先清理旧原文和闭合工具回合，再用最新模型替换旧响应，必要时保留名称目录并通过工具读取完整定义。压缩不调用 LLM 总结。

上下文足够时可以直接返回，不强制先搜索；修改已有定义仍要求完整定义已实际出现在有效历史中。目录不算完整定义。对象查询省略 top_k 或 lookup_object.limit 时采用 min(8, max_search_results)，来源检索还受单次 max_tool_blocks 限制；页大小必须是严格正整数。响应可省略空的 condition、field_relations、keywords、unresolved；最终协议 Schema 仍为 7.0.0。

link 先复核对象范围，将确认越界的条目写入内部 excluded 记录；其余成员按概念归组并合并。party 合并产生的无歧义名称映射只用于角色引用字段，信道按实际端点、底层传输和连接方向比较。channel 的旧候选值不作为不可修改的事实，link 可依据 RFC 纠正端点、建连方向和传输方式，并清理重复或无依据的端点来源；合并结果不重新套用旧 party 映射。有依据的实际连接差异继续保留，无法确认时保留原成员和 Issue。既有全局 party 名称映射的上下文问题另行改造。关系 link 先处理 step，再处理依赖。

两处补全各默认最多 5 轮。编排状态用 tracking id 保持工作列表稳定，Issue 本身只有 description 和 object_names。每项读取最新候选并即时提交。零进展不会提前终止；新问题留到下一轮。已有对象修订按当前问题范围和完整定义读取记录确定替换目标；多个同名候选无法定位时返回问题，不追加同名副本。完整读取并定位的范围内 channel 可替换 endpoint_sources，删除重复或错误来源；普通抽取仍只允许追加补充。独立有效修改可保留，截断输出和未通过类型校验的片段不能作为有效结果。

使用 --no-complete 或 compile.repair.enabled=false 可关闭两处 LLM 补全，保留抽取、link、基础 inventory 和最终校验。禁用时不构造 repair 模型，不生成 round 目录，报告 stop_reason=disabled。

## 模型配置与额度

[config.example.json](config.example.json) 提供完整配置模板。支持 Anthropic API 和 OpenAI-compatible API（包括支持工具调用的 GPT、DeepSeek 或本地服务）。编译使用 PydanticAI 的完整工具消息协议；纯文本旧客户端不能直接作为 Agent。

- --workers 默认 8，是对象/关系并发的回退值；全局 link、complete 串行。
- --objects-workers、--interactions-workers 分别覆盖阶段配置，互不形成隐藏的总并发上限。
- --block-lists 默认 8，控制固定来源划分，不随 workers 改变。
- --context-limit-tokens 显式设置总上下文容量；否则使用模型元数据，容量未知时预检报错。
- --context-compact-at、--context-compact-to 默认 0.8、0.5，控制触发与目标比例。
- --objects-repair-max-rounds、--interactions-repair-max-rounds 覆盖各自轮数，必须为正整数。
- compile.agent 控制每个 trial 的请求数、工具数、输出重试、时间和工具单次返回上限；重试保留历史并重新计数。

完整请求字节保护默认 16 MiB，与模型 token 容量分开；显式较低值继续生效。预算包含历史、指令、工具和输出 Schema，并预留输出及安全余量。link 的 64 KiB 只限制本组新增内容。内置 OpenAI/Anthropic SDK 发送前还核对最终正文；不可容纳的当前输入明确失败，不静默截断。

模型覆盖键为 llm.stages.objects、interactions、repair、link_partition、link_merge，也可用 objects_repair、interactions_repair 等阶段限定键。compile.context 配置容量与压缩比例；阶段 request_limits.context_window_tokens 优先于全局容量。llm.cache_dir 相对于配置文件解析，逐请求缓存放在 agent-v6 子目录，缓存键包含真实有效历史；工具调用命中缓存后仍执行当前检索。

旧 compile.window、WindowOptions、三个窗口 CLI 参数和对应 Python 参数已删除。agent.max_context_blocks 改为单次工具返回数量 max_tool_blocks。Luna 示例显式使用 1,050,000 tokens；代理部署可覆盖。

~~~bash
rsc config check --config config.json
rsc llm check --config config.json
~~~

检查不发送模型请求；llm check 是客户端配置检查，不代表真实模型工具能力已经通过实测。

## 模型与身份

顶层为 `meta`、`parties`、`messages`、`channels`、`steps`、`step_dependencies`、`unresolved`。party、message、associated field 和 channel 没有定义 ID，其他对象用协议 name 引用它们。角色不固定为 client/server。完整契约见 [protocol.schema.json](schemas/protocol.schema.json)。

- `parties`：名称和一句 description。
- `messages`：名称和 associated fields。消息和字段都没有 description，也没有 identification 或信道列表。字段只保留 name。
- `channels`：名称、一句 description、传输方式、端点角色和连接建立方向。`endpoint_sources` 只含 `party_name`、`parameter` 和短字符串 `from`。无连接信道的 initiator 与 acceptor 为 `null`。
- `steps`：Python 分配 `id`，LLM 给出并持久保存 `name`。交互用 message、channel、sender 和 receiver 的 name 引用。没有 effects。
- `step_dependencies`：没有 ID、强度或 alternative group。`prior_step` 和 `dependent_step` 是 step name。`condition` 是短条件，可以为空。`field_relations` 只含 `prior_field` 和 `dependent_field`，可以写成 `CSeq` 或 `SETUP.CSeq`；没有字段时列表为空。
- `unresolved`：每条 Issue 只有 `description` 和 `object_names`。没有具体对象时 `object_names` 为 `[]`。

message 只表示实际交换的具体协议消息或协议明确规定的传输单元；不将抽象类别、命令组或字段组件单独作为消息。语法块中的产生式、表格中的分类名称，不自动构成消息。具体响应码只有在对应响应实际携带后续交互可能需要的 associated field 时，才单独作为消息类型；响应码本身及普通说明文字不构成细分依据。associated_fields 只保留跨消息关联、建连、分支及交互状态所需的信息，并且只挂在实际携带该字段的消息上；触发该消息的另一条消息不会因此获得该字段。

LLM 按 RFC 术语命名，并在上下文已有同一概念时复用该名称。Python 只校验结构和类型，不检查这些 name 是否存在、是否唯一、是否属于预期 message。名称未命中或多个候选不会拒绝输出，也不会因此触发补全。同名不表示同一概念，link 不会全局替换相同字符串。

当前 protocol Schema 为 **7.0.0**。旧 1.x–6.x 模型需要从 RFC 重新编译，不自动迁移。

`validate protocol` 校验 Schema、step ID 唯一性、无连接信道的结构约束，以及源文档快照校验和。它不把名称引用当成外键。语义是否准确仍需使用人工标注评估。

## 输出与失败处理

| 产物 | 内容 |
| --- | --- |
| protocol.json | 最终通过校验的完整或部分模型 |
| compile-report.json、unresolved.json | 阶段执行/请求计数、生效配置、对象数、退出状态及问题 |
| sources/、block-lists.json | 来源文档、结构索引和固定列表划分 |
| lists/、keyword-index.json、block-extractions.jsonl | 列表私有快照、关键词索引和不可回改的逐块产物 |
| session-messages.jsonl、context-compactions.jsonl | 会话新增消息、阶段交接、压缩前后 token 及原因 |
| retrieval-contexts.jsonl、model-requests.jsonl、model-responses.jsonl、tool-calls.jsonl | 真实交付材料、模型消息及工具轨迹 |
| timings.jsonl、timing-summary.json | 阶段、焦点和 block 起止及分项耗时；日志写入时间与字节统计，正常取消时也保留 |
| provider-requests.jsonl | 内置 provider 最终请求正文，不含认证头 |
| user-messages.jsonl | 最终请求中的完整 user message，保留原文及结构，可通过 run_id/request_index 关联请求 |
| objects-link/、interactions-link/ | 摘要、划分、排除/合并决策、角色名称及 step ID 映射、模型和问题 |
| objects-repair/、interactions-repair/ | 补全模型、报告和逐轮工作列表、请求、决策及缺失历史 |
| objects-repair/inventory.json | 已验证基础清单 |

移动模型时携带 sources/。Issue 只保留 description 和 object_names；运行阶段、轮次和调用错误写在编译日志里，不写进 Issue。

性能排查先看 `timings.jsonl`：`stage.*` 是阶段墙钟时间，`focus`、
`block` 包括准备、历史管理、Agent、提交和持久化；关键词随抽取返回。模型响应另外区分
`preparation_ms`、`provider_latency_ms` 与 `cache_overhead_ms`；Agent 记录包含
`validation_ms`。`timing-summary.json` 汇总这些操作及各日志文件的写入开销。
并发任务与父子操作的时间存在重叠，不能直接相加作为墙钟时间。

## Logfire 在线追踪

RSC 已包含 Logfire 依赖。新启动的 Agent 默认开启正文采集
（`InstrumentationSettings(include_content=True)`），记录输入消息、模型回复、
工具参数与返回，以及调用耗时、用量、重试和异常。Agent 追踪附带
`rsc.run_id`、`rsc.stage`、`rsc.phase`，可与本地 JSONL 关联。

在当前环境完成 Logfire 项目登录，或设置 `LOGFIRE_TOKEN` 后，照常运行 compile。
没有项目凭据时不交互式登录，也不向云端发送追踪。`LOGFIRE_SEND_TO_LOGFIRE=false`
可显式关闭上传，`LOGFIRE_SERVICE_NAME` 可覆盖默认服务名 `rsc`。
嵌入 Python 应用时保留调用者已配置的 OpenTelemetry provider。

本地完整请求日志继续保留。此接入不采集 HTTP 认证头；正文仍经过 Logfire 默认脱敏。
已经运行的进程不会自动加载本次修改，需要在新的 compile 中生效。

--force 只替换编译器管理的产物，不清空请求缓存或无关顶层文件。非空输出目录默认禁止覆盖；输入与输出不能重叠。退出码为 0（完整）、1（部分或失败）、2（参数、批次、配置或输出路径错误）。部分结果标记 meta.complete=false；没有有效对象时不生成空模型。

Python 同步或异步入口：

~~~python
from rsc import compile_protocol, compile_protocol_async

report = compile_protocol(
    "work/input", "FTP/RFC959", "work/model",
    config="config.json",
    objects_workers=8,
    interactions_workers=4,
    repair_options={"enabled": False},
    context_options={"max_tokens": 1050000, "compact_at": 0.8, "compact_to": 0.5},
)
~~~

可注入 model_factory(stage, phase)，返回 PydanticAI Model；异步调用使用 await compile_protocol_async(...)。兼容 client_factory(stage) 仅接受 Model 或完整 as_pydantic_model() 适配器。输入和能力预检在模型请求之前完成。

## 测试和离线检查

~~~bash
pytest -q
ruff check rsc tests scripts
python scripts/replay_repair.py work/model --out work/repair-preview
~~~

离线预览读取本版的 link 快照、逐块产物、关键词索引与来源，生成逐 Issue 工作列表及初始材料，不创建模型、不恢复执行，也不把初始 payload 大小冒充最终请求预算。

保留 TXT/HTML/XML、28 份 RFC、结构索引、FTP/RTSP 标注与图约束回归。删除 task 分组、窗口、独立关键词调用和其他废弃功能的专用测试；有效的预算、延期、失败、范围与交付检查迁移到会话接口。测试边界见 [pipeline.md](docs/pipeline.md#8-验收与测试清理)。

[FTP](tests/fixtures/protocol/ftp.json) 与 [RTSP](tests/fixtures/protocol/rtsp.json) 标注是验收子集。真实模型结果可用以下命令辅助人工核对：

~~~bash
python scripts/evaluate_protocol.py work/model/protocol.json --gold tests/fixtures/protocol/ftp.json
~~~

工具严格比较结构与文字，同义表达可能漏匹配；Mock 测试通过不能证明语义准确率。自动化测试不调用付费模型。旧 units.jsonl、protocol 1.0.0 和旧 task 产物不迁移，需重新解析、编译。保留 index、doctor、schemas、配置检查和 validate/stats。
