# RSC 合并前三类扫描（parties + channels + messages 单轮）计划

## 1. 目标与验收

把 `rsc extract` 的扫描布局从 4 轮（parties / channels / messages / steps）改为 **2 轮**：

```text
basics（parties + channels + messages 同一次 assignment）→ validate-basics（1 个 Fix 阶段）
steps（交互单元：steps + states + step_dependencies）      → validate-steps（1 个 Fix 阶段）
```

验收：

1. 每个 batch 的扫描 assignment 从 4 个降到 2 个；category 校验/Fix 阶段从 4 个降到 2 个（前三类共用一个 Fix）。
2. 前三类实体在**同一次对话**里写完：先 parties，再 channels（用刚写好的 party 名当端点），再 messages；同 batch 内引用自洽，不再依赖"上一轮写过 parties"的记忆。
3. `pytest rsc` 全绿，`ruff check rsc` 干净。
4. **真实运行验收：FTP 与 TLS1.3 各跑 high 与 medium，共 4 组**（见 §9.5）：
   - FTP：`rfc959.html` + `rfc3659.html`，`extract.2h-high.toml` / `extract.2h-medium.toml`；
   - TLS1.3：`rfc8446.html` + `rfc6066.html` + `rfc7301.html`，同上两个 config；
   - 每组都要求：扫描 assignment 数 = 2 × batch 数、`validate-basics` 只出现一个阶段、review/gap 指标与同协议同档位基线持平、发布结果不劣于基线。

**review 说明**：review 是"所有扫描结束后，每个冻结 batch 并行审一次"，与类别数无关；合并不会改变 review 轮次与 gap 分组，只省掉 2 个 category Fix 阶段和 2 × batch 数 次扫描 assignment。

## 2. 范围与非目标

- 只改扫描布局、可写范围推导、扫描提示词、校验/Fix 阶段数与 resume 兼容。
- 不改：实体 schema、`protocol.json`/`unresolved.json` 结构、review/gap 逻辑与工具集、`sources.py` 的 batch 划分、限速与并发配置。
- 不加配置开关：布局是代码里的常量，新任务一律 2 轮。
- 不改旧的 4 轮代码路径语义：老 checkpoint 按它自己的布局 resume（见 4.5）。

## 3. 现状与动机（实测数据）

数据来源：`rsc/runs/ftp-par-20261123T124519Z/extract-checkpoint.json`、`rsc/runs/tls-2h-high-20261123T081416Z/extract-checkpoint.json`（`records` 里 `phase=="scan"` 与 `phase=="category"` 的 `elapsed_seconds` 求和）。

| 运行 | parties | channels | messages | steps | 前三类合计 | category Fix |
| --- | --- | --- | --- | --- | --- | --- |
| FTP medium（4 轮） | 0.8 min | 1.4 min | 2.1 min | 4.9 min | 4.3 min | channels 1 次 1.7 min |
| FTP medium（并行版起点） | 0.9 | 1.2 | 3.7 | 10.3 | 5.8 | channels 2 次 1.7 min |
| TLS high | 1.7 | 3.1 | 8.3 | 23.1 | 13.1 | channels 1 次 0.7 min |

实体量（FTP 并行版）：parties 9、channels 5、messages 64、steps 23、deps 11。前三类每轮都很短，但每个 batch 的原文被重复发送 4 次、对话被重建 4 次。合并后每 batch 原文只发 2 次，扫描 assignment 减半；预计每跑省 3–6 分钟墙钟和约 1/3 的批次原文输入 token。收益以结构简化与 token 为主，不是大幅缩短总时长（steps 与全局校验仍占大头）。

`party_channel` 覆盖检查会提前到 basics 结束后（以前要等 channels/messages 轮），缺通道端点能在第一个 Fix 阶段一起修。

## 4. 设计

### 4.1 布局常量（`rsc/rsc/extract/registry.py`）

```python
ORDER = ("parties", "channels", "messages", "states", "steps", "step_dependencies")
SCAN_KINDS = ("basics", "steps")
SCAN_WRITES = {
    "basics": ("parties", "channels", "messages"),
    "steps": ("steps", "states", "step_dependencies"),
}
LEGACY_SCAN_KINDS = ("parties", "channels", "messages", "steps")
```

`scan_writes()` 不变：`SCAN_WRITES.get(kind, (kind,))`，旧的单类布局仍能用。命名取 `basics`；`Assignment.kind` 是字符串，记录 `kind="basics"`，Fix 名为 `validate-basics`、`validate-basics-2`……

### 4.2 可写/可修/ready 的统一推导

