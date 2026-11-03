# PID 1 Fuzzer：nix 隔离、常驻 libpcap 抓包与进程树回收

## 当前实施修订：sudo 启动、私有 IPC 与抓包预检

> 本节记录本次 sudo 启动、IPC 隔离和抓包预检的实施范围，并覆盖下文中与这些部分冲突的旧决策。其余已经实现的 forkserver、Python IPC、抓包生命周期、case 清理和 corpus 行为保持不变。

### 启动身份与权限

- 普通用户构建 Release 二进制，然后用 `sudo ./target/release/mcn-fuzz ...` 启动；不再安装带 file capabilities 的 `/usr/local/libexec/mcn-fuzz` 副本。
- 增加全局 `--run-as USER`，接受用户名或可解析到现有账户的数字 UID，拒绝 UID 0。sudo 默认同时使用 `SUDO_UID` 与 `SUDO_GID`（缺一即失败）：主组是 `SUDO_GID`，补充组由 `getgrouplist` 按该 GID 构造。显式 `--run-as` 仍使用账户 passwd 主组。直接以 root 启动而没有 `SUDO_UID`/`SUDO_GID` 时必须显式给 `--run-as`。解析必须在 fork 前完成；无有效身份时在启动期失败。
- 先由特权启动器创建 PID namespace 并 fork。namespace 子进程完成 NET/MNT namespace、`lo`、proc 和抓包句柄准备及私有 tmpfs 挂载后，依次执行 `setgroups`、`setresgid`、`setresuid`，清空 capabilities、设置 `NoNewPrivs`，核验 UID/GID/补充组，并核验 `CapEff`、`CapPrm`、`CapInh`、`CapAmb` 均为空且 `NoNewPrivs` 为 1。身份变化后重新设置 `PDEATHSIG` 并通过父 pidfd 检查父进程仍存活。
- 外层监督进程保持 root，直到收到子进程已完成降权的 ready 消息；随后自身降到相同目标身份。监督进程在此之前保留通过 pidfd 终止和回收初始化失败子进程的权限。启动握手必须有真实的 poll/deadline；TERM/INT 和任何初始化失败都要终止并回收子进程。
- 所有新建 campaign 输出由目标用户拥有。已有输出目录若目标用户不可访问则给出明确错误；不以 root 创建输出，不递归 chown 用户目录。

### 私有 IPC 与批量子任务

- 在子进程私有 mount namespace 中单独挂载 `/dev/shm` 与 `/tmp/iceoryx2` tmpfs；iceoryx2 tmpfs 使用目标 UID/GID、mode 0700。缺失的 mountpoint 可创建；不得 chmod/chown 宿主已有目录，也不挂载整个 `/tmp`。tmpfs 与 namespace 随实例退出回收。
- capture 开关不改变 namespace 和 IPC 隔离；capture-off 只跳过 pcap 打开、抓包线程和 pcap 预检。
- `bench-capture` 每个 trial/condition 以及 `accept` 的每个独立场景均通过新的受监督子进程启动，防止在已 unshare 的进程中重复创建 PID namespace，并确保输出归目标用户。父编排器只收集结果并写最终报告；基础设施失败保留失败记录，不计为零开销。保持 benchmark 的五组配对顺序和每条件 60 个固定 seed case。

### pcap 预检与脚本

- 双向 loopback marker 定义为单一常量，并始终以 `marker.len()` 搜索；现有 marker 是 17 字节，修复错误的 18 字节窗口。增加可证明双向捕获成功的回归检查，不能用“任意错误也通过”的测试替代。
- pcap nonblocking 设置和收包错误必须传播。预检循环和 drain 使用有界等待，避免阻塞读取越过启动 deadline。无法确认双向捕获时，在执行任何 case 前失败，并报告 pcap 初始化/读取/超时阶段。
- `setup-capture.sh` 只检查 Linux/libpcap/pkg-config 等依赖并以普通用户构建 Release；移除 filecap 安装、撤销权限和 `/usr/local` 写入。README 示例更新为普通用户构建、sudo 启动。

