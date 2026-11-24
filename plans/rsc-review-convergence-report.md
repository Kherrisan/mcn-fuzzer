# RSC Review 收敛改造与真实测试报告

## 结论摘要

- 已按 `plans/rsc-review-convergence.md` 移除后三类类别阶段的并行 document review，改为全部扫描与类别确定性 Fix 之后、全局校验之前，按冻结 batch 顺序串行执行 `Review → 当前 batch Gap Fix → 下一 batch`。
- Review 是带工具的 agentic loop：可读当前 batch 文本、按 batch 查询实体、跨 batch 检索实体与原文，用 `record_gap` 记录 gap，用 `complete_review()` 结束。Reviewer 只读。
- 有 gap 时交给可修改全部六类的 Fixer，以无参数 `complete_gaps()` 结束；宿主保存后直接推进下一 batch，不复审、不做逐 gap 验收。
- 全局阶段仍只做原有确定性校验与必要全局 Fix，不再做文档审查。
- 全流程 `uncovered` 已改名 gap/gaps；保留默认 5 次/秒 Review API 共享限速（含重试）、角色授权、超时/失败不跳过与检查点恢复。
- 真实测试：FTP（RFC 959 + 3659）67 分钟完成并发布，结构校验 0 错误；TLS（RFC 8446 + 6066 + 7301）111 分钟完成全部 review/gap 阶段，但最终确定性全局校验剩余 7 个结构错误、6 次全局 Fix 后仍未通过，未发布。

## 旧任务停止核实

```text
启动本轮前：rsc extract 进程 0
docftp2（t1g5）：finished 0，FTP 正常结束并已发布
doctls2（t1g6）：finished 2，TLS 因 review batch provider 连接错误退出
rsc-review-smoke（t1g2）、doc-ftp-high（t1g3）、doc-tls-high（t1g4）：
  上一轮搭建时已停止的早期尝试
```

没有仍在运行的旧 luna-doc-review 进程需要停止；产物保留在 `runs/luna-doc-review-ftp-20261122T143916Z` 与 `runs/luna-doc-review-tls-20261122T143916Z`。未触碰其他 agent、任务或 hc core。

## 修改文件

```text
rsc/rsc/extract/review.py     六类实体概念、嵌套属性、审查边界；review/gap 提示组装
rsc/rsc/extract/throttle.py   共享异步请求限速器（默认 5 次/秒）
rsc/rsc/extract/registry.py   Assignment 新增 review/gap 阶段；工作区授权与可见类别
rsc/rsc/extract/tools.py      list_entities 支持 batch 过滤并返回完整定义；
                              record_gap / complete_review / complete_gaps；
                              审查角色只读、完成工具互斥
rsc/rsc/extract/runner.py     删除类别并行审查；新增串行 review_gaps 阶段、
                              review/gap agent 与提示、阶段检查点与恢复
rsc/rsc/extract/cli.py        review_enabled、review_requests_per_second
rsc/README.md                 新流程说明
rsc/extract.example.toml      示例配置
rsc/tests/test_extract.py     新流程测试（替换旧类别审查测试）
```

## 实现要点

### Review 提示词

介绍六类实体（Party、Channel、Message、State、Step、Step dependency）及关键属性，并展开：

```text
endpoint_sources；before_send/after_send/on_receive；
processing 的 reads/writes/rule；state.<name>/message.<field>/message_bytes；
step 条件与 dependency 条件；field_relations 的 prior/dependent/derive；
evidence 的文档/block/章节/field/note；注册表 ID 与 aliases 的区别
```

明确审查边界：不要求原文使用相同属性名、hook 名或变量名；不要求 State 承载算法；不在错误类别强加内容；不引入未确认字段。提示词中无特定协议或消息示例。

### 调度与角色

```text
run(): scans() → review_gaps() → finalize()
review_gaps(): 按 batch 顺序
  Review 阶段：只读工具 + record_gap + complete_review
  Gap Fix 阶段（仅当有 gap）：六类可写 + complete_gaps()
  每个 batch 保存 stage/cursor，完成后才进入下一 batch
```

授权（工具入口检查当前 assignment）：

```text
review：查询/原文/record_gap/complete_review；禁止实体修改与任何 Fix 完成工具
gap：查询 + 全部六类修改 + complete_gaps；禁止 complete_review
scan：complete_batch；category/global：complete_fix（保持原样）
```

