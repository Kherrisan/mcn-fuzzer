# RSC 持续会话流水线、输入输出与数据结构

本文对应根目录 `rsc/` 的当前实现。第三方副本独立。输入是调用者选择的同一协议版本 RFC 集合，输出 `protocol.json`；不生成 grammar、完整状态机或消息编解码器。

## 0. 执行流程与职责

```text
parse（ingest）→ batch 预检、结构索引、固定 block lists
→ objects（列表并行，每块同时提取对象与关键词）
→ objects_link → objects_complete
→ interactions（原列表会话继续，每块同时提取关系与关键词）
→ interactions_link → interactions_complete → finalize
```

| 阶段 | 输入 | 输出 | 程序负责 | LLM 负责 |
| --- | --- | --- | --- | --- |
| parse | TXT/HTML/XML 文件或目录 | Document、Batch、解析报告 | 格式解析、规范化、来源定位和落盘 | 无 |
| prepare | 完整 Batch、配置 | Corpus、StructuralIndex、BlockListPlan、会话 | 完整性、模型容量预检和确定性分列表 | 无 |
| objects | 当前焦点 block、列表历史 | 基础对象、keywords、Issue | 调度、工具执行、结构校验和提交 | 按原文提取，复用已有概念 |
| objects_link | 汇集的基础对象、协调会话 | 概念分组、合并模型、问题 | 有界分组、串行合并和提交 | 同义概念识别和整合 |
| objects_complete | 当前 Issue、协调会话 | 修订对象、剩余问题 | 多轮遍历、即时提交、停止判断 | 按需检索后补全 |
| interactions | 当前焦点 block、延续的列表历史 | steps、依赖、keywords、Issue | 私有状态和 step ID 分配 | 单向交互与依赖提取 |
| interactions_link | 汇集关系、协调会话 | 合并后的关系、问题 | 先 step 后依赖，串行提交 | 同义关系整合 |
| interactions_complete | 当前 Issue、协调会话 | 修订模型、剩余问题 | 多轮补全编排和结构校验 | 关系及必要支撑定义的补全 |
| finalize | 最后模型和问题 | protocol.json、unresolved、报告 | 校验、统计、发布、退出状态 | 无 |

【程序】决定外层阶段与焦点游标；【提示词】驱动 Agent 在当前 turn 中调用工具或返回 JSON。Agent 不能自行改变外层阶段。PydanticAI 管理类型化消息、工具派发、JSON 响应与有限校验反馈。代码注释、docstring、注解说明及 Field(description=...) 使用英文。

## 1. 入口与会话所有权

```bash
rsc ingest /path/to/rfcs --recursive --out work/input
rsc compile work/input --protocol 'FTP/RFC959+RFC3659' --out work/model \
  --config config.json --objects-workers 8 --interactions-workers 4 \
  --block-lists 8 --context-limit-tokens 1050000 --json
rsc validate protocol work/model/protocol.json
rsc stats work/model --validate --json
```

`compile_protocol()` 和 `compile_protocol_async()` 接受 `context_options`。模型工厂为 `model_factory(stage, phase)`；兼容 `client_factory(stage)` 仅接受工具型 Model/完整适配器。预检必须先于付费模型调用。

| 会话 | 阶段 | 所有权与并发 |
| --- | --- | --- |
| 每个 list 的会话 | objects → interactions | 同 list 单写入者；list 之间 asyncio 并行 |
| 全局协调会话 | objects_link → objects_complete → interactions_link → interactions_complete | 一个持续历史，所有调用串行 |

对象汇集后，协调会话接收一次公共模型快照。对象 link/complete 后，各列表会话接收一次最新模型及阶段交接消息，再继续关系抽取。关系汇集后，协调会话接收最新关系。交接包含模型 revision，声明快照替代旧定义；不拼接其他列表的原始聊天记录。

`--workers` 是抽取并发的回退值；`--objects-workers`、`--interactions-workers` 分别覆盖。`--block-lists` 默认 8，划分不随 workers 改变。补全可用 `--no-complete` 关闭，关闭时不创建补全模型或发起补全调用，原有问题保留。

## 2. 共用模型与问题结构

schema **7.0.0** 的最终模型与语义中间对象使用下列字段。Agent response 使用对应的 Proposed 类型，省略 Python 生成的 step `id`，并额外返回关键词。party、message、associated field、channel、dependency 和 Issue 没有定义 ID。Python 不为它们生成、注入或隐藏 ID。只有 step 保留 Python 分配的 `id`，并另外持久保存 LLM 给出的 `name`。

名称按协议术语生成。上下文里已有同一概念时复用该名称；RFC 区分方向或情境时才加限定，例如 `active data connection`。不为了唯一而加哈希、序号或伪 ID。程序不把“不校验引用”写进提示词。

