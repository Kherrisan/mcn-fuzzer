# Protocol Model、Coverage Analysis、Generator Synthesis 与 Mutation 流程

本文描述当前 mcn-fuzz 的核心数据模型和运行流程，供架构图、时序图或交给其他工具重绘使用。

## 1. 总体架构

系统可以分成四层：

1. **静态协议模型层**：`protocol.json`、`catalog.json`、协议文档、party binding。
2. **Rust 运行时层**：LibAFL fuzzing stage、proposal mailbox、candidate FIFO、corpus、bitmap feedback。
3. **Python generator 层**：`generator.py`、`@step_handler` dispatch、`Ctx`、`Channel`、`Listener`。
4. **Coverage / agent 层**：LLVM coverage replay、snapshot、MCP coverage service、coverage analysis Codex agent、synthesis Codex agent。

一句话概括：

> Protocol model 提供命名化的协议步骤、party/channel 和依赖；Rust 将 proposal 实例化为 trace，执行 generator 与 SUT，收集 PCGUARD bitmap 和 corpus；LLVM coverage replay 产生源码覆盖快照；coverage agent 根据快照提出 trace proposal；synthesis agent 把 proposal 变成 `generator.py`；mutation stage 先执行一次 baseline，再变异 trace 并进入普通 LibAFL feedback。

## 2. Protocol Model

### 2.1 模型文件

核心输入是 `protocol.json`。模型使用名字作为实体身份，不使用数字 ID、digest 或 schema version。

```json
{
  "meta": {
    "protocol": "FTP/RFC959+RFC3659",
    "complete": false,
    "documents": [
      {"doc_id": "rfc959", "rfc": "RFC 959", "path": "sources/rfc959.json"}
    ]
  },
  "parties": [
    {"name": "user-PI", "description": "FTP client"},
    {"name": "server-PI", "description": "FTP server"}
  ],
  "messages": [
    {"name": "USER", "associated_fields": [{"name": "username"}]}
  ],
  "channels": [
    {
      "name": "control connection",
      "description": "FTP control channel",
      "transport": "TCP",
      "connection": "connection_oriented",
      "endpoint_party_names": ["user-PI", "server-PI"],
      "initiator_party_name": "user-PI",
      "acceptor_party_name": "server-PI",
      "endpoint_sources": []
    }
  ],
  "steps": [
    {
      "name": "send USER",
      "action": "send",
      "channel_name": "control connection",
      "condition": "after greeting",
      "message_name": "USER",
      "sender_name": "user-PI",
      "receiver_name": "server-PI"
    }
  ],
  "step_dependencies": [
    {
      "prior_step": "service greeting",
      "dependent_step": "send USER",
      "condition": "after 220",
      "field_relations": [
        {
          "prior_field": "banner",
          "dependent_field": "banner",
          "derive": "direct reuse"
        }
      ]
    }
  ],
  "unresolved": []
}
```

### 2.2 Step action

`steps[]` 是按 `action` 区分的联合类型：

| action | 关键字段 |
|---|---|
| `send` | `name`, `channel_name`, `condition`, `message_name`, `sender_name`, `receiver_name` |
| `listen` | `name`, `channel_name`, `condition`, `party_name` |
| `connect` | `name`, `channel_name`, `condition`, `party_name` |
| `close` | `name`, `channel_name`, `condition`, `party_name` |

语义：

- `send`：一个协议消息。
- `listen`：party 开始监听，不代表 peer 已连接。
- `connect`：party 主动建立连接。
- `close`：关闭该 party 的 transport。
- 网络动作不会伪造一个 message。

### 2.3 Channel

Channel 描述通信关系：

- `endpoint_party_names`
- `initiator_party_name`
- `acceptor_party_name`
- `transport`
- `connection`
- `endpoint_sources`：upstream、动态端口等部署信息来源

### 2.4 Dependency 与 Field Relation

`step_dependencies` 描述 step 之间的前置或数据关系：

- `prior_step`
- `dependent_step`
- `condition`
- `field_relations[]`
  - `prior_field`
  - `dependent_field`
  - `derive`

Data dependency 最终会变成 generator 侧的：

```text
ctx.save_state(prior_field, value)
ctx.load_depending_state(prior_field)
```

### 2.5 加载后的 Rust 模型

Rust 加载后形成 `IndexedModel`：

