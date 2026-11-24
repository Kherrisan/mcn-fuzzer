# RSC 并行 Review、按文档聚合 Gap 与只读 Reviewer

本计划接续 `plans/rsc-review-convergence.md`，只替换它的**调度部分**：

```text
现在：Review batch 1 → Fix batch 1 gaps → Review batch 2 → Fix batch 2 gaps → …（全串行）
改为：Review 全部 batch（按 batch 并行） → 按 document_id 聚合成 gap assignment（串行修复） → 全局校验
```

`rsc-review-convergence.md` 第一项（Review 提示词中的实体语义说明）与 `rsc-document-based-review.md` 的输入投影原则保持不变。本计划**取代** `rsc-review-convergence.md` 第二项里的两条约束："同一时刻只运行 Reviewer 或 Fixer 中的一个、不并行启动多个 batch 的 Review"、"每个 batch 的 gaps 单独交给 Fixer"。

与 `plans/rsc-interaction-unit-merge.md` 的关系：那份计划把 states/steps/step_dependencies 三个扫描合并为一轮，并在非目标里写了"不改 review_gaps / gap 阶段（仍按 batch 串行）"。本计划只改 review/gap 的调度，不动扫描与 batch 划分，两者可以先后独立落地；若先实施交互单元合并，请同时删掉那条已过时的非目标。

本计划不改变 `protocol.json` / `unresolved.json` 的对外结构。

## 1. 目标

1. **Reviewer 使用独立只读 toolset**：只暴露 5 个只读工具 + `record_gap` / `complete_review`，从工具层面消除写操作，而不是只靠 `mutate()` 的运行时拒绝。
2. **Review 按 batch 并行**：同一时刻可跑 `review_concurrency` 个 batch reviewer；每个 batch 独立完成、独立落盘、独立重试，一个 batch 失败不拖垮其他 batch，失败 batch 在 resume 时重跑。
3. **Gap 按 `document_id` 聚合**：Review 结束后把全部 gap 按文档分组（组内上限 `gap_group_max`），每组一个只面对该文档 gap 的 Fixer assignment，组间串行执行。
4. **`record_gap` 改为 block_id 定位**：reviewer 直接引用段落 id，宿主只校验"这个 block 属于当前 batch"；引文可以传、原样存，但**不做任何匹配校验**——彻底消除"文本差一个标点就记不下来"的失败模式。

预期收益（基于第 2 节测量）：Review 墙钟 20.7 分钟 → 8–9 分钟（纯等待时间并行化）；Review 请求数基本不变（`record_gap` 的 9 次文本匹配失败改为 0，省的是重试与重想，不是请求总量）；Gap 墙钟 28.9 分钟 → 25–27 分钟（分组后 assignment 从 13 个降到 6 个左右，但每组要读更多原文，时间收益有限）。**主要收益是质量**：同一文档内的同主题 gap 由同一个 Fixer 统一命名与写法，跨 batch 重复 gap 只修一次；Review 并行还让 2h 预算里能塞下更多全局修复轮次。

## 2. 测量基线

数据来源：`rsc/runs/tls-2h-high-20261123T081416Z`（`extract-checkpoint.json` 的 `records` 与 `llm/review/*.jsonl`、`llm/gap/*.jsonl`、`source-plan.json`）。该运行 review/gap 全部完成，进入全局校验后剩 3 个错误、未发布，因此数据不含"被全局校验裁剪"的偏差。

| 指标 | 值 | 来源 |
| --- | --- | --- |
| Review batch 数 | 17 | `review.batches` |
| Review 请求数 / 墙钟 | 273 / 1240.2 s（20.7 min，平均 73 s/batch） | `records[phase=review]` |
| Gap assignment 数 | 13（17 batch 中 4 个 gap 为 0） | `records[phase=gap]` |
| Gap 记录数 / 请求数 / 墙钟 | 39 / 201 / 1734.6 s（28.9 min，5.2 req/gap） | 同上 |
| `record_gap` 调用（按 `tool_call_id` 去重） | 48：39 通过（正好 39 条 gap）/ 9 因文本匹配失败被拒（3.3%） | `llm/review/*.jsonl` |
| 这 9 条被拒的成因 | 6 条句子级逐字可救；3 条为改写或 ASCII 图/表格重排 | 逐条回放（见第 6 节） |
| block 规模 | 每 batch 95 个 block（中位 102，min 6 / max 140，共 1616）；每 block 约 208 字符 | `source-plan.json` |
| 跨 batch 同主题 gap | TLS 0 条，FTP 1 条 | 两份 run 的 gap 名称对比 |

Review 成本结构的关键结论：单次请求成本主要由**未缓存输入**决定，与上下文总长关系不大；因此并行化省的是墙钟（等模型的时间），不是 token。可省的请求量也有限——被拒 `record_gap` 只有 9 次，跨 batch 完全重复的 gap 只有 1 条——所以本计划的重点放在墙钟和修复质量上，不指望大幅降 token。