### 2.1 五类对象

```yaml
Party:
  name: string
  description: string

AssociatedField:
  name: string

Message:
  name: string
  associated_fields: AssociatedField[]

EndpointSource:
  party_name: string
  parameter: string
  from: string

Channel:
  name: string
  description: string
  transport: string
  connection: connection_oriented | connectionless
  endpoint_party_names: string[]
  initiator_party_name: string | null
  acceptor_party_name: string | null
  endpoint_sources: EndpointSource[]

Step:
  id: string
  name: string
  message_name: string
  channel_name: string
  sender_name: string
  receiver_name: string
  condition: string

FieldRelation:
  prior_field: string
  dependent_field: string

StepDependency:
  prior_step: string
  dependent_step: string
  condition: string
  field_relations: FieldRelation[]

Issue:
  description: string
  object_names: string[]
```

`description` 是一句短说明。message 和 field 没有 description，也没有 identification。message 不列出信道；信道使用关系是 `step.channel_name`。交互适用条件继续写在 `step.condition` 和依赖的 `condition` 中。

`from` 合并了旧的来源类别、字段定位、常量和解释。字段来源写成 `PASV response.port`，固定端口写成 `"21"`。无连接信道的 initiator 与 acceptor 必须为 null；这是结构约束。

依赖两端是 step 的 name，不是 message name，也不是 step id。`condition` 可以为空字符串。没有字段关系时 `field_relations` 为 `[]`。字段名可以是 `CSeq`，也可以是 `SETUP.CSeq`。不记录 strength、alternative group、字段关系类型或增量。

最终 `protocol.json` 顶层是 `meta`、五类集合和 `unresolved`。`meta.schema_version` 固定为 `7.0.0`。旧模型不自动迁移。

### 2.2 Issue

LLM response、补全工作列表和最终 unresolved 使用同一个 Issue：

```yaml
description: string
object_names: string[]
```

`object_names` 写受影响对象或字段的名称；即使 step 仍有 id，这里也写 step name。没有具体对象时列表为空。Python 不检查这些名称是否存在、是否唯一、是否属于预期 message。名称问题不产生引用错误 Issue，不触发重试或补全，也不删除对象。

轮次、尝试次数、延期原因、run id 和来源 block 写在 `GapHistory`、repair 日志和 compile-report 中，不写回 Issue。补全响应没有 `resolved_issue_ids`。一条 Issue 在模型实际发生变化、且响应没有原样返回同一 description 与 object_names 时才关闭。只声称已解决但模型没变，不会关闭。

### 2.3 step ID 与名称引用

`GlobalIdAllocator` 只接受 kind `steps`。Agent response 不定义 step id；Python 先为新 step 注入 `local:` 临时 id，提交时换成正式 step id。若该 step name 在当前模型里恰好对应一个 step，Python 复用那个 step id，不另造一个。

party、message、field、channel 和 dependency 的内部仓库可以用 `类别:序号:名称` 做实现索引。这个索引不是语义定义 ID：不写入发布模型，不出现在 Agent response，也不作为 `id: null` 放进发给 LLM 的摘要。

发给模型的 `search_objects` 摘要只在 step 上包含 `id`。其他四类对象的摘要没有 `id` 字段。`lookup_object` 按协议名称查找；step 也可以用 step id 查找。零个匹配返回 missing，多个匹配返回 `candidate_names` 和各自的完整定义，不替模型选择。返回体不包含 `owner_id` 或 `resolved_id`。

BlockArtifact 里的对象快照同样遵守这条边界。除 step 外不携带定义 ID。提交时复用的已有定义用 `reused_names` 记录名称，避免把名称伪装成对象 ID。

Python 对名称引用只检查字段是否存在、类型是否为字符串或字符串列表、以及 null 是否允许。不检查引用目标是否存在。新建 object、step 或 dependency 不要求先检索；可见集合非空时也可以直接新建。修改已有定义时，完整定义必须已经出现在上下文中。这是编辑权限，不是外键校验。

### 2.4 契约版本

| 契约 | 当前版本 |
| --- | --- |
| protocol schema | 7.0.0 |
| prompt | rsc-agent-prompts-v12 |
| request cache | rsc-agent-request-v8 |
| context | rsc-agent-context-v10 |
| retrieval tools | rsc-retrieval-tools-v9 |

protocol schema 为 7.0.0，包含五类集合。旧模型需要重新编译，不自动迁移。prompt、retrieval tools、context 和 request cache 契约同步升级，隔离旧结构的请求缓存。

## 3. parse、预检和结构索引

### 3.1 parse：原文解析

