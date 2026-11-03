# Protocol Schema 简化计划

状态：已实现；本次继续精简对象集合，按要求未运行测试。

本文记录已确认的简化方向：删除 `effects`；party、message、associated field
和 channel 全部取消定义 ID，由 LLM 按命名规则生成 name，其他对象直接引用 name。
step_dependencies 也取消 ID，只记录前后 step 的名称、依赖条件及两端字段名称。
party 和 channel 使用简短 description；message 和 field 不再输出描述。
message 删除 identification 和信道引用；端点来源使用简短表达。交互适用条件保留在 step 和依赖的 condition。
提示词要求名称符合协议术语并保持一致；Python 不校验这些名称引用的正确性。

本文描述的契约已实现。protocol schema、Agent response、抽取、补全、link、检索和校验使用 schema 7.0.0。旧模型不自动迁移。

## 1. 删除 effects

从最终模型、Agent response 和语义中间产物中删除：

```text
EffectTarget
Effect
ProposedEffect
Step.effects
ProposedStep.effects
MergedStep.effects
```

不再单独输出 listen、open_channel、close_channel、update_context、update_state
副作用。连接端点地址、端口及其来源继续由 channel 的 `endpoint_sources` 表达；
step 之间的顺序和条件继续由 `step_dependencies` 表达。

抽取、补全、link、投影、校验、统计及 prompt 中的 effects 处理同步删除。

## 2. 四类对象完全取消定义 ID

| 删除的定义字段 | 保留的名称字段 |
| --- | --- |
| `parties[].id` | `parties[].name` |
| `messages[].id` | `messages[].name` |
| `messages[].associated_fields[].id` | `messages[].associated_fields[].name` |
| `channels[].id` | `channels[].name` |

这些对象在最终模型、Agent response 和语义中间模型中均不定义 ID。
Python 不再为它们生成或注入 ID，也不保留用于恢复精确关联的隐藏对象 ID。
field 仍嵌在所属 message 内；不同 message 可以有同名 field。

除上述四类定义外，dependency 及其字段关联也取消 ID，alternative group 整体删除。
step 的 ID 暂时保留；step 增加持久化的 name，供依赖使用名称引用。
Issue 也取消 ID，统一只保留 description 和 object_names。
文档、block、运行日志的标识继续用于来源定位，不属于上述语义对象的定义 ID。

### 2.1 精简对象描述和重复关联

| 对象或字段 | 目标调整 |
| --- | --- |
| party.meaning | 改为简短 description |
| channel.meaning | 改为简短 description |
| message.meaning / description | 删除，不再输出消息描述 |
| field.meaning / description | 删除，不再输出字段描述 |
| message.identification | 删除 |
| message.channel_ids / channel_names | 删除；实际信道由 step.channel_name 表达 |

description 用短语或一句话概括对象的含义或使用情境，不要求全面定义；不要把已删除
字段的内容重新堆进 description。step.condition 和 dependency.condition 继续保留。

message 保留 name 和 associated_fields；field 只保留 name，删除 purposes。
associated_fields 继续使用对象列表，不改为字符串数组。
channel 的传输方式、端点角色和连接建立方向继续保留。

### 2.2 消息范围、粒度与关联字段

message 只表示实际交换的具体协议消息或协议明确规定的传输单元；不将抽象类别、命令组或字段组件单独作为消息。语法块中的产生式、表格中的分类名称，不自动构成消息。具体响应码只有在对应响应实际携带后续交互可能需要的 associated field 时，才单独作为消息类型；响应码本身及普通说明文字不构成细分依据。associated_fields 只保留跨消息关联、建连、分支及交互状态所需的信息，并且只挂在实际携带该字段的消息上；触发该消息的另一条消息不会因此获得该字段。

抽取、link 和补全的提示词与 final/proposed Schema 使用同一组规则，规则中不举例。messages 的单例也接受范围复核；确认不属于消息的候选通过 excluded 排除，不将其强行合并到一个有效消息。该语义边界由 LLM 根据原文判断，不增加 Python 对字段名称引用的正确性校验。补全在当前 Issue 的可编辑范围内、完整读取目标消息后，可以替换 associated_fields 列表，以删除范围外字段或纠正字段归属；channel 的修订规则见下一节；其他嵌套事实的删除保护继续保留，不增加 Python 语义或引用正确性校验。