## 3. 改动一：Reviewer 只读 toolset

现状：Review、Gap、Scan、Fix 共用同一个 `MCPToolset`（`rsc/extract/runner.py:521`），15 个工具全部可见；Reviewer 只能靠 `mutate()` 里 `phase == "review"` 的运行时检查拒绝写入（`rsc/extract/tools.py:143`）。工具目录越大，每次请求的 catalog 越大，且 Reviewer 有可能尝试写工具后被拒、白花一轮请求。

实现：

- `make_server(current, *, readonly=False)`（`tools.py:127`）。把工具注册拆成两段：`_read_tools(server)` = `list_entities`、`search_entities`、`get_entity`、`read_source`、`validate_entities`；`_write_tools(server)` = 其余 10 个（`add_entities`、`update_entities`、`merge_entities`、`record_unresolved`、`solve_unresolved`、`complete_batch`、`complete_fix`、`record_gap`、`complete_review`、`complete_gaps`）。`readonly=True` 时只注册读工具 + `record_gap` + `complete_review`（共 7 个）。
- `Runner.__init__` 新增 `self.review_toolset = MCPToolset(make_server(lambda: ACTIVE.get(), readonly=True), max_retries=config.max_tool_retries)`，`review_agent.toolsets=[self.review_toolset]`（`runner.py:528-541`）；`self.toolset` 仍是全量，供 scan/category/global/gap Fixer 使用。
- `validate_entities` 的 `require(work, ...)`（`tools.py:397`）加入 `"review"`。它本身只读，Reviewer 用它自查"我怀疑缺失的东西是不是别的类别已经建了"是合理的，现在会因 phase 不匹配报错。
- `run()`（`runner.py:1320`）用 `contextlib.AsyncExitStack` 同时进入 `self.toolset` 与 `self.review_toolset`，并分别计算 `self.catalog_tokens` 与 `self.review_catalog_tokens`。
- `stage_attempt()`（`runner.py:973`）现在固定用 `self.catalog_tokens` 算预算（`runner.py:988`）；改为接收 `catalog_tokens` 参数（默认 `self.catalog_tokens`），`execute_stage()` 透传，review 调用传 `self.review_catalog_tokens`。否则 Reviewer 的预算会被多算 8 个工具、平白压缩可用的历史空间。
- `probe()`（`runner.py:1344`）仍建全量 server，不受影响。

理由补充：`mutate()` 的拒绝保留作为第二道防线；只读 toolset 让"Reviewer 不能改实体"变成结构约束，并让 `test_review_role_is_read_only_and_completion_tools_are_separated`（`tests/test_extract.py:1846`）从行为断言升级为清单断言（见第 10 节）。

## 4. 改动二：Review 按 batch 并行

### 4.1 工作区绑定从实例属性改为 ContextVar

现状：`self.active`（`runner.py:518`）是单例，`stage_attempt` 进入时 `self.active = work`（`runner.py:975`）、退出时置 `None`（`1030`）；工具通过 `make_server(lambda: self.active)` 取到唯一的工作区。并行后两个 batch 会互相覆盖。

实现：

```python
ACTIVE: ContextVar[Workspace | None] = ContextVar("rsc_active", default=None)

class Runner:
    @property
    def active(self):
        return ACTIVE.get()

    @active.setter
    def active(self, work):
        ACTIVE.set(work)
```

`stage_attempt` 保留现有 `self.active = work` / `finally: self.active = None` 写法即可：每个 `asyncio` task 在创建时复制上下文，task 内 `set` 只影响自己及其子任务，工具调用（同 task 或继承上下文派生的子任务）看到的正是本 batch 的 workspace。`execute()`（`runner.py:709`）与 `retry()` 的清理点（`785`、`808`）不用改。

### 4.2 并行调度

`review_gaps()`（`runner.py:1033`）拆成三段：

```python
async def review_gaps(self):
    if not self.config.review_enabled:
        return
    await self.review_all()      # 并行，只读
    await self.gap_groups()      # 串行，按 document_id 聚合
```

`review_all()`：