**驱动方式：** 【程序】本节文件发现、解析、结构识别、校验及写入均由代码执行，不调用模型。

实现入口为 `ingest_path()`。

**输入：** 单个文件或目录，以及 `IngestOptions`。实际解析器支持 TXT、HTML、XML。虽然 document Schema 的格式枚举保留了 `pdf`，当前 parse 步骤没有 PDF 解析入口。

处理顺序：

1. 发现输入文件，应用递归、包含/排除模式等选项。
2. 读取字节，计算 `input_sha256`，检测格式并解码文本。
3. 识别 RFC 编号并生成 `doc_id`；相同编号不同内容需要不同文档身份。
4. 相同内容已处理或已有相同哈希产物且没有 `--force` 时，记录 `unchanged`。
5. 调用对应解析器，提取章节、文本块和原文位置映射；保留段落、列表、表格、语法块、图示等结构。
6. 校验解析结果，分别写文档和 parse 报告；单份文档失败会记录在批次中。
7. 写 `batch.json`，列出全部输入及其处理状态。`--dry-run` 不生成正式批次。

**数据结构：**

```text
IngestOptions(dataclass):
  out_dir, recursive, include, exclude, format, force,
  dry_run, follow_symlinks, command
InputFile(dataclass): path, relative_path
ParseResult(dataclass): document, report
DocumentResult(dataclass):
  doc_id, input_path, relative_path, format, input_sha256,
  status, document_path, report_path, counts, error
BatchResult(dataclass): batch_id, input_root, out_dir, documents, batch_path

Document = {
  meta: {schema_version, rfc, title, source_id, source_path, input_sha256, format, ...},
  sections: Section[], blocks: Block[], span_map?: SpanMapEntry[]
}
Section = {section_id, path: string[], title, level, span, parent_section_id?}
Block = {
  block_id, section_path: string[], block_type, text, original_span, order,
  parent_block?, lead_in_block_id?, attrs?
}
SpanMapEntry = {
  block_id?, normalized_start, normalized_end, source_start, source_end, transform?
}
Batch = {
  schema_version, batch_id, input_root, created_at, rsc_version, command,
  documents: BatchEntry[], summary
}
BatchEntry = {
  doc_id, input_path, format, input_sha256, status,
  document, report, counts, error
}
```

其中 `BatchEntry.document`、`report` 为相对 parse 输出目录的路径。详细字段及位置格式见 [document.schema.json](../schemas/document.schema.json)、[batch.schema.json](../schemas/batch.schema.json)、[common.schema.json](../schemas/common.schema.json)。

**落盘输出：**

```text
work/input/
  batch.json
  <doc_id>/
    document.json
    ingest-report.json
```

### 3.2 编译前预检：`load_batch()`

**驱动方式：** 【程序】按参数、文件与 Schema 执行检查，失败立即终止。

**输入：** parse 输出目录、协议标识、输出目录、并发/补全/预算配置。

构造 LLM 客户端前执行以下检查：

- 参数合法；输入输出目录不能重叠；非空输出目录默认拒绝覆盖。
- `batch.json` 满足 Schema 且文档列表非空。
- 每个条目状态必须是 `ok` 或 `unchanged`，文档路径存在且不能逃出输入目录。
- 文档满足 Schema；`source_id` 与批次 `doc_id` 相符，记录的 `input_sha256` 相符。
- 文档有文本块，同一文档内部的 block ID 唯一；重复文档身份不能对应不同内容。

这里比较批次与文档中保存的身份和哈希，不重新读取原始 RFC 文件计算输入哈希。模型快照另计算 `document_sha256`，后续离线校验使用该摘要。

**输出：** 按 `doc_id` 排序的 `documents: dict[str, Document]`。预检失败抛出 `CompileError`，CLI 返回 2，不开始模型调用。

### 3.3 结构索引与语料容器：`Corpus`

**驱动方式：** 【程序】章节、block、引用索引及正文词项检索由确定性规则生成和执行。

**输入：** `documents`。

每份文档通过确定性规则生成 StructuralIndex，不调用 LLM。**以下精简结构已实现。** 构建器、Schema、校验器、查询、CLI 摘要和测试已同步移除旧结构实体、定义及规范约定字段，索引版本为 1.0.0；契约见 [index.schema.json](../schemas/index.schema.json)：

```text
StructuralIndex = {
  meta,
  sections: IndexedSection[],
  blocks: IndexedBlock[],      # Implemented document-local block lookup
  references: Reference[]
}
IndexedSection = {section_id, path, title, span, parent_section_id?}
Reference = {reference_id, from_block, ref_type, raw, target_section_id?, target_rfc?, resolved, span}
```