### 验收

- 覆盖 sudo 默认身份恢复、`--run-as` 用户名和 UID、无调用者身份、UID 0 拒绝、降权失败、父进程退出竞态、启动中断及目标身份下的输出属主；核验 forkserver、Python server/worker、SUT 的最终凭据和 `NoNewPrivs`。
- 验证同时运行的 campaign 具有独立 `/dev/shm` 与 iceoryx2 服务目录，宿主既有 `/tmp/iceoryx2` 所有权/权限不变；检查 TERM、初始化失败和父进程退出后 namespace 进程被回收。
- marker 回归覆盖正向/反向 17 字节匹配、缺少一侧 marker、pcap 错误和 deadline；失败必须发生在首个 case 之前。
- 分别用 LightFTP、OpenSSL 跑 capture-off/on 冒烟验证；执行五组配对 Release+stats benchmark，报告真实 libpcap 版本、启动、预检、停止、压缩、保存成本及吞吐量。capture-on 初始化失败不得作为有效测量样本。

### 实施与验证记录

sudo 启动方案已取代 file capabilities（2026-11-03）。普通用户构建 Release，用 `sudo ./target/release/mcn-fuzz` 启动；不安装 `/usr/local/libexec/mcn-fuzz`，也不再给二进制加 file capabilities。`libpcap-dev` 提供 libpcap 1.10.4。capture-on 在 namespace 内打开 `lo`，降权前持有句柄。LightFTP 与 OpenSSL 的五组配对报告保留在 `fuzz/capture-bench/sudo-lightftp` 与 `fuzz/capture-bench/sudo-openssl`，标签是 startup identity，不重跑这两组数字。多场景 `accept` 由外层编排器逐次 re-exec 隐藏的 `accept-job`；每个 job 新建 supervise/PID+NET+MNT namespace，父进程只收集退出状态。PID 1 不安装 SIGCHLD handler：Python worker 退出时的异步信号会打断 libafl forkserver 和 iceoryx2 IPC 等待；case 清理阶段通过 pidfd 定向回收孤儿。直接 `unshare(CLONE_NEWPID)` 在无特权用户下仍是 `EPERM`。此计划取代旧 ptcpdump 方案及三层 `launcher → init → Fuzzer` 方案。

`cargo fmt --all -- --check` 通过；Rust 单测 37/37、Python 单测 19/19 通过（均用 sudo 执行，以允许本地 loopback socket）；Release+stats 构建通过。LightFTP capture-on 完整 accept 的 main/hang/crash jobs 均通过，main 完成 100 次资源泄漏循环及 3 次变异迭代，FD 数 24→24、generator server 数 1→1。OpenSSL accept 与 capture-off/on smoke 已通过。两条并行 LightFTP campaign 使用不同 PID/MNT namespace inode，各自挂载 `/dev/shm` 和 `/tmp/iceoryx2` tmpfs；PID 1 UID/GID 为 1005，capabilities 全零、NoNewPrivs=1，corpus 属主为 UID 1005。另在 generator worker 和 SUT 正在运行时检查了 Fuzzer、forkserver、Python server、worker、SUT 的 UID/GID/补充组、四类 capabilities 和 NoNewPrivs，全部符合目标身份；向监督进程发送 TERM 以及 SIGKILL 监督进程后，namespace 进程均退出，宿主无残留 SUT/generator 进程。五组配对吞吐量及各阶段耗时见上述报告。

目标运行结构：

```text
外层监督进程
└─ Fuzzer：新 PID namespace 的 PID 1
   ├─ LibAFL forkserver
   │  └─ SUT：每 case 新建，可产生后代
   └─ Python server：常驻、单线程
      └─ generator worker：每 case fork，可产生后代
```

Fuzzer 自己作为 PID 1 执行 LibAFL、generator 协调、进程树回收和常驻抓包。进程树在它退出时由内核清除。外层监督进程只创建和监督这个实例，不运行目标和 Python generator。