- 候选 = `sources.batches` 中 `review.batches[batch_id].stage != "reviewed"` 的 batch（旧流程迁移见第 7 节）。
- `semaphore = asyncio.Semaphore(config.review_concurrency)`；每个 batch 一个 task，进入时 `async with semaphore`。
- 每个 task：新建 `Workspace`（`deepcopy(registry)` 只读）、`work.gaps = entry 已有 gaps`、`execute_stage(work, self.review_agent, prompt, root/"llm"/"review"/f"{batch_id}.jsonl", catalog_tokens=self.review_catalog_tokens)`、成功后写 `entry = {stage:"reviewed", gaps, review_seconds, review_calls, review_usage, review_retry_*}`。
- 失败隔离：task 内 `try/except StageFailed`，失败写 `entry = {stage:"review_failed", error: str(exc)[:200], review_seconds, review_calls}`，不重新抛出；`asyncio.gather(*tasks, return_exceptions=True)` 之后，若有失败则抛出第一个失败（保留 `test_review_failure_keeps_batch_unfinished_and_resume_retries_it` 的语义：进程退出、checkpoint 保留、resume 重跑该 batch）。不能用 `TaskGroup`：它会在第一个失败时取消兄弟任务，正好违反"互不拖累"。
- 每个 batch 完成即落盘（`entry` 整体替换 + `self.state.update`），不依赖阶段结束。
- 结果按 batch 顺序汇总后再打印，避免并行时的乱序输出；同时打印一行 `review 17/17 in 8.4 min (concurrency 3)`。

### 4.3 `save()` 的并发语义

现状：`save()`（`runner.py:564`）是 `checkpoint = {**self.state, **updates}; atomic_json(...); self.state = checkpoint`，而调用点普遍写 `records=self.state["records"] + [record]`（读-改-写）。两个协程交错执行这两个调用点会丢记录。

实现：

- `save()` 改为 `self.state.update(updates)` 后原子写盘（语义等价，但不再整份复制）。
- 新增 `self.record(entry)`：`self.state["records"].append(entry)` 后写盘；所有 `records=... + [...]` 调用点改为 `self.record(...)`（scan/category/review/gap/global 各处）。
- 上述两个方法内部不含 `await`，在单个事件循环里是原子的；因此不需要 `asyncio.Lock`。若后续 save 需要 `await`（例如异步压缩），再加 `self.save_lock`。
- 仍保持"每个 batch 一落盘"，崩溃损失最多一个在飞 batch。

### 4.4 限速与超时

- Review 请求继续走共享 `RateLimitedModel(base, self.review_limiter)`，`review_requests_per_second` 默认 5（`cli.py:33`）。限速是**全局**的，所以并发 3 并不会把请求速率放大到 3 倍，只是减少"模型思考期间的等待空洞"。
- 单 batch 超时仍是 `assignment_timeout_seconds=900`；并行总墙钟 ≤ 900 + 队列时间，不需要为并行单独加超时。
- provider 429 / 连接错误的处理沿用 `execute_stage` 的 `assignment_retries` 重试（带 `retry_delay()` 退避）。并发 3 时若出现 429，建议把 `review_concurrency` 降到 2，不需要改代码。

## 5. 改动三：Gap 按 document_id 聚合

### 5.1 为什么串行、为什么按文档

- **为什么必须串行**：`Workspace.data = deepcopy(previous)`（`registry.py:271`），Fixer 结束时用 `self.save(registry=work.data)` 整份覆盖 registry，实体 ID 来自 `next_entity` 递增。两个 gap 组并行 = 丢更新 + ID 冲突。要做组间并行必须引入实体级合并，本计划不做。
- **为什么按文档聚合**：同一 RFC 内的 gap 常常是同一主题分散在多个 batch（TLS 实测 0 条完全同名重复，但同主题跨 batch 分布很常见，例如"某个 state 无 producer"在 3 个 batch 各报一次）。聚合后一个 Fixer 一次看懂整份文档的缺口，命名与 `source`/`processing` 写法一致，第二遍读同一 block 的调用也合并。
- **为什么可以跨 batch 阅读**：`read_source` 只依赖 `current().sources`（`tools.py:265`），不接受 batch/phase 限制，文档级 Fixer 能读该文档任意 block；`evidence()`（`registry.py:385`）也只校验 `document_id`/`block_id` 存在。所以聚合不需要把多 batch 原文塞进 prompt。

### 5.2 数据模型

Review 完成时给每条 gap 分配宿主侧稳定标识（不引入 hash、不改 `protocol.json`）：

```json
{"gap_id": "rfc8446-g7", "status": "pending", "kind": "states", "name": "...",
 "missing_attributes": ["..."], "block_id": "blk-000101",
 "evidence_text": "reviewer 引文（可选，原样保存，不校验）",
 "document_id": "rfc8446", "section_path": ["4", "2", "2"],
 "batch_id": "batch-00009", "note": "cited from batch-00009"}
```

- `gap_id = f"{document_id}-g{n}"`，`n` 按 batch 顺序、batch 内 gap 顺序连续递增，确定性可复现，resume 时对同一批 gap 得到同一 ID。
- `status`：`pending` → `done` / `unresolved`；旧流程迁移来的 gap 用 `legacy_done`（第 7 节）。
- `evidence_text` 是可选引文，**原样保存、不参与定位也不校验**（第 6 节）；定位一律以 `block_id` 为准。它只作提示与审计，fixer 仍必须 `read_source(block_id=...)` 回读原文。

### 5.3 分组

