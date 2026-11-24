# RSC 交互单元合并计划（states / steps / step_dependencies 单轮次写入）

## 目标

1. 把 `states`、`steps`、`step_dependencies` 合并为一个「交互单元」，每个 batch 只跑一次 scan assignment，三类实体在同一轮内一起写。
2. 让 `processing` 真正出现：step 的构造/校验/派生必须落到 `reads/writes/rule`，不再写成 `condition` 散文。
3. 让 `state` 由 producer 自然产生：某个后续 step 消费的值，由同一 hook 的 `writes: state.<name>` 建立。
4. 提示词同步改造：写作顺序、密码握手专项要求（恢复老版 crypto 段）、禁止只写 condition、提交前 `validate_entities`。
5. 明确所有受影响的限制条件与不变量，避免合并后出现越权写入、断点错位、校验死锁。

## 非目标

- 不改实体 schema（六类实体、字段都不变）。
- 不改 `review_gaps` / `gap` 阶段（仍按 batch 串行 Review + Gap Fix）。
- 不改源文本 plan 与 batch 划分（`source-plan.json` 不变）。
- 不改 review 的只读授权与 5 req/s 限速。
- 不引入新的 MCP 工具。

## 现状诊断

### 数据（`runs/tls-2h-medium-20261123T035942Z`，TLS1.3，RFC 8446+6066+7301）

```text
steps 37 / processing ops 2（before_send 2, after_send 0, on_receive 0）
states 3（其中 2 个是 gap fix 时顺手建的，1 个标成 external）
step_dependencies 47 / records 186
17 个 states scan 全部 changed_entities=[]（唯一整类空转；对照 steps 8/17、deps 6/17、
messages 10/17 有产出）
steps validation 走到 12 轮 category Fix，全部在搏 channel_requires_close / step_without_dependency
```

老流程同 3 个文档：states 47、steps 61、deps 70、ops 315（3.4 ops/step）。

### 代码成因

| 位置 | 内容 | 效果 |
| --- | --- | --- |
| `rsc/extract/registry.py:24` | `ORDER` 把 states 排在 steps 之前 | states 扫描时注册表里还没有 step，看不到「跨步骤值」 |
| `rsc/extract/registry.py:305` | scan 只能写本类 | states scan 无法改 step 补 producer；steps scan 无法建 state |
| `rsc/protocol/references.py:189-201` | `source=processing` 的 state 被读却无人写 → 结构错误 | states scan 一建 processing state 就报错，agent 直接放弃建 |
| `rsc/extract/validation.py:114-124` | `check_state` 只在已存在 state 无人读写时报错 | 「缺 processing / 缺 state」完全没有信号，`states validation: 0 structural, 0 coverage` |
| `rsc/extract/runner.py:97-146` | states/steps 规则只有抽象描述 | 无写作顺序、无 crypto 要求，agent 把逻辑写进 `condition` |

## 改动清单

### 1. `rsc/extract/registry.py`：声明扫描轮次与写入范围

```python
# 新增，紧邻 ORDER
SCAN_KINDS = ("parties", "channels", "messages", "steps")
# states 与 step_dependencies 不单独扫描，随 steps 一轮一起写
SCAN_WRITES = {"steps": ("steps", "states", "step_dependencies")}


def scan_writes(assignment):
    return SCAN_WRITES.get(assignment.kind, (assignment.kind,))
```

`allowed()`（第 305 行）改为：

```python
if self.assignment.phase == "scan" and kind not in scan_writes(self.assignment):
    raise ValueError(
        f"this scan can only write {', '.join(scan_writes(self.assignment))}"
    )
```

`repairable_categories`（第 281 行）在交互单元的类别校验上放开到全类：

```python
if self.assignment.phase == "category" and self.assignment.kind == "steps":
    return ORDER          # 交互单元的 Fix 可同时修 states/steps/deps
return ORDER[: ORDER.index(self.assignment.kind) + 1]
```