`meta` 记录索引版本及来源文档身份。章节索引用于按需定位目标章节；引用索引用于找到本批次内的相关原文，供 Agent 通过工具读取。

内存中的 `Corpus` 提供以下查找结构：

| 属性 | 类型/用途 |
| --- | --- |
| `documents` | `doc_id → Document` |
| `indexes` | `doc_id → StructuralIndex` |
| `blocks` | `(doc_id, block_id) → Block` |
| `search_terms` | `(doc_id, block_id) → set[str]`，来自章节标题和块正文的词项 |

当前 `Corpus.search()` 按查询与块的词项交集大小排序，再按文档和块顺序稳定排序，目前采用章节标题与正文词项检索，没有 LLM keyword 索引。

#### 3.3.1 block 索引（已实现）

**目标：** 给定 `(doc_id, block_id)`，直接找到 block 所属章节，以及它在同一文档中的前后相邻 block。该索引由 parse 结果中的章节路径和块顺序确定性构建，不调用 LLM。

**输入：** `Document.sections`、`Document.blocks`。

**输出：** 每份文档的 `StructuralIndex.blocks`，每个原文 block 对应一条 `IndexedBlock`：

```text
IndexedBlock = {
  block_id: string,
  section_id: string | null,
  order: integer,
  prev_block_id: string | null,
  next_block_id: string | null
}
```

| 字段 | 含义 |
| --- | --- |
| `block_id` | 对应 `Document.blocks` 中的原有 block ID |
| `section_id` | block 直接所属的章节，即 block 的 parent section；文档前导内容未归属章节时为 `null` |
| `order` | 原文块顺序，沿用 `Block.order` |
| `prev_block_id` | 同一文档中前一个 block；文档首块为 `null` |
| `next_block_id` | 同一文档中后一个 block；文档末块为 `null` |

`section_id` 指向 `IndexedSection.section_id`。找到所属章节后，可继续沿章节自己的 `parent_section_id` 查询上级章节。原始 `Block.parent_block` 表达块的嵌套关系，与这里的所属章节分别保留。

索引不复制 `text`、`original_span` 等正文数据，读取原文仍使用已有的 `Corpus.blocks[(doc_id, block_id)]`。每份 `StructuralIndex` 归属于一份文档，因此索引条目只保存本地 ID；跨文档查询必须同时提供 `doc_id`。

**构建步骤：**

1. 由章节建立 `section_path → section_id` 映射。
2. 对每个 block，用其 `section_path` 精确查找所属章节；无章节的前导块记为 `null`，非空路径找不到章节时报错。原文或解析结果若重复使用同一路径，继续按 original_span 的来源与范围唯一定位；仍有歧义时报错，不选最后一章。同名路径的文字引用保持未解决。
3. 按 `Block.order` 排列同一文档的全部块，包括 heading、paragraph、list_item、table、grammar、diagram 等。
4. 遍历有序块，为每个块写入前后邻居 ID，首尾分别使用 `null`；不通过 block ID 的数字部分推算相邻关系。
5. 按所属章节分组，建立同章节的有序 block ID 列表，支持限定在当前章节内查询邻居。
6. 校验覆盖完整、块 ID 和顺序无重复、章节引用存在，以及前后关系互相对应；相邻块绝不跨文档连接。

**已实现的内存查询结构：**

| 属性 | 类型/用途 |
| --- | --- |
| `Corpus.block_index` | `(doc_id, block_id) → IndexedBlock`，读取所属章节和前后邻居 |
| `Corpus.sections_by_id` | `(doc_id, section_id) → IndexedSection`，读取章节及其父章节 |
| `Corpus.section_blocks` | `(doc_id, section_id 或 null) → 有序 block_id[]`，读取直接归属同一章节的块 |

**已实现的查询接口：**

```text
get_block_index(doc_id, block_id) -> IndexedBlock
get_parent_section(doc_id, block_id) -> IndexedSection | null
get_neighbor_blocks(
  doc_id, block_id, before=1, after=1, scope="document"
) -> {before: Block[], after: Block[]}
```

邻居结果不包含当前块，各数组均按原文顺序返回。`scope="document"` 使用文档内相邻关系，允许越过章节边界；`scope="section"` 仅在直接归属同一章节的块列表内查找，不包含子章节的块。到达边界时返回实际存在的邻居，不足部分不补空对象；查询不存在的文档或块时报错，与首尾没有邻居、前导块没有章节的情况区分。

例如，同一文档有以下顺序：

| block | 所属章节 | 前一个 block | 后一个 block |
| --- | --- | --- | --- |
| `b1` | `s1` | `null` | `b2` |
| `b2` | `s1` | `b1` | `b3` |
| `b3` | `s2` | `b2` | `null` |