```python
for doc_id, docs_batches in group_by_document(batches):   # 文档顺序 = manifest 顺序
    pending = [gap for batch_id in docs_batches
               for gap in review["batches"][batch_id]["gaps"]
               if gap["status"] == "pending"]
    for k, chunk in enumerate(chunks(pending, config.gap_group_max), start=1):
        group_id = f"gap-{doc_id}-{k}"       # 例如 gap-rfc8446-2，不会与 batch id 冲突
        ...串行执行...
```

- 组顺序：文档顺序 → 文档内 batch 顺序 → gap 顺序。同一文档超过 `gap_group_max` 条时切成多个组，仍串行。
- `gap_group_max` 默认 8：既有"一次看全文档主题"的上下文优势，又限制单次 `deepcopy` 的修改面（一次改 8 个 gap 比一次改 20 个更容易通过确定性校验）。
- 分组只依赖 checkpoint 的 gap 列表，不依赖注册表快照，因此 resume 时重算分组是稳定的；已完成组（`review.groups[gid].status == "done"`）跳过。

### 5.4 执行与 prompt

- `Assignment`（`registry.py:127`）新增 `document_id: str | None = None`。文档组创建 `Assignment(group_id, "gap", document_id=doc_id)`；`work.gaps = chunk`。`asdict(assignment)` 只出现在 scan/category 的 prompt，新增字段对其无影响。
- `prompt()`（`runner.py:570`）：`phase == "review"` → `review_prompt`；`assignment.id in self.sources.batches` → 现有 `gap_prompt`（保留单 batch 路径，便于单测与回退）；否则 → 新增 `gap_backlog_prompt(...)`。**不能再走 `self.sources.batches[assignment.id]`**，否则文档组 id 会 KeyError。
- 新增 `review.gap_backlog_prompt(protocol, group_id, document_id, gaps, extensions, batch_ids)`（`review.py:138` 附近）：

```json
{"protocol": "...", "stage": "gap", "assignment": "gap-rfc8446-1",
 "document_id": "rfc8446", "source_batches": ["batch-00009", "batch-00010"],
 "gaps": [{"gap_id": "...", "kind": "...", "name": "...",
           "missing_attributes": ["..."],
           "block_id": "blk-000101", "section_path": ["4.2.2"],
           "evidence_text": "reviewer 引文（未校验，仅作提示）"}],
 "linked_entities": [{"id": "E41", "kind": "states", "name": "..."}],
 "enabled_extensions": [],
 "instruction": "..."}
```

  不发送整份文档原文（gaps 里的句子已经足够定位，最多 8 × 4000 字符仍在预算内）。指令要求：
  1. 对每条 gap **先**用 `read_source(document_id=..., block_id=...)` 回读它引用的那一段，确认该段确实缺这条内容（引用不支撑时按 `section_path` 或名称检索重新定位，仍找不到就放进 `unresolved_gap_ids`），再用 `list_entities(batch_id=...)` / `search_entities` / `get_entity` 确认现状；
  2. 同一主题的多条 gap 应合并成一致的实体、`source`、`processing`、`conditions`，不要为每条 gap 各造一个近似实体；
  3. 收尾必须 `validate_entities`，修掉本组引入的 producer/悬挂引用/field-reference 问题；
  4. 最后 `complete_gaps(summary, unresolved_gap_ids)`。
- `linked_entities` 用 `summary()`（id/kind/name）投影，最多 60 行，避免一次把整份文档的实体定义塞进 prompt；细节靠 `get_entity`。取"证据 block 属于本组任一条 gap"的并集，再对同文档内同名/别名命中补几条。

### 5.5 完成与残留

- `complete_gaps`（`tools.py:570`）签名改为 `complete_gaps(summary: str = "", unresolved_gap_ids: list[str] | None = None)`：
  - 校验 `unresolved_gap_ids` ⊆ `work.gaps` 的 `gap_id`，未知 ID 报错（防止模型编造 ID）；
  - `work.completion = {"status": "gaps_completed", "summary": summary, "unresolved_gap_ids": [...]}`；
  - 无参数调用仍然接受（等价于"全部已处理"），单 batch 路径与旧测试不用改。
- 宿主处理：组内 gap 全部标 `status="done"`，`unresolved_gap_ids` 中的标 `unresolved` 并写 `resolution_note`（来自 `summary`）；组记录 `{status:"done", gap_ids, seconds, calls, usage, unresolved_gap_ids}` 落盘。
- 残留 gap：写进 `review["residual_gaps"]`（`gap_id`/`kind`/`name`/`document_id`/`status="unresolved"`），最终打印数量；**不写进 `unresolved.json`**（保持对外产物结构不变），并在最终 `records` 里留一条 `phase="gap-residual"` 记录。可选（低成本、已列在实施顺序里）：在 `prompt()` 的 global 分支加 `context["residual_gaps"]`，让全局 Fixer 有机会顺手补，但不作为发布阻断条件。

