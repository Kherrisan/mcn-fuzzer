# 收集 coverage replay 的 SUT 输出并提供给分析 agent

## 目标与已确定的选择

在 LLVM source coverage replay 阶段分别累计保存 SUT 的 stdout 和 stderr，case 之间写独占一行的分隔标记，并向 coverage analysis agent 提供对应 snapshot 的日志文件路径。

- stdout 和 stderr 分别保存为两个文件。
- 跨 replay batch 追加：同一个 campaign/build 的后续 batch 保留之前输出，在文件末尾继续写入。
- agent 直接使用文件工具按需读取；不增加 MCP 工具，不把完整日志塞进 prompt。
- 范围是 source coverage replay，包括启动和最终收尾时的 coverage replay；不扩展普通变异或 CLI 单例 replay。
- 只收集 SUT 实际写入两路 FD 的字节；不包含 generator 输出、网络响应或 stdin。不强制改变 SUT 的用户态缓冲策略，被 SIGKILL 时尚未 flush 的内容无法恢复。

## 现有实现与接入点

- `fuzz/src/coverage/mod.rs` 的 `replay_pending()` 遍历 pending cases，通过 `CoverageSession` 管理独立 coverage forkserver、case 清理和 snapshot 发布。
- `fuzz/src/runtime/backend/forkserver.rs` 的 `spawn_coverage()` 当前使用 `debug_child(false)`，两路输出被丢弃。
- `fuzz/src/coverage/schedule.rs` 为 analysis request 写入 request.json 并渲染 `fuzz/prompts/coverage-analysis-task.md`。
- LibAFL 0.16.1 提供 `StdOutObserver` / `StdErrObserver`，均为 `OutputObserver<T>` 的别名。它们的 `new_file(name, file)` 可绑定普通文件 FD，builder 通过 `stdout_observer()` / `stderr_observer()` 将 FD 重定向给 forkserver，子进程继承。
- 本项目不通过 `ForkserverExecutor::run_target()` 执行 testcase，不能依赖 observer 的 pre_exec/post_exec hook 自动重置或读取输出；利用文件 FD 直接追加即可。

## 日志捕获与 case 边界

1. 在每个 build 的 coverage 根目录维护 `sut.stdout.log` 和 `sut.stderr.log`：

   ```text
   coverage.output/<build-id>/sut.stdout.log
   coverage.output/<build-id>/sut.stderr.log
   ```

2. 在启动 coverage forkserver 前打开两个文件，使用 create + append，并将文件的克隆句柄交给 `StdOutObserver::new_file()` / `StdErrObserver::new_file()`。保留 Rust 端句柄写标记，保持 FD 生命周期覆盖整个 replay batch。
3. 将 observer 接入现有 backend，必要时调整其 observer tuple。普通 fuzz forkserver 不启用新的输出重定向，现有 debug_child 行为保持一致。不进行进程级 stdout/stderr 重定向，不增加捕获 subprocess 或 pipe 读取线程。
4. 日志放在 fixture/workdir 外部，防止每轮恢复 fixture 时删除日志。
5. 两份日志都写入可识别的单行标记：

   ```text
   === MCN_REPLAY {"event":"batch_start","batch":"..."} ===
   === MCN_REPLAY {"event":"case_start","batch":"...","case":1,"case_id":"corpus:...","filename":"..."} ===
   ...SUT output...
   === MCN_REPLAY {"event":"case_end","batch":"...","case":1,"case_id":"corpus:...","outcome":"..."} ===
   === MCN_REPLAY {"event":"batch_end","batch":"...","outcome":"..."} ===
   ```

   batch 使用本次 replay attempt 的唯一标识，避免失败重试重复使用标识；case_id 使用已有 `CoverageCaseId::key()`。filename 可为空，内容通过 JSON 序列化转义，标记前后保证换行。
6. batch_start 写在 forkserver 启动之前，使启动输出有明确归属。case_start 写在实际启动该 case 之前；case_end 写在该 case 的 SUT 和后代完成回收之后，再开始下一 case。batch_end 写在 forkserver 关闭和剩余后代回收后。
7. 空输出也保留标记。正常返回、异常退出、超时、启动失败和其他错误路径都尽可能写结束标记，并记录实际错误。无法完成回收时明确记录 cleanup 未完成，并遵守现有停止 batch 的行为。
8. 失败 attempt 的输出保留在累计日志中，不随 profraw 回滚删除，标记必须能区分失败输出与成功纳入 snapshot 的 case。
9. 日志按字节保存，不要求 SUT 输出为 UTF-8，不做静默截断或轮转。捕获文件创建、写入、结束同步和快照复制失败时返回明确的 coverage 错误，不发布不完整 snapshot；错误路径要确保已启动进程仍被回收。

## Snapshot 与 analysis agent

1. batch 结束、forkserver 回收之后，将累计日志复制到 snapshot 的 `.incoming-*` 目录，与 coverage/profdata/checkpoint 一起原子发布：

   ```text
   coverage.output/<build-id>/snapshots/00000N/sut.stdout.log
   coverage.output/<build-id>/snapshots/00000N/sut.stderr.log
   ```

2. snapshot 日志是截至该 batch 的完整累计副本；不能用硬链接到持续追加的源文件。新 batch 不得改变旧 snapshot 的日志。
3. 在 snapshot metadata 中记录两份日志的文件名和字节数；这些文件均来自 replay，不包含 fuzzer、agent 或 generator 输出。
4. analysis request.json 增加 `sut_stdout_log`、`sut_stderr_log`，值为该 snapshot 中的绝对路径。prompt 增加对应占位符，并明确这是累计历史、以 case 标记区分，各 case 的执行结果需结合阅读。
5. 提示 coverage agent 按错误关键词或 case_id 搜索并分段读取日志，结合 coverage、源码、handler 和协议依赖推理阻塞条件、约束及 proposal。不得一次读取/输出整个累计日志，不将缺少日志内容解释为某路径必然未执行。
6. 不修改 MCP load/query 接口，也不向 synthesis agent 传入这些日志。
7. 旧 snapshot 缺少文件时，request 字段使用 null，prompt 明确标记 unavailable，继续允许 coverage analysis；不伪造历史日志。已有 coverage 目录首次启用捕获时，从新 batch 开始追加并通过 batch_start 体现范围。

## 验证与验收

- 定向测试 SUT 分别写入两路输出，至少执行两个 case 和两个 batch；验证分流、case 标记、无换行输出、空输出、跨 batch 追加，以及旧 snapshot 在后续追加后内容不变。
- 使用超过常见 pipe buffer 的输出验证不会因未消费 pipe 而阻塞；验证非 UTF-8 字节保留。
- 覆盖异常退出、超时和 batch 失败路径，确认进程清理、结束标记、失败输出保留和日志 I/O 错误传播。
- 验证 analysis request/prompt 指向同一个 snapshot 的日志，旧 snapshot 缺日志时仍可派发。
- 运行相关定向测试并构建当前代码。使用新结果目录跑一次短 LightFTP campaign，开启 coverage analysis 和 synthesis，确认日志落盘、agent 实际读取日志并继续产出 accepted proposal；记录命令、耗时、request ID 和证据路径。
- 若 LightFTP 实际输出较少或为空，使用定向测试证明捕获能力，不将空文件本身报告为故障。
- 完成后总结改动、检查结果、真实 campaign 结果及剩余限制，不纳入工作区原有未跟踪的 SVF/OpenSSL 实验代码或数据，不自行提交或 push。