`repairable_categories` 原来用 `ORDER.index(kind)`，合并 kind 会 KeyError；改为按 `scan_writes` 的最后一项取前缀：

```python
scope = scan_writes(self.assignment)
return ORDER[: ORDER.index(scope[-1]) + 1]
```

- `basics` → `ORDER[:3]`
- `steps` → `ORDER[:6]`（与原 `if kind == "steps": return ORDER` 等价，该特例已删除）
- 旧单类 `parties/channels/messages` → 与原行为完全相同

`ready` 保持 `repairable_categories[:-1]`：basics 扫描期间 `ready == ("parties", "channels")`、`deferred_checks() == ["channel_step"]`；basics Fix 期间 `ready == ORDER[:3]`、deferred 为 `["channel_step", "message_send"]`；steps 扫描/Fix 与原来一致。

### 4.3 提示词

新增 `BASICS_RULES`：要求一次写完三类，顺序 parties → channels → messages，**不允许**写 steps/states/dependencies（缺 send/缺 channel action 用 `record_unresolved` 记账），提交前 `validate_entities` 一次。扫描分支按 scope 是否含 steps 选择附加规则：

```python
context["rule"] = "\n".join(CATEGORY_RULES[k] for k in scope)
context["rule"] += "\n" + (INTERACTION_RULES if "steps" in scope else BASICS_RULES)
```

Fix 提示里写死的"Repair related messages, states, steps and dependencies…"改成按 `repairable_categories` 拼接。

### 4.4 校验与 Fix

`scans()` 与 `validate_category()` 结构不变，kind 变成 `basics`：记录 `kind="basics"`、日志 `llm/category/basics/validate-basics.jsonl`、run.log 打 `basics 1/N` 与 `basics validation 1: …`；`max_repair_rounds`、拒绝条件、请求上限（scope>1 时用 `max_interaction_requests_per_assignment=128`）都不变。

### 4.5 resume 与既有 checkpoint

布局从 checkpoint 读取：`self.scan_kinds = tuple(state["scan_kinds"] or SCAN_KINDS)`，只接受 `SCAN_KINDS` 或 `LEGACY_SCAN_KINDS`；`scans()`、`verify_coverage()` 用 `self.scan_kinds`；`cli.py` 的 resume 守卫改成"必须是已知布局"。6 轮（states 独立）旧 checkpoint 仍拒绝。**4 轮旧目录仍可 resume**（例如 `runs/ftp-par-20261123T124519Z` 已完成扫描，只差 review/gap）。

### 4.6 记录、日志、指标

scan 记录 `kind="basics"`；`llm/scan/basics/batch-000NN.jsonl`；`extract-run.json.scan_kinds` 新任务为 `["basics","steps"]`；`hook_stats`、gap 组、`protocol.json` 不变。

### 4.7 review / gap 不变

`review_all()`、`gap_groups()` 与三个 review prompt 都不感知扫描布局。

## 5. 契约（公共字段）变化

| 位置 | 变化 |
| --- | --- |
| `extract-run.json.scan_kinds` | 新任务 `["basics","steps"]` |
| `extract-checkpoint.json.scan_kinds` | 同上；旧目录保持原值 |
| checkpoint `records[].kind`（scan/category） | `"basics"` |
| `llm/scan|category/basics/*.jsonl` | 新路径 |
| `protocol.json` / `unresolved.json` | 不变 |
| CLI | resume 接受两种布局，拒绝其它 |

## 6. 边界与失败模式

| 场景 | 行为 |
| --- | --- |
| basics 扫描里写 step/state/dependency | `allowed()` 报 "this scan can only write parties, channels, messages"；提示词要求 `record_unresolved` 记账 |
| 某 batch 的 messages 很多（FTP 一轮 64 个） | 仍是一个 assignment；超请求上限按现有语义 seal 部分成果，`validate-basics` 继续修 |
| 跨类别问题（party 无 channel） | basics 校验即可见，basics Fix 可一起修 |
| 缺 send step | `message_send` 仍 defer 到 steps，basics Fix 不负责 |
| 12 轮上限 | 到顶后剩余问题交给全局校验 |
| resume 在 basics 中途 | `category=0`、`batch=k` 续跑 |
| 4 轮旧 checkpoint | 按自己的布局 resume，行为不变 |
| 新目录混用旧布局 | 不允许：`scan_kinds` 创建时写死并校验 |

## 7. 测试

新增：`test_basics_scan_writes_three_categories`、`test_basics_scan_ready_and_deferred`、`test_basics_fix_repairable_categories`、`test_basics_prompt_orders_parties_then_channels_then_messages`、`test_legacy_four_pass_checkpoint_resumes`。