### 5.6 日志与记录

- 日志路径 `root/"llm"/"gap"/f"{group_id}.jsonl"`（现在按 batch id）。
- `records` 追加 `{"phase": "gap", "assignment": group_id, "document_id": doc, "gap_count": n, "calls": ..., "usage": ..., "elapsed_seconds": ...}`，`batch` 字段改为 `assignment` 与 `document_id`。阅读 `records` 的脚本（`/tmp/analyze_journal.py` 等）需要同步改字段；`verify_coverage()` 只看 `phase == "scan"`，不受影响。

## 6. 改动四：`record_gap` 以 block_id 定位（删除文本匹配）

现状（`tools.py:488`）：`record_gap` 收一段 `evidence_text`，用 `normalize_evidence()`（`tools.py:58`）折叠空白、统一标点后做**整段连续子串**匹配，找不到就 `raise ValueError("evidence_text must come from the current batch")`；命中到的 block 才是 `block_id`/`section_path` 的来源。**定位依赖模型复述的文本，任何标点、空白、跨段拼接的差异都会让 gap 记不下来**——实测 48 次调用里 9 次因此失败（3.3%）。

改为：**block_id 由 reviewer 直接给，文本完全退出定位**。

```python
@server.tool
async def record_gap(
    kind: Kind,
    name: Annotated[str, Field(min_length=1, max_length=300)],
    block_id: str,                                 # 定位：必填、校验归属
    evidence_text: str | None = None,              # 引文：可选、原样存、不校验
    missing_attributes: list[str] | None = None,
) -> dict:
```

- 校验 `block_id` 在当前 batch 的 `segments` 里；不在则报错并列出本批前几个合法 id（`blk-000101` 这类编号，抄错能当场纠正）。block 允许被相邻 batch 共享（`sources.block_batches`），所以"属于本批"按 segments 成员判断。**这是唯一的校验，与文本无关。**
- `document_id` 与 `section_path` 由宿主从该 block 取（`document_id = batch["doc_id"]`、`section_path = block["section_path"]`），不再由匹配结果派生。
- `evidence_text` 给了就 `strip()` 后原样存进 gap（不折叠空白、不统一标点、不比对原文）；超过 4000 字符报错（纯存储上限），其余情况一律照收，不做证据校验。
- `note` 仍由宿主写 `cited from <batch_id>`。
- 去重键改为 `(kind, name, block_id, missing_attributes)`：**引文不参与去重**，否则同一缺口换句话再记一次就会重复。
- **删除 `normalize_evidence()`（`tools.py:58-71`）以及整段/跨块匹配逻辑**；`min_length=10` 约束移除（`max_length=4000` 保留为存储上限）。
- review 侧配合：`review_prompt`（`review.py:120`）把 `batch["segments"]` 渲染成带 id 的文本行，例如 `[blk-000101] （§2.2）…`；`start`/`end` 不再发送（跨度不再使用）。`REVIEW_INSTRUCTIONS` 写明：引用某段就用其 `block_id`，不要复述原文。
- 旧 gap 的兼容：迁移来的 gap 带 `evidence_text`，与新 gap 一样原样保留并在 prompt 里标注为"未校验引文"；定位仍只按 `block_id`。

这套为什么可行：batch 里一段就是一块（每 batch 95 块、每块约 208 字符），reviewer 面前就 95 个 id，且 id 与它正在读的段落相邻；而"用原文文字去反查是第几块"要求模型逐字复述，恰恰是它做不稳的事。引文则退化成"给人看的注脚"——存着方便事后审计，不再承担定位职责，所以它写得准不准都不影响流程。

风险与缓解：引文不校验，所以它可能是转述；缓解是它只作为提示出现（prompt 里标注"未校验"），fixer 一律以 `block_id` 回读为准。模型可能引用一个真实但不相干的 block。缓解方式是 gap fixer 的动作顺序被强制为"先 `read_source(block_id)` 确认该段确实缺这条内容 → 不支撑就按 `section_path`/名称重新定位 → 仍找不到就写进 `unresolved_gap_ids`"，并在 `records` 里统计"引用不支撑"的次数；**不再**用文本相似度做前置门槛（那会把定位问题重新绑回复述质量）。

## 7. 检查点结构与迁移

新结构：

```json
"review": {
  "batches": {
    "batch-00009": {"stage": "reviewed", "gaps": [...],
                    "review_seconds": 61.2, "review_calls": 14, "review_usage": {...}}
  },
  "groups": {
    "gap-rfc8446-1": {"status": "done", "document_id": "rfc8446",
                      "gap_ids": ["rfc8446-g1"], "seconds": 210.4,
                      "calls": 33, "unresolved_gap_ids": [], "usage": {...}}
  },
  "residual_gaps": []
}
```

