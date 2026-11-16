# Coverage analysis 闭环诊断 MCP

## 1. 目标与实现范围

新增 Rust HTTP MCP 服务，让 coverage analysis 查询历史 proposal、生成产物及其贡献，判断下一轮应该探索新路径，还是通过新 proposal 修正 generator。

- 使用官方 Rust MCP SDK `rmcp`、Tokio、Axum，提供 Streamable HTTP。
- 保留现有 Python coverage MCP，两个服务同时注册给 coverage analysis。
- 数据仅保存在内存中，按数据项更新，不替换全量快照；不持久化、不恢复历史统计。
- 复用 LibAFL `MapNoveltiesMetadata` 统计贡献，复用已有 index → 源码位置导出文件。
- 不改变 `BitmapProgress`、停滞触发条件或 `GeneratorInput`。
- 不新增 replay 统计或 replay MCP 返回字段。
- 修正通过新 proposal → synthesis 完成，保留旧 generator 和统计。

## 2. 数据结构

### 2.1 注册索引与共享数据

```rust
#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize)]
struct ProposalId(String);

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct GeneratorKey(PathBuf); // 规范化后的绝对路径

// 仅 fuzzer 线程使用，无需锁。
struct RegistryWriter {
    by_id: BTreeMap<ProposalId, Arc<ProposalRecord>>,
    generator_owners: BTreeMap<GeneratorKey, ProposalId>,
    shared: Arc<IntrospectionStore>,
}

// 与 Tokio HTTP 线程共享。
struct IntrospectionStore {
    campaign: CampaignIdentity,
    proposals: RwLock<Vec<Arc<ProposalRecord>>>,
    symbols: SymbolLookup,
}

struct CampaignIdentity {
    run_id: String,
    started_at_unix_ms: u64,
}

struct ProposalRecord {
    identity: ProposalIdentity,
    data: RwLock<ProposalData>,
}

struct ProposalIdentity {
    id: ProposalId,
    origin: ProposalOrigin,
    analysis_request_id: Option<String>,
    session: String,
    snapshot: Option<String>,
    created_at_unix_ms: u64,
}

enum ProposalOrigin {
    Coverage,
    AutonomousSynthesis,
}
```

proposal ID 使用来源前缀：

- coverage analysis：`coverage:<request_id>`。
- 自主 synthesis：`synthesis:<request_id>`。

analysis 派发时即建立记录，即使尚未产出 proposal，也能显示“正在 proposal”。seed 不创建虚构的 proposal。

同步执行模块通过 fuzzer 线程内的 `Rc<RefCell<RegistryWriter>>` 访问索引；借用仅覆盖登记或查找，不跨执行调用持有。Tokio 只持有 `Arc<IntrospectionStore>`。

### 2.2 Proposal 内容、状态与产物

```rust
struct ProposalData {
    status: ProposalStatus,
    proposal: Option<Arc<Proposal>>,
    synthesis_request_id: Option<String>,
    artifacts: Option<GeneratorArtifacts>,
    stats: ProposalStats,
}

enum ProposalStatus {
    Proposing,
    Queued,
    Skipped,
    Synthesizing,
    Synthesized,
    Failed,
    Cancelled,
}

struct GeneratorArtifacts {
    generator_py: PathBuf,
    params_json: PathBuf,
}
```

复用现有 `Proposal` 类型，验收后冻结其内存内容。不额外保存 proposal 路径、phase、状态原因、转换历史或 lifecycle；产物不保存发布时间。

| 事件 | 状态 |
| --- | --- |
| analysis 派发 | `proposing` |
| proposal 接受并进入 mailbox | `queued` |
| no_proposal 或被 mailbox 新 proposal 替换 | `skipped` |
| synthesis 派发 | `synthesizing` |
| 产物验收通过并进入候选队列 | `synthesized` |
| analysis/synthesis 失败或超时 | `failed` |
| campaign 结束时任务尚未完成 | `cancelled` |

自主 synthesis 从 `synthesizing` 开始登记，验收后补充 proposal。`synthesized` 只代表产物发布成功；后续执行错误不改变该状态。重复、迟到结果不能覆盖终态。

### 2.3 统计数据