### Gap 记录

`record_gap(kind, name, evidence_text, missing_attributes)`：

```text
宿主校验 evidence_text 来自当前 batch 文本
附加 document_id、匹配 block、section_path、batch_id、note
按 kind/name/evidence_text/missing_attributes 去重
不要求 agent 生成全局 gap ID，不自动展开 evidence 原文
```

### 恢复与失败

```text
检查点保存 review.cursor、每 batch stage（review/reviewed/gap/done）、gaps 与耗时
Review/Gap 失败或超时不自动封存，不推进 cursor；恢复继续同一阶段
Gap 失败时保留已提交的部分 registry 修改；Review 失败时保留已记录 gaps
```

## 测试证据

```text
pytest tests      335 passed（78 subtests）
ruff check        All checks passed
```

新增/更新测试：

```text
Review 提示词包含六类实体、嵌套结构与边界，且无协议示例
record_gap 证据必须来自当前 batch；宿主附加定位；重复提交去重
串行顺序：review(batch1) → gap(batch1) → review(batch2)
零 gap batch 直接 done，不进入 Fix，不产生 gap 日志
Reviewer 只读；complete_review/complete_gaps/complete_fix 角色互斥
Review 失败保留 stage=review 与 cursor，恢复重跑同一 batch
Gap 失败保留 stage=gap 与部分修改，恢复从 gap 阶段继续
list_entities(batch_id) 返回完整定义与 evidence 定位（不含原文）
complete_gaps 无参数（schema 无 properties）
共享限速：并发 batch 共用 5 次/秒、重试受限、可配置、取消及时
```

## 真实测试

### 配置与终端

```text
模型      openai-codex:gpt-6-luna，effort=high
配置      rsc/extract.2h-high.toml，run_timeout_seconds=7200
FTP       RFC 959 + RFC 3659
          terminal conv-ftp（t1g8）
          task dir runs/convergence-ftp-20261122T170540Z
TLS       RFC 8446 + 6066 + 7301
          terminal conv-tls（t1g9）
          task dir runs/convergence-tls-20261122T170540Z
启动      2026-11-22 17:05:40（两条同时）
```

### 总体时间与结果

| 协议 | 结束时间 | 总耗时 | 结果 |
| --- | --- | ---: | --- |
| FTP | 18:12:28 | 67 分钟 | published=True，protocol.json 结构校验 0 错误 |
| TLS | 18:56:41 | 111 分钟 | published=False，6 次全局校验后仍余 7 个结构错误 |

FTP 最终模型：parties 4、channels 4、messages 52、states 34、steps 71、step_dependencies 137、unresolved 0。

### 阶段时间

| 阶段 | FTP | TLS |
| --- | ---: | ---: |
| 六类扫描 | 28.7 分钟 | 37.2 分钟 |
| 类别确定性 Fix（轮数） | 7 轮 / 13.6 分钟 | 15 轮 / 24.1 分钟 |
| Review（batch 数 / 调用 / token） | 12 / 294 / 5.98M in, 29k out | 17 / 263 / 6.13M in, 39k out |
| Review 耗时 | 18.3 分钟 | 20.0 分钟 |
| Gap Fix（batch 数 / 调用 / token） | 6 / 33 / 0.72M in, 5k out | 9 / 63 / 2.05M in, 17k out |
| Gap Fix 耗时 | 2.8 分钟 | 7.4 分钟 |
| 全局校验 + 必要全局 Fix | 约 3 分钟（通过） | 约 22 分钟（6 次未通过） |

### 逐 batch Review / Gap Fix

FTP（12 batch，8 个 gaps，分布 6 个 batch）：

```text
batch-00001  3 gaps  review 189s/57c  gap  53s/ 7c
batch-00002  1 gap   review 119s/28c  gap  24s/ 5c
batch-00003  0       review  67s/17c
batch-00004  0       review 124s/33c
batch-00005  0       review 112s/30c
batch-00006  1 gap   review  77s/21c  gap  30s/ 8c
batch-00007  0       review  10s/ 4c
batch-00008  0       review 144s/42c
batch-00009  1 gap   review  73s/15c  gap  18s/ 4c
batch-00010  1 gap   review  99s/29c  gap  26s/ 6c
batch-00011  1 gap   review  54s/11c  gap  16s/ 3c
batch-00012  0       review  27s/ 7c
```