- 删除 `review["cursor"]`：并行下"游标"没有意义，恢复粒度改为逐 batch 的 `stage`。
- 单 batch 阶段值：`new`（未审）、`reviewed`（已审）、`review_failed`（上次失败，resume 重试）。
- 进行中的组：`review.groups[gid] = {"status": "running"}` 先写，成功改 `done`，失败改 `failed`；resume 时 `failed`/`running` 的组重新执行（与旧流程 resume 重跑 gap fix 的语义一致，可能重复已写入的实体修改，但确定性校验兜底）。
- 旧 checkpoint 迁移（不拒绝 resume，避免丢掉 `tls-2h-high` 这类已跑到全局校验的任务）：

| 旧 `review.batches[batch_id].stage` | 迁移后 |
| --- | --- |
| `new` / `review` | 丢弃 entry，重新 review（review 只读，重跑安全） |
| `reviewed` | 保留 gaps，`status="pending"`，进入文档分组 |
| `gap` | 保留 gaps，`status="pending"`（上一次 gap fix 可能已部分写入，重跑覆盖同样的 gap） |
| `done` | 保留 gaps，`status="legacy_done"`，不重新修复 |

`cursor` 直接删除即可。迁移代码放在 `__init__` 恢复路径里（`runner.py:495` 之后，与 `scan_kinds` 校验并列），只在 `resumed` 时执行；新任务不写迁移日志。

## 8. 配置

`rsc/extract/cli.py:20`（`ExtractConfig`）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `review_concurrency: int` | 3 | Review 同时进行的 batch 数，`ge=1`；`=1` 退化为旧串行顺序（用于对照实验） |
| `gap_group_max: int` | 8 | 单个 gap assignment 覆盖的 gap 上限，`ge=1` |
| `review_requests_per_second` | 5（不变） | 全局限速，含重试 |
| `assignment_retries` / `max_tool_retries` | 2 / 4（不变） | 并行下的失败重试 |

`extract.2h-*.toml` 增加 `review_concurrency = 3`（或按 provider 敏感度用 2）。不新增独立 review 模型配置。

## 9. 文件级改动清单

| 文件 | 改动 |
| --- | --- |
| `rsc/rsc/extract/tools.py` | `make_server(current, readonly=)` 拆 `_read_tools` / `_write_tools`；`validate_entities` 允许 `review`；`record_gap(kind, name, block_id, evidence_text=None, missing_attributes=None)` 只校验 block 归属、引文原样存不校验、删除 `normalize_evidence`；`complete_gaps(summary, unresolved_gap_ids)` |
| `rsc/rsc/extract/runner.py` | `ACTIVE` ContextVar + `active` property；`review_toolset` + `review_catalog_tokens`（`stage_attempt`/`execute_stage` 收 `catalog_tokens`）；`review_gaps` 拆 `review_all` + `gap_groups`；`save`/`record` 原子化；`prompt()` 增加文档组分支；旧 checkpoint 迁移 |
| `rsc/rsc/extract/registry.py` | `Assignment.document_id`；`Workspace` 无需改动（`gaps` 已有） |
| `rsc/rsc/extract/review.py` | `gap_backlog_prompt()`；`review_prompt` 的 segments 渲染成 `[block_id]` 前缀行（去掉 `start`/`end`）；`REVIEW_INSTRUCTIONS` 增加"用 block_id 引用段落"；复用现有实体语义说明 |
| `rsc/rsc/extract/cli.py` | `review_concurrency`、`gap_group_max` |
| `rsc/extract.example.toml`、`extract.2h-*.toml` | 新配置项 |
| `rsc/README.md` | 新调度流程、配置说明、`records` 字段变化 |
| `rsc/tests/test_extract.py` | 见第 10 节 |

## 10. 测试

新增（沿用现有 fake agent / `FunctionToolset` 风格）：