改写：`test_complete_run_and_resume`（basics 日志路径、turns 断言）、`fake_model`（basics 一次追加三类 fixture）、`test_category_validation_is_visible_and_repairable`（basics 两批次重复名称 → same_name → 动态 merge）、`test_scan_and_fix_prompt_describe_write_scope` 与 `test_completion_tools_return_repairable_categories` 参数表各加 basics 两例。

## 8. 文档

- `rsc/README.md`：四轮改两轮，说明 basics 一次 Fix、缺 send 归交互单元。
- 本文件；`plans/rsc-parallel-review-gap-by-document.md` §13 补一句扫描布局改为 2 轮。

## 9. 实施步骤与验证

1. `registry.py`：常量 + `repairable_categories` 前缀化。
2. `runner.py`：`BASICS_RULES`、扫描规则分支、Fix 提示句、布局从 checkpoint 读取。
3. `cli.py`：resume 守卫改已知布局。
4. 测试；`pytest` 全量与 `ruff`。
5. **真实运行验收（四组，串行）**

   | # | 协议 | RFC 输入 | config | 新目录 |
   | --- | --- | --- | --- | --- |
   | 1 | FTP | `tests/fixtures/profuzzbench-rfc/FTP/rfc959.html`、`rfc3659.html` | `extract.2h-medium.toml` | `runs/ftp-2h-medium-<TS>` |
   | 2 | FTP | 同上 | `extract.2h-high.toml` | `runs/ftp-2h-high-<TS>` |
   | 3 | TLS1.3 | `.../TLS/rfc8446.html`、`rfc6066.html`、`rfc7301.html` | `extract.2h-medium.toml` | `runs/tls-2h-medium-<TS>` |
   | 4 | TLS1.3 | 同上 | `extract.2h-high.toml` | `runs/tls-2h-high-<TS>` |

   每组核对：`basics 1/N` 与 `basics validation 1` 出现且只有一个 `validate-basics` 阶段；scan 记录数 = 2 × batch 数且 `kind` 只有 `basics`/`steps`；review/gap 请求数与墙钟跟同协议同档位基线持平（±20%）；扫描阶段输入 token 与墙钟下降；发布结果不劣于基线（基线：`ftp-2h-high-20261123T081416Z`、`ftp-2h-medium-20261123T094514Z` 已发布；`tls-2h-high-20261123T081416Z`、`tls-2h-medium-20261123T094514Z` 未发布）。

   串行执行（provider 限流/出口不适合并行），每组约 1.5–2.5 h，可用同一命令 resume。

## 10. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| 单 assignment 变长（三类一起写） | 128 请求上限 + seal 语义；必要时把 basics 拆回 parties+channels / messages（只改常量与 `SCAN_WRITES`） |
| 模型被 messages 吸引、漏写 parties/channels | `BASICS_RULES` 强制顺序 + `validate_entities` 自检 + `party_without_channel` 立即暴露 |
| 老目录与新目录混用 | `scan_kinds` 创建时写死、resume 校验 |
| 前三类 Fix 问题变多 | 实测这三类 Fix 仅 0–2 轮；超限交给全局校验 |

回退：`SCAN_KINDS = LEGACY_SCAN_KINDS`、`SCAN_WRITES` 去掉 `basics` 键；resume 分支保留即可新旧都能跑。

## 11. 实施状态

已落地（2026-11-23）：

- `registry.py`：`SCAN_KINDS=("basics","steps")`、`SCAN_WRITES` 两键、`LEGACY_SCAN_KINDS`；`Workspace.repairable_categories` 改为按 `scan_writes` 最后一项取 `ORDER` 前缀，`steps` 特例删除（两种布局结果一致）。
- `runner.py`：`BASICS_RULES`；扫描规则按 scope 是否含 steps 选择 `INTERACTION_RULES`/`BASICS_RULES`；Fix 提示改为按可修类别拼接；`scan_kinds` 从 checkpoint 读取并校验；`scans()` 与 `verify_coverage()` 改用 `self.scan_kinds`。
- `cli.py`：resume 守卫接受 `SCAN_KINDS` 与 `LEGACY_SCAN_KINDS`。
- 测试：`tests/test_extract.py` 69 → 78（新增 5 个 basics/legacy 测试，`fake_model` 与 3 个既有测试改写，2 个参数表各加 basics 两例）。全量 `pytest` 370 passed、78 subtests；`ruff check rsc tests` 只剩 `tests/test_protocol*.py` 的 3 条历史告警（与本次无关）。
- `rsc/README.md` 改为两轮扫描的说明。