`ready` 公式不变（`repairable_categories[:-1]`），但结果随之变化：交互单元扫描期间 ready = parties..steps，
于是 `state_processing` 检查**当场生效**（`validate_entities` 会立刻报 producer / dangling reference），
`step_dependency` 检查仍延后到类别校验。这正是我们要的正向压力。

### 2. `rsc/extract/runner.py`：4 轮扫描 + 提示词

`scans()`（第 779 行）：

```python
async def scans(self):
    batch_ids = tuple(self.sources.batches)
    for index, kind in enumerate(SCAN_KINDS):
        if index < self.state["category"]:
            continue
        for position in range(self.state["batch"], len(batch_ids)):
            work = self.workspace(Assignment(batch_ids[position], "scan", kind))
            history, record = await self.retry(work)
            self.save(batch=position + 1, history=history,
                      registry=work.data, records=self.state["records"] + [record])
            print(f"{kind} {position + 1}/{len(batch_ids)} {work.assignment.id}", flush=True)
        await self.validate_category(index, kind)
```

`verify_coverage()`（第 1105 行）期望列表同步：

```python
expected = [(kind, batch) for kind in SCAN_KINDS for batch in self.sources.batches]
```

`prompt()`（第 481 行）按写入范围拼规则：

```python
if assignment.phase == "scan":
    scope = scan_writes(assignment)
    if len(scope) == 1:
        context["rule"] += f" During this scan, write only {assignment.kind}."
    else:
        context["rule"] = "\n".join(CATEGORY_RULES[k] for k in scope) + INTERACTION_RULES
```

新增常量 `INTERACTION_RULES`（通用措辞，不出现具体协议）：

```text
During this scan write steps, states and step_dependencies together for this batch,
as one coherent model rather than three passes. Order your own work:
  1) every send / listen / connect / close step with its processing hooks;
  2) each value a later step consumes becomes state.<name>, written by the same hook
     that computes it (`writes: state.<name>`); never declare a state no step reads or writes;
  3) dependency edges binding the prerequisite step and any field_relations.
Source-stated construction, verification, derivation, counter or key updates must appear in
reads / writes / rule. A condition that only narrates them is not an operation.
Before complete_batch: run validate_entities and repair every producer, dangling-reference
and missing-dependency row it reports.
```

### 3. 提示词优化（`CATEGORY_RULES`，第 80-146 行）

1. 新增 `CRYPTO_RULES`，同时挂到 `states` 与 `steps`（恢复老流程
   `rsc/workflow/instructions.py:32-35` 的 crypto 段）：

```python
CRYPTO_RULES = (
    "On cryptographic handshakes, capture the key schedule inputs and derived secrets, "
    "transcript or handshake-hash boundaries and exceptional updates such as rekeying, "
    "proof and message verification, and resumption or early-data material carried from "
    "the earlier connection. Keep each such value as an endpoint-owned state when a later "
    "step consumes it, and name the hook that writes it."
)
```

2. `states` 规则里删掉「producer 必须同一次改动给出」的硬表述，改为「声明即绑定」：

```text
Declare a state only when a step in this batch reads or writes it. A processing state must
be written by the hook that computes it; keep the state and its writer in the same assignment.
Enrich an existing state of the same owner and concept instead of adding a near-duplicate.
```

3. `steps` 规则补一句强制项：

```text
A send step whose source prescribes construction, validation, derivation or state advance
must carry at least one processing operation in the matching hook.
```

4. `rsc/extract/review.py` 的 reviewer 检查项各加一条：`step_without_processing` 视为 gap；
   「派生值只写在 rule / unresolved 里、没有 state 承载」视为 gap。

### 4. `rsc/extract/validation.py`：新增可见但可豁免的检查

在 `RegistryIndex.coverage()` 的 `if "steps" in ready:` 分支加：

```python
for key in self.by_kind["steps"]:
    value = self.records[key]["value"]
    if value["action"] != "send":
        continue
    if not any(op for op in (value.get("processing") or {}).values()):
        add("step_without_processing", key,
            f"Step {value['name']!r} sends {value['message_name']!r} with no processing",
            advisory=True)
```