```text
IndexedModel
  ├── protocol_name
  ├── complete
  ├── steps: StepRecord[]
  ├── messages
  ├── channels
  ├── parties
  ├── dependencies
  ├── unresolved
  ├── documents
  └── snapshot
```

加载时校验：

- party 名唯一
- message 名唯一
- step 名唯一
- channel 引用存在
- step 的 party/channel 可被 campaign binding 承载

### 2.6 Party binding

Campaign 配置把 model party 分为 SUT、generator 和 unavailable：

```toml
[parties]
sut = ["server-PI", "server-DTP"]
generator = "*"
```

绑定结果：

```text
Binding: party name -> sut | generator | unavailable
```

模型步骤只有在参与 party 和 channel endpoint 都能被 binding 承载时，才允许进入 proposal/trace。

### 2.7 Catalog

Rust 将模型导出为 Python 侧使用的 `catalog.json`：

- `binding`
- `parties`
- `steps`
- `dependencies`
- `documents`
- `protocol_name`

Catalog 不包含 ID、hash、分数、遍历顺序或选择策略。

## 3. Proposal 与 Trace Instance

### 3.1 Coverage proposal

Coverage agent 提交的 proposal 结构：

```text
Proposal
  ├── origin: "coverage" | "autonomous"
  ├── session
  ├── build
  ├── snapshot
  ├── goal
  │     ├── handler_ids[]
  │     └── reason
  ├── parties: instance name -> model party name
  ├── channels: instance name -> channel binding
  ├── trace: ProposalStep[]
  │     ├── name
  │     ├── step
  │     ├── action
  │     ├── condition
  │     ├── message
  │     ├── channel
  │     ├── sender / receiver / party
  │     ├── reason
  │     ├── constraints[]
  │     └── mutable_fields[]   ← synthesis copy 后出现
  ├── mutations[]              ← coverage proposal 有，synthesis copy 会剥离
  └── dependencies[]
```

重要区别：

- `mutations` 是 coverage agent 给 Rust 的变异建议。
- Rust 在交给 synthesis 前会：
  - 去掉 `mutations`
  - 把 mutation 意图转换成每个 step 的 `mutable_fields`
  - 保留 dependencies
- synthesis agent 看到的是“可执行 trace + 可变异字段”，不是变异操作。

### 3.2 Rust 实例化后的执行输入

Rust 将 proposal 实例化为 `GeneratorInput`：

```text
GeneratorInput
  ├── generator: PathBuf
  ├── trace: TraceInstance
  │     ├── steps: StepInstance[]
  │     └── dependencies: DependencyInstance[]
  └── params: Map<String, ParamValue>
```

`StepInstance`：

```text
StepInstance
  ├── name
  ├── step
  ├── action
  ├── condition
  ├── message
  ├── channel
  ├── sender
  ├── receiver
  ├── party
  ├── fields
  ├── omitted_fields
  ├── inputs
  └── broken_dependencies
```

字段含义：

- `fields`：Rust 已决定的最终字段值。
- `omitted_fields`：显式缺省字段，不能回退到默认值。
- `inputs`：目标字段到 `prior_instance.field` 的绑定。
- `broken_dependencies`：变异策略故意破坏的依赖。

`DependencyInstance`：

```text
DependencyInstance
  ├── prior
  ├── dependent
  ├── kind: "order" | "data"
  ├── condition
  ├── fields[]
  └── origin: "proposal" | "mutation"
```

说明：

- Proposal 层的 `parties/channels` 是实例级绑定，Rust 会校验。
- 执行输入不再单独携带 PartyInstance/ChannelInstance 对象。
- `StepInstance` 保留 `channel/sender/receiver/party` 名称。
- `TraceInstance::validate()` 校验：
  - step 实例名唯一
  - dependency 的 prior/dependent 存在
  - dependency 顺序在 trace 中合法
  - data dependency 必须有 field
  - 同一 dependent field 不能绑定多次
  - `StepInstance.inputs` 与 dependency binding 一致

## 4. 运行时阶段顺序

主循环 stage 顺序：

```text
GeneratorSynthesisStage
        ↓
GeneratorMutationStage
        ↓
LlvmCoverageStage
        ↓
CoverageAnalysisStage
        ↓
回到 SynthesisStage ...
```

LibAFL 中对应：

```text
synthesis → mutation → coverage → analysis
```

补充：

- coverage 未到期时，coverage stage 直接跳过。
- analysis 没有待处理 snapshot/proposal 时，analysis stage 直接跳过。
- seedless 模式在进入主循环前，会直接运行 mutation stage 消费首个 synthesis candidate，直到产生第一个 corpus case。