### 2.3 简化 endpoint_sources

每个端点来源只保留 party_name、parameter 和 from。from 是简短字符串，合并旧结构
中的 source 类别、field 引用、value 常量和 description 解释，不再分别输出这些字段。

channel 的旧端点、建连方向和传输值可在 link 中依据 RFC 纠正，不要求保留原候选中的错误值。重复或无依据的 endpoint_sources 可删除；范围内、完整读取且定位明确的 channel 也可在补全中替换完整来源列表。普通抽取继续只允许追加补充。有依据的实际连接差异保留，无法判断时保留原候选和 Issue。合并后的 channel 不重新套用旧 party 名称映射；全局名称映射对上下文的处理尚待另行改造。

```yaml
endpoint_sources:
  - party_name: server
    parameter: port
    from: PASV response.port
  - party_name: server
    parameter: address
    from: control connection peer address
```

固定端口可写成 `from: "21"`。来源表达直接沿用字段名称、常量或简短上下文说明，
不要求枚举分类或结构化字段定位。Python 只校验其结构和类型，不验证来源引用正确性。
相关提示词、补全和校验删除依赖旧 source 枚举及 field/value 组合的规则。

## 3. 所有相关引用改用 name

| 当前引用字段 | 目标引用字段或表达 |
| --- | --- |
| `messages[].channel_ids` / `channel_names` | 删除；信道使用关系放在 step |
| `channels[].endpoint_party_ids` | `endpoint_party_names` |
| `channels[].initiator_party_id` | `initiator_party_name`，仍允许 null |
| `channels[].acceptor_party_id` | `acceptor_party_name`，仍允许 null |
| `channels[].endpoint_sources[].party_id` | `party_name` |
| `channels[].endpoint_sources[].source` / `field` / `value` / `description` | 合并为简短字符串 `from` |
| `steps[].message_id` | `message_name` |
| `steps[].channel_id` | `channel_name` |
| `steps[].sender_id` | `sender_name` |
| `steps[].receiver_id` | `receiver_name` |
| `step_dependencies[].prior_step_id` | `prior_step`：前置 step 的 name |
| `step_dependencies[].dependent_step_id` | `dependent_step`：后置 step 的 name |
| `field_relations[].prior_field_id` | `prior_field`：字段名或限定字段名 |
| `field_relations[].dependent_field_id` | `dependent_field`：字段名或限定字段名 |

Issue 在 LLM response、语义中间模型及最终 unresolved 列表中统一为：

```yaml
Issue:
  description: string
  object_names: string[]
```

删除 Issue.id、kind、object_ids 和 details；Python 也不向 Issue 注入这些字段。
object_names 使用受影响的对象或字段名称，即使 step 自身仍有 ID，这里也引用 step
name。问题无法关联到具体对象时，object_names 可以为 []。依赖问题可列出前后 step
名称，并在 description 中简短说明条件和问题。

Issue 的名称引用遵循相同规则：Python 仅检查结构和类型，不验证名称是否正确。
运行阶段、来源 block、调用错误、补全轮次和执行状态等诊断信息写在独立运行日志或
编排状态中，不嵌入 Issue。

### 3.1 提示词要求与程序校验边界

LLM 按以下提示词规则生成并引用 name：

1. 优先沿用 RFC 中的术语，名称简短且有语义。
2. 已有上下文中存在同一概念时，尽量沿用已有名称。
3. RFC 明确区分方向或使用条件时，使用必要限定词，例如 `active data connection`
   和 `passive data connection`。
4. 不为保证唯一性而强制加入哈希、序号或伪 ID。

提示词不使用“模糊性”“允许模糊引用”等表述，也不将程序不校验引用的策略作为
放宽命名要求的提示发给 LLM。Agent 指令、Schema 描述和工具说明均遵循上述命名规则。

Python 仅对这些名称引用做 Schema 结构校验，例如字段是否存在、是否为字符串或
字符串列表、是否允许 null，以及非空约束。不校验引用名称是否存在于对象集合、是否
唯一、是否精确匹配、是否指向预期对象，以及字段是否属于指定 message。