## 1. 目标与固定决策

- 保留 LibAFL Forkserver、PCGUARD、基础覆盖反馈、调度和 Python `generate(ctx, params)`。Rust 把变异后的完整参数字典通过现有 iceoryx2 + CBOR IPC 发给 Python server；generator 继续通过原生 socket 与 SUT 通信。
- 使用 nix 创建匿名 PID、NET、MNT namespaces。每个 campaign/benchmark 条件一个 namespace，case 串行运行。
- 抓包使用 `pcap` crate，在每个 namespace 的 `lo` 上长驻读取。用网络隔离限定抓包范围，不用 PID 映射或端口过滤，覆盖 FTP 动态数据连接。
- Python server 直接 fork 每个 case 的 worker，不保留独立 template，也不增加 Rust 直连 template 的控制通道。Run 和 Cancel 都走现有 iceoryx2 服务。
- 每个 case 在根进程结束后清理所有后代；清场成功后才结束抓包和读取覆盖率。
- 使用标准 `OnDiskCorpus`、LibAFL testcase metadata 和 `GeneratorInput::to_file` 导出隐藏 `.pcap` sidecar。抓包后及解压后不增加文件内容校验。
- 缺少依赖、权限、namespace 支持或抓包就绪失败时，必须在第一个 case 前报错，不静默关闭抓包。

## 2. 启动、namespace 与权限

### 启动顺序

1. 外层监督进程（`sudo` 启动的 `mcn-fuzz`）读取 campaign、验证依赖、准备目录及 `WorkerId`，打开自身 pidfd，建立私有生命周期/启动握手。此时完成所有需要创建子进程或线程的普通准备工作。
2. 单线程调用 nix `unshare(CLONE_NEWPID)`，随后立即 `fork()`。父分支是外层监督进程；子分支成为新 PID namespace 的 PID 1，即 Fuzzer。改变 `pid_for_children` 后，监督进程不再创建线程或子进程。
3. fork 返回后，监督进程保持 root，直到子进程完成降权并发出 ready。收到 ready 后，监督进程降到同一目标身份，清空 effective、permitted、inheritable、ambient capabilities，设置 NoNewPrivs。初始化失败或握手超时则通过 pidfd 终止并回收 PID 1。
4. PID 1 设置 `PDEATHSIG=SIGKILL`，检查继承的父 pidfd 以覆盖父进程在注册前已退出的竞态；随后自行 `unshare(CLONE_NEWNET | CLONE_NEWNS)`，设 `MS_PRIVATE | MS_REC`，挂载匹配此 PID namespace 的 `/proc` 并启用 `lo`。
5. PID 1 主线程在启动业务子进程前配置 libpcap；然后清空自身 capabilities、设置 NoNewPrivs。它以单线程状态启动 forkserver 并完成握手，再启动 Python server、等待 generator 预加载和服务 Ready。
6. 所有需要 fork 的服务启动后，才创建抓包线程并将 libpcap 句柄移交给它。线程 Ready 后做受控 loopback 收发预检并丢弃预检数据，PID 1 确认初次清场后才开始加载/执行 case。

PID namespace 语义决定第一次 fork 的子进程成为 PID 1；创建新 PID namespace 不会把当前进程改成 PID 1。[PID namespace 文档](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html) NET/MNT 由 PID 1 自行 unshare，因此外层监督进程保留原网络和挂载环境。

### 权限

不安装带 file capabilities 的副本，也不写 `/usr/local/libexec/mcn-fuzz`。启动命令是 `sudo ./target/release/mcn-fuzz ...`。sudo 默认身份使用 `SUDO_UID` 和 `SUDO_GID`，补充组按该 GID 构造；`--run-as` 使用账户主组。拒绝 UID 0。不得增加 `CAP_BPF`、`CAP_PERFMON`、`CAP_DAC_READ_SEARCH` 或 `CAP_SETPCAP`。