查询 `b2` 的 parent section 得到 `s1`；默认前后邻居是 `b1`、`b3`；限定 `scope="section"` 时只有前一个邻居 `b1`。

**后续使用位置：**

- 按需读取来源时，用所属章节查询标题及祖先章节，用同章节邻居补充上下文，由工具参数决定章节范围。
- 补全检索命中一个 block 后，用索引补充邻近段落和所属章节；需要跨章节上下文时显式使用文档范围查询，并继续遵守请求预算。
- 从逐块抽取记录或审计日志的 `(doc_id, block_id)` 出发，直接定位原文、所属章节和前后块，方便人工排查。

`StructuralIndex.blocks` 已随结构索引文件一起保存；加载索引时重建内存字典。`Corpus.blocks` 保存原文，block_index、sections_by_id 和 section_blocks 保存结构查询数据；两者保持分工。

### 3.3.2 划分并行 block list（已接入主入口）

**驱动方式：** 【程序】对 parse Batch 的文档/块引用做确定性划分，原 Batch 和 Document 不改写；只规划焦点归属，不预建 Window。

`--block-lists N` 默认 8，指定最多 N 个非空列表。对象抽取用 `--objects-workers O`，关系抽取用 `--interactions-workers I`，分别限制本阶段同时运行的列表数；未单独指定时回退到 `--workers W`，其默认值为 8。`--workers` 不是阶段专用值的额外上限；link、complete 始终串行。所有指定值必须为正整数，实际活跃列表数不超过 `min(本阶段 workers, 非空列表数)`。

列表划分独立于这三个 workers 值，改变并发度不改变模型看到的历史；同一划分同时用于 objects 与 interactions。需要超过默认 8 个列表的并发时，应同时提高 block_lists；不能为了满足 workers 自动重新划分列表。参数优先级见第 7 节。

**本轮并发范围：** 只有 ID 分配元数据支持跨列表共享原子读写。暂不设计 obj、interaction、KeywordIndex、Registry 或 ExtractionStore 的跨列表共享可变状态，也不引入这些业务状态的读写锁或阶段内增量快照发布机制。各列表只修改自己的状态，全部列表终态后由编排器统一汇集；原文、结构索引及上一阶段已完成的冻结产物仍可共享只读。协调会话依次处理 link 分组并即时提交有效合并结果。

1. 按 doc_id 和 Block.order 建立全局 sequence。优先在章节/文档边界切分连续区间，按规范化原文字节量平衡；过大的章节才在 block 边界拆分。切点与相同权重的选择规则固定并写入计划版本。
2. 每个焦点恰好属于一个列表，保留列表内原始顺序；列表数量不能超过块数。原文邻居可以跨列表读取，但不会作为额外焦点重复编译。
3. 每个列表持有私有 Registry、KeywordIndex 增量、ExtractionStore 和候选模型。关系阶段需要分配 step id 时，各列表共用第 2.3 节的 GlobalIdAllocator；锁只保护 step id 预留，不暴露其他列表的对象内容。对象抽取在本列表已提交历史中检索；关系抽取额外共享上一阶段冻结的全局基础 inventory。
4. 同阶段其他列表的候选和关键词增量不实时可见，即使对方先完成。并发调度不能改变输入或查重结果。公共 Corpus 原文与结构索引只读共享。
5. 全部列表取得终态后，编排器按列表序号和 block sequence 汇集有效候选、关键词记录与问题，形成 link 的冻结输入。失败列表不阻塞其余列表；未完成焦点保留失败记录，最终报告不完整。

```text
BlockListPlan = {
  partition_version, requested_lists: integer,
  lists: BlockList[]
}
BlockList = {list_id, order: integer, blocks: FocusBlock[]}
BlockListState = {
  list_id, stage: objects | interactions,
  block_cursor: integer,
  status: pending | running | completed | partial | failed,
  base_model_revision: integer, local_revision: integer,
  model: Model, unresolved: Issue[]
}
```

Registry、索引和结果仓库由状态关联的服务持有。列表内可以先完成一个新块并推进；列表间不需要逐块等待。只有两个抽取阶段分列表并行，complete 仍按 Issue 逐项执行。link 使用的“概念组”另见第 5 节。



## 4. 持续消息与按需检索

### 4.1 会话、turn 与消息

`SessionState` 保存 session ID、类型化 ModelMessage 历史、当前阶段、turn 边界、最新模型 revision、对象快照、token 统计和压缩记录。Agent 对象可按阶段切换指令与输出类型，逻辑会话持续。

每个 block、link 分组、Issue 是一个 turn。PydanticAI 调用使用 `message_history`；运行结束保存实际有效历史，不能将 `all_messages()` 再次追加到旧历史。模型输出经结构校验后才提交，程序记录提交状态和当前模型版本。

