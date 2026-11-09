# Protocol name 引用与 proposal mutations

## 目标

从 protocol JSON 到 coverage proposal、synthesis 和执行进度，实体引用统一使用唯一 name；移除实体 ID 及 protocol、proposal、trace 的摘要身份。Coverage analysis 生成有序 step instance trace、party/channel instances，以及源码支持的字段变异和 trace 重复声明。最终在 LightFTP 上连续运行一小时验证完整链路。

## 命名与数据结构

- Protocol 中 party、message、channel、step 使用各自集合内唯一的 `name`。Step 删除原始 `id`、派生 `step_id` 和 catalog `source_id`；dependency、handler mapping、图节点和查询均按 step name 引用。重名和未知名称报错，不做模糊解析。
- Handler 不生成摘要 ID；唯一符号用函数名，同名符号用 `相对源码路径::函数名`。
- Proposal 升级为 v2，移除 `context`、`protocol_id`、`proposal_id`、`trace_id` 以及 proposal 文件摘要凭证。保留 `session`、`request_id`、`build`、`snapshot`、`graph_revision` 等由服务补齐的请求元数据。
- Proposal 的 `parties` 和 `channels` 用实例 name 作 key，分别指向模型 party/channel；channel 端点引用 party instance name。
- `trace` 是有序 step instance 列表。每个 instance 用语义化 `name` 标识，并用 `step` 指向模型 step name；重复实例仍各有独立 name。服务按模型补齐 action、condition、msg 等定义信息，并校验 action、端点和 campaign party binding。
- 同步升级 protocol schema、catalog、handler mapping 和图 checkpoint 格式；旧格式明确拒绝，不做隐式迁移。

## Mutations

字段变异和结构变异统一放入 `mutations`。字段变异例如：

```json
{"op":"field","steps":["login_first"],"field":"username",
 "suggestion":"探索空值和长度边界","evidence":"src/auth.c:120"}
```

- `steps` 可用 instance name 数组选择若干 step，也可用 `"[start:end]"` 选择 trace 中含首尾的连续区间；区间端点按当前有序 trace 解析。
- `field` mutation 应用于所选 send steps；允许 coverage analysis 根据源码增加模型中未列出的字段。Coverage 给出字段名、设计建议和源码依据，synthesis 决定参数名、类型、初值及范围，默认按 step instance 隔离。
- Duplicate 数组形式表示各选中 step 在其原位置分别重复；区间形式表示将整个所选子序列作为整体，在原区间后重复。
- Duplicate 的 `count_param` 控制额外副本数，初值为 0，运行时限制为 0..32；非整数按 0 处理，展开后超过 4096 个 step 明确失败。副本复用 party/channel 和字段参数，不递归触发 duplicate。
- 不允许 duplicate 选择集合彼此重叠，或同一 instance 的同一字段被重复声明；field 与 duplicate 可以作用于同一 step。Instance name 禁止使用 `:[]`，区间语法允许冒号两侧空白。

## 实现与交接

- 更新 protocol 生产、schema、校验、合并、catalog 导出和 RSC 提示词/工具，使 name 成为唯一实体引用；删除对应的 ID/hash 生成、编码、比对和跨语言一致性代码。
- 更新 Python coverage graph/MCP、Rust protocol 模型、proposal 类型/校验、coverage-synthesis handoff、autonomous draft、artifact 结果校验和进度记录。服务按 request 保存已接受的 proposal，使用请求归属及已保存内容完成交接，不依赖内容摘要 ID。
- 移除 proposal/trace/protocol 摘要 ID 作为请求目录、checkpoint 关联、attempted/published 去重键的用途；改由 session/request 标识关联运行状态。非身份用途的构建缓存或普通文件校验逻辑保持原样。
- 更新 coverage 与 synthesis prompts：Coverage 根据源码及覆盖证据形成实例 trace 和 mutations；synthesis 严格按实例拓扑实现字段参数与重复语义。

## 验收

- Schema/引用测试覆盖 name 唯一性、未知/重名引用、handler 同名限定、重复 step instance、多 party/channel instance 及非法 endpoint/action。
- Mutation 测试覆盖 field 单选/多选、模型外源码字段、数组分别重复、区间整体重复、次数 0/1/2、非法/重叠选择、展开上限，以及 field 与 duplicate 组合。
- Python/Rust 回归及 coverage MCP → proposal → synthesis → replay 交接验证不再要求 `protocol_id`、`proposal_id`、`trace_id` 或摘要凭证；确认请求隔离和实例 name 引用有效。
- 完成构建、测试和 LightFTP `accept` 后，用独立输出目录运行现有 `fuzz/targets/lightftp/campaign.toml`，其 `[campaign].timeout_secs = 3600` 已配置为一小时。运行命令为 `sudo ./target/release/mcn-fuzz run --campaign targets/lightftp/campaign.toml`；启动前将 coverage 与 synthesis 的 output 改为本次专用目录，避免覆盖既有结果。
- 一小时验收要求 campaign 到达配置 deadline 后正常退出；记录实际 wall time、完成的 fuzz iteration/case、corpus/crash 数、coverage map occupancy、coverage analysis proposal 与 synthesis/mutation 交接结果、超时或错误日志。若提前失败或挂起，记录触发点并修复后重跑完整一小时；不得将短跑、启动成功或 accept 测试代替长跑结果。

## 默认与边界

- 所有模型和实例 name 在各自命名空间内唯一并精确匹配；Step instance 使用显式语义 name，使调整 trace 顺序后 mutations 仍引用原实例。
- 一小时从 `run` 开始计时，campaign 的 3600 秒 deadline 包含 agent 初始化和运行阶段；完整迭代结束后可能略超 deadline。
- 清理仅针对被用作 protocol/proposal/trace 身份的 digest/hash ID；普通构建缓存及文件完整性机制不因本计划移除。
