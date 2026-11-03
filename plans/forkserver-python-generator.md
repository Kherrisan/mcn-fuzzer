# Forkserver + PCGUARD + Python generator：简化实现计划

> namespace 隔离、常驻抓包和 namespace 进程树回收，以 [nix 隔离、常驻 libpcap 抓包与进程树回收](nix-libpcap-process-reaping.md) 为准。Python worker 与 SUT 的启动屏障、IPC、timeout、Result 和回收，以 [Fuzzer 管理 Python worker 与 SUT 生命周期](python-forkserver-child-lifecycle.md) 为准；该计划覆盖并取代本文较早的 template、Cancel、串行启动和 case 收尾流程描述。任何 timeout 后都不重建常驻 server/forkserver。

## 1. 目标与复用原则

新建独立的 `fuzz/` 工程，采用普通 Forkserver：一个 case 执行一次完整的 Python generator 交互，并使用一个新的目标子进程。

已确定：

- LibAFL 负责 fuzzer、observer、覆盖反馈、调度及基础统计。
- AFL++ PCGUARD 编译期插桩提供唯一覆盖反馈和统计来源，插桩范围为 LightFTP 自身源码。
- Python 通过 `ctx/channel` 使用原生 socket，支持控制连接和 PASV 数据连接。
- 保留现有 **iceoryx2 + CBOR** 通信。
- 保留面向未来 Nyx-Net 的 `TargetBackend` 和 Python `Transport` 接口。

直接复用以下实现：

| 功能 | 复用来源 |
| --- | --- |
| Forkserver 创建、握手和控制协议 | LibAFL `ForkserverExecutor` 及其公开 API |
| PCGUARD 插桩及目标计数器 | AFL++ 编译器和运行时 |
| 共享 coverage bitmap | LibAFL 共享内存和 map observer |
| 覆盖 map observer、反馈和调度 | LibAFL 标准组件 |
| Python generator 加载 | 现有 `Registry` |
| Python socket 交互 | 现有 `Ctx/Channel/Listener` |
| Rust/Python RPC | 现有 `GeneratorClient`、Python server 和 CBOR 格式 |
| 参数变异 | 参考并迁移现有 generator mutator |

直接使用 LibAFL Forkserver、共享内存和覆盖反馈组件，保持 executor 与目标后端之间的接口精简。

本方案替代此前的 Intel PT 方案：移除 Intel PT 依赖、配置、采集和解码代码，以及仅为 attach PT 设计的 ready/go 启动门闩。构建、运行和验收均使用 PCGUARD。

固定 LibAFL 系列依赖为 `0.16.1`、AFL++ 为 `v5.03c`、LightFTP 为 `5980ea1`。

## 2. 简化后的目录结构

一个 Rust crate、一个 Python package；模块先保持紧凑。

```text
fuzz/
├── Cargo.toml
├── Cargo.lock
├── README.md
│
├── src/
│   ├── main.rs                 # run / replay / doctor 入口
│   ├── app.rs                  # 组装 LibAFL 和运行流程
│   ├── config.rs
│   ├── input.rs                # GeneratorInput
│   ├── mutator.rs              # 迁移现有参数变异
│   ├── executor.rs             # CaseExecutor
│   ├── generator.rs            # 迁移现有 GeneratorClient
│   ├── report.rs               # 执行结果及产物保存
│   └── backend/
│       ├── mod.rs              # TargetBackend、统一目标结果
│       └── forkserver.rs       # 直接使用 LibAFL Forkserver
│
├── python/
│   ├── pyproject.toml
│   ├── generator_server/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── server.py           # 迁移现有 iceoryx2 server
│   │   ├── registry.py         # 复用现有 Registry
│   │   ├── context.py          # 保留用户可见 ctx API
│   │   ├── transport.py        # 从现有 socket 代码提取
│   │   └── worker.py           # 模板与每 case 隔离
│   └── tests/
│
├── harness/
│   └── coverage_probe.c        # 验证共享 map 和分支反馈的受控目标
│
├── targets/lightftp/
│   ├── build.sh
│   ├── forkserver-session.patch
│   ├── campaign.toml
│   ├── fftp.conf
│   ├── generators/ftp_session.py
│   ├── seeds/
│   └── fixture/
│
└── tests/                      # Rust 和端到端测试
```

覆盖率读写直接使用上游共享内存和 observer；不再增加单独的采集器模块。

## 3. Rust 执行结构

### 后端与 executor