外层监督进程在收到子进程 ready 之前保持 root，以便用 pidfd 回收初始化失败的 PID 1；随后降到目标身份。PID 1 在完成 `lo`、proc mount、私有 tmpfs 与 pcap handle 初始化后执行 `setgroups`/`setresgid`/`setresuid`，清空 effective、permitted、inheritable、ambient capabilities，设置 NoNewPrivs，并核验这四项为空且 NoNewPrivs 为 1。降权后不再依赖 file capabilities。运行 case 前，监督进程、Fuzzer、forkserver、Python server、worker 与 SUT 均无 capabilities。

pidfd、supervisor 通道和抓包句柄默认 CLOEXEC；Python 直接 fork worker 时显式关闭其不需要的继承 FD。仅依赖 CLOEXEC 不足以清除纯 fork 的 FD。

### 标识与多 job

- 使用 OS 随机 nonce 与 job 序号生成 `WorkerId`；随机值不可用时报错。服务名、client ID、日志、临时目录、捕获 ID 和输入文件名使用它，不能用 namespace 内 PID 作为跨实例唯一标识。
- 显式给 LibAFL 配置实例私有输入文件，避免不同 namespace 中同 PID 的默认 `.cur_input_<pid>_<counter>` 冲突。每个实例用独立工作目录、corpus 和 crash 目录；不得并发 restore 同一个 target workdir。
- `run`、`replay` 和单个探针直接启动一个监督/Fuzzer 对。`bench-capture` 与多场景 `accept` 由仍具备启动权限的外层编排器逐次 re-exec 独立监督/Fuzzer 对（`bench-job`，以及隐藏的 `accept-job`）。每个 job 新建 PID+NET+MNT namespace。编排器只收集退出状态，失败后继续剩余 job，最后汇总错误；它不运行 generator/SUT，也不在同一进程里多次 `Session::open`。未显式 `--run-as` 时子进程继承 `SUDO_UID`/`SUDO_GID`，不改写成账户主组。
- capture-off 仍创建相同 PID/NET/MNT namespaces，执行相同孤儿清理；只跳过 pcap handle、抓包线程和抓包预检。

## 3. PID 1 的信号处理与进程回收

### 信号和外层监督