```rust
#[derive(Default)]
struct ProposalStats {
    mutations: MutationStats,
    executions: ExecutionStats,
    contribution: ContributionStats,
}

#[derive(Default)]
struct MutationStats {
    attempts: u64,
    changed_candidates: u64,
    skipped_or_unchanged: u64,
}

#[derive(Default)]
struct ExecutionStats {
    started: u64,
    completed: u64,
    valid: u64,
    invalid: u64,
    infrastructure_errors: u64,
    target_outcomes: BTreeMap<String, u64>,
    generator_statuses: BTreeMap<String, u64>,
    generator_exits: BTreeMap<String, u64>,
    last_error: Option<ExecutionError>,
}

struct ExecutionError {
    at_unix_ms: u64,
    category: String,
    message: String,
}

#[derive(Default)]
struct ContributionStats {
    corpus_cases: BTreeSet<u64>,
    feedback_novelty_indices: BTreeSet<u32>,
}
```

统计口径：

- `attempts`：外层 mutator 调用次数，不是内部字段修改次数。
- `changed_candidates`：实际改变输入的候选数量，作为对外“变异次数”。
- `skipped_or_unchanged`：跳过或变异后与原输入相同。
- `started`：实际进入 executor 的次数，过滤后未执行的候选不计。
- `completed`：executor 正常返回的次数，包含目标 crash/timeout；Rust 执行错误计入 `infrastructure_errors`。
- 有效性和状态分类来自现有 `CaseReport`，按已完成执行统计。
- `corpus_cases`：实际新增 corpus ID，去重后的长度为累计贡献；包含 synthesis baseline 和变异后代。solutions 不计入，case 后续删除不扣减。
- `feedback_novelty_indices`：新增 corpus testcase 的 `MapNoveltiesMetadata.list` 并集，排除 index 0。
- novelty 包括首次命中和命中次数 bucket 提升，不能解释为全部都是新覆盖位置。
- 不记录 `first_hit_indices`，不增加第二套已见 bitmap。
- 错误文本沿用现有脱敏逻辑，最多 4 KiB；协议拒绝本身不自动判为 generator 错误。

### 2.4 源码映射

```rust
enum SymbolLookup {
    Available(Arc<BTreeMap<u32, Frame>>),
    Unavailable { reason: String },
}
```

复用现有 `Frame { function, file, line }`，不重复维护另一份位置表。

将现有 `decode_index` 包装成公开的单表读取接口。在 campaign 启动、创建 `IntrospectionStore` 时读取一次 `coverage.index_map`，先于 HTTP MCP 服务和 coverage analysis agent 启动：

1. 读取配置指定的映射文件。
2. 解压并解析为 `BTreeMap<u32, Frame>`。
3. 通过 `Arc` 放入 store，此后保持只读。
4. MCP 查询直接访问内存，不重复读取文件，也不运行 symbolizer。

不要求加载反向 `location_map`。映射未配置或读取失败时保存不可用原因，其他 MCP 查询正常工作，源码查询返回 `mapping_unavailable`。同一 campaign 内默认目标构建和映射不变，不做热加载。

当前导出格式每个 index 保存一个选定 frame，没有列号；MCP 返回 `column: null`。本次不修改导出格式或扩展 inline 调用链。

## 3. 数据更新与同步

### 3.1 内部更新接口

以下为接口签名示意，`Result` 使用项目现有错误处理约定。

```rust
// RegistryWriter
fn register(&mut self, identity: ProposalIdentity)
    -> Result<Arc<ProposalRecord>>;

fn get(&self, id: &ProposalId)
    -> Option<Arc<ProposalRecord>>;

fn bind_generator(&mut self, path: GeneratorKey, id: ProposalId)
    -> Result<()>;

fn owner_for_generator(&self, path: &Path)
    -> Option<Arc<ProposalRecord>>;

// ProposalRecord
fn accept_proposal(&self, proposal: Arc<Proposal>) -> Result<()>;
fn set_status(&self, status: ProposalStatus) -> Result<()>;
fn synthesis_started(&self, request_id: String) -> Result<()>;
fn publish_generator(&self, artifacts: GeneratorArtifacts) -> Result<()>;

fn record_mutation(&self, changed: bool);
fn execution_started(&self);
fn execution_finished(&self, report: &CaseReport);
fn execution_failed(&self, error: ExecutionError);
fn corpus_added(&self, corpus_id: u64, novelty_indices: &[usize]);
```