### 11.1 真实运行结果

第一组 **FTP medium**：`runs/ftp-2h-medium-20261123T141737Z`（新布局 2 轮，已发布），对照 `runs/ftp-2h-medium-20261123T094514Z`（旧布局 4 轮，已发布）。

| 指标 | 旧布局 4 轮 | 新布局 2 轮 | 变化 |
| --- | --- | --- | --- |
| 扫描 assignment | 48 | 24 | −50% |
| 扫描墙钟 | 9.2 min | 8.0 min | −13% |
| 扫描输入 token | 6.08M | 4.98M | −18% |
| 前三类扫描（parties+channels+messages） | 4.3 min / 2.88M | basics 2.4 min / 1.70M | −44% / −41% |
| category 阶段数 | 8（含 parties/channels/messages 各一次） | 5（basics 一次 + steps） | −3 个阶段 |
| category 墙钟 | 5.6 min | 3.3 min | −41% |
| review | 12 batch / 3.8 min | 12 batch / 4.2 min | 持平（仍每 batch 一次、并行 3） |
| gap | 3 组 / 1.7 min | 2 组 / 1.3 min | 按文档分组不变 |
| 全局校验 | 1 次 / 1.0 min | 1 次 / 0.4 min | — |
| 发布 | 是（unresolved 11） | 是（unresolved 2，residual 0） | 不劣于基线 |

`basics` 的校验一次通过（1 条 `category-validation` 记录、0 问题），没有触发 Fix；`validate-steps` 5 轮修复（旧布局 steps Fix 6 轮）。实体量 4 parties / 3 channels / 5 messages / 26 steps / 22 deps，对照旧布局 4 / 3 / 9 / 31 / 25：messages 少了 4 个，是本轮唯一的质量降幅，先记为观察项，四组跑完一起判断是否需要把 messages 的枚举规则写得更硬。

第二组 **FTP high**：`runs/ftp-2h-high-20261123T143433Z`（新布局，已发布），对照 `runs/ftp-2h-high-20261123T081416Z`（旧布局，已发布）。

| 指标 | 旧布局 4 轮 | 新布局 2 轮 | 变化 |
| --- | --- | --- | --- |
| 扫描 assignment | 48 | 24 | −50% |
| 扫描墙钟 | 37.0 min | 19.7 min | −47% |
| 扫描输入 token | 17.45M | 10.68M | −39% |
| 前三类扫描 | 10.4 min / 4.68M | basics 5.9 min / 3.93M | −43% / −16% |
| 交互单元扫描 | 26.6 min / 12.77M | 13.7 min / 6.75M | −48% / −47% |
| category 阶段数 | 6（parties/channels/messages 各一次 + steps 6 轮） | 3（basics 一次 + steps 3 轮） | −3 阶段 |
| category 墙钟 | 10.1 min | 6.4 min | −37% |
| review | 12 batch / 24.1 min / 316 req | 12 batch / 16.8 min / 243 req | −30% / −23% |
| gap | 10 组 / 11.6 min | 4 组 / 12.0 min | 组数少、单组更长 |
| 全局校验 | 1.2 min | 3.5 min / 50 req | 变长（问题更集中） |
| 发布 | 是，unresolved 5 | 是，unresolved 2，residual 0 | 不劣于基线 |

`basics` 同样一次校验通过、0 问题、无 Fix。**质量观察项**：新布局的交互单元实体数为 steps 54 / states 6 / deps 53，旧布局 68 / 19 / 151——deps 降幅最大。两组都发布成功，但 deps 数量差异需要结合 TLS 两组再看：如果 TLS 也出现同样幅度的 deps 下降，就要单独查"每个 exchange 的 prerequisite 边是否写全"，而不是当成布局合并的副作用。

第三组 **TLS medium**：`runs/tls-2h-medium-20261123T152231Z`（新布局，未发布），对照 `runs/tls-2h-medium-20261123T094514Z`（旧布局，未发布）。

| 指标 | 旧布局 4 轮 | 新布局 2 轮 |
| --- | --- | --- |
| 扫描 assignment | 68 | 34 |
| 扫描墙钟 / 输入 token | 15.1 min / 12.67M | 17.5 min / 17.76M（**变差**） |
| basics / 前三类校验 | parties/channels/messages 各一次，channels 与 messages 各 1 轮 Fix | 一次通过，0 问题 |
| steps Fix | 12 轮（打满上限）/ 18.9 min | 12 轮（打满上限）/ 17.2 min |
| review / gap | 17 batch / 5.6 min，4 组 / 2.2 min | 17 batch / 3.8 min，2 组 / 2.2 min |
| 全局校验 | 6 次后失败（48 个 `message_without_send`） | **6 次后失败**（35 `step_without_dependency` + 6 `missing_reference` + 3 structure + 1 `state_without_access`） |
| 实体量 | 3 / 3 / 46 / 35 / 7 states / 20 deps | 2 / 1 / 41 / 48 / 22 states / 12 deps |