## 5. Coverage Analysis 流程

Coverage Analysis 分为两层，不能混为一个“coverage”框：

1. **Coverage replay**：Rust/LLVM 生成源码覆盖率快照。
2. **Coverage agent analysis**：Codex 读取快照，提出 proposal。

### 5.1 Coverage replay

输入：

- corpus
- solutions
- coverage binary
- `llvm-profdata` / `llvm-cov`
- build manifest
- callgraph / CFG

流程：

```text
corpus/solutions
      ↓
mcn-fuzz coverage replay
      ↓
coverage forkserver
      ↓
coverage.profdata
      ↓
llvm-cov export
      ↓
coverage.json
      ↓
snapshot NNNNNN
```

Snapshot 目录通常包含：

```text
coverage/
  snapshots/000001/
    coverage.json
    coverage.profdata
    checkpoint/progress.json
    sut.stdout.log
    sut.stderr.log
    latest.json
```

同时维护：

- `current.profdata`
- `latest.json`
- `cov_over_time.csv`
- 累计 stdout/stderr
- build fingerprint
- `SourceCoverageProgress`

Coverage replay 的约束：

- 使用独立的 coverage binary 和 forkserver。
- 不修改主 fuzzing 的 PCGUARD bitmap。
- 不向主 corpus 添加 case。
- 每次枚举当前 corpus/solutions。
- replay 失败不会停止主 fuzz loop。

### 5.2 两套覆盖率指标

| 名称 | 用途 |
|---|---|
| PCGUARD shared map | 主 fuzz loop 的 bitmap feedback，决定 corpus/新颖性 |
| LLVM source coverage | coverage replay 产生，用于 coverage agent 分析源码行/分支 |

因此：

```text
PCGUARD bitmap growth ≠ LLVM source line/branch growth
```

画图时必须把这两者画成不同节点。

### 5.3 Coverage agent 初始化

Coverage agent 初始化时：

1. 读取 protocol model/catalog。
2. 读取 `handlers.json`。
3. 把 inbound message 映射到 handler/source function。
4. 准备 MCP coverage service。

`handlers.json` 是 message → handler 的映射：

```json
{
  "mappings": [
    {
      "message": "USER",
      "handlers": [
        {
          "symbol": "ftpUSER",
          "location": "ftpserv.c:292",
          "evidence": "...",
          "uncertain": false
        }
      ],
      "step_names": ["send USER"],
      "reason": null
    }
  ]
}
```

### 5.4 每个 analysis snapshot

Coverage agent 通过 MCP 工具加载：

```text
load_coverage_json
load_protocol
load_handlers
load_cfg
load_callgraph
begin_analysis
```

查询：

```text
show_overview_code_coverage
show_handler_code_coverage
show_func_code_coverage
show_source_coverage
get_reachable_functions
get_reaching_handlers
get_protocol_steps
get_step_dependencies
```

分析目标：

1. 找未覆盖或覆盖不足的 handler。
2. 找 handler 后面可达但未覆盖的函数/分支。
3. 找能到达该 handler 的协议步骤。
4. 用 model dependency 和源码条件构造最小前置 trace。
5. 选择 concrete mutations。

### 5.5 Proposal 提交与 commit

```text
coverage agent
    ↓ submit_proposal
Python MCP service
    ├── 校验 step 名
    ├── 校验 handler 关联
    ├── 校验 party/channel 实例
    ├── 校验 dependencies
    ├── 填充 action/condition/message
    └── 冻结 proposal.json
    ↓
Rust AnalysisRequest.accept
    ├── 校验 result identity
    ├── 读取 proposal
    ├── 校验 proposal 对 model/binding 合法
    ├── 计算 proposal 文件 SHA256
    └── MCP commit
    ↓
AcceptedProposal
    ↓
ProposalMailbox（one-slot）
```

### 5.6 历史 proposal 去重

Coverage prompt 在提交前要求读取之前的：

```text
requests/<id>/proposal.json
requests/<id>/result.json
```

比较：

- goal handler 集合
- 有序 model step 序列
- concrete fields
- mutations

分类：

- exact duplicate
- near duplicate（只有改名/注释变化）
- near duplicate with concrete adjustment
- distinct

允许复用旧 trace 的条件：

