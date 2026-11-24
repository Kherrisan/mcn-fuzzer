# RSC Document-based Review 实现与验收报告

## 结论摘要

- 已实现 `states`、`steps`、`step_dependencies` 三个类别 Fix 阶段的文档审查：每轮用最新注册表审查全部冻结 batch，逐 batch 并行、只读、无 MCP 工具、结构化输出 `uncovered`；宿主补来源、去重、写检查点，并作为 `document_review` 交给类别 Fix。
- 类别通过条件：确定性校验为空 **且** 一次新审查为空；审查结果只能由重新审查更新。
- 全局 Fix 不审查；遗留 uncovered 写入 `document_review_reference`，仅作参考，不阻断发布。
- 所有 document-review 请求共享一个异步限速器，默认 5 次/秒，可配置，provider 重试计入；不改变 Scan/Fix 请求策略。
- 验收：FTP（RFC 959 + 3659，Luna high，2h 上限）在 106 分钟内完成并发布 `protocol.json`，结构校验 0 错误；TLS（RFC 8446 + 6066 + 7301）在 80 分钟时因一个 batch 的 provider 连接错误退出，进度已保存（16/17 batch 可复用）。
- 关键限制：三个类别都在 12 轮修复上限处仍未清零 uncovered（55→53、89→105、31→43），审查为采样式发现，无法用“为空”判定收敛。这是本轮最主要的剩余问题。

## 修改文件

```text
rsc/rsc/extract/review.py     新增：审查范围、结构化输出、输入投影、合并去重、计数
rsc/rsc/extract/throttle.py   新增：共享异步限速器与 RateLimitedModel
rsc/rsc/extract/runner.py     审查 agent、review_batch/review_category、类别 Fix 接入、
                              document_review 提示、遗留参考、检查点、日志、耗时字段
rsc/rsc/extract/registry.py   Workspace.document_review
rsc/rsc/extract/tools.py      validate_entities / complete_fix 返回审查与 stale 标记
rsc/rsc/extract/cli.py        document_review_enabled、document_review_requests_per_second
rsc/README.md                 使用说明
rsc/extract.example.toml      示例配置
rsc/tests/test_extract.py     审查与限速测试
plans/rsc-document-based-review.md   补充共享限速要求
```

## 实现要点

### 审查范围与输入

| 当前类别 | 检查的实体类别 |
| --- | --- |
| states | parties、channels、messages、states |
| steps | 前五类，含 steps |
| step_dependencies | 全部六类 |

每个 batch 的输入：该 batch 原文与 block 定位、evidence 与该 batch 重叠的实体（完整 `value`，按 ID 去重，证据只保留 document/block/section/field 定位）、已扫描类别的名称与别名简表、类别、协议与启用扩展。不发送整份 RFC，不发送证据原文，不发送实体操作历史与完整引用边。

### 输出与合并

Pydantic 结构化输出六类 `uncovered` 字符串列表。宿主：

```text
补充 document_id / batch_id 来源
按类别 + 去首尾空白文本去重，保留全部来源
过滤审查范围外的类别（模型有时会报 steps/step_dependencies）
```

### 调度与判定

```text
全部 batch 同时 asyncio.gather 派发，不设固定并发数
每轮重新审查；注册表或轮次变化使缓存失效
确定性 issue 为零也审查
通过 = 确定性 issues 为空 且 uncovered 为空
到 max_repair_rounds 仍不空 → 保存 reference，进入下一类别
```

### 限速

```text
RequestRateLimiter：锁内预留时间槽，锁外 await asyncio.sleep
RateLimitedModel 包装审查模型，每次底层 request（含重试）先过限速
默认 5 次/秒，文档配置 document_review_requests_per_second，必须 > 0
取消时 sleep 立即中断，不阻塞事件循环
```

### 检查点、恢复与日志

- `document_review`：类别、轮次、注册表快照、逐 batch 状态与 uncovered、调用数、token、`wall_seconds`、`batch_seconds`。
- 恢复：同一类别、轮次、注册表快照相等 → 复用已完成 batch，只重试未完成项；注册表变化或新轮次 → 全量重审。
- 日志：`llm/document-review/<kind>/<kind>-<round>-<batch>.jsonl`，沿用 request/response 日志。
- `document-review` 记录写入 checkpoint `records`，含逐轮 uncovered 分类计数与耗时。

### 工具返回

`validate_entities` 与 `complete_fix` 返回 `document_review` 和 `document_review_stale`（本次 assignment 已有写入时为 true，表示尚未复审）。

## 测试证据

```text
rsc/.venv/bin/python -m pytest tests -q
338 passed, 78 subtests passed

rsc/.venv/bin/python -m ruff check rsc/extract tests/test_extract.py
All checks passed
```

新增测试（`rsc/tests/test_extract.py`）：

```text
审查输入：scope 过滤、完整实体、按 ID 去重、evidence 不含原文
uncovered 合并：去重、保留全部来源
零确定性 issue 时 uncovered 独立触发类别 Fix，修复后重新审查
同一注册表快照复用结果；注册表修改后全量重审
所有 batch 真实并行（峰值并发 ≥ 2）
审查失败保存已完成 batch；恢复只重跑失败 batch
超预算报错且不截断
轮次上限保存 reference，不阻断全局发布
全局 Fix 只收参考，不触发审查
仅 states/steps/step_dependencies 可审查
并发 batch 共享 5 次/秒限速；重试受限；可配置；取消及时
```

