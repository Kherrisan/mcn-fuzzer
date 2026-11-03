# 基于 Nyx-Net 和 host Python generator 的独立 fuzzing 引擎

## 目标与执行模型

采用“每个 case 独立执行、结束后恢复 guest 状态”的语义。Rust LibAFL 负责参数变异、语料调度和反馈；Python generator 运行在 host，通过 Python Nyx-Net SDK 与 guest 中的 SUT 交互。协议报文字节通过 Nyx 的共享 payload buffer 传递，由 guest Nyx-Net I/O adapter 注入或截获目标连接的 I/O。

首版范围：

- 新建独立引擎，保留旧 LibAFLstar 项目供参考。
- 一个 case 执行一次完整的同步 `generate(ctx, params)`，允许根据实际响应决定后续发送内容。
- Python runtime、generator 及其依赖全部位于 host；guest 只包含 SUT、Nyx-Net I/O adapter 和启动辅助程序。
- 使用单一初始 guest 快照；同一 case 内 guest 按 SUT 原有的阻塞 I/O 语义等待输入，host 按需写入共享 payload channel，不显式暂停／恢复 VM。
- 首先支持单个 TCP 服务端连接、源码覆盖率和受控的小型 C 服务。guest 每轮不执行 `fork()`，不调用 SUT 的 `__AFL_LOOP`。
- Nyx 端到端测试在另行准备的合适主机完成。

```text
Host
  Rust LibAFL：{generator, params} 变异、调度、coverage feedback
                 │ case RPC / 完整结果与 bitmap
                 ▼
  Python driver：NyxNetSession + libnyx native adapter（唯一 VM owner）
                 ⇅ host IPC
  Python generator worker：generate(ctx, params) → Python Nyx-Net SDK

  Python driver ⇄ Nyx payload buffer：协议字节与 I/O 帧
                 ⇅ 并发读写有界 mailbox
Guest           Nyx-Net I/O adapter ⇄ SUT
                 └──── 同一个 guest 恢复快照 ────┘
```

## 上游调研与复用结论

调研日期：2026-11-02。已找到 Python host 控制代码和 libnyx 绑定，但尚未找到直接满足“host Python 响应驱动多轮交互”的完整 Nyx-Net SDK。采用“参考已有绑定，在仓库内实现 Python SDK，并补齐底层交互接口”的方案。