block 新增输入只有：

```text
{phase, focus_block: {doc_id, block_id, block_type, text}}
```

协议、阶段和上下文契约作为控制信息提供。没有自动邻居、标题、章节、引用、关键词命中、party inventory 或同块对象注入。模型通过历史及工具查阅。

原文与工具结果是不可信数据。旧 user message 不会在新 block 到来时改写；工具正文直接放在 tool return 中，不替换成动态窗口引用。

### 4.2 响应与关键词

```text
ObjectExtractionResponse = {
  parties, messages, channels,
  keywords: [{text}], unresolved: Issue[]
}
InteractionExtractionResponse = {
  steps, step_dependencies,
  keywords: [{text}], unresolved: Issue[]
}
```

`keywords` 和空 `unresolved` 可省略，由 Python 补为 `[]`。最终模型不包含 keywords。独立 keywords Agent、提示词、客户端预检和请求计数已经移除；关键词调用用量计入对应 objects/interactions 调用。

`KeywordIndex` 按阶段、doc_id、block_id 保存记录，关键词规范化大小写和空白，查询按命中数量及稳定来源顺序排序，同一 block 只返回一次。对象抽取只写本 list；barrier 汇总为公共只读快照。关系阶段继承公共索引，并写本 list 的关系关键词，结束后统一合并。失败、空关键词和成功记录保持区别。

### 4.3 检索工具

| 工具 | 输入 | 返回与作用 |
| --- | --- | --- |
| search_text | query、top_k、cursor | 原文词项命中及有界正文 |
| search_keywords | keywords、top_k、cursor | 已索引 block，按命中数及来源顺序返回 |
| read_blocks | 文档限定 block_refs、可选 range | 实际原文片段及可见的逐块产物 |
| get_parent_section | block_ref、cursor | 所属章节、祖先和章节 block 清单 |
| get_neighbor_blocks | block_ref、before、after、scope | 同文档的前后 block，默认限制直接章节 |
| search_objects | query/kind/filters、top_k、cursor | 当前仓库内已有概念摘要 |
| lookup_object | name、depth、limit | 匹配的完整对象与必要引用闭包 |

工具限制是单次返回数量和正文大小，不累计成会话生命周期的已读取 block 上限。每个正文片段最多 2,400 字符；长块返回 `text_length`、`next_start`，模型通过 `read_blocks` 的 `range` 继续读取。来源检索默认页大小为 `min(8, max_search_results, max_tool_blocks)`，对象查询默认页大小为 `min(8, max_search_results)`。空查询、非法范围、非法 cursor 和页大小错误明确反馈；有限结果不代表目标不存在。模型按需检索，不强制每个新对象先调用工具。

已有定义只有在完整内容实际出现在有效历史中才算已读取。压缩成名称目录后，编辑前须重新读取完整定义。名称引用本身不做存在性、唯一性或归属校验。

### 4.4 party 范围与命名

party 必须直接收发目标协议消息或作为信道端点。仅在注册流程、背景说明、本地存储/UI 架构或实现示例中出现的实体不构成 party。内部组件和人只有承担直接协议通信职责时才提取；账户、能力、操作和方向本身不会创建新角色。

先判断角色，再选择名称。同一角色复用已有名称，不因大小写、措辞或局部能力不同而重新命名；无匹配项时优先采用 RFC 术语。整体进程及组件只有承担独立通信职责时才分别建模，不因架构中提及容器进程而增加 party。保留有依据的组件职责与不同实际端点，数据方向独立于连接建立方向。

信道按通信用途、底层传输、端点角色和连接建立方向归组；命令、载荷、示例 IP/端口、描述措辞或端点来源详略本身不创建新信道。未知值表示信息缺失，不自动构成冲突。工具按词项和存储值检索，filters 不展开同义词；候选检索不承担语义等价判断。

### 4.5 message 范围、粒度和字段归属

message 只表示实际交换的具体协议消息或协议明确规定的传输单元；不将抽象类别、命令组或字段组件单独作为消息。语法块中的产生式、表格中的分类名称，不自动构成消息。具体响应码只有在对应响应实际携带后续交互可能需要的 associated field 时，才单独作为消息类型；响应码本身及普通说明文字不构成细分依据。associated_fields 只保留跨消息关联、建连、分支及交互状态所需的信息，并且只挂在实际携带该字段的消息上；触发该消息的另一条消息不会因此获得该字段。

普通消息允许 associated_fields 为空。共享响应类型应表示实际交换的响应形式，不是响应码分类。合法消息仅有字段范围或归属错误时，link 保留该消息并输出字段修正 Issue，交给补全处理。

