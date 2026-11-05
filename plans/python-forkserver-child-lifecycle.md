# Fuzzer 管理 Python worker 与 SUT 生命周期

## 目标

Python server 按 forkserver 风格启动每个 case 的 worker：fork 后立即把 worker PID 返回给 Fuzzer，之后由 Python server 用 `waitpid` 回收 worker，并把退出状态返回给 Fuzzer。SUT 继续由 LibAFL forkserver 启动和回收。

Fuzzer 同时监控 generator worker 和 SUT child，统一处理启动期限、运行期限、peer grace、信号终止及 case 结果。任何 timeout 路径都不重建 Python server 或 SUT forkserver。

## IPC 与状态结果

- 一次 Python `Run` 请求可在现有 iceoryx2/CBOR 请求上收到两条响应：
  - `Started`：包含 worker PID、`worker_id` 和 `case_id`。
  - `Result`：包含 `worker_id`、`case_id` 以及 `waitpid` 的退出码或信号，不包含 PID。
- 保留请求，直到对应 `Result` 到达；按请求和 case 标识匹配响应，拒绝不匹配或重复响应。
- iceoryx2 响应通道至少允许同一请求发送 `Started` 和 `Result` 两条消息，并关闭安全溢出模式；资源不足或发送失败按 IPC 错误处理。
- Python server 直接 `waitpid` worker，不增加 worker 到 server 的结果管道。server 不检测 worker 是否退出后再额外发送生命周期通知；它只在回收后发送约定的 `Result`。
- 删除 `Cancel`、`CancelAck`、Rust 到 Python 的逐 case 取消握手及 Python server 对 worker 的 case timeout 管理。
- worker 以稳定的退出码表达 generator 结果：成功、脚本异常、I/O timeout、连接错误、EOF、TLS alert 等。详细异常写入 `generator.log`。Fuzzer 同时保留 generator 逻辑状态和进程退出原因：
  - worker 自己因 I/O timeout 退出：进程原因为 `exited`，generator 逻辑状态为 `timeout`。
  - Fuzzer 因 case/peer deadline 终止 worker：进程原因为 `timeout`。
  - worker 自然退出或被外部信号终止：分别记录 `exited` 或 `signaled`，并记录适用的退出码或信号。
- `Started` 中的 PID 只用于运行时监控。`Result` 和持久化 case 结果均不记录 PID；移除报告中的 `child_pid`、`forkserver_pid` 及默认 note 中的 PID。

## CLI 参数

参数须传递给普通运行、replay、accept、benchmark 和内部 job 入口。

| 参数 | 默认值 | 用途 |
| --- | ---: | --- |
| `--startup-timeout-ms` | 1000 ms | 从提交双方启动请求起，等待 SUT 和 generator PID 的期限 |
| `--case-timeout-ms` | campaign 的 `case_ms`，当前 10000 ms | 双方都 Started 后的整例运行期限 |
| `--peer-exit-grace-ms` | 100 ms | 任一根进程先退出后，另一根进程的退出宽限期；启动 deadline 到期后也使用此宽限期 |
| `--result-timeout-ms` | 100 ms | 已确认根进程退出后，等待对应 `Result` 的最长时间 |
| `--timeout-failure-limit` | 5 次 | 启动超时与 Result/status 超时各自的连续失败上限；任一计数超过上限时退出 |
| `--sut-grace-kill-is-timeout` | 关闭 | 是否把 SUT 因 peer grace 到期被杀分类为 Timeout |

拒绝零值、溢出值及无法安全换算的时长；帮助信息和运行报告显示最终生效值。启动超时与 Result/status 超时使用两个独立的连续失败计数，成功后分别清零；整例 case timeout、peer grace kill 不计入这两项计数。默认前 5 次可恢复超时记为失败并继续，第 6 次（计数超过 5）清理后退出当前 job。报告分别列出两类超时及其当前连续计数。

## 双方启动与启动超时