名称引用检查不作为提交、link、补全或 finalize 的门槛；不因名称匹配结果自动产生
引用错误 Issue、触发重试或补全、删除对象或拒绝输出。LLM 仍可依据原文和上下文报告
发现的语义问题。检索可以按名称返回候选，但其匹配结果不承担引用正确性校验。

这里的校验调整涵盖上述四类对象，以及依赖中的 step 和 field 名称引用。
step 自身暂留 ID，但依赖不使用该 ID；Python 不将 step 名称解析为 ID 来校验依赖。

### 3.2 目标结构示例

```yaml
Party:
  name: string
  description: string            # 简短概括

AssociatedField:
  name: string

Message:
  name: string
  associated_fields: AssociatedField[]

Channel:
  name: string
  description: string            # 简短概括
  transport: string
  connection: connection_oriented | connectionless
  endpoint_party_names: string[]
  initiator_party_name: string | null
  acceptor_party_name: string | null
  endpoint_sources: EndpointSource[]

Step:
  id: string                    # Python 生成，当前保留
  name: string                  # LLM 生成并保存，供依赖引用
  message_name: string
  channel_name: string
  sender_name: string
  receiver_name: string
  condition: string

EndpointSource:
  party_name: string
  parameter: string
  from: string                  # 字段名称、常量或简短来源表达

StepDependency:
  prior_step: string             # 前置 step 的 name
  dependent_step: string         # 后置 step 的 name
  condition: string              # 简短依赖条件；无额外条件时为空字符串
  field_relations:
    - prior_field: string        # 前置消息的 field name，可带 message name
      dependent_field: string    # 后置消息的 field name，可带 message name
```

旧的 `FieldRef = {message_id, field_id}` 删除。字段引用可直接写 `CSeq`，或写
`SETUP.CSeq`。后者是 message name + field name 的名称表达，不要求构成严格的
外键路径，不以复杂转义或唯一定位机制作为使用前提。

dependency 两端的 step 已各自提供 `message_name`，所以 `prior_field` 和
`dependent_field` 都可以写 `CSeq`。有助于理解时也可写成 `SETUP request.CSeq`
和 `SETUP response.CSeq`。

## 4. 简化 step_dependencies

`step_dependencies` 仍在 interactions 阶段与 steps 一起提取。每条记录仅包含
`prior_step`、`dependent_step`、`condition` 和字段名称对列表 `field_relations`。
两个 step 引用直接使用各自的 name；不要混用 message name 和 step name。
没有涉及字段时，`field_relations` 为 `[]`，不为凑齐结构而推测字段。

删除依赖的 `id`、`strength`、`alternative_group`，
以及字段关系的 `id`、`kind`、`increment`、`description`。不要求完整描述依赖的
强制程度、替代前置条件或字段算术关系。必要信息可以用简短的 condition 表达，
不把原有详细契约作为必需内容重新塞进 condition。

例如：

```yaml
step_dependencies:
  - prior_step: send SETUP request
    dependent_step: send SETUP response
    condition: response to the corresponding SETUP request
    field_relations:
      - prior_field: CSeq
        dependent_field: CSeq
```

这条记录只说明请求与响应之间的依赖及涉及的字段，不额外声明字段是 equal、increment
或其他关系。LLM 无需为依赖及字段名称对生成 ID，Python 也不注入 ID。

step 的 name 从抽取响应保留到最终模型，不能再作为临时标签在提交时删除。
命名提示应区分必要的消息和方向，并尽量复用已有 step name；不加入“模糊性”说明。
Python 不检查前后 step 名称是否存在或唯一，也不检查字段是否属于对应消息。

## 5. link、补全和检索的同步调整

- 基础对象 link 不再依赖根对象 canonical ID；合并结果保留 LLM 选择的名称和语义内容。
- link 响应支持内部 excluded 名称及理由，party、message、channel 的范围复核涵盖单例；排除记录不进入最终对象 Schema。
- 分组的输入索引、member_indexes 及排除项 index 仅定位请求内的同名候选，不是语义对象 ID，
  不写入最终对象或名称。不同同名定义被拆组时必须用其输入索引明确成员。
- repair 对已选中的已有候选执行替换，同名歧义无法消除时记录问题，避免追加同名副本。
- link、补全和检索摘要使用精简后的名称与 description；不要求恢复 message/field 描述、
  identification 或 message 信道列表。