> 必须改变一个可能改变执行路径的具体细节，例如字段值、长度、顺序、dependency、duplicate count、data-path step，并在 `goal.reason` 中引用前一个 request id。

否则：

```text
status = "no_proposal"
```

## 6. Generator Synthesis 流程

### 6.1 触发

Synthesis stage 从 `ProposalMailbox` 取 accepted proposal。

如果没有 proposal：

- coverage 模式：等待 proposal，不自行生成。
- autonomous/seedless 模式：agent 自己写 proposal draft，再生成。

### 6.2 Proposal → synthesis request

Rust 会：

1. 复制 accepted proposal 到 request 目录。
2. 去掉 `mutations`。
3. 把 mutation 建议转换成每个 step 的 `mutable_fields`。
4. 保留 trace、dependencies、goal、parties、channels。
5. 将 request id 作为外部 identity，不写进 proposal 内容。

请求目录：

```text
synthesis/requests/<request-id>/
  proposal.json
  result.json
  generator.py
  params.schema.json
  params.json
  published/
    generator.py
    params.schema.json
```

### 6.3 Synthesis prompt 输入

Agent 会看到：

- proposal（mutable fields 版本）
- catalog
- protocol model / documents
- party roles
- deployment extra files
- generator API/lifecycle
- generator scaffold
- 空 `params.schema.json` 模板
- validator / request identity

Scaffold：

```text
python/generator_server/template.py
```

包含：

- 公共 imports
- `ctx` helper
- `_field()` 字段解析：`fields → omitted_fields → inputs → params`
- listener/accept 模式
- 13 个 ProfuzzGym target 的 handler 样例

Schema 模板：

```text
python/generator_server/params.schema.json
```

空结构：

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

### 6.4 Agent 产物

Agent 必须写：

```text
generator.py
params.schema.json
params.json
result.json
```

### 6.5 generator.py API

当前 API 是 step handler，不是手写 trace 循环：

```python
from generator_server import global_init, step_handler

@global_init
def initialize(ctx, params):
    # 每个 testcase 一次
    ...

@step_handler("exact model step name")
def handler(ctx, params):
    instance = ctx.step_instance
    ...
```

运行时：

```text
Registry 导入 generator.py
    ↓
build_dispatch(module)
    ├── 收集 @step_handler
    ├── 收集可选 @global_init
    └── 构造 generate(ctx, step_trace)
    ↓
每个 case fork 一个 worker
    ↓
optional initializer
    ↓
按 step_trace 顺序 dispatch
    ↓
finally: ctx.close_all()
```

### 6.6 Ctx 常用接口

```text
ctx.host / ctx.port
ctx.step_instance
ctx.step_name
ctx.timeout_s / ctx.connect_timeout_s

ctx.save_state(field, value)
ctx.load_state(field, default)
ctx.load_depending_state(field)

ctx.send(data, channel=None)
ctx.recv(channel=None, until=..., max_bytes=...)
ctx.connect(host, port, proto="tcp", timeout=...)
ctx.accept(host, port, proto="tcp", timeout=...)

listener.take(timeout)
listener.close()
channel.close()
ctx.close_extra()
ctx.close_all()
```

`Ctx.states` 生命周期：

```text
generator invocation 开始
    ↓
handler 可 save/load
    ↓
连接关闭/重连不清理
    ↓
generator 结束才清理
```

### 6.7 校验与发布

两级校验：

1. **Agent 自己调用固定 validator**
   - Python 编译
   - `generate`/handler 签名
   - params schema 子集
   - params 类型
   - import probe

2. **Rust acceptance**
   - result.json identity
   - proposal 合法性
   - artifacts 是否存在
   - params 是否满足 schema/可变字段
   - Python 代码/入口签名（RustPython，不执行）

通过后：

```text
published/generator.py
published/params.schema.json
```

并构造：

```text
GeneratorInput
  ├── generator path
  ├── TraceInstance
  └── params
```

同时生成：

```text
TraceOrigin {
  request_id,
  session,
  step_names,
  added_steps
}
```

### 6.8 进入 candidate FIFO

Synthesis 不直接执行 generator，也不把初始输入加入 corpus。

它只做：

```text
SynthesisCandidates.enqueue(
  request_id,
  GeneratorInput,
  TraceOrigin
)
```

持久化：

```text
synthesis/candidates.json
```

FIFO 中只保存尚未 baseline 的 pending candidate。

## 7. Mutation / Execution 流程

### 7.1 Stage 选择输入

