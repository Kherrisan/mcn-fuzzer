# 实时 step 和 dependency 执行统计

## 目标

让 coverage analysis 通过 campaign introspection MCP 查询协议 step 和有向依赖在已发布 generator 中出现、在普通 fuzz exec 中被包含、实际成功执行的情况。每轮 exec 收尾后立即更新，包含种子、synthesis 候选和 corpus 后代。coverage replay 不计入普通 exec。

## MCP 工具

- `list_step_stats(parties?, channels?, offset?, limit?)` 返回匹配的模型 step 和 `present_cnt`、`exec_cnt`、`succ_cnt`。
- `list_step_dependency_stats(parties?, channels?, offset?, limit?)` 返回匹配的有向依赖及三个计数。
- `get_step_dependency_stats(steps, direction?, offset?, limit?)` 按模型 step 查询入边和出边。direction 为 incoming、outgoing 或 both，默认 both。去重并标明匹配方向。
- 筛选名称使用协议模型。每个筛选项内部取并集，不同筛选项取交集。Step 按参与方和 channel 匹配。依赖的任一端点同时满足筛选条件即可匹配。包含零计数的模型条目，稳定排序和分页。沿用 Rust 现有 MCP 结构化 schema。

## 计数口径

- `present_cnt` 按已发布 generator 原始 trace 中的实例计数，同一个 step 出现多次就计多次。依赖按原始 trace 中的依赖实例计数。同一发布记录重复登记不重复计算。
- `exec_cnt` 按每轮已发起的普通 exec 所含的变异后 trace 实例计数，前序失败导致未到达的后续实例也计入。依赖按当轮 trace 的依赖实例计数。
- Step `succ_cnt` 在相应 handler 正常返回时增加。依赖 `succ_cnt` 在两个端点成功完成，且前驱先完成时增加。通过实例名解析端点后按模型名称聚合，保留可区分的依赖条件与字段关系。
- 单独标记旧 generator 或不可观测调度，提供成功状态不可观测数量。未发起 exec 的基础设施错误不计入 exec。合法 close 正常返回计为成功。

## Python 反馈

- Rust 为每轮 exec 创建独立进度 JSONL 文件，经现有 Run 请求传路径。Python 进入标准 step 调度器时先写调度器启动记录，再于每个 step 正常完成后追加一行成功记录，含实例下标、实例名和模型 step 名。每行独立 JSON 和换行，写入及时对父进程可见。
- Rust 在 worker 结束、超时或终止后读取完整 JSONL 行，结合本轮 GeneratorInput 计算 step 和 dependency 的计数，一次提交本轮增量并清理进度文件。处理空文件、损坏或截断尾行及观测缺失，不能把未知状态当成功。每轮路径隔离，避免并发或重试混淆。
- 即使初始化失败或执行中途退出，保留已完整写入的成功记录。保持现有运行结果、replay 日志与 generator 兼容。统计属于当前 campaign，不回填旧 campaign。
- 更新 analysis 提示词，使其能发现并按需查询这些工具。

## 验证

- 覆盖正常执行、重复实例、step 变异导致的新增/删除/换序、依赖端点顺序、初始化失败、断连、I/O 超时和 worker 强制终止。前序成功记录应保留，未到达 step 不计成功。
- 验证 JSONL 截断、不可观测 generator、并发隔离和重复发布的计数，确保 replay 不重复累计。
- 通过真实 MCP 调用验证筛选、零计数、分页、入出边去重以及两轮 exec 之间即时可见。
