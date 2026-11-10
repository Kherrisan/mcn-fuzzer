# RSC 提交结果式并行流程

本计划替代 `rsc-parallel-parse-review.md` 中的 LLM Review/Fix、reading receipt、报告及增量复审设计。目标是减少 agent 往返和维护代码，保持最终 version 8 protocol.json 格式。

## 流程

1. Server 复用现有 ingest/source batch 和章节局部性划分。按原文 token 权重分配连续、接近等量的 batch 区间，默认 3 个 Parser。
2. Parser 按 batch 顺序提交实际提取结果：parties、messages、channels、steps、step_dependencies。Server 为所有 item 分配简单内部 ID；除 dependencies 外均有 name。每个 Parser 共用自己的局部 item 集合，允许按已返回 ID 更新已有定义。可补读任意来源以发现跨范围依赖。
3. 冻结 Parse 结果。一个 Parser 读取完整名称索引，必要时查询完整定义、引用和 RFC，将同一概念聚类。所有具名 item 恰好属于一个同类型 group，允许 singleton；dependencies 不参与名称聚类。
4. 默认 3 个 Merge worker 并行处理 groups。每个 group 提交一个 canonical item，保留最小数字 ID，统一名称、去重并整合来源约束。Worker 在同一会话内连续处理 groups，避免为每个 group 重启进程。
5. Server 生成并执行 revision table，包含旧 ID 到 canonical ID、删除 ID、修改 item/path/before/after 及责任 group。先汇总所有结果，再统一替换结构引用。Review 仅执行确定性检查，通过后发布。

## 六个 tools

- `read_source_batches`：完整来源 batch；分页继续读 remaining。
- `search_source`：定位跨范围上下文。
- `query_model`：局部提取结果、冻结 index/items、groups、检查后的 candidate。
- `submit_parse_result`：提交一个 batch 的实际 items，可完整更新自己已有 item。
- `submit_clustering_result`：提交所有具名 item 的完整分组。
- `submit_merge_result`：提交一个 group 的 canonical value、evidence，及必要的 dependency/text updates；也可报告 regroup_reason。

删除 claim_assignment、apply_changes、advance_phase 及旧 review/finding/resolution 工具。提交成功后由 server 决定继续派发、进入下一阶段或结束；响应返回下一 batch/group 和 stop_turn。所有角色共享读取能力及 schema/evidence 校验实现。

## 合并与冲突

- Parse 引用绑定明确 ID 或唯一局部 name，不能按全局最后一个同名项随意选取。跨 Parser 的悬空/歧义引用可暂存至 Merge。
- 聚类必须完整、无重复、同类型。相同概念的重复和冲突定义由 Merge agent 依据 RFC 合成一个结果。
- 所有 Merge agent 读取同一个冻结输入，不直接并发修改全局模型。最终结构引用经完整 ID 替换表转换为 canonical name。
- dependency 的写入者是 dependent_step 所属 group；更新后只对完全相同的 dependency 去重，不同条件保留。
- 自由文本不做全局字符串替换。跨 group 修改须明确 id/path/before/after/evidence；与其他提案或 canonical 定义冲突时返回责任 group。
- 确定性检查复用 schema、唯一名称、结构引用、连接角色、字段引用检查。受影响 groups 局部重做；同名冲突或错误聚类退回 Cluster。未变化的 group 结果复用。
- 最多两轮修正，仍有硬错误则失败，不导出 protocol.json。报告明确 semantic_review=false；结构通过不能声称独立语义审查通过。

## 调度、恢复与代码边界

- 复用 HC supervisor、native Codex/Pi adapters；串行初始化避免共享 SQLite 争抢，ready 后公平并行派发。HC terminal 与 thread 同名。
- 提交参数、响应及完成状态原子持久化；相同请求重试返回原响应，不同内容重试拒绝。确认进程死亡才替换 attempt，撤销旧凭证，保留已提交的 batches/groups。
- 派发前持久化 send intent，首个有效工具调用确认接收，不再增加 claim 往返。重启可继续自动推进；不因等待超时而重复派发。
- 不在 SQL 写事务中执行 LLM 推理或 HC 命令。保留 task-local service/supervisor 锁，避免竞争写入和跨 campaign 清理。
- 新状态 storage v3，不做旧数据库原地迁移；旧任务用原源码快照恢复，历史只读审计单独保留。
- 代码保持简洁，避免通用任务框架；删除旧 Review/Fix/mutation runtime，复用来源读取、分页、协议校验、证据、引用路径及合并函数。同步删除失效测试、旧配置和提示词，历史实验脚本归档。

## 验证

1. 自动化测试覆盖并发提交、partition、跨范围引用、字段/角色检查、dependency 去重及归属、文本冲突、局部修正、重聚类、幂等、旧 attempt fencing、重启和 MCP socket/stdio。
2. Fake HC 检查串行初始化、并行派发、故障替换与会话恢复；实际 Luna/high 小协议验证完整链路。
3. 同一源码及 RFC959/RFC3659 快照，比较 parse_workers=1 和 3，merge_workers 均为 3，使用 Luna/high，各新输出目录、最多 40 分钟。报告完整流水线及各阶段时间、调用/拒绝、重复读取、修正次数和可用的原生 token 用量。不恢复无限优化循环，也不提前保证 20 分钟目标。
4. Audit 从提交记录重建提取结果及 revision table，与最终 protocol 对照。结构正确、运行耗时及人工语义检查分别报告。