1. `test_review_toolset_is_read_only`：`make_server(lambda: fake, readonly=True)` 的工具清单恰为 7 个且不含任何写工具；`tools.py:143` 的拒绝仍生效。
2. `test_review_batches_run_concurrently`：2 个 batch、fake agent 每次 `await asyncio.sleep(0.2)`，断言墙钟 < 串行和（用 `monotonic()` 差值），且两个 workspace 的 `active` 不串（各自 gap 落在各自 batch）。
3. `test_review_failure_is_isolated`：3 个 batch，1 个抛 `StageFailed`，断言另外 2 个 `stage == "reviewed"` 已落盘、失败的那个 `review_failed`，整体仍抛错；resume 后只重跑失败的 batch（断言成功 batch 的 `review_calls` 不变）。
4. `test_gaps_grouped_by_document`：两个文档、3 个 batch 产生 5 条 gap，断言生成 `gap-<doc>-1` 两组、组内 `gap_ids` 正确、组间串行（执行顺序）。
5. `test_gap_group_respects_max_and_ids_are_stable`：`gap_group_max=3` 时 7 条 gap 切成 3 组；resume 重算得到相同 `group_id` 与 `gap_id`。
6. `test_complete_gaps_records_unresolved_ids`：`complete_gaps(summary, ["rfc8446-g2"])` 把 g2 标 `unresolved`、其余 `done`；未知 ID 报错；无参数调用保持旧行为。
7. `test_record_gap_uses_supplied_block_id`：给当前 batch 的合法 `block_id`，断言 gap 的 `block_id`/`document_id`/`section_path` 全部来自该 block。
8. `test_record_gap_keeps_unmatched_text_verbatim`：传入与原文完全不同的引文，断言 `recorded=true`、`evidence_text` 原样保留（不折叠空白、不改写），不报错。
9. `test_record_gap_rejects_unknown_block_id`：给别的 batch 的 block id → 报错，错误信息给出本批合法 id 示例。
10. `test_record_gap_dedupes_by_block`：同 `(kind, name, block_id, missing_attributes)` 的第二次调用返回 `recorded=false`；引文不同也算重复。
11. `test_old_review_checkpoint_is_migrated`：构造旧结构（`cursor` + `done`/`reviewed`/`gap`），断言迁移后的 `review.batches[*].stage` 与 gap `status` 符合第 7 节表格，且不重复修复 `legacy_done` 的 gap。

需要改写：

- `test_batches_review_then_fix_strictly_serially`（`tests/test_extract.py:1778`）→ 改为"并行 review + 组后修复"，断言顺序从"逐 batch 交错"改为"先全部 reviewed、再出现 gap 记录"。
- cursor 断言（`1841`、`1911`、`1960`、`1966`）→ 改为 `stage` 断言。
- `test_review_failure_keeps_batch_unfinished_and_resume_retries_it`（`1881`）保留语义，断言 `review_failed`。
- `test_gap_failure_resumes_at_gap_stage`（`1920`）→ 组失败后 `groups[gid].status == "failed"`，resume 重跑该组。
- `test_review_records_gaps_with_batch_evidence_and_deduplicates`（`1710`）与两处 handler fixture（`1794`、`1934`）：把 `evidence_text` 参数换成 `block_id`。
- `test_review_rejects_evidence_outside_the_batch`（`1757`）→ 并入新测试 9（报文改为"block 不属于本批"）。
- `test_record_gap_accepts_a_sentence_across_block_boundary`（`2160`）→ 删除：跨块拼接随文本匹配一起消失。

回归：`rsc` 全量 pytest、`ruff check`。

## 11. 实施顺序

分 5 步，每步可独立验证、可回退：

1. **`record_gap` 改 block_id + review prompt 渲染**（第 6 节）。只动 `tools.py` / `review.py`，不碰调度；用历史 gap 回放做离线验证（按新参数重放 48 次调用，应全部落库），并改掉第 10 节列出的 4 个受影响测试。
2. **只读 toolset + 双 catalog**（第 3 节）。行为等价（reviewer 本来也写不了），先落地降低后续复杂度。
3. **ContextVar + `save`/`record` 原子化**（第 4.1、4.3 节）。不改流程，跑现有测试确认无回归。
4. **并行 review**（第 4.2 节），gap 仍按 batch 修复。跑 FTP 一份（RFC 959 + 3659，batch 少、快）做真实对照：墙钟、请求数、失败隔离。
5. **按文档聚合 gap + `complete_gaps` 新签名 + 迁移**（第 5、7 节）。跑 TLS 三 RFC 一次（batch 多、gap 跨 batch）。

真实对照指标（每份 run 记录到 `plans/` 下的结果小节或 run.log 摘要）：

| 指标 | 基线（串行） | 目标 |
| --- | --- | --- |
| Review 墙钟 | 20.7 min | ≤ 9 min |
| Review 请求数 | 273 | ≤ 270（并行不改请求总量） |
| `record_gap` 文本匹配失败 | 9 | 0（只校验 block id，文本不校验） |
| gap 引文质量 | 无从衡量 | `records` 统计 fixer 报"引用不支撑"次数 |
| Gap 墙钟 | 28.9 min | ≤ 27 min |
| Gap assignment 数 | 13 | ≤ 7 |
| 跨 batch 重复 gap 修复次数 | 1（FTP） | 0 |
| 最终发布 / 全局错误 | 未发布（3 错误） | 不劣于基线 |

启动方式不变：`hc run --bg --name <lowercase> --cwd ... --no-parent --outlive-parent`，模型 `openai-codex:gpt-6-luna`，`run_timeout_seconds=7200`。

## 12. 风险与非目标