`corpus_added` 在同一次记录写锁内完成：

1. 插入 corpus ID。
2. 若 ID 已存在，直接返回，避免重复统计。
3. 将 novelty index 经范围检查转换成 `u32`，排除 index 0 后合并到集合。

### 3.2 更新位置

- **Analysis**：派发、接受、跳过、失败及 mailbox 替换时更新状态。
- **Synthesis**：关联 synthesis request；发布时保存产物，并建立 generator 路径归属。
- **Mutation stage**：记录变异结果。`evaluate_filtered` 返回新增 corpus ID 后，读取 testcase 的 `MapNoveltiesMetadata`，更新 corpus 和 novelty 贡献。baseline 与普通变异使用同一处理函数。
- **Executor**：通过 generator 路径查找记录，统计实际执行和已有 `CaseReport`。每次执行开始清除旧 report，防止错误路径沿用上次结果。
- **工作线程**：analysis/synthesis 结果交回 fuzzer 后再更新注册表，工作线程不直接访问 `RegistryWriter`。
- **Replay**：不接入本次统计，不能增加普通执行次数或贡献。

归属复用已有 `TraceOrigin` 和 generator 路径，后代继续归属原 proposal。路径必须唯一绑定；路径关联与 `TraceOrigin` 冲突时明确报错，不静默覆盖。未登记的 seed 或恢复输入不推测其历史 proposal 归属。

当前 observer 已开启 `.track_novelties()`，不新增 observer 或修改反馈算法。若新增 corpus 缺失预期 metadata，记录诊断日志并保留 corpus 计数，不扫描 bitmap 伪造 novelty。LibAFL 的 solutions 分支不保证携带普通 coverage feedback 的 novelty metadata，不从该分支补记本次贡献。

### 3.3 锁与一致性

- fuzzer 独占 `by_id`、`generator_owners`，因此外层索引不加锁。
- 本地 map 与共享列表持有同一个 `Arc<ProposalRecord>`，不复制记录内容。
- 新增记录时短暂锁住共享列表；高频统计更新只锁对应记录。
- Tokio 查询先从共享列表取得记录引用，释放列表锁后读取记录。
- 不嵌套持有列表锁和记录锁；锁内不进行 I/O、序列化或 `.await`。
- 单条记录的计数和集合由同一把锁保护，查询保持自洽；列表不保证所有行来自同一时刻。
- 使用 `std::sync::RwLock` 保护短时内存访问，不为独立计数引入 atomic。
- 锁中毒作为内部错误处理，不返回伪造的零统计。

## 4. HTTP MCP 接口

使用 `serde`、`schemars`、`rmcp` 定义 schema，枚举采用 `snake_case`，工具标注为只读。

```rust
struct ListProposalsRequest {
    statuses: Option<Vec<ProposalStatus>>,
    offset: Option<usize>,
    limit: Option<usize>,
}

struct GetProposalRequest {
    proposal_id: ProposalId,
    index_offset: Option<usize>,
    index_limit: Option<usize>,
}

struct ResolveBitmapIndicesRequest {
    indices: Vec<u32>,
}

struct Page<T> {
    total: usize,
    offset: usize,
    limit: usize,
    items: Vec<T>,
    next_offset: Option<usize>,
}

struct QueryResponse<T> {
    run_id: String,
    observed_at_unix_ms: u64,
    data: T,
}
```

| 工具 | 返回 |
| --- | --- |
| `list_proposals` | 分页的身份、当前状态、目标摘要、generator 路径、变异/执行次数、corpus 数量和 novelty index 数量 |
| `get_proposal` | 身份、当前状态、完整 proposal、synthesis request ID、产物路径、统计摘要及分页的 `feedback_novelty_indices` |
| `resolve_bitmap_indices` | 按输入顺序返回每个 index 的文件、行号、可选列号、函数及解析状态 |

内部 corpus ID 集合不直接返回，只返回数量。无需 `index_kind` 参数。