1. 清理上一 case 状态并清空本轮 coverage map，随后背靠背提交 SUT forkserver 和 Python server 的启动请求，不等待其中一方的同步启动调用返回后才提交另一方。
2. 并行处理双方启动响应、pidfd 退出事件、Python `Result` 及协议错误。启动阶段必须等待双方都返回 PID；其中一方先退出时，记录退出事实，但不提前启动另一方的 peer grace。
3. `--startup-timeout-ms` 到期时立即输出 warning，并再等待 `--peer-exit-grace-ms`。启动期限到期即视为本 case 启动失败；即使双方在 grace 内返回 PID，也终止两侧 child、收集可取得的退出状态、将本轮标为 `startup_timeout` 并跳过反馈。
4. 如果启动期限和 grace 结束后双方 PID 均已知，清场后复用现有常驻 server/forkserver，继续下一轮。此次启动超时使启动超时计数加一；若该计数超过 `--timeout-failure-limit`，清理后退出当前 job。若之后某一 case 的双方 PID 均在启动期限内返回，则启动超时计数清零。
5. 如果 grace 结束后任一启动请求仍没有对应 PID，启动请求状态无法证明已收敛。终止该请求对应的常驻 Python server 或 SUT forkserver、另一侧已知的 case child 及其后代，清理 namespace，并以基础设施错误结束当前 job。该启动超时仍使启动超时计数加一，但此不可恢复错误不等待计数达到上限。**不重建任何 server/forkserver，也不继续向仍有未决启动请求的实例发送 case。**
6. 未能启动完整双方的 case 标为无效：不提交 coverage、crash 或其他 LibAFL feedback，不作为有效 corpus 输入；报告中保留 `startup_timeout` 或基础设施错误原因。

## 运行期退出与 timeout

- 双方均 Started 后开始单调时钟 `--case-timeout-ms` deadline。通过 pidfd 监控已报告的 PID；同时读取 SUT forkserver status 和 Python `Result`，但退出判定由 Fuzzer 的 pidfd 事件驱动。
- 任一根进程先退出，启动另一方的 `--peer-exit-grace-ms` deadline。若另一方在宽限期内退出，则按各自真实状态分类；若仍存活，到期向其发送 SIGKILL。
- case deadline 到期时，先向所有仍存活的 child 发送 SIGKILL，再等待状态；整例分类为 Timeout。发送信号和收集状态分开执行，确保在等待任一侧前已对双方完成终止操作。
- SUT 因 generator 先退出而在 peer grace 后被杀，默认属于正常 teardown，不单独制造 Timeout；启用 `--sut-grace-kill-is-timeout` 后分类为 Timeout。SUT 自身异常致命信号仍分类为 Crash。generator 被 Fuzzer 因 peer/case deadline 杀死时记录 `timeout`。
- 若 pidfd 显示进程已退出或 `pidfd_send_signal` 返回 ESRCH，视为退出竞态并读取已保存状态，不把它当作新的信号失败。
- 所有正常 case timeout 都只终止本轮 child，保留常驻 server/forkserver；不得因 timeout 自动重启它们。

## Result 等待、PID 安全与回收