`add()` 增加 `advisory` 参数并写入 `checks[-1]["advisory"] = advisory`。
计数与拒收只看阻塞项：

- `Workspace.issues()`（registry.py:662）过滤 `advisory`，用于 `complete()` 的拒收与
  `validate_category` 的错误计数；
- `validation_report`（tools.py:372）保留 advisory 行，另加 `advisory_count`。

这样 agent 在 `validate_entities` 里看得到「这步没有 processing」，但不会被它卡死；
是否豁免由 review 与全局阶段判断（源文确实未规定处理时可记为 gap 说明）。

### 5. 配置与预算

交互单元的单个 assignment 工作量约为三类之和，默认值不够：

| 参数 | 现值 | 建议 |
| --- | --- | --- |
| `max_requests_per_assignment` | 64 | 96–128 |
| `assignment_timeout_seconds` | 600 | 900–1200 |
| `max_tool_calls_per_assignment` | 192 | 256 |
| `run_timeout_seconds` | 7200 | 不变（2h 由外部控制） |

同时新增 `extract.2h-medium.toml` 的对应项（本次已建，只含 model/effort/timeout）。

### 6. 测试（`rsc/tests/test_extract.py`）

需要改写的既有断言：

```text
:349  (kind, batch) for kind in ORDER            →  SCAN_KINDS
:914  scan records for kind in ORDER             →  SCAN_KINDS
:982  ("scan","states", ORDER[:4])               →  删除该参数化用例
:970  "During this scan, write only steps."      →  断言 steps 的写入范围含 states/deps
```

新增用例：

1. `allowed()`：steps scan 可写 states 与 step_dependencies，不可写 messages；
2. `repairable_categories`：交互单元的 category Fix 返回 `ORDER`；
3. `deferred_checks`：交互单元扫描期间只有 `step_dependency` 延后；
4. `verify_coverage`：缺少任一 (SCAN_KINDS, batch) 组合时报错；
5. advisory：`step_without_processing` 出现在 `validate_entities` 但不计入拒收。

## 限制条件与不变量检查表

| # | 约束 | 位置 | 合并后 | 必须保持 |
| --- | --- | --- | --- | --- |
| 1 | scan 只写本类 | `registry.py:305` | 交互单元可写三类 | 仍禁止跨单元越权（messages scan 不能写 steps） |
| 2 | producer 硬检查 | `protocol/references.py:195` | 不放宽 | processing state 最终必须有 `writes`，只是现在同轮就能满足 |
| 3 | 类别校验可写范围 | `registry.py:281` | steps 单元放开到 `ORDER` | 不得让 review 有写权限（`review` 仍返回 `()`） |
| 4 | 覆盖检查按 ready 门控 | `validation.py:8` `COVERAGE_STAGES` | 不变 | `state_processing` 在交互单元扫描内即阻塞、`step_dependency` 延后 |
| 5 | 断点语义 | `state["category"]`/`batch` | category 变为 0..3 | 旧 checkpoint（0..5）必须拒绝续跑 |
| 6 | 扫描完整性 | `runner.py:1105` | 期望改为 SCAN_KINDS | 不能漏 batch，顺序仍是 kind 优先、batch 次之 |
| 7 | 实体种类枚举 | `tools.py` `Kind` / `ORDER` | 不变（仍是六类） | `list_entities(kind=...)`、`entity_counts`、`protocol_model` 不受影响 |
| 8 | review/gap 授权 | `registry.py:305` gap 分支 | 不变（gap 可写全部） | Review 仍只读，5 req/s 限速不变 |
| 9 | assignment 预算 | `cli.py:25/35/38` | 上调 | 不能超过 context/batch 预算推导上限（`cli.py:112` 的 `maximum`） |
| 10 | 单批 workload | 提示词 | 三类合一 | 必须留 `sealed partial work` 逃生路径（现有 `retry()` 已具备） |

另外两点容易踩：