- 列表按登记顺序返回，先过滤再分页；默认 50 条，最多 200 条。
- novelty index 按数值升序分页；默认 256 条，最多 1,024 条。
- 批量解析最多 1,024 项。
- 越界 offset 返回空页；非法 limit、超限批次和未知 proposal ID 返回明确错误。
- 映射状态区分 `resolved`、`unknown_index`、`no_debug_location`、`mapping_unavailable`；映射加载失败不影响 proposal 查询。
- tools 描述写明计数口径、分页、只读属性，以及实时统计与冻结 snapshot 的区别。

服务生命周期：

- analysis 启用时，在 seed 执行前创建 store、读取映射并注入相关模块。
- 独立 OS 线程运行 Tokio runtime，监听 `127.0.0.1:0`，挂载 `/mcp`。
- 监听就绪后注入 `mcp_servers.campaign_introspection.url`，再启动 coverage analysis agent。
- 服务启动失败时明确终止初始化。
- campaign 结束时标记未完成任务、取消服务并回收线程。
- 每个 campaign 使用独立 store 和端口，不使用全局单例。

## 5. 提示词与验收

修改 coverage analysis init/request：

- init 介绍两个 MCP 服务及工具，完成查询预热，保留现有 handler 映射任务。
- 每轮先查 proposal 列表，再按需读取详情、generator.py、params 和现有回放日志，替换批量扫描历史 proposal 的指令。
- 区分未执行、预算不足、探索饱和和具体实现错误；不能仅凭零贡献判定错误。
- 明确 novelty 可能来自命中次数 bucket 提升；源码解析用于定位反馈变化，不等价于新增源码行覆盖。
- 实时统计可能晚于当前冻结 snapshot，判断时结合现有 source coverage 和回放日志。
- 避免重复派发仍在排队或 synthesis 的相同探索。
- 修正通过新 proposal 完成，在 `goal.reason` 引用旧 proposal ID、证据及具体修正要求；允许有明确实现错误证据时沿用 trace。
- 提示词保持协议无关，不修改旧产物。

验收测试：

- proposal 状态转换、mailbox 替换、自主 synthesis、超时及迟到结果。
- baseline/变异后代归属、路径冲突、未变化变异、过滤未执行和 corpus 去重。
- 使用真实 `MapNoveltiesMetadata` 验证首次命中与 bucket 提升都会贡献 index，同一 index 在单个 proposal 内去重。
- solutions 不误计 corpus，replay 不修改统计，缺失 metadata 不伪造贡献。
- 执行失败不沿用旧 `CaseReport`。
- 并发追加、查询和局部更新无死锁，单条记录一致。
- 现有 index-map 文件读取、缺失映射、未知 index、空列号及分页校验。
- 验证映射只在初始化时读取，工具请求不再读取映射文件。
- 真实 HTTP MCP 初始化、工具发现与调用、多 campaign 隔离和关闭。
- 小型闭环测试核对 proposal → synthesis → baseline/变异 → corpus 的统计，并运行现有 coverage/synthesis 回归测试。

## 6. 现有代码与调研参考

- `fuzz/src/engine/executor.rs`：`HitcountsMapObserver`、`.track_novelties()`、`MaxMapFeedback` 和执行报告。
- `fuzz/src/engine/mutation_stage.rs`：synthesis baseline、普通变异、corpus ID 和 `TraceOrigin`。
- `fuzz/src/engine/synthesis/`：请求派发、产物验收和候选队列。
- `fuzz/src/coverage/schedule.rs`：analysis 派发、proposal 接受、mailbox 和 MCP 配置注入。
- `fuzz/src/coverage/symbol_map.rs`：现有映射导出、`Frame`、压缩文件解码。
- `fuzz/src/app/config.rs`：已有 `coverage.index_map` 和 `coverage.location_map`。
- `fuzz/prompts/coverage-analysis-init.md`、`fuzz/prompts/coverage-analysis-task.md`：agent 初始化与分析提示词。
- 本地 LibAFL 0.16.1 的 `feedbacks/map.rs`、`fuzzer/mod.rs`：novelty metadata 生成、corpus 入库和 solutions 分支语义。
- [官方 Rust MCP SDK](https://github.com/modelcontextprotocol/rust-sdk)：`rmcp` 和 Streamable HTTP 服务。
- [Tokio shared state](https://tokio.rs/tokio/tutorial/shared-state)：短时同步锁和共享状态访问。