- Python server 通过 `waitpid` 回收 worker，并用退出码/信号构造 `Result`。SUT 状态仍从 forkserver status 通道取得。Fuzzer 并行轮询这些状态，不为一个迟到的 Result 阻塞另一侧的进程监控。
- 对每个已确认退出的根进程，按 `--result-timeout-ms` 限时收集对应状态。收到后记录退出原因、退出码或信号；超过期限输出 warning、将该 case 标为结果不完整并使 Result/status 超时计数加一。计数未超过上限时，保留带请求/case 标识的过期响应 tombstone；迟到响应只用于排空和同步，不能改写已定案结果或被误配给后续 case。SUT status 按 forkserver 的请求顺序关联，覆盖图在旧 status 尚未排空前不得复用。某一 case 的双方 Result/status 均按时收齐后，Result/status 超时计数清零；第 6 次连续超时（默认上限为 5）完成清理并退出当前 job。通信通道损坏、状态无法安全关联等基础设施错误仍立即结束 job；任何情况都不重建 server。
- 启动 grace 内收到的早期退出与 Result 暂存起来；case 的 startup outcome 确认后再归档。启动请求从未返回 PID 的一侧不等待一个无法关联的 child Result。
- PID 只通过 pidfd 及启动时保存的 `/proc` starttime、预期父进程关系进行校验。若 worker 在 `pidfd_open` 前已经退出，以匹配的 `Started`/`Result` 和 server `waitpid` 结果处理；绝不向未经校验或可能已复用的裸 PID 发信号。
- Python server、SUT forkserver 的父子回收关系保持原样：Python server 回收 worker，forkserver 回收 SUT，Fuzzer 不抢占它们的 wait 状态。namespace 监督进程负责最终收养和清理遗留后代。
- 进程状态和 Result 收集后继续现有 namespace 后代清理、pcap 排空/压缩及 metadata 保存流程。有效性标记控制 LibAFL feedback，避免不完整执行污染语料。

## 实现范围

- `fuzz/python/generator_server/server.py`：fork worker、发送 `Started`、`waitpid`、发送匹配的 `Result`；移除 Cancel 管理。
- `fuzz/python/generator_server/worker.py`：将 generator 的成功、异常和协议错误映射为稳定退出码；详细诊断写入日志。
- `fuzz/src/runtime/generator.rs`：支持单请求多响应、校验请求标识、增量接收 `Started`/`Result`，删除 Cancel 和固定 2 秒结果等待。
- `fuzz/src/runtime/backend/forkserver.rs`：将 SUT 启动拆成发送 fork 请求和非阻塞/限时读取 child PID，支持和 Python 启动并行；分离 kill 与 status 收集。
- `fuzz/src/engine/executor.rs`：实现双启动屏障、pidfd 监控、peer grace、case deadline、Result deadline、分类和无效执行 feedback 门控。
- `fuzz/src/app/config.rs`、`app/mod.rs`、`main.rs`、`app/accept/`、`app/bench.rs`：定义并向所有入口传递 CLI 参数及 job 级累计超时失败策略。
- `fuzz/src/runtime/report.rs`：持久化 generator 退出原因和逻辑状态，不持久化 PID。

## 验收

- 双方按时 Started；任一方先退出；一方提前退出但另一方仍在启动；启动 deadline 到期后在 grace 内迟到；grace 后 PID 仍缺失并停止 job，确认没有 server/forkserver 重建。
- 混合启动超时与 Result/status 超时，验证两类计数互相独立、对应成功分别清零、前 5 次可恢复失败继续、第 6 次连续失败超限退出；未决 PID 或损坏 IPC 的不可恢复错误立即结束 job。超时 case 不进入 coverage、crash、corpus feedback。
- generator/SUT 分别先退出、peer grace 内退出、grace 后被杀、case timeout；验证默认和 `--sut-grace-kill-is-timeout` 分类。
- 正常退出、非零退出、信号终止、generator 自身 I/O timeout，分别验证 `Result`、generator 状态和持久化结果。
- Result 与 SUT status 在 100 ms 内到达及超时未到达；验证前 5 次连续超时记数并按请求顺序排空/同步、一次双方状态均按时到达后清零、第 6 次连续超时退出 job；不伪造状态或重建常驻进程。
- worker 快速退出、PID 在 `pidfd_open` 前消失、PID 复用防护、错误/重复/迟到 IPC 响应；验证无错误发信号、无僵尸或 namespace 孤儿泄漏。
- 以 LightFTP 和 OpenSSL 执行 capture-off/on 冒烟及 Release+stats 五组配对吞吐测试；报告有效 case、启动超时/跳过数、抓包阶段耗时与吞吐量，不能把无效 case 当作零成本样本。