- **引用解析**：`references.py` 的 `state.<name>` 解析按 `(party, name)` 唯一匹配。同轮写入
  若先把 step 引用了一个还不存在的 state，`validate_entities` 会报 `missing_reference`，
  但因为是 advisory/最终态校验，不会中断；因此提示词必须写明「先写 state 再被引用，或同批补齐」。
- **同轮重复实体**：`same_name` 检查要求全局唯一，交互单元一次写三类时最容易撞名，
  提示词应强调「Enrich an existing state of the same owner and concept」。

## 兼容与迁移

1. `extract-run.json` 增加 `"scan_kinds": list(SCAN_KINDS)`；resume 时若与当前代码不一致，
   直接报 `cannot change scan layout on resume`，要求新目录（与现有 `cannot change configuration
   on resume` 一致）。
2. 旧 checkpoint 的 `category` 语义是 `ORDER` 下标，无法映射到新的 4 轮；统一拒绝续跑。
3. 已发布 run 的产物（`protocol.json`）不受影响；`rsc stats`、`rsc extract --probe` 不变。

## 验证方案

1. 单测：`pytest tests -q`（340+ 现有用例全绿，新增 5 个用例）。
2. 30 分钟验证跑（`run_timeout_seconds=1800`），TLS1.3 medium，RFC 8446+6066+7301：

```text
门槛：ops/step ≥ 1.5         （现状 0.05）
      states ≥ 10            （现状 3）
      deps/step ≥ 1.5        （现状 1.27）
      records 中 states scan 空转条数 = 0
      validate_entities 的 advisory_count 逐批下降
```

3. 通过后跑 2h 全量：对照组仍是 `tls-2h-medium-20261123T035942Z`，
   额外检查 RFC 6066 是否产生 evidence（现状 0 块引用）。

## 风险与回退

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 单 assignment 过长，撞 `assignment_timeout` | 部分 work 被 seal | 上调 timeout；必要时把交互单元拆成「2 batch 一 assignment」 |
| 同轮三类写入放大 token | 上下文压缩更频繁 | 已启用 `stub_before_drop`、`keep_full_turns=2`，可降 `history_tool_chars` |
| advisory 检查被无视 | states 仍低 | review 侧把「无 processing」判 gap；指标里保留 ops/step |
| 断点不兼容 | 旧 run 不能续 | 明确拒绝并提示新目录；旧产物只读 |
| 越权写入回归 | 数据错乱 | `allowed()` 保持严格白名单，补单测 |

回退方式：`SCAN_KINDS` 改回 `ORDER`、`SCAN_WRITES` 置空、`repairable_categories` 恢复原分支、
`verify_coverage` 恢复 ORDER；提示词改动独立可回退；advisory 检查可整段删除。

## 分阶段落地

1. **P1 机制**：`SCAN_KINDS` + `scan_writes` + `allowed` + `repairable_categories` + `scans()` +
   `verify_coverage` + resume 拒绝（含单测）。
2. **P2 提示词**：`INTERACTION_RULES` + `CRYPTO_RULES` + states/steps 规则改写 + review 检查项。
3. **P3 预算**：`max_requests_per_assignment` / `assignment_timeout_seconds` / `max_tool_calls`。
4. **P4 advisory 检查**：`step_without_processing` + 计数过滤 + 单测。
5. **P5 验证**：30 分钟跑 → 达标后 2h 跑 → 与 `tls-2h-medium-20261123T035942Z` 对照。

## 实施状态（2026-11-23）

P1–P4 已落地，`pytest tests -q` 349 passed / 78 subtests，`ruff check rsc/extract tests/test_extract.py` 通过。P5 验证跑已启动。