抽取、link 和补全提示词与 final/proposed Schema 使用同一组英文规则，不在规则中加入示例。用途判断由 LLM 完成，不重新引入 purposes 或 Python 名称归属校验。消息 link 同时复核范围和粒度，不能仅做同名合并；确认越界的候选通过 excluded 排除。补全在当前 Issue 的可编辑范围内、完整读取目标消息后，可以替换 associated_fields 列表，以删除范围外字段或纠正字段归属；channel 的修订规则见下一节；其他嵌套事实的删除保护继续保留，不增加 Python 语义或引用正确性校验。

## 5. link、补全、提交与输出

link 按类别先复核范围，再将有效成员归组；单例也接受 party/message/channel 范围复核。内部 LinkPartitionResponse 增加可省略的 excluded: [{name, reason, index?}]；groups 与 excluded 一起按出现次数完整覆盖输入。输入 summary.index、组的 member_indexes 和排除项的 index 仅定位本次分组中的候选；当不同定义使用同名且被拆到不同组或排除项时必须显式定位，不进入协议对象。只有确认越界时才排除，存在歧义时保留并记录 Issue。多成员组串行整合，共用协调会话，排除及名称调整进入 exclusions.jsonl、party-name-map.json 和持续历史，不进入最终协议对象结构。

party 合并后，既有名称映射继续应用到角色引用字段，保留冲突或无法定位的名称；泛称在不同连接上下文中的全局映射问题尚未改造。channel 合并结果不再次套用旧 party 映射，避免覆盖 LLM 刚完成的角色纠正。step 合并保留一个 Python step ID 并记录映射。

channel 的旧候选值不是已确认事实。link 根据 RFC 可修正端点、连接发起方与接受方、传输方式，并删除重复或无依据的 endpoint_sources；Python 不再要求结果保留所有旧端点、非空建连角色或旧传输值。仍检查 Schema 和无连接信道的建连角色必须为 null。实际连接的有依据差异由 LLM 保留；无法确认修正或合并时返回 deferred 和 Issue，原成员保留。合法信道只有字段错误时不排除；保留的单例返回修正 Issue，多成员组可直接在 merge 中纠正，仍无法解决时再报告 Issue。

补全只有在当前 Issue 范围内、完整读取并定位目标 channel 后，才能用完整定义替换该对象及 endpoint_sources，允许减少重复或错误来源。普通抽取继续只允许追加补充；同名目标无法确定时保留 Issue，不追加副本。

`partition_max_input_bytes`、`merge_max_input_bytes` 默认 65536，仅约束本组新增 payload。不能将它们当作继承会话的总体字节上限。现有跨类别 `max_partition_calls=64` 及其未完成报告保留，类别预算分配另行调整。

补全每轮冻结 `round_issue_worklist`（内部 tracking_id 顺序），逐项读取最新仓库，按 Issue 即时提交。两个阶段默认各最多 5 轮；无缺失或达到限制时停止。每次只提交当前阶段允许的对象；不使用 task 分组。剩余问题保持 Issue 的两字段结构。repair 使用当前问题限定范围、内部候选定位和实际读取的完整定义确定替换目标；唯一目标被原位修订，无法消除的同名歧义返回问题，不自动追加副本。

block 每个 trial 独立计数请求、工具、输出纠错和超时，最多额外重试 3 次。重试延续历史，不把历史 turn 的纠错或工具错误计入新预算。失败时补齐未执行工具的明确错误返回，避免下一次请求含悬空调用。完整类型化候选可保留独立有效部分；截断 JSON 不能伪装成有效候选。

最终 `validate_protocol` 检查结构、step ID、无连接信道约束及来源快照哈希。名称未命中不自动触发 Issue。有效部分可发布但 `meta.complete=false`、退出 1；全部失败不生成空的 protocol.json。非法输入/配置退出 2，完整成功退出 0。

## 6. 上下文预算与三级压缩

```text
ContextOptions = {max_tokens: integer | null, compact_at: 0.8, compact_to: 0.5}
可用输入容量 = 模型总容量 - 输出预留 - 安全余量
```

容量优先级为阶段 request_limits.context_window_tokens → compile.context.max_tokens → SDK 模型元数据。容量未知时预检失败，不能猜测代理上限。Luna 示例配置显式为 1050000，可覆盖。这里的 max_tokens 是总上下文容量；llm provider 的 max_tokens 仍是输出上限。

请求统计全部有效历史、当前输入、指令、工具和输出 Schema。GPT 使用本地 tiktoken；缺少对应计数器时采用保守估计并记录方法。达到可用容量 80% 时压缩，目标 50%。当前焦点、Issue、未完成工具回合必须保留。