| 上游 | 已有能力 | 本项目的处理 |
| --- | --- | --- |
| [Nyx-Net](https://github.com/RUB-SysSec/nyx-net/blob/main/README.md) | 保存论文实验版本；README 指向后续 Nyx framework | 复用网络 I/O hook 思路；选择并固定可配套构建的 Nyx 组件版本 |
| [libnyx](https://github.com/nyx-fuzz/libnyx/blob/f07a41fcf0a01be5f8b8397b26cb914a149b9198/README.md) | 官方提供 C/Rust 接口，管理 VM、payload、bitmap 和执行结果 | Python 通过 `ctypes` 使用固定版本的 C ABI，新增交互执行 adapter |
| [Themis Python binding](https://github.com/CISPA-SysSec/themis/blob/eb3404dbe585f770e317d190f691ff23c3a47519/themis/fuzzer/libnyx.py) | 已有 `ctypes` 封装，但使用旧式 `nyx_new(...)` ABI 和硬编码库路径 | 参考封装方式，按选定版本头文件重新声明类型、参数、返回值和资源生命周期 |
| [AFL++ Python 调用示例](https://github.com/AFLplusplus/AFLplusplus/blob/stable/afl-cmin.py) | `get_nyx_map_size()` 使用 config 对象初始化 libnyx | 参考较新 ABI 的初始化流程 |
| [kAFL Python host runner](https://github.com/IntelLabs/kafl.fuzzer/blob/cc6bace3629e56325c8c1a6a9863bd11c2747786/kafl_fuzzer/worker/qemu.py) | Python 管理共享内存、aux buffer 和 UNIX control socket，区分单次 `run_qemu()` 与整轮 `send_payload()` | 参考底层握手、事件处理和 ABI 校验，不把整套 kAFL 调度器作为依赖 |
| [Nyx-Net payload replayer](https://github.com/nyx-fuzz/packer/blob/7967b432c1994059a02013c9bc8fdd22230df48a/packer/nyx_net_payload_executor.py) | 将导出的 `packet(...)` 序列回放到真实 TCP/UDP 或 stdout | 可供回放工具参考，不能直接承担 VM 内的交互 SDK |

需要补齐的底层能力：

- 当前 [`libnyx::send_payload()`](https://github.com/nyx-fuzz/libnyx/blob/f07a41fcf0a01be5f8b8397b26cb914a149b9198/fuzz_runner/src/nyx/qemu_process.rs#L342) 自动消费 `NYX_HPRINTF` 并继续运行；`nyx_exec()` 面向整轮结果。实现时让整轮 `nyx_exec()` 在 driver 的执行线程中运行，同时由 driver 的 IPC/mailbox 服务读写共享 payload buffer，支持 generator 阻塞式多轮 `send/recv`。只增加 Python FFI 包装不足以做到共享区的并发读写与同步。
- 上游 [Nyx-Net I/O hook](https://github.com/nyx-fuzz/packer/blob/7967b432c1994059a02013c9bc8fdd22230df48a/packer/linux_x86_64-userspace/src/netfuzz/inject.c#L286) 对目标连接的 `send/write` 直接返回成功并丢弃输出；输入耗尽也可能直接 release。必须改为捕获响应和等待后续 host 输入。
- [QEMU payload 映射](https://github.com/nyx-fuzz/QEMU-Nyx/blob/ff1c89732115274e912a2809fcba58e67df23dfd/nyx/memory_access.c#L295) 使用共享映射，并将这些页排除在快照恢复之外；写保护是可配置项。因此本方案拟将 payload 划为双向 mailbox，并显式清理跨 case 数据。这是需要原型验收的工程设计，上游没有现成的双向消息队列契约。
- I/O 通知不依赖 HPRINTF 或 Nyx 的显式 yield。payload 的 guest→host 区由 guest 直接写入，host driver 轮询或等待 mailbox 状态变化并唤醒阻塞的 Python SDK；HPRINTF 仍只用于诊断日志。共享映射并发读写、内存屏障和 libnyx 执行期间访问 payload 指针的安全性必须由首个原型验证；失败时再评估 QEMU 管理的共享 I/O 通道。

## 实现方案

### 独立工程与输入契约

- 在仓库新增 `fuzz/`，包含 Rust 引擎、host Python SDK/runtime、libnyx native adapter、guest I/O adapter、打包工具和测试目标。
- 提供 `pack`、`run`、`replay`、`doctor` 命令。配置描述 SUT、启动参数、固定 TCP endpoint、host generator、host Python 依赖、payload 容量和超时预算。
- 保留 `{generator, params}` 语料形式。generator 路径登记为 host manifest 内的稳定 ID，同时保存脚本哈希和 Python 依赖版本；运行时只变异参数。
- `{generator, params}` 只在 host 调度与执行层流转。payload 传递 generator 实际生成的报文字节及 I/O 控制帧，guest 不解释 Python 脚本或参数。
- 固定 LibAFL、libnyx、QEMU-Nyx、packer 和 guest adapter 的兼容版本及 ABI。补丁保存在仓库内，并给出可重复构建步骤；不混用论文版 Nyx-Net 与任意新版组件。

### Python host SDK 与 VM 所有权

新增 Python 包 `nyxnet`，提供两层接口：

- 底层 `NyxNetSession`：配置及启动 VM、协商 ABI/capabilities、开始 case、阻塞式 `send/recv`、结束／中止 case、等待整轮执行结果、读取 coverage/result 和关闭 VM。这些是本项目拟新增的 API，不能假定 stock libnyx 已提供。
- generator 层 `ctx`：保留 `generate(ctx, params)`、`ctx.send(data, channel=None)`、`ctx.recv(channel=None, until=None, max_bytes=4096)` 和 `Channel.close()` 的现有调用形式。通过 session 代理完成虚拟连接上的 I/O。

`NyxNetSession` 使用 `ctypes` 封装 libnyx 的固定版本 C ABI；native adapter 暴露共享 payload mailbox 的安全读写和跨线程原子操作，保留上游 page fault、日志、crash 和 timeout 处理。driver 只在一个执行线程调用 libnyx 控制函数；mailbox 服务线程只访问长期有效的共享 buffer 和 host IPC，不能并发调用 libnyx 控制 API。库路径由配置或打包结果决定，初始化检查符号、ABI、payload/bitmap 容量和协议版本。

Python driver 是唯一 VM owner，独占 libnyx handle、control socket、payload 与 bitmap。Rust 通过 host IPC 提交 case 并接收终态结果和 bitmap，使用自定义 LibAFL executor/observer 接入反馈。generator worker 通过 SDK 代理请求 driver；Rust executor 和 generator worker 都不直接操作 VM。

host IPC 首版使用 UNIX socket 与带长度前缀的 CBOR，并限制帧大小；传输字节使用 CBOR byte string。case ID、序号、错误和终态字段明确编码。Python SDK、host IPC 和 guest mailbox 使用各自显式版本的契约，不沿用旧 generator server 的隐式 session 状态。

首版只暴露一个已配置的默认 TCP channel。`ctx.connect()`、UDP、监听和第二条连接明确返回“不支持”。现有 FTP PASV 示例依赖第二 TCP 连接，不能作为首版兼容验收目标。

### payload channel 与交互执行

payload 按固定布局划分为控制头、host→guest 环形缓冲区和 guest→host 环形缓冲区。协议头使用固定字节序和宽度，至少包含 magic、version、case ID、单调递增读写游标、状态和 EOF/关闭标志。Nyx 外层输入长度字段与内部 mailbox 布局分别定义并在两端校验；不能把 stock `nyx_set_afl_input()` 的整段覆写当作逐消息队列操作。

- host→guest：host SDK 将 `send()` 的字节流写入输入环；guest 的 hook 将其作为目标连接的可读字节提供给被 hook 的 `read/recv`。case 结束时 host 标记 EOF，adapter 在缓冲排空后向 SUT 返回 EOF。
- guest→host：guest 的 hook 将 SUT `write/send` 字节流写入输出环；host SDK 的 `recv()` 等待输出可读后返回数据。guest 结束、异常和 channel 关闭通过独立状态标志报告。
- 两个方向采用有界单生产者／单消费者 ring、单调游标与 acquire/release 内存屏障。缓冲满时生产者按既定的有界等待策略背压；接收方消费后推进游标。case ID 防止过期 mailbox 数据串入下一轮，不能静默截断或丢弃响应。
- 初始化关闭 payload 写保护，guest 对分配给自己的输出区写入；host 验证所有长度、序号和状态。此模式下 guest 写 payload 是协议行为，不能继续套用只读输入模式的错误判定。
- host 与 guest 在同一 guest case 运行期间并发访问各自方向的 ring；游标和状态的原子发布顺序保证消费者只读取完整数据。二进制报文不经过 HPRINTF 的字符串编码。
- payload 页不随 guest 快照恢复。driver 在确认 guest 停止／恢复到初始边界后，清零共享区、更新 case ID 并重新初始化双方游标；guest 内队列、fd 状态和解析状态则由快照恢复。

case 交互流程：

1. 恢复初始 guest 快照、清空 mailbox 并启动本轮 generator；driver 在执行线程启动一次完整的 `nyx_exec()`，在服务线程转发 SDK 请求和 mailbox 数据。
2. `ctx.send()` 将字节写入 host→guest ring 后返回或等待缓冲空间。guest adapter 的 `read/recv` 从 ring 取字节；缓冲暂空时按正常阻塞式接收等待 host 写入，Nyx VM 持续运行。
3. SUT `write/send` 的输出由 adapter 写入 guest→host ring。driver 服务线程把字节交给 SDK；Python `ctx.recv()` 阻塞到数据可读或截止时间，再将响应交给 generator。
4. generator 根据响应继续 `send/recv`，直至返回；整个期间保持同一个 guest 进程、TCP channel、mailbox 和覆盖 bitmap。I/O 往返不触发 VM 暂停、恢复快照或独立的 `nyx_exec()`。
5. generator 返回后，host 标记输入 EOF。guest adapter 让 SUT 按协议处理 EOF；guest harness 在输入排空并达到约定完成条件后 release。driver 等待 `nyx_exec()` 返回，再汇总覆盖、crash 或 timeout。

SUT 可能先发送 greeting，因此 SDK 的 `recv()` 可以是 generator 的首个 I/O 操作；guest 在 guest→host ring 发布 greeting，generator 直接阻塞等待它。`ctx.send()` 的成功表示字节已被 mailbox 接纳，SUT 是否处理完由随后响应、EOF 或执行终态体现。

host SDK 直接消费 guest→host ring；generator 尚未调用 `recv()` 时 ring 可暂存输出，但其容量和每 case 总字节数有明确上限。worker 异常时由 driver 标记 abort 并结束执行；若无法确认 guest 已 release、crash 或 timeout，先销毁并重建 VM，不能复用仍在运行的实例进入下一轮。

### guest I/O adapter、生命周期与覆盖率

- 基于 Nyx-Net 的网络 hook 编写适配层，接管首版目标实际使用的 `accept`、`read/recv`、`write/send`、`readv/writev`、关闭与等待调用；未覆盖的目标 I/O 路径明确拒绝或在 `doctor` 中报告。
- guest adapter 对 SUT 暴露阻塞式 socket 语义：`read/recv` 在输入环为空时等待，数据发布后返回字节；SUT 的 `write/send` 则把响应写入输出环。等待由 guest 内的 hook/relay 完成，不调用 Nyx release，也不结束或重开 `nyx_exec()`。原型确定低开销、可快照恢复的等待实现，并验证 guest 执行 watchdog 不会把正常等 host 输入误判为超时。
- guest bootstrap 在快照前建立选定的本地连接；adapter 在目标 fd 已登记、覆盖率已注册、SUT 尚未执行本轮协议 I/O 的边界建立初始快照。server-first greeting 必须位于快照后的执行范围。
- 输入按 TCP 字节流处理，host `send()`、payload 分片及 SUT `recv()` 边界不保证一一对应；维护部分读取、剩余字节、短写、EOF 和关闭状态。首版按需实现所支持的 `poll/select/epoll` 就绪语义，不能让目标等待真实网络输入。
- 调整 Nyx-Net 原有“输入耗尽即 release”和“输出直接丢弃”的逻辑，由显式 case 协议管理终止。guest adapter 不再依赖原字节码解释器预先拿到整段交互输入。
- guest 的阻塞式网络 hook 只阻塞 SUT 的 I/O 调用；Nyx VM 保持运行，等待 mailbox 数据期间不执行 snapshot restore、release 或清空 bitmap。唯一初始快照统一恢复 SUT 内存、guest 队列、fd 和 RAMFS 文件状态。
- SUT 保留 AFL++ 源码覆盖率插桩，guest shim 初始化 bitmap 并注册给 Nyx。Python driver 在最终完成／crash／timeout 时复制 bitmap 与结果交给 Rust observer，确保下一轮不会覆盖待提交的数据；首版不采用 Intel PT。
- 复用并修改一套 Nyx guest harness/preload 负责初始化、网络 hook 和退出路径，避免多个组件重复接管轮次。真实 crash、正常退出和 guest agent 错误分别报告。

### host Python 状态与超时

guest 快照只能恢复 guest，不能恢复 host Python 的 globals、随机源或 SDK 缓冲。为保持每个 case 独立，首版使用专门的 host generator 模板进程预先导入脚本，每个 case 从模板 `fork()` 出隔离 worker；模板不创建后台线程，不持有 VM/control/payload 资源，子进程通过 SDK 代理访问 driver。结束或异常后销毁子进程。

该 host 隔离成本计入基准测试。guest 不发生每 case fork，Python 依赖不重复导入；“快照建立后整个系统不再创建进程”不再作为验收条件。模板自身不执行 case，避免其状态被前一轮污染；随机种子从可重放输入派生。host 外部文件写入和外部服务访问不属于 guest 快照的恢复范围。

超时区分以下语义：

- `recv` 截止时间：host SDK 在等待 guest→host ring 时计时；超时返回给 generator。若有 guest 侧接收截止时间，也由 adapter 在 guest 运行中执行。
- guest 执行预算：限制完整 case 的 guest 执行，包括目标代码和 adapter 等待。host generator 自身的计算时间由独立墙钟预算限制；driver 不能在每次收发后重置 guest 总预算。
- host generator/watchdog 预算：限制死循环、IPC 卡死、无限事件流和结束 drain。墙钟 watchdog 用于终止失控执行，其触发不作为协议行为确定性的依据。

当 guest `read/recv` 等待输入且 ring 为空时，guest adapter 依照受控阻塞策略等待；host 的 `send()` 写入后 guest 自动读到数据。host `recv()` 等待输出时，driver 持续检查输出 ring 和整轮 `nyx_exec()` 终态，必要时报告 guest timeout/crash，避免 generator 永久挂起。两端的接收截止时间分别由对应一侧执行。

移除原虚拟调度器后，不假定已经具备完整虚拟时间。首版只承诺受控单线程目标和显式等待路径的时间语义；guest mailbox 等待时钟、Nyx 执行预算与 host 收发截止时间分别定义，不能把 host polling 延迟直接解释为 SUT 接收超时。

### 结果与回放

- 区分 SUT crash、guest 执行 timeout、普通 `recv` timeout/EOF、generator error、SDK/ABI/VM 基础设施错误和未完成 drain。Python 或 adapter 失败不计为 SUT 漏洞。
- generator 最后只发送时，发送 EOF 并让 SUT 按协议处理剩余字节和关闭状态；guest release 或 Nyx crash/timeout 作为本轮终态，不能因暂时没有 mailbox 数据就提前 release。后台任务和未建模 timer 不在首版保证内。预算耗尽须记录为 timeout，不能直接当正常结束。
- 保存 `{generator, params}`、随机种子、generator/依赖版本、双向逻辑 I/O transcript、结果和覆盖摘要。逻辑 transcript 合并 mailbox 分片，不依赖搬运分块大小。
- `replay` 复用相同 SDK、guest adapter 和 case 边界；既支持重新运行 generator，也支持固定 transcript 回放并检查响应。出现响应差异时明确报告。

## 分阶段实现与验收

1. **交互通道原型。** 固定兼容的 Nyx 版本，在受控 C 目标上完成“host Python 写 payload → guest 阻塞式读取 → guest 将含 NUL 的二进制响应写回 payload → host 阻塞式读取响应并发送第二条消息 → SUT 收到 EOF → 最终 release”。Nyx VM 在整个交互期间连续运行；验证共享映射并发可见性、原子游标、写保护配置、整轮执行预算和覆盖累计。原型未通过前，不把交互能力视为已具备。
2. **Python SDK 与 guest adapter。** 完成 ABI 检查、分帧、背压、部分读取、收发缓冲、EOF/关闭、错误分类、资源回收和 host worker 隔离；补齐目标需要的 hook 与等待调用。
3. **LibAFL 接入。** 实现 Rust executor/observer、参数变异、完整结果与 bitmap 回传、语料保存及同路径回放。
4. **端到端验收与基准。** 在具备 Nyx 条件的主机完成以下检查，再记录性能。

验收项：

- **交互正确性：** 响应驱动的后续发送、server-first greeting、拆包、粘包、二进制 NUL、分隔符、EOF、关闭、接收超时和末尾只发送。
- **传输完整性：** 双向超过单帧容量的报文、背压、无静默丢包；过期 case/sequence、非法长度和损坏帧可被检测，不能误算 SUT crash。
- **case 边界：** 多轮阻塞式收发共用一个 `nyx_exec()` 和 guest 状态；只有 case 终止后才恢复快照。验证终态覆盖包含最后一次发送触发的分支，且 host 持有执行期间不会清空 bitmap。
- **确定性：** 对受控目标，同一 case 重复执行 1,000 次，SUT bitmap、规范化逻辑 transcript 和结果一致；不比较墙钟时间、日志或 mailbox 分片。
- **恢复完整性：** 使用 `A → B → A` 同时验证 guest 内存、fd/队列、RAMFS 与 host Python globals、ctx 缓冲、随机源和共享 payload；crash、timeout、generator 异常后仍能执行正常 case。
- **覆盖反馈：** 目标包含可区分的分支和确定性崩溃输入，验证 Rust 收到的是 SUT 覆盖率，LibAFL 据此保留新语料；Python/SDK 自身执行不混入目标反馈。
- **执行成本：** 分别记录 guest 恢复、host worker fork、Python 生成、payload 往返、native adapter 和 coverage 回传成本；与基础 forkserver 基线比较。
- 本机先完成 SDK/帧协议和目标适配检查；缺少 Nyx 条件时明确跳过 VM 验收，不自动切换后端并宣称通过。

## 默认与后续边界

首版采用 Linux x86-64、单 fuzz worker、单线程受控目标、单一默认 TCP channel、源码可编译目标和单一初始快照。Python generator 通过 `ctx` 通信；不承诺任意真实时钟、外部服务、后台线程或 host 外部副作用具有确定性。

提供与固定 Nyx 组件配套的主机准备说明和环境检查，不在本次实现中修改当前主机内核或 KVM 配置。旧 Nyx-Net 与后续组件的快照格式、agent ABI 可能不同，必须使用配套版本。[Nyx VM 兼容说明](https://github.com/nyx-fuzz/Nyx/blob/main/docs/01-Nyx-VMs.md)

动态多 TCP、FTP 控制／数据连接、UDP、监听、多线程／后台 timer 的完整等待语义、增量状态快照、host worker 隔离优化及 RSC 模型到 generator 的生成链路，留到首版验收后扩展。
