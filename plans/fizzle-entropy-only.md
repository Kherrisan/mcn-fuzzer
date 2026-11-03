# Fizzle 随机数确定化整合方案

## 目标与范围

基于 `third-party/fizzle` 的 `c99c479`，提取独立的随机数 preload 库，仅向 SUT 加载。继续使用现有 LibAFL forkserver、Python generator、原生 socket、namespace 和 libpcap 抓包流程。

本方案待实现。上游当前没有可直接启用的 `entropy-only` 模式，需要新增独立库，解除随机数 hooks 对 Fizzle Scheduler 和共享状态的依赖。

确定化的基础保证是：相同 seed、相同实现版本、相同随机 API 调用顺序，得到相同随机字节。真实多线程调度、时间和外部 I/O 事件仍可能影响调用顺序；本方案不承诺整个并发执行完全确定。

## 1. 独立的 entropy 库

在 Fizzle workspace 中增加独立 `cdylib` crate，例如：

```text
third-party/fizzle/
├── fizzle/                    # 原有完整 harness
└── fizzle-entropy/            # 新增：独立随机数库
    ├── Cargo.toml
    └── src/
        ├── lib.rs
        ├── rng.rs            # 私有 PRNG 状态、seed 与线程安全
        ├── hooks.rs          # 随机 API 拦截
        └── random_fd.rs      # 随机设备 FD 的识别和读取
```

产物为 `libfizzle_entropy.so`。复用 Fizzle 的 ChaCha20 算法与随机 API 覆盖思路；独立实现初始化和状态访问，避免加载完整 `libfizzle.so` 后连带启用 socket、线程调度、虚拟时间、进程和 fuzz-round hooks。

PRNG 状态使用普通进程私有内存，不使用 Fizzle 的跨进程共享状态。用锁保护真实多线程调用；处理 fork 时的锁状态，避免 child 继承其他线程持有的锁。需要 atfork 协调时注册轻量回调，不替换原生 `fork()` 行为。

## 2. 只向 SUT 注入

复用 `Campaign.target.extra_env` 到 `ForkserverExecutor::builder().env(...)` 的现有路径，注入库与 seed。不要在启动 Fuzzer 的全局环境中设置 preload，避免 Python server 和 Fuzzer 继承。

整合后可使用以下配置；库和 seed 环境变量均为本计划拟新增接口：

```toml
[target]
extra_env = [
  ["LD_PRELOAD", "/绝对路径/libfizzle_entropy.so"],
  ["FIZZLE_ENTROPY_SEED", "1"],
]
```

- 使用绝对路径，因为 SUT 的工作目录会切换到 campaign workdir。
- 与目标已有环境变量合并，例如 OpenSSL 的 `OPENSSL_CONF`；已有 preload 库需要保留并明确加载顺序。
- 启动前检查库路径及 seed 格式，错误时在首个 case 前报错。
- seed 的解析和字节编码必须稳定，不依赖 Rust 默认哈希、机器地址或宿主环境。

对应入口为 `fuzz/src/backend/forkserver.rs` 中的 `ForkserverBackend::spawn`。

## 3. 固定 seed 与每 case 状态

第一版采用固定 campaign seed，每个 SUT root case 使用相同起始随机序列。

当前后端是普通非 persistent forkserver（`.is_persistent(false)`）。只要 forkserver 父进程停在固定 checkpoint，并且不在 case 之间消费随机数，每个 child 都会继承同一份私有 PRNG 快照。若初始化发生在 child 首次调用时，也使用同一固定 seed。

因此第一版不增加逐 case seed IPC。不得使用 PID、时间、`worker_id` 或 `case_id` 重新 seed；这些值会随运行变化，破坏重放。还需确认目标自身是否在 forkserver checkpoint 之前初始化内部 PRNG，并验证其继承和 fork 后 reseed 行为。

保存 case 时记录生效 seed、PRNG 算法和实现版本；replay 必须能恢复相同配置。信息应随保存的 case 保留，不能只出现在临时日志或 PCAP 中，也不应混入传给 generator 的协议参数。

未来若需要每个输入使用不同 seed，再增加持久化的 seed 属性或有版本的稳定输入派生算法，以及 child 的明确 reset 入口。forkserver 启动后的环境不会自动逐 case 更新。未来 persistent 或 Nyx 后端也应在 case/snapshot 恢复入口应用同一 reset 合约。