两组都没发布。**这组暴露了 merge 的两个真问题**（不是"变快变慢"，是质量问题）：

1. **basics 阶段 effort 分配失衡**：17 个 batch 里模型把力气放在 messages（41 个），channels 只写了 1 个、parties 2 个（旧布局有独立 channels 轮，强制产出 3 个 channel）。TLS 有 TCP 与 DTLS 两套传输，1 个 channel 明显偏少，而现有确定性检查里没有"缺整类实例"这一类，`party_without_channel` 也只在 party 完全没做端点时才报。
2. **deps 边写不全**：48 个 step 只配到 12 条 dependency，35 个 `step_without_dependency` 一直到全局校验都没修掉——和 FTP high 的 deps 53 vs 151、FTP medium messages 9→5 是同一类现象。

扫描 token 反而涨了 40%（17.76M vs 12.67M）：合并后的 basics 每 batch 平均 5.8 次请求，比旧的三轮合计 4.75 次更多，每多一次请求就重发一次完整 batch 文本——"少发两遍 batch"的收益被"basics 内部请求数变多"吃掉了。

### 11.2 八个 profuzzbench 协议的高档扫描（新 prompt，2 并发，2 h 上限）

运行目录 `runs/pfb-<协议>-high-20261123T172142Z`，驱动脚本 `.tmp/sweep_protocols.sh`，汇总日志 `runs/sweep-2h-high.log`；每组用 `extract.2h-high.toml`（gpt-6-luna, effort high），外层 `timeout 7200`。

| 协议 | 墙钟 | 发布 | parties | channels | messages | steps | deps | basics | steps 扫描 | 停在哪 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DTLS | 63 min | ✅ | 2 | 1 | 17 | 29 | 31 | 8.0 | 10.1 | 全局校验通过 |
| FTP | 39 min | ✅ | 4 | 3 | 4 | 16 | 40 | 6.5 | 12.3 | 全局校验通过 |
| RTSP | 124 min | ❌ | 4 | 16 | 5 | 94 | 50 | 21.8 | 54.4 | **2 h 超时**，steps Fix 第 8 轮仍 113 问题（54 structure / 38 dep） |
| SSH | 75 min | ✅ | 2 | 1 | 35 | 79 | 77 | 4.8 | 21.1 | 全局校验通过 |
| DNS | 96 min | ✅ | 6 | 7 | 3 | 32 | 31 | 12.8 | 40.7 | 全局校验通过 |
| SMTP | 71 min | ✅ | 6 | 5 | 30 | 85 | 96 | 13.0 | 25.7 | 全局校验通过 |
| SIP | 120 min | ❌ | 10 | 19 | 9 | 66 | 79 | 25.8 | 41.8 | **2 h 超时**，steps Fix 打满 12 轮后 review 进行中 |
| TLS | 113 min | ❌ | 3 | 6 | 25 | 96 | 143 | 21.9 | 31.5 | 全局校验 6 次失败（17 `state_without_access` / 9 structure） |

8 组里 5 组发布。**抽象 party 规则生效**，而且 prompt 里没有任何协议专有名词：

- TLS 现在有 3 parties（含 `TLS-terminating middlebox`）和 6 channels，其中 4 条是"client→middlebox""middlebox→server"（TLS 1.3 与 1.2 各两条）——上一版（旧 prompt）只有 2 parties / 2 channels；
- SMTP 写出 6 parties（client / server / relay / gateway / originating system / delivery system）和 5 条会话通道；
- DNS 写出 6 parties（Resolver / Name Server / Primary / Secondary / Stub Resolver / Recursive Server）和 7 条通道；
- DTLS、SSH 各 1 条通道——这两个协议本来就只有一个传输，属正确结果。

两组超时都是**批量太大**（RTSP 36 batch、SIP 33 batch）而不是布局问题：扫描段本身 76 / 68 分钟，剩下的时间不够 12 轮 Fix + review + gap。TLS 的失败是 `state_without_access`（17 条）这类建模收敛问题，与布局无关——旧 prompt 的 2 轮跑同一协议也是全局校验失败。

待办：RTSP/SIP 用 4 h 上限重跑各一次（只改 `timeout`，不改布局），确认是预算问题而非能力问题。