| 级别 | 删除/替换 | 保留 |
| --- | --- | --- |
| 1 | 已完成 turn 的原文与闭合工具交互 | 已提交对象/关系响应、简短提交记录 |
| 2 | 累积旧响应 | 仓库的最新模型快照、未解决 Issue、尚未处理分组 |
| 3 | 过大的完整模型快照 | 名称目录、当前问题、最近相关结果；完整定义通过工具检索 |

目录只是 `{kind, name, description?}` 简表，不是删除仓库数据。目录本身过大时也按需检索。压缩由程序执行，不调用 LLM 总结、不合并概念、不改写事实；只能在完整消息/工具回合边界处理，不能截断 JSON。

输出预留与 safety_tokens 保留独立检查。完整请求字节默认上限为 16 MiB，显式较低值仍生效并可提前触发压缩。最终 SDK wire body 也检查预算。单个当前输入在压缩后仍无法容纳时明确失败，不静默截断。

## 7. 配置、缓存与审计

```json
{
  "compile": {
    "context": {"max_tokens": 1050000, "compact_at": 0.8, "compact_to": 0.5},
    "agent": {"max_tool_blocks": 32},
    "request_limits": {"default": {"max_input_bytes": 16777216}}
  }
}
```

CLI：`--context-limit-tokens`、`--context-compact-at`、`--context-compact-to`。旧 `compile.window`、WindowOptions、三个 `--context-blocks-before/after`、`--context-keyword-blocks` 及对应 Python 参数删除，给出迁移错误。`agent.max_context_blocks` 改为单次调用的 `max_tool_blocks`。

请求缓存按真实有效消息、当前指令、工具、Schema、provider/model 和生成参数计算。历史压缩或工具结果改变后产生新键；审计 session ID、时间戳不作为语义身份。缓存命中工具调用仍执行当前工具，不能把旧返回当作当前仓库事实。默认缓存子目录 `agent-v6`。

| 产物 | 内容与落盘时点 |
| --- | --- |
| sources/、block-lists.json | 准备阶段保存原文、结构索引及固定来源划分 |
| objects.jsonl、interactions.jsonl | 各块执行、有效提案、提交和计时；阶段结束按来源稳定排序 |
| keywords.jsonl、keyword-index.json | 随两种抽取产生的分阶段关键词记录和汇总索引 |
| block-extractions.jsonl、lists/ | 不可回改的逐块产物和列表模型快照 |
| session-messages.jsonl | 新增会话消息、turn、阶段交接；增量写入 |
| context-compactions.jsonl | 压缩级别、原因、模型 revision、前后 token 与消息数量 |
| retrieval-contexts.jsonl、tool-calls.jsonl | 真实交付材料、工具参数、返回、错误及耗时 |
| model-requests/responses.jsonl | 实际类型化请求、Schema、模型返回和用量 |
| provider-requests.jsonl、user-messages.jsonl | 最终 provider 正文和 user 消息，不含认证头 |
| objects-link/、interactions-link/ | 输入快照、摘要、划分、排除/合并/延期决定、角色名称映射、结果和报告 |
| objects-repair/、interactions-repair/ | 每轮输入、worklist、历史、修订、剩余问题和报告 |
| timings.jsonl、timing-summary.json | 阶段墙钟、Agent/模型/校验分项及审计写入成本；取消时也保留 |
| protocol.json、unresolved.json、compile-report.json | 最终模型、剩余问题、配置/调用/失败/计时摘要 |

压缩只影响后续模型输入，不删除原始审计。--force 仅清理编译器管理产物，不清空请求缓存或无关文件。不同来源不能共享写入中的候选或关键词索引；只有 step ID 分配服务共享并保持原子操作。

## 8. 验收与测试清理

自动化使用 FunctionModel、MockTransport 和本地语料，不调用付费模型。覆盖：同列表跨块/跨阶段历史、列表隔离、协调会话串行、工具实文送达、双阶段关键词、重试预算重置、失败工具闭合、三级压缩、模型容量预检、缓存、排序及部分失败。继续执行 TXT/HTML/XML、28 份 RFC、结构索引和协议 Schema/CLI 回归。

删除旧窗口邻居/keyword 配额、Window 投影/裁剪、每块新上下文和独立 keywords 调用专用测试；将有效的来源范围、分页、完整定义读取与缓存断言迁移到新工具/会话接口。不以保持旧测试数量为目标。

FTP/RTSP 标注与真实模型质量单独评估，Mock 通过不等于语义正确。外部历史输出不迁移成新会话，需要重新 compile。link 跨类别预算分配仍属于独立问题。本次角色、信道及消息范围规则、name-only 关联字段、排除及替换逻辑只做静态检查，未运行自动化测试。