TLS（17 batch，27 个 gaps，分布 9 个 batch）：

```text
batch-00001  0  review  42s/ 9c
batch-00002  1  review  44s/10c gap  21s/ 3c
batch-00003  5  review 122s/26c gap  61s/ 8c
batch-00004  5  review 158s/28c gap  80s/ 8c
batch-00005  2  review  39s/ 8c gap  34s/ 3c
batch-00006  0  review  69s/20c
batch-00007  2  review  72s/15c gap  27s/ 4c
batch-00008  2  review  56s/13c gap  43s/ 5c
batch-00009  0  review   8s/ 3c
batch-00010  0  review  18s/ 4c
batch-00011  6  review  51s/ 9c gap 109s/24c
batch-00012  0  review  95s/22c
batch-00013  0  review   9s/ 4c
batch-00014  0  review  71s/11c
batch-00015  1  review 108s/23c gap  22s/ 4c
batch-00016  0  review  17s/ 8c
batch-00017  3  review 222s/50c gap  47s/ 4c
```

### 关键缺口抽查

FTP（已发布，抽查均落地）：

```text
gap: REST restart command 必须紧跟 RETR/STOR
  → 最终 deps 含 Send REST command → Send RETR/STOR command，带条件
gap: MLSD 数据连接关闭
  → 最终 steps 含 Send MLSD command、Send MLSD initial 150 response、
    Transmit MLSD listing entries、Close FTP passive user-server data connection
gap: 257 成功 MKD 响应字段
  → 最终 messages 含 Successful MKD response (257)
```

TLS（未发布，部分落地）：

```text
gap: Alert 需要 level 字段
  → registry 已有 Alert 消息
gap: 兼容模式 dummy ChangeCipherSpec
  → registry 已有 ChangeCipherSpec record
gap: Client-learned server supported-group preferences（states）
  → 已创建该 state，但没有 processing producer，成为最终 7 个结构错误之一
```

### TLS 未发布的剩余错误

```text
structure state 'Client-learned server supported-group preferences' has no processing producer
structure state 'Client CertificateRequest context' has no processing producer
structure state 'Client requested signature_algorithms' has no processing producer
structure state 'Client requested signature_algorithms_cert' has no processing producer
structure state 'Client requested certificate_authorities' has no processing producer
structure state 'Client requested oid_filters' has no processing producer
structure state 'Client requested client_certificate_type' has no processing producer
```

这些 state 是 gap 驱动补充进来的，但 Fixer 没有在对应 Step processing 中写入（或写入未被结构性检查认可）。`complete_gaps()` 按设计不做即时确定性校验，问题留到全局校验暴露；全局 Fix 6 次未能补齐生产者。

## 与旧方案的对比

```text
旧：类别阶段每轮并行审查全部 batch，36 轮审查、uncovered 不收敛、FTP 106 分钟
新：全局阶段每 batch 一次 Review + 有 gap 才 Fix，FTP 67 分钟、TLS 111 分钟
    FTP 8 gaps / TLS 27 gaps 全部有一次针对性 Fix，无重复全量扫描
```

新方案明显更快，且 gap 数量有限、可审计；FTP 的 gap 全部落地。主要遗留问题是 gap 驱动新增的 state 与确定性结构校验之间存在缺口。

## 剩余问题

1. **Gap Fix 与确定性结构校验的衔接**：新增 State 必须同时产生 producer（写入它的 processing），否则全局校验失败。完整方案可在 `complete_gaps()` 返回前运行一次只读结构检查（不阻断推进），把缺失生产者列回给 Fixer；或让提示词明确要求新增 State 时必须落到某个 Step 的 writes。
2. **TLS 未发布**：7 个 state 缺 producer，6 次全局 Fix 未解决；可提高全局 Fix 次数或把该结构错误作为 gap 修复阶段的提示。
3. **Review 调用偏高**：FTP 单 batch 最高 57 次请求、TLS 最高 50 次；查询工具充分使用，但成本高。可考虑初始上下文附带当前 batch 实体简表以减少探索性查询。
4. **状态对比**：FTP 发布模型规模明显大于旧流程（旧：states 26/steps 21/deps 99；新：states 34/steps 71/deps 137），说明 gap 驱动补充有效，但需注意是否引入过细实体。
5. **限速验证**：单测覆盖共享 5 次/秒、重试、取消；本报告未从真实日志单独统计发送间隔。