`GeneratorMutationStage` 每次运行：

```text
candidate FIFO 非空？
  ├── 是：取一个 synthesis candidate
  └── 否：由 scheduler/corpus 选择普通 testcase
```

候选优先，保证 synthesis 结果不会无限等待。

### 7.2 Candidate baseline

对 synthesis candidate：

```text
取 candidate 原始 GeneratorInput
      ↓
执行一次原始 params/trace
      ↓
baseline 结果不进入 feedback
      ↓
baseline 不加入 corpus
```

baseline 用于验证 generator 能跑，并观察初始 trace 的真实行为。

### 7.3 变异迭代

baseline 后，使用普通 `GeneratorMutator`：

```text
最多 128 次 mutation
```

每次：

```text
clone 原始输入
    ↓
mutate
    ↓
执行 SUT/generator
    ↓
LibAFL evaluate / feedback
    ↓
新颖性 case 进入 corpus
```

### 7.4 字段变异

- 约 70% 概率优先字段变异。
- 有 `fields` 的 step 中随机选字段。
- 约 20% 概率删除字段，加入 `omitted_fields`。
- 其余情况变异 `ParamValue`：
  - 整数边界
  - 字符串截断/替换/大小写/特殊字符
  - bytes
  - list 元素
  - 类型变化

### 7.5 Step 变异

- 55%：duplicate block
- 23%：delete
- 22%：reorder
- block 长度最多 4
- duplicate 后：
  - 实例名唯一化
  - 内部 `inputs` 重映射
  - 继承内部 dependency
  - 尝试新增兼容 dependency
  - `origin = "mutation"`

### 7.6 Dependency 策略

- 约 90%：保留依赖，并要求 `TraceInstance::validate()` 通过。
- 约 10%：允许破坏依赖，写入 `broken_dependencies`。

### 7.7 Corpus 与 provenance

只有变异后通过正常 feedback 的 case 才进入 corpus。

进入 corpus 时：

```text
Testcase
  ├── GeneratorInput
  └── TraceOrigin metadata
```

这样后续可以知道 case 来自哪个 synthesis request。

baseline 永远不进入 corpus。

### 7.8 Bitmap stagnation 反馈

`BitmapProgress` 观察主 PCGUARD map：

- seed/baseline 只扩展 baseline，不增加 stagnation streak。
- 普通 mutated case 如果没有新 bitmap slot：
  - `consecutive_no_coverage += 1`
- 新 slot：
  - streak 清零
- 达到阈值（默认 256）：
  - 请求 source coverage
  - coverage replay 后进入 synthesis pending

```text
mutation executions
      ↓
PCGUARD map
      ↓
BitmapProgress
      ├── new slot → reset streak
      └── 256 连续无新 slot
              ↓
       SourceCoverage due
              ↓
       coverage replay
              ↓
       Synthesis due
```

## 8. 流程闭环

```text
                  ┌─────────────────────────────┐
                  │      protocol.json          │
                  │  parties/messages/channels  │
                  │  steps/dependencies         │
                  └─────────────┬───────────────┘
                                │ load + validate
                                ▼
                  ┌─────────────────────────────┐
                  │       IndexedModel          │
                  │       + Binding             │
                  └───────┬─────────────┬───────┘
                          │             │
                    catalog          prompts
                          │             │
                          ▼             ▼
┌─────────────────────────────────────────────────────────────┐
│                    Rust fuzzing main loop                    │
│                                                             │
│  SynthesisStage → MutationStage → CoverageStage → Analysis  │
│        │               │               │              │     │
│        │               │               │              │     │
│        │               ▼               ▼              ▼     │
│        │          GeneratorInput   LLVM snapshot   Proposal │
│        │               │               │              │     │
│        │               ▼               │              │     │
│        │          SUT execution        │              │     │
│        │               │               │              │     │
│        │               ▼               │              │     │
│        │        PCGUARD bitmap ────────┘              │     │
│        │               │                              │     │
│        │               ▼                              │     │
│        │           Corpus/Feedback                    │     │
│        │               │                              │     │
│        │               └───────────────┐              │     │
│        │                               ▼              │     │
│        │                       coverage replay        │     │
│        │                               │              │     │
│        │                               ▼              │     │
│        └────────────────────── ProposalMailbox ◄──────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

更准确地说：

```text
Coverage replay
   → snapshot
   → CoverageAnalysis agent
   → proposal.json
   → Python MCP validation
   → Rust validation + commit
   → ProposalMailbox
   → Synthesis agent
   → generator.py/params
   → GeneratorInput
   → SynthesisCandidates FIFO
   → Mutation baseline
   → mutations / feedback
   → corpus
   → coverage replay ...