| 阶段 | 落地内容 |
| --- | --- |
| P1 机制 | `registry.py`：`SCAN_KINDS`、`SCAN_WRITES`、`scan_writes()`、`allowed()` 白名单、`write_scope`、`repairable_categories`；`runner.py`：`scans()` 按 `SCAN_KINDS`、`verify_coverage()` 期望同步、`Runner` 校验 checkpoint 的 `scan_kinds`；`extract/cli.py`：`extract-run.json` 写入并校验 `scan_kinds` |
| P2 提示词 | `CRYPTO_RULES` 挂到 states/steps；`INTERACTION_RULES` 仅在三合一扫描时拼接；states 规则改为「声明即绑定」；steps 规则加「构造/校验/派生必须落到 processing」；`review.py` 加两条 gap 判据 |
| P3 预算 | `max_requests_per_assignment` 64→96、`max_interaction_requests_per_assignment` 64→128、`max_tool_calls_per_assignment` 192→256、`assignment_timeout_seconds` 600→900；`extract.example.toml` 同步 |
| P4 advisory | `validation.py` 的 `step_without_processing`（`advisory=True`）；`Workspace.issues()` 过滤 advisory、`advisory_issues()` 单列；`validate_entities` 返回 `advisory_count` 与 `advisory` 行；`solve_unresolved` 只要求阻塞项消失 |

一处与原文的偏差：`repairable_categories` 对 `kind == "steps"` 在 scan 与 category 阶段都返回 `ORDER`，否则 `ready = repairable_categories[:-1]` 到不了 `steps`，`state_processing` 不会当场生效。扫描实际可写范围由新增的 `Workspace.write_scope`（`scan_writes`）报告给 `complete_batch`，所以六类「可修复」与三类「可写」不再混淆。

P5 验证跑（TLS1.3，RFC 8446+6066+7301，medium）：实测 17 个 batch 的 parties 扫描约需 9 分钟，四轮扫描合计约 40 分钟，30 分钟预算到不了 steps 扫描，因此改用 `run_timeout_seconds=5400`（`extract.verify-medium.toml`）。跑完四轮扫描与 steps 类别校验后即可核对门槛，不必等到 review/global。

```text
rsc/runs/tls-verify-20261123T0655       容器内日志 /tmp/rsc-verify-tls.log
对照 rsc/runs/tls-2h-medium-20261123T035942Z
```

### P5 第一轮观察（`tls-verify-20261123T0655`，07:05 人工中止）

机制成立，但提示词过约束，导致 15/17 个 batch 直接放弃：

```text
batch-00002   11 steps / 1 state / 8 deps / 12 次请求 / before_send 与 on_receive 都写了
batch-00003..17   每个只用 1 次请求就 complete_batch，0 个实体
对照老流程同一批：steps 8 个 batch 有产出，batch-00005 产 10 个实体
汇总：steps 15、ops 13、states 2、deps 8 —— 远低于门槛
```

放弃时的摘要原文：「the current scan range lacks already-registered processing state and
complete cross-handshake lifecycle coverage, requiring later repair.」即模型把「一次写成一个
完整交互单元」理解为「本 batch 必须自带 state 与依赖，否则不许写」，于是整批跳过。

另外 steps 类别 Fix 造出重复步骤（`ClientHello initial send` 3 个、`ServerHello receive` 2 个），
触发 `same_name` 与 `ambiguous_reference`。

提示词修正（已落地，测试通过）：

1. `INTERACTION_RULES` 由「one coherent model rather than three passes」改为「write ... together
   in one sitting」，并显式允许部分产出：本 batch 支持多少写多少，states/依赖可由后续 batch 或
   本轮的类别 Fix 补齐，「Do not defer a source-supported step because the surrounding
   interaction is not yet complete」。
2. 「never declare a state no step reads or writes」改为「when you declare a state, name the step
   and hook in this batch that reads or writes it」。
3. 提交前自检由「repair every producer / dangling-reference / missing-dependency row」改为
   「repair the rows this batch introduced」，并声明只报后续 batch 未写实体的行属于那个 batch。
4. 新增「A directional transmission is one step ... put receiver-side work in that step's
   on_receive instead of adding a second step」与「give every step a name that is unique in the
   registry」，同时写进 `CATEGORY_RULES["steps"]` 以便 Fix 阶段也生效。

第二轮验证跑：`rsc/runs/tls-verify2-20261123T0705`（容器日志 `/tmp/rsc-verify-tls2.log`）。