- 端点来源合并使用 party_name、parameter 和 from，不再校验来源类别及 field/value 组合。
- message 内的 field 仍可进行语义合并，但不再生成、保留字段 ID，也不要求返回字段的
  `nested_id_map`。effects 及其 ID 映射一起删除。
- step 暂时保留当前 ID 处理；step name 作为持久语义字段。
- Issue 不再分配 ID，也不返回 kind、object_ids 或 details。补全按当前工作列表中的
  Issue 及其内容进行处理；轮次、尝试次数和状态由独立编排状态跟踪，不回填进 Issue。
- 补全响应中的 resolved_issue_ids 随 Issue ID 一起删除；围绕当前 Issue 记录处理结果，
  尚未解决的问题仍用 description 和 object_names 返回，不要求模型回显问题 ID。
- dependency 和字段名称对的 link 不再使用 canonical ID、nested ID 映射或 ID 重定向；
  按前后 step 名称、condition 和字段名称对进行语义去重，保留不同条件的记录。
- 删除 alternative group 的分组、合并及校验流程；不再提取强度和详细字段关系。
- link 统一名称时由 LLM 判断概念身份；无歧义的 party 名称映射只作用于角色引用字段，
  Python 不验证名称外键正确性。同名不代表同一概念，不能无差别全局替换所有字符串。
- 对象仓库、`search_objects`、`lookup_object`、补全可编辑范围和 Issue 定位改为支持
  类别、名称及上下文，允许返回多个候选；不要求这四类对象有 ID 才能查询或读取。
- 保留原文块、模型请求和逐阶段产物日志用于追溯，不重新引入已删除的语义对象 ID。

## 6. 实现与验证范围

实现时同步修改：

- Pydantic final model、全部 Agent response model 和发布的 JSON Schema；associated_fields 只含 name，旧 purposes 被移除。
- meaning 改为简短 description；删除 message/field 描述、message.identification 和 message 信道列表。
- EndpointSource 改为 party_name、parameter、from，删除旧来源枚举及字段组合规则。
- 删除四类对象、dependency 和字段名称对的 ID 分配、注入、重定向、缺失 ID 占位，
  以及名称引用正确性校验逻辑。
- 删除依赖强度、alternative group、字段关系类型与增量的抽取和校验逻辑。
- 将 step name 从响应局部标签改为持久字段，依赖全程使用 name 引用。
- 统一所有阶段的 Issue 结构，删除 Issue ID、分类枚举、object_ids、details 和
  resolved_issue_ids；同步调整问题去重、补全工作列表、关闭问题及日志关联逻辑。
- objects/interactions 抽取、complete、link、仓库、工具和检索产物投影。
- Agent prompt、Schema 字段说明、工具说明和缓存契约版本；发给 LLM 的说明只要求
  按协议术语一致命名，不加入“允许模糊引用”类指令。
- fixtures、Mock 响应、FTP/RTSP 标注样例、pipeline 文档和配置相关说明。

删除仅验证 effects、已取消对象 ID 唯一性、严格外键匹配或字段 ID 映射的过时测试。
删除仅验证依赖强度、alternative group 和详细字段关系类型、增量的测试。
删除只验证旧 Issue 字段及 resolved_issue_ids 的过时测试；保留并调整问题记录与多轮
补全回归，确认 Issue 只含两个字段，运行诊断独立保存，无具体对象时允许空名称列表。
删除只验证旧 meaning、消息识别信息、message 信道关联和旧端点来源结构的过时测试；
调整有其他验证目的的样例。字段来源和常量来源共用简短 from 表达。
调整仍有意义的回归，覆盖普通名称、限定字段名、同名对象和 link 命名统一，
以及 step name 持久化、最简依赖结构和空字段列表。
验证名称未命中、存在多个候选或字段归属不匹配时，Python 不因引用检查拒绝输出、
生成错误或触发补全；类型错误、缺少必需字段等 Schema 结构问题仍应被拒绝。
保留仍适用于其他 ID、解析、索引、运行限额和失败报告的测试。

这是不兼容的 Schema 变更。实现后提升 `schema_version`，旧模型从 RFC 重新编译，
不自动迁移。