## 实测验收

### FTP（Luna high，2h 硬超时）

```text
任务目录 runs/luna-doc-review-ftp-20261122T143916Z
启动 14:39:16，结束 16:25:36（106 分钟）
结果 published=True，protocol.json 结构校验 0 错误
实体：parties 4、channels 3、messages 7、states 26、steps 21、step_dependencies 99
unresolved.json：2 条（依赖匹配无法完全对齐、若干 reviewed dependency 缺名称）
hc 终端：docftp2（t1g5）
```

各类别耗时（扫描 / 审查墙钟 / Fix）：

| 类别 | 扫描 | 审查轮数 | 审查墙钟 | Fix 轮数 | Fix 墙钟 |
|---|---:|---:|---:|---:|---:|
| states | 3.7m | 12 | 5.2m | 12 | 21.0m |
| steps | 3.7m | 12 | 7.3m | 12 | 18.6m |
| step_dependencies | 5.3m | 12 | 6.6m | 12 | 19.2m |
| 前四类 | 13.7m | — | — | — | — |
| 全局校验/发布 | — | — | — | — | 约 1m |

审查成本（36 轮）：

```text
调用 432 次（12 batch × 36 轮）
输入约 6.2M tokens，输出约 379k tokens
审查墙钟合计约 19.1 分钟
```

uncovered 变化：

```text
states            55 → 53（12 轮，未清零）
steps             89 → 105（12 轮，未清零）
step_dependencies 31 → 43（12 轮，未清零）
遗留 reference 43 条，最终未阻断发布
```

### TLS（Luna high，2h 硬超时）

```text
任务目录 runs/luna-doc-review-tls-20261122T143916Z
启动 14:39:16，15:59:41 退出（80 分钟）
结果 published=False
hc 终端：doctls2（t1g6）
失败点：steps 第 7 轮审查，batch-00011 provider 连接错误
        已完成 16/17 batch 结果已保存，恢复时只需重跑 batch-00011
之前最后一次 Fix 失败：validate-steps-6 模型未提交（已按策略封单继续）
```

| 类别 | 扫描 | 审查轮数 | 审查墙钟 | Fix 轮数 | Fix 墙钟 |
|---|---:|---:|---:|---:|---:|
| states | 7.5m | 12 | 6.0m | 12 | 17.7m |
| steps | 6.3m | 7（第 7 轮失败） | 4.0m | 6 | 11.9m |
| 其他 | 10.7m | — | — | — | — |

uncovered 变化：

```text
states  78 → 83（12 轮，未清零）
steps  137 → 148（7 轮，未清零）
```

## 缺口抽查

已修复的样例（对照首轮 uncovered 与最终 protocol.json）：

```text
"FTP logical byte size"           → 最终 states 中有 Server-DTP logical byte size
"User-DTP data port / listening"  → 最终 states 中有 User-DTP data port
"data connection full duplex"     → 进入 channels 描述
```

仍存在的样例：

```text
"transfer byte size is always 8 bits" → 未成为独立 state（可能被并入 processing 或遗漏）
ABOR 的两个分支                      → 最终 steps 是通用 "User-PI send FTP command"，
                                       未单列 ABOR；属于建模粒度差异，未能确认已覆盖
```

unresolved 中的两条也与审查相关：依赖条目无法与 registry 中的名称精确匹配，说明审查给出的自然语言缺口在转换为模型修改时仍有语义对齐损耗。

## 不收敛的原因（实测）

解析 FTP/TLS states 每轮审查的原始输出：

```text
相邻两轮 uncovered 文本精确重合：0
内容词 Jaccard ≥ 0.5 的相似条目：约 6%–38%
其余 60%–90% 为上一轮未出现的新表述或新细节
单个 batch 每轮报 0–15 条，同一 batch 逐轮波动
```

结论：审查是采样式的，每轮注意到的段落与表述不同；目录外类别（steps/step_dependencies）此前还会混入 states 结果（本轮运行后已加 scope 过滤修复）。因此“一次审查为空”在本设计下几乎不可达，三个类别都停在轮次上限。

## 剩余问题

1. **收敛判据**：建议改为“已知缺口清单清零”或“连续两轮无新增缺口”，而不是要求任意一次审查为空；可借鉴 ProtocolGuard 的一次性规则清单思路，把审查产物固化为稳定条目。
2. **遗漏基准**：情态动词只覆盖 23%（FTP）与 28%（TLS）的句子，不能作为完整性基准；更稳的是 block 级覆盖对账（每个 block 被引用或显式豁免），再叠加情态句优先级。
3. **审查成本**：FTP 36 轮审查 432 次调用、6.2M 输入 token；即使不收敛也占了约 19 分钟墙钟。需要在固定清单方案下把审查降为一次性或按需触发。
4. **失败处理**：TLS 的 provider 连接错误导致整体退出（符合计划“重试耗尽后保存进度并报错”），但 2h 验收因此未完成；可在验收时对单 batch 失败做一次跨轮重试。
5. **耗时字段**：本轮运行记录的 review `elapsed_seconds` 是逐 batch 之和（会高估），已改为 `wall_seconds` 与 `batch_seconds`；报告中的墙钟由 JSONL 时间戳重算。
6. **建模粒度**：FTP 的 steps 采用通用命令/应答步骤，审查按具体命令（ABOR/ACCT）提缺口，二者语义不完全对齐，导致部分条目无法“修掉”。