- PID 1 在创建其他线程前安装 SIGINT、SIGTERM handler。SIGCHLD 保持默认 disposition，避免 worker 正常退出时的异步信号打断 forkserver/iceoryx2 系统调用；PID 1 在每 case 清理阶段用 pidfd 定向 wait 收割孤儿，不使用 `waitpid(-1)`。禁止设置 `SA_NOCLDWAIT`。
- 所有 iceoryx2 Node 显式禁用其默认终止信号 handler，避免与 PID 1 的统一信号管理冲突。
- 外层监督进程保存 PID 1 的 pidfd，接收其 Ready、Result/Error 与退出状态。收到用户终止请求时发送一次 SIGTERM 并启动 5 秒关闭 deadline；到期则通过 pidfd 发 SIGKILL。PID 1 被杀时，Linux 内核清除整个 PID namespace 中的剩余进程。[PID namespace 文档](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
- 监督通道仅传实例生命周期消息；case 参数、Run、Cancel 和 generator 结果仍走现有 iceoryx2，不增加 template 通道。PDEATHSIG 是父进程死亡时的兜底；fork 会清除它，PID 1 必须在 fork 后立即重设并检查继承 pidfd。[PDEATHSIG 文档](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)

### 等待所有权

所有 wait/reap 决策在 Fuzzer 主线程完成，抓包线程不等待进程。常驻名单仅为 PID 1 Fuzzer、LibAFL forkserver、Python server，登记 `(pid, starttime)` 和 pidfd；SUT、generator worker 及其后代永不进入名单。

| 进程 | 正常回收者 | Fuzzer 获取结果的方式 |
| --- | --- | --- |
| SUT 根 | AFL forkserver | forkserver 状态管道 |
| generator worker 根 | Python server | Run terminal / CancelAck |
| forkserver | LibAFL 持有的进程所有者 | 上游 API/Drop |
| Python server | Rust GeneratorClient | Child/pidfd |
| 已收养孤儿 | Fuzzer PID 1 | 按 pidfd 定向 `waitid` |

禁止 `waitpid(-1)`。收养子进程的 reaper 只 wait 已成为 PID 1 直属子进程的、明确登记为非常驻的进程；不得消费仍由 forkserver 或 GeneratorClient 管理的状态。nix 0.31 提供按 pidfd 的 `waitid(Id::PIDFd, WEXITED | WNOHANG)`，优先复用它。[wait 文档](https://man7.org/linux/man-pages/man2/waitpid.2.html)

### 每 case 清理

1. 先取得 SUT 根的 forkserver 终态和 generator worker 根的 server 终态；需要取消时让 Python server 完成根 worker 的有界终止与 wait。
2. 由 PID 1 扫描整个私有 `/proc`，按 pidfd 对所有非常驻 TGID 发 SIGTERM，给最多 200 ms 宽限，再对剩余者发 SIGKILL。清理总预算为 2 秒，扫描时持续发现新 fork，并对新进程执行同一清理。
3. 根所有者继续消费自己的 wait 状态；PID 1 用定向 `waitid` 回收已收养孤儿并重复扫描，直到没有非常驻活进程或 zombie。
4. 清场成功后才结束抓包、压缩和读取稳定覆盖率；任何失败都禁止下一 case。

杀进程范围是整个 namespace 内所有非常驻进程，涵盖 setsid、double-fork 和自设 subreaper；wait 范围仅限已收养的非常驻直属子进程。若出现持续 fork、D 状态任务、根所有者无法回收 zombie 或达到 deadline，终止整个实例；不能把仍可能变化的 bitmap 当正常反馈。

LibAFL forkserver 的析构会 wait forkserver 根，因此正常关闭先确认/结束当前 SUT、使用 builder 的 SIGKILL fallback 终止 forkserver，再由上游析构消费它自己的状态。Python worker 的取消 ACK 表示根 worker 已回收，不表示其后代已清场；后续仍必须执行 PID 1 的全树清理。forkserver 或 Python server 意外退出均为实例致命错误，不自动恢复。

## 4. Python server、常驻抓包与 testcase 持久化

### Python 执行

删除 `generator_server/template.py` 生产路径。Python server 保持单线程，预加载本 job 所需 generator 后，直接 fork 每个 case 的 worker；generator API 和 native socket 用法不变。

- iceoryx2 不启动后台线程；本地 0.8.1 的 Node/request-response server 初始化也观测到单 OS 线程。[iceoryx2 FAQ](https://github.com/eclipse-iceoryx/iceoryx2/blob/main/FAQ.md)
- server Ready 前及每次 fork 前检查 `/proc/self/task`。预加载 generator 不得启动后台线程或常驻辅助进程，导入失败/超时按启动失败处理。
- 变异参数先编码为普通 bytes，fork 后由 worker 解码。worker 不操作或析构继承的 iceoryx2 Node、Server、ActiveRequest 或 sample；子分支进入独立 worker 函数，所有出口 `os._exit()`，并关闭无关 FD。验证异常、GC 和取消后父 server 的 IPC 仍可用。
- server event loop 同时处理 iceoryx2 请求、worker 结果、worker 退出和 deadline；结果管道用非阻塞增量 frame parser/writer，不能在收到半帧或管道满时阻塞 Cancel。
- 同一实例最多有一个 Run 在执行，但 Run pending 与 Cancel pending 可并存；服务未完成请求容量设为 4。
- Cancel 用独立 request ID 指定 `(WorkerId, case_id)`；重复取消幂等、旧取消不影响后续 case。Cancel 可先于对应 Run 到达：记录取消水位，CancelAck 后迟到 Run 不得再 fork。
- 每个 Run 只有一个 terminal，CancelAck 与 terminal 可乱序到达；Rust 保留两个 pending，处理自然完成与取消竞态。SUT 根退出时先 true nonblocking poll 一次，无现成 terminal 就立即 Cancel，删除 500 ms grace。generator 先完成时仍按 campaign 的 `exit_grace_ms` 等 SUT。

### libpcap 生命周期

Fuzzer 主线程在创建线程前打开 `pcap::Capture` 并配置，再移交唯一抓包线程；capture handle 只由该线程访问。用 nix poll 等待 pcap selectable FD 和命令 eventfd，每次有限批次读包，确保 Begin/End/Shutdown 不会被持续报文饿死。

默认配置：`lo`、ingress、promisc=false、snaplen 262144、microsecond 时间戳、buffer 4 MiB、immediate mode、nonblock；每 case PCAP 上限 64 MiB；quiet 5 ms、End 最大 100 ms；zlib level 1。

```text
初始化 → Ready → Idle（持续排空并丢弃）
                    │ Begin(case_id) / ACK
                    ▼
                 Capturing
                    │ End（PID 1 已确认所有后代清场）
                    ▼
                 Draining → EndAck(buffer, 计数, 状态) → Idle
任意致命错误 → Failed（禁止下一 case）
```

- 开始 case 前执行带 5 秒 deadline 的受控 loopback 收发预检，确认 libpcap 能交付 packet 后丢弃预检数据。工具、权限、接口、activate、方向、selectable FD、线程 Ready 或预检失败均在首个 case 前报错。
- Begin 清理当前可读队列，建立该 case writer 和统计基线后 ACK；Idle 已满足 quiet 时不人为再等 5 ms。Begin 前清除旧 metadata/attachment。
- End 仅在 SUT 根、worker 根及所有非常驻后代清理完成后发送。读到空队列并且距最后一包已有 5 ms quiet 后完成；新包重置 quiet，最迟 100 ms 返回。End 后线程恢复 Idle；压缩在 Fuzzer 侧进行。
- 用 `pcap-file` 将包记录写入 `PcapWriter<Vec<u8>>`，带实际 datalink、snaplen 和 timestamp precision。64 MiB 上限计入 PCAP global header、record header 和 caplen；超限保留完整记录前缀，继续排空并标记 incomplete，不保留半条记录。
- 分开记录观测、保存、容量丢弃和 libpcap stats 增量；不从 received-captured 推算 drop。已知丢包、snaplen 截断、容量截断标 incomplete。
- 5 ms quiet 界定本次收集窗口，不能保证内核未来绝不会产生迟到的 TCP 重传；报告和验收明确此边界。

### Observer 与保存

- 保留 `crate::capture::PcapMetadata` 类型路径和 LibAFL SerdeAny 注册；新增字段带旧数据兼容默认值，记录 capture backend/version、WorkerId、case ID、状态、计数及原因。PID 仅作诊断，不用于过滤。
- 新 Begin/End、压缩等时间写入新字段，不复用旧 helper 的 ready/stop/read 时间语义。
- `PcapToMetadataFeedback` 返回 false，只在 coverage feedback 或 crash objective 接纳 testcase 时附上metadata。`PendingCapture` 只附到新的 testcase clone，不进入输入 JSON/hash/equality/mutation。
- 使用标准 `OnDiskCorpus`；`GeneratorInput::to_file` 保存原参数 JSON 并流式解压隐藏的 `.<input-filename>.pcap` sidecar。捕获后及解压后不加文件校验。replay 执行但不自动写入 corpus。
- 保留执行唯一的 input/sidecar 名称，避免 LibAFL 因重复输入跳过保存、丢失新抓包。只为被接纳的 corpus/crash case 写 sidecar。
- 抓包线程、End/drain、序列化或压缩故障设置 sticky fatal。清场成功时允许本次正常 feedback 保存可用前缀后结束实例；压缩失败明确报错，不伪造空成功附件。进程清理失败时不读取可能变化的 bitmap。
- 在 `run_target` 入口（计数及 restore 前）、最后一次 iteration、replay 和 job 结束检查 sticky fatal；种子载入和 mutational stage 会绕开 `Session::evaluate`。

## 5. 模块边界、脚本与实施验收

保持改动集中于现有 `main/app`、`executor/backend`、`capture/input`、`generator` 和 `fuzz/python/generator_server`：增加 runtime namespace/监督/reaper 代码及 Python 分帧模块；不增加独立 init、通用 controller 或替代 forkserver。

- `main/app`：单 job 外层监督与 Fuzzer PID 1 入口；batch 命令复用同一入口逐次启动独立 job。
- `runtime`：nix namespaces、capability drop、pidfd/process identity、PID 1 清场与 supervisor 生命周期握手。
- `CaseExecutor`：按“Begin → SUT/Run → 两根终态 → 全树清场 → End → feedback”编排，不接管上游 forkserver 控制协议。
- `TargetBackend`、Python `Transport` 和 capture 记录/来源保持边界，之后能替换 Nyx-Net 后端，不改 generator 输入和 corpus 存储。
- 移除 `capture.executable`、ptcpdump PID 参数/Ready stderr 解析、BTF/eBPF 检查、逐 case temp PCAP、SIGUSR1 stats、helper stop/reap 及独立 template 启动。Campaign 配置保留 enabled/file_limit，加入 drain quiet/timeout 默认值 5/100 ms。
- 删除 `setup-ptcpdump.sh`，新增 `setup-capture.sh`。脚本检查 Linux、libpcap 与 pkg-config，并以普通用户构建 Release。不安装 file capabilities，不写 `/usr/local`。README 示例为普通用户构建、sudo 启动。保留历史报告原始数据并标明旧后端。
- 错误政策测试：缺依赖/权限/namespace/pcap Ready 在首个 case 前失败；Python 根或 forkserver 意外退出、cleanup 2 秒超时、supervisor/PID 1 SIGKILL 均不能留下继续运行的目标进程。
- 进程 fixture 覆盖 SUT/generator 留 child、忽略 TERM、setsid、double-fork、持续 fork、自设 subreaper、结果 pipe 被后代持有、SIGCHLD 与业务根并发退出；验证 owner 的 wait 状态不丢失，PID 1 reaper 仅回收已收养进程。
- Python 覆盖连续 fork、globals 隔离、父 IPC 在异常/GC/取消后可用、Cancel 先于 Run、重复/迟到 Cancel、Run terminal 与 CancelAck 乱序、worker half-frame/满 pipe/deadline。
- Capture 覆盖 Idle/Begin/End、尾包 quiet 重置、100 ms drain 超时、64 MiB 完整记录边界、stats drops、sticky fatal 与最后一个 case 的错误传播。PCAP 内容验证限测试/离线验收，不增加生产 post_exec 或解压校验。
- 用 LightFTP LIST/RETR 验证动态 FTP 数据端口和双向流量；用 OpenSSL valid/alert/timeout 验证结果分类。两个目标至少各运行 100 个 capture-on case，并做重复、并行实例的 process/zombie/FD/service/namespace 泄漏检查。
- Release + stats 下，capture-off/on 都使用相同两层结构、Python server 和清场流程。每目标 5 对交替顺序试验，每条件新 job，完整 seed 周期 warm-up 后执行 60 个固定 seed case。
- JSONL + Markdown 报告启动/退出、namespace/pcap activate、Begin、交互、Cancel、孤儿回收、End drain、序列化、zlib、保存成本及吞吐/延迟分位数。线程序列化可能与交互重叠，分开报告，不能直接累加成独占墙钟时间；基础设施错误保留 partial report，不能作为零开销样本。
- capture-on/off 都必须实际跑完；权限缺失、只通过单元测试或仅有旧 ptcpdump 报告都不算完成。
</proposed_plan>