```

## 9. 适合画图的节点清单

### 静态模型层

```text
[protocol.json]
[IndexedModel]
[Campaign party binding]
[catalog.json]
[protocol documents / RFC]
[handlers.json]
```

### Rust 运行时

```text
[Rust mcn-fuzz]
[GeneratorSynthesisStage]
[GeneratorMutationStage]
[LlvmCoverageStage]
[CoverageAnalysisStage]
[ProposalMailbox]
[SynthesisCandidates FIFO]
[GeneratorMutator]
[BitmapProgress]
[Corpus]
```

### Python generator 层

```text
[Python generator server]
[Generator Registry]
[generator.py]
[@step_handler dispatch]
[Ctx / Channel / Listener]
[params.schema.json]
```

### Coverage 分析层

```text
[LLVM coverage binary]
[coverage.profdata]
[coverage.json]
[snapshot NNNNNN]
[MCP coverage service]
[Codex coverage agent]
[proposal.json]
[result.json]
```

### Synthesis 层

```text
[Codex synthesis agent]
[proposal copy with mutable_fields]
[generator scaffold]
[params schema template]
[fixed validator]
[Rust acceptance]
[published generator]
[TraceOrigin]
```

### 外部执行层

```text
[SUT process / forkserver]
[PCGUARD shared map]
[SUT stdout/stderr log]
[corpus + pcap metadata]
```

## 10. 关键连线

```text
protocol.json → IndexedModel
IndexedModel + campaign binding → catalog.json
protocol.json + catalog + handlers.json → coverage agent prompt
proposal.json → synthesis request
synthesis request + scaffold + schema template → generator.py
generator.py + params.schema.json → Rust acceptance
Rust acceptance → GeneratorInput
GeneratorInput → SynthesisCandidates FIFO
SynthesisCandidates FIFO → MutationStage
MutationStage baseline → SUT
MutationStage mutations → SUT
SUT → PCGUARD bitmap
PCGUARD bitmap → Corpus/Feedback
Corpus → CoverageStage
CoverageStage → coverage snapshot
coverage snapshot → MCP service
MCP service → CoverageAnalysis agent
CoverageAnalysis agent → proposal.json
proposal.json → ProposalMailbox
ProposalMailbox → SynthesisStage
```

## 11. 画图时需要强调的边界

1. **Protocol model 不是 runtime trace**
   - model 是命名化的协议定义。
   - proposal 是选定的实例化 trace。
   - `GeneratorInput` 是 Rust 已决定字段值和依赖绑定后的执行输入。

2. **Proposal 和 synthesis copy 不同**
   - coverage proposal 有 `mutations`。
   - synthesis 看到的 proposal 去掉 mutations，换成 `mutable_fields`。

3. **Synthesis 不执行 generator**
   - synthesis 只生成和校验 artifact。
   - 真正执行在 mutation stage。

4. **Baseline 不属于 corpus**
   - candidate baseline 执行一次。
   - 只有变异后的 novel child 才进 corpus。

5. **Coverage replay 不更新主 bitmap**
   - 独立 coverage binary / forkserver。
   - 独立 snapshot。
   - 只服务源码覆盖率分析。

6. **PCGUARD bitmap 和 LLVM source coverage 是两个指标**
   - bitmap 决定 fuzz feedback。
   - source coverage 决定 coverage agent 看什么。

7. **Proposal mailbox 和 candidate FIFO 不同**
   - mailbox：一个 accepted proposal，等待 synthesis。
   - FIFO：已生成 generator，等待 mutation baseline。
   - 都不是 corpus。

8. **Request identity 在 proposal 外**
   - proposal 本身不携带 request_id/version/graph_revision。
   - request id 存在于 result.json、AcceptedProposal、TraceOrigin 和目录名。

9. **Mutation 只操作 trace instance/params**
   - 不修改 generator.py。
   - generator 只按顺序执行 step，并按 `ctx` 接口解析字段和状态。

10. **失败隔离**
    - coverage replay 失败不停止主 fuzz。
    - synthesis 失败不污染 corpus。
    - proposal 失败不进入 mailbox。
    - candidate baseline 失败则丢弃该 candidate。