## 4. 随机 API 覆盖与语义

覆盖目标实际使用的入口：

- `getrandom()`、`getentropy()`。
- `rand()/srand()`、`random()/srandom()`。
- `arc4random*()`。
- `/dev/random`、`/dev/urandom` 的打开与读取路径。
- 目标实际使用 libc `syscall(SYS_getrandom, ...)` 时，覆盖这个入口；其他 syscall 保持正确透传，不能照搬 Fizzle 对未支持调用直接 panic 的逻辑。

随机设备需要窄范围 FD 跟踪，覆盖实际使用的 `open/openat/fopen`、`read/readv/fread` 以及 `dup/close` 生命周期。其他文件和 socket FD 使用真实 libc。按路径识别时还需考虑相对路径、符号链接等情况；不能将随机设备简单替换为 `/dev/zero`。

复用上游代码时修正 API 语义，包括：

- `rand()`、`random()` 的合法返回范围。
- `getentropy()` 的长度约束、返回值和错误语义。
- `arc4random_uniform()` 的边界值和均匀取样。
- 明确 `srand/srandom` 策略：固定序列模式不能因目标执行 `srand(time(NULL))` 再次引入真实时间。采用固定 seed 派生的重置策略，并记录该语义。

LD_PRELOAD 无法覆盖应用内联 syscall 指令或 CPU `RDRAND/RDSEED`。需根据目标构建确认实际熵源，不能把 hooks 列表当作已经覆盖全部随机来源的证明。

## 5. OpenSSL 的目标级适配

系统熵确定化与 OpenSSL 最终随机输出确定化需要分别验收。

本地 OpenSSL 3.5.9 使用 `no-shared/no-module/no-asm` 构建。其系统熵路径优先调用 `getentropy()`，并有 libc `syscall(__NR_getrandom)` fallback，可由 entropy 库处理。

但源码的 nonce 路径还会混入 PID、线程标识和时间；另一路径包含 DRBG 对象地址和计数器。因此，仅固定系统熵不能直接保证 `RAND_bytes`、TLS ServerHello random 或临时密钥重复一致。

如果验收要求包含最终 OpenSSL 随机输出，需要在目标 fuzz 构建中接入确定性的 RAND provider，或增加专用随机生成适配，控制相关 seed、nonce 和随机输出。不要通过全局伪造 PID 或时间达到这一目的。当前静态链接的 libcrypto 内部调用，也不能假定可由 preload `RAND_bytes` 符号统一拦截。

相关源码：

- `fuzz/.deps/openssl/openssl-3.5.9/providers/implementations/rands/seeding/rand_unix.c`：系统熵与 PID/线程/时间 nonce。
- `fuzz/.deps/openssl/openssl-3.5.9/providers/implementations/rands/drbg.c`：DRBG nonce 路径。
- `fuzz/targets/openssl/build.sh`：目标构建配置。

## 6. 验收

- 同一 seed 和调用序列重复运行，随机 API 输出一致；不同 seed 能产生不同序列。
- 检查 API 边界、合法范围与错误行为，确认非随机文件及原生 socket 不受影响。
- 在普通 forkserver 中验证 `A → B → A`，以及跨 campaign 重启后的 `A`；随机状态不从上一 case 泄漏。
- 验证真实多线程调用的状态安全，以及 fork 后不存在 PRNG/随机 FD 锁死。
- 保存 case 后 replay 恢复相同 seed 和实现版本。
- 分别对 LightFTP 和 OpenSSL 运行现有 generator，确认覆盖率、进程生命周期和抓包仍正常。
- OpenSSL 分别核对系统熵输出和最终 RAND/TLS 随机字段，不能仅凭 entropy hook 命中宣布最终输出确定。
- 比较重组后的协议 payload 或具体随机字段；原生 TCP 端口、序列号与抓包时间戳不作为随机数一致性的判据。
- 用现有 Release benchmark 比较启用前后的吞吐量，记录随机库的额外成本。

## 实施顺序

1. 提取独立 entropy 库，完成 API 语义与固定 seed。
2. 通过目标专用环境接入普通 forkserver，完成跨 case 与 replay 验收。
3. 核实并补齐 LightFTP/OpenSSL 实际熵源覆盖。
4. 若要求 OpenSSL 最终随机字段确定，完成目标级 RAND 适配与验证。
5. 完成协议交互、抓包和性能回归记录。