`ForkserverBackend` 直接持有上游 `ForkserverExecutor`，通过 builder 初始化和握手，通过 `forkserver_mut()` 等公开 API 控制 child。[上游接口](https://github.com/AFLplusplus/LibAFL/blob/0.16.1/crates/libafl/src/executors/forkserver.rs#L777)

`TargetBackend` 只定义一次 case 所需的操作：

| 操作 | 含义 |
| --- | --- |
| `prepare_case` | 准备工作目录和本轮执行资源，清空共享覆盖图 |
| `start_case` | 请求 Forkserver 创建 child，取得 PID |
| `poll_target` | 查询目标是否结束 |
| `finish_case` | 回收目标，将本轮共享覆盖图交付给 observer |
| `abort_case` | 按明确原因中止并清理目标 |

后端负责目标执行及其覆盖率来源。未来 Nyx-Net 后端实现相同操作，向 executor 返回同一形式的目标结果和 coverage map。

`CaseExecutor` 实现 LibAFL 的 `Executor`、`HasObservers`，只协调 backend 和 Python client。执行次数在这里递增一次，observer 的调用顺序沿用 LibAFL 框架。

### 每个 case 的流程

1. 清空当轮 map，恢复目标工作目录。
2. 请求 Forkserver 创建 child，取得 PID。
3. 启动 Python generator；首次真实 socket 连接做有界重试，等待 LightFTP 监听就绪。
4. 目标的 PCGUARD 插桩代码直接更新共享覆盖计数器；Rust 同时检查目标退出状态、Python RPC 结果和总截止时间。
5. generator 完成后关闭本轮连接，等待目标处理 EOF 并退出；必要时中止。
6. 确认目标终止后读取共享覆盖图，返回目标执行结果，由 LibAFL 处理反馈和 objective。

协议交互由 socket 的阻塞式 `send/recv` 自然推进。覆盖记录从 child 启动开始，不需要逐 case attach 或启动门闩。

### PCGUARD 与共享覆盖图

使用 `afl-clang-fast` 编译 LightFTP，设置 `AFL_LLVM_INSTRUMENT=PCGUARD`。LightFTP 有多个线程，编译时设置 `AFL_LLVM_THREADSAFE_INST=1`。编译及运行环境清除 `AFL_LLVM_ONLY_FSRV`、`AFL_GCC_ONLY_FSRV`，Forkserver builder 使用普通非 persistent 模式，不启用 `.fsrv_only(true)`。[AFL++ PCGUARD 实现](https://github.com/AFLplusplus/AFLplusplus/blob/v5.03c/instrumentation/SanitizerCoveragePCGUARD.so.cc)

首版配置：

- Rust 使用 LibAFL `UnixShMemProvider` 分配实际 coverage bitmap，并通过 `__AFL_SHM_ID` 告知目标。
- 默认 map 容量 64 KiB；按上游 Forkserver 握手校验目标要求的大小，容量不足时明确报错并提供配置入口，不能截断目标覆盖图。
- 目标 child 的线程共享同一 bitmap，guard 编号不跨 case 重置；每个 case 在启动 child 前清空计数器。
- 使用 `HitcountsMapObserver`、`MaxMapFeedback`、`QueueScheduler`；目标结束后由 observer 完成计数分桶，防止重复处理。
- Rust 与 Python 程序本身不使用目标的覆盖率插桩构建；外部 GnuTLS 等依赖使用未插桩版本。
- 覆盖统计统一使用 `coverage_map_occupancy` 等中性名称，记录来源 `pcguard`；不保留 PT trace 字节数、地址过滤或权限参数。

覆盖记录不依赖 Intel PT、perf 权限或 trace 解码。CPU 亲和性只作为可选执行配置，不作为覆盖率正确性要求。

## 4. Python 与 LightFTP 的最小改动

### Python runtime

迁移现有 `generator_server`，保留包名、`generate(ctx, params)` 和主要 RPC 格式，减少已有脚本改动。

- `registry.py` 复用现有加载及缓存逻辑。
- `context.py` 保留 `send/recv/connect/accept`；将原生 socket 操作提取到 `transport.py`。
- `Channel` 对脚本提供不暴露原生 socket/fd 的句柄，方便以后替换 transport。
- 修正按分隔符接收时的尾部缓冲，明确 EOF、timeout 和接收上限。
- Rust client 将现有同步等待拆成提交、轮询和取消，使 executor 能同时检查目标状态。

继续采用每 case 隔离 Python globals 的设计：

```text
Rust GeneratorClient
    ⇅ 现有 iceoryx2 + CBOR
Python server
    ⇅ campaign 内复用的内部管道
单线程 Python 模板
    └─ fork → case worker → 原生 socket → LightFTP
```

server 启动全新解释器作为模板；模板复用 `Registry` 导入脚本，自身不执行 case。每轮 worker 新建 `Session/Ctx`，结束时关闭所有连接。模板不持有 iceoryx2 对象，避免从可能已有后台线程的 IPC server 中直接 fork 执行 Python。

取消或通信失效时，Rust 清理整个 Python 进程组，下一轮重新初始化。每次 campaign 使用独立的 iceoryx2 服务名。

### LightFTP 与反馈

- 使用带 PCGUARD 插桩的普通 Forkserver 构建。
- 保留原协议循环；首个控制会话结束后依次等待相关线程退出，成功返回 0。
- generator 完成登录、PASV、LIST/固定文件 RETR、读取数据 EOF、读取 `226`、QUIT。
- 首次真实控制连接承担就绪检查，不另建探测连接。
- 每轮恢复独立 FTP 工作目录。
- 调度使用 `QueueScheduler`，变异仅作用于参数，时间只用于统计。

结果分别记录目标状态和 generator 状态。真实目标 crash 优先于随后的连接错误；普通 I/O timeout、脚本异常和主动清理信号不计为目标 crash。

## 5. 验收与默认值

- **PCGUARD 集成：** 验证目标真实写入共享 map，受控不同分支能产生不同反馈，case 间计数正确清零，多个目标线程的执行能够记录；不能只凭运行时的 map 标记字节宣布覆盖有效。
- **Python：** 复用已有测试，补充 globals 隔离、接收尾部缓冲、超时取消和异常连接清理。
- **LightFTP：** 验证成功/失败登录、LIST、RETR、QUIT、直接断开及末尾只发送；连续至少 100 轮检查进程、fd 和端口泄漏。
- **状态恢复：** 通过 `A → B → A` 检查目标会话、Python globals 和文件目录恢复。
- **反馈与回放：** 不同协议路径产生覆盖反馈并保留语料；保存的参数输入可以通过相同执行路径回放。
- **环境：** `doctor` 验证编译器、Forkserver 握手、实际共享 map 和分支反馈；不检查 perf 或 PT。验收在普通用户权限下运行。

默认 Linux x86-64、单 fuzz worker，连接预算 2 秒、单次 I/O 1 秒、整个 case 10 秒、目标退出宽限 5 秒。统计报告 PCGUARD map 的占用和新增覆盖，不将其解释为精确源码覆盖百分比。