- **Review 快照变旧**：并行时所有 reviewer 面对同一份 registry 快照，后审的 batch 不再"看到"前面 batch 的 gap 修复。可能多报一些同主题 gap——正是第 5 节按文档聚合要吸收的；但若某文档拆在 3 个组，同主题仍可能跨组重复，需要 `validate_entities` 与全局校验兜底。
- **并发与 provider 限流**：`review_requests_per_second` 是全局的，但 3 个 reviewer 同时长思考时仍可能触发 provider 端的并发上限；用 `review_concurrency=2` 降级，不改代码。
- **Gap 组变大**：一次改 8 个 gap 的确定性校验失败概率高于 1 个 gap；`gap_group_max` 可降到 4。
- **block 选错**：reviewer 可能引用真实但不相干的段落；靠 gap fixer 先回读确认（不支撑则重定位或报 `unresolved_gap_ids`）兜底，并统计"引用不支撑"次数，不做文本匹配门槛。
- **文档级 prompt 无原文上下文**：只给证据句 + locator，依赖 Fixer 主动 `read_source`；若实测 Fixer 偷懒（不读原文就写），把每条 gap 的 `section_path` 前缀 block 原文附上（≤3 block/ gap），代价是 prompt 变大。
- **非目标**：不做 gap 组间并行（写冲突）；不做实体级合并；不做 review 的跨 batch 共享内存（例如"某 state 已被别的 batch 报告"）；不改变 `protocol.json` / `unresolved.json` 结构；不处理全局校验收敛（`message_without_send` 这类结构错误是另一条线，见 `runs/tls-2h-medium-20261123T094514Z` 的 48 错误）。

## 13. 实施状态

第 11 节的五步已全部落地，实现与上文有 4 处细节差异，都是实施时才暴露的：

| 项 | 计划 | 实现 | 原因 |
| --- | --- | --- | --- |
| 并行工作区绑定 | `ACTIVE` ContextVar 可行，否则退回"每 batch 一个 toolset" | ContextVar 直接可用 | 用探针验证：`MCPToolset` 的每次工具调用都在调用方 task 的上下文副本里执行（同步工具也一样），并行 task 各自 `set` 互不影响，无需退回方案 |
| gap 组切分 | `chunks(pending, gap_group_max)` | 先按**文档全部 gap**（含已完成的）切分，再取组内 `pending` 子集执行 | 按 pending 切分时，已完成的组会让后面的组边界左移，导致同一个 `gap-<doc>-N` 覆盖不同 gap，未修 gap 可能永远轮不到。已加回归断言：模拟"第 1 组完成后崩溃"，resume 重跑 2、3 组且 gap 归属不变 |
| 批次阶段值 | `reviewed` / `review_failed` / 旧流程 `done` | 完成的 batch 停在 `reviewed`，`done` 只作为迁移来的历史值 | 组的完成状态记在 `review.groups`，batch 不再需要 `done`，也避免与迁移表混淆 |
| `complete_gaps` | 新签名，无参调用仍接受 | 两个参数都有默认值，schema 里 `required` 为空 | 单 batch 路径与旧调用方零改动 |
| 工具注册拆分 | `_read_tools(server)` / `_write_tools(server)` 两个内部函数 | 读工具保留 `@server.tool`，8 个写工具集中到一个注册循环 | 避免把约 200 行写工具整体缩进一层；效果相同，测试断言 readonly 恰好 7 个、全量 15 个 |
| `gap_id` 分配时机 | Review 完成时 | `gap_groups()` 开头按文档与 batch 顺序统一分配 | 编号规则只依赖 manifest 顺序，与 review 的并行完成顺序无关，所以放在用时分配同样稳定，且迁移来的旧 gap 也一并拿到 id |

其余按计划实现：reviewer 只读 toolset 恰好 7 个工具、双 catalog 预算、并行 `review_all()`（`asyncio.gather(return_exceptions=True)` + 信号量，失败隔离后整体抛错、resume 只重跑失败 batch）、按文档串行的 `gap_groups()`、`record_gap` 只校验 `block_id`、旧 checkpoint 迁移。

验证：`rsc` 全量 `pytest` 361 通过（其中 `tests/test_extract.py` 69 个，含第 10 节列出的 11 个新测试与 12 处改写/删除），`ruff check rsc` 通过（`tests/test_protocol*.py` 的 3 条历史告警与本次改动无关）。渲染后的 review prompt 比原来直接塞 `segments` JSON 小约 38%（TLS batch-00001：9818 → 6056 tokens），并行之外还白捡了一点输入压缩。

真实对照运行：FTP（RFC 959 + 3659，`extract.2h-medium.toml`）为 `rsc/runs/ftp-par-20261123T124519Z`，TLS（RFC 8446 + 6066 + 7301）待 FTP 结果确认后再跑；第 11 节指标表的结果在两次跑完后补在下面。

后续：扫描布局在同一批次里又合并为两轮（parties + channels + messages 一轮、交互单元一轮），见 `plans/rsc-merge-foundation-scans.md`。该改动只动扫描/Fix/校验阶段数，review 仍是"所有扫描结束后每 batch 并行审一次"，gap 仍按文档分组，本计划的行为与指标不受影响。
