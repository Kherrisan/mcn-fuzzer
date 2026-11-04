# LLVM 源码覆盖率增量重放 Stage

## 1. 行为与接口

新增可选的 `LlvmCoverageStage`，与现有 PCGUARD bitmap 反馈独立，用于统计源码级函数、行、region、分支及函数实例覆盖。

- 重放主 corpus 和 crash solutions 中保存的 `GeneratorInput`。
- 默认每 100 轮 fuzz 执行一次；初始 seeds 加载后和正常退出前各补跑一次。
- 每次只处理尚未成功采集的 case；没有新 case 时不调用 LLVM 工具、不创建快照。
- 增加 `[source_coverage]` 配置：`enabled`、覆盖版 `binary`、`output`、`interval`、`llvm_profdata`、`llvm_cov`。默认关闭，启用后 `interval=100`，使用本机 LLVM 18 工具。
- 重放执行次数单独统计，不增加主 fuzz 的 executions，也不更新 bitmap feedback、objective 或 corpus。

**贡献归因采用批次粒度。** 若一批重放让任一源码覆盖指标增长，该批全部有效 case ID 被加入贡献集合；不能保证其中每一个 case 单独产生了新覆盖。输出元数据明确记录 `attribution: "batch"`。

## 2. 覆盖版构建与重放

为 LightFTP、OpenSSL 分别构建独立覆盖版本，使用底层为 Clang 18 的 `afl-clang-fast`。所有编译、链接步骤都限定在 `AFL_LLVM_ONLY_FSRV=1` 的构建环境中，并附加：

```text
-O2 -g -fno-lto
-fprofile-instr-generate
-fcoverage-mapping
-fprofile-update=atomic
-mllvm -runtime-counter-relocation
```

- `AFL_LLVM_ONLY_FSRV=1` 使 AFL 编译包装器链接 `afl-compiler-rt.o`、关闭 PCGUARD 插桩；源码覆盖由 LLVM profile runtime 负责。目标直接携带 AFL forkserver，保留现有目标补丁、配置及依赖构建方式。
- 编译和运行所需的 forkserver-only 环境仅作用于覆盖版；主 fuzz 继续使用 PCGUARD 构建和执行配置。不能使用会禁用 forkserver 的 `AFL_DISABLE_LLVM_INSTRUMENTATION`。
- LightFTP 统计自身源码；OpenSSL 统计本次插桩构建的应用及静态库。未插桩的系统库不计入覆盖范围。
- 每批创建独立的重放会话：启动覆盖版 SUT 的常驻 AFL forkserver 和专用 Python server。每轮由 forkserver 创建新的 SUT child，Python server 创建新的 generator worker，使用相同 generator 和 params，串行执行。
- 重放通过现有 forkserver 后端的启动请求、PID 回传、退出状态与信号处理路径接入，不另建直接启动 SUT 的执行后端。共用双进程生命周期、超时及后代回收逻辑；重放使用独立 workdir、端口、日志和 IPC 名称，关闭 pcap。
- namespace 清理必须保留主 fuzz 的 forkserver、Python server 等常驻进程。重放失败不得终止这些进程，也不重建它们。
- 本批 coverage forkserver 和专用 Python server 在批次结束时关闭；发生不可恢复错误则清理本批进程并提前结束本批，下一次 stage 再处理未完成 case。本批内不因 timeout 重建 forkserver 或 Python server。

### AFL runtime 与 LLVM profile runtime

- AFL runtime 通过 constructor 在 `main()` 前启动 forkserver。每轮 child 从 forkserver 内返回并继续执行目标程序，每批仅启动时 exec 一次覆盖版 binary，每个 case 只 fork。
- forkserver 握手、child PID、原始 wait status 和协议 FD 生命周期由上游 AFL runtime 实现，Fuzzer 使用 LibAFL 的公开 forkserver API 驱动。
- 不启用 persistent mode、early forkserver 或手工 deferred 初始化；首先验证默认 constructor 初始化顺序即可满足覆盖采集。
- LLVM profile 的连续计数映射可能在父进程建立并由 child 继承。因此，不能在本批 forkserver 存活时替换、移动或回滚它正在使用的 raw profile 文件。
- 本机临时探针已验证 forkserver-only 与 LLVM 连续 profile 的组合：正常执行 A、B，再执行 A 并 SIGKILL，累计得到 A=2、B=1、main=3，PID/status 回传与覆盖导出正常。LightFTP/OpenSSL 仍需目标级验收。

重放的进程关系为：

```text
Fuzzer（namespace PID 1）
├── 主 fuzz forkserver → PCGUARD SUT
├── 主 fuzz Python server → generator worker
├── 本批 coverage forkserver（覆盖版 SUT 内的 AFL runtime）→ LLVM coverage SUT child
└── 本批 coverage Python server → generator worker
```

### 复用现有 executor

- 将现有 forkserver 后端的通信、child PID/status 管理与 bitmap 反馈分开。主 fuzz 使用现有 PCGUARD 模式；coverage 重放使用上游 `ForkserverExecutor` 的 `.fsrv_only(true)`，不挂载 map observer，也不接入 map feedback。
- 启动程序就是覆盖版 SUT，使用渲染后的目标参数。运行时继续设置 forkserver-only 环境；构建阶段的同名环境也必须设置，不能仅靠运行时开关移除已经生成的 PCGUARD 插桩。
- AFL runtime 即使处于 forkserver-only 模式，仍可能宣告 map size 或要求哑 map。沿用上游 map-size 握手校验并为 coverage 会话隔离兼容性所需的资源，不将其作为覆盖数据读取；不能继承、覆盖或连接主 fuzz 的 `__AFL_SHM_ID` 和 bitmap。
- 主 fuzz 的保留环境变量校验与清理规则继续生效；由 coverage 后端内部设置允许的 forkserver-only 环境，不通过修改全局环境或放宽主 fuzz 校验来接入。
- coverage 重放复用现有 case 协调逻辑与生命周期策略，计数和结果保持独立，不增加第二套原生进程执行流程或独立 controller。
- namespace 清理的常驻集合同时包含主 fuzz forkserver/server 与本批 coverage forkserver/server。每轮只回收相应 SUT、generator worker 和遗留后代；批次收尾再移除本批常驻进程。

## 3. 在线累计与 state metadata

覆盖运行时直接更新累计 raw profile：

```text
LLVM_PROFILE_FILE=<working-raw-directory>/%m%c.profraw
```

`%m` 使用运行时在线合并，`%c` 配合 counter relocation 保存异常退出前的计数。本机 LLVM 18 探针已验证多次正常运行及 SIGKILL 后的累计行为；实际目标仍需验收。参见 [LLVM 源码覆盖文档](https://clang.llvm.org/docs/SourceBasedCodeCoverage.html)。

- 每个 case 完全退出并清理后再执行下一个，禁止跨 case 并发写入累计 profile。
- `LLVM_PROFILE_FILE` 在启动本批 coverage forkserver 前设置，指向本批固定的工作 raw 目录。所有 child 沿用相同 `%m%c` 路径与运行时映射，case 之间不重新设置文件名，也不重置 LLVM profile 计数器。
- 累计过程不再执行“旧 profdata 加新 profraw”的合并。
- 每批结束后先停止本批 forkserver、Python server 并回收遗留进程，确认 raw 不再被更新，再仅索引当前完整 raw 数据一次：

```bash
llvm-profdata-18 merge -sparse <累计 raw 文件列表> -o coverage.profdata.tmp
llvm-cov-18 export <覆盖版 binary> \
  -instr-profile=coverage.profdata.tmp > coverage.json.tmp
```

这里的 `merge` 用于 raw 到 indexed profdata 的转换；旧 profdata 不作为输入。JSON 保留完整导出内容。参见 [llvm-cov export](https://llvm.org/docs/CommandGuide/llvm-cov.html#export-command)。

新增可序列化的 state metadata，保存：

- 已成功采集的 case ID 集合。
- 累计贡献集合、上一批覆盖 summary、最新快照编号。
- 构建指纹和 case ID 对应的输入指纹。

采用稳定的 `CoverageCaseId = corpus 类型 + 输入指纹`；输入指纹包含规范化的 GeneratorInput 和 generator 源码摘要。同时保存可用的 LibAFL `CorpusId` 与输入文件名用于定位，避免主 corpus、solutions 或重启后的编号冲突。

跨进程启动时，从持久化 checkpoint 恢复 metadata，并枚举现存 corpus/solutions 输入重新关联。仅有旧 profdata、缺少对应 raw/checkpoint 时不直接续写，告警并要求使用新的覆盖输出目录。

## 4. 快照、续接与失败处理

输出布局：

```text
source-coverage/<构建指纹>/
├── current.profdata
├── latest.json
└── snapshots/
    └── 000001/
        ├── coverage.profdata.zlib
        ├── coverage.json
        ├── metadata.json
        └── checkpoint/
            ├── progress.json
            └── raw/
```

- 每批从上一份已提交 raw checkpoint 复制出工作副本，继续累计。
- 每个 case 开始前保留 raw 回滚副本。采集失败时结束本批，先彻底回收本批 forkserver/server、child 和遗留后代，再恢复该副本；禁止在 forkserver 存活时通过替换 raw 文件回滚。此前已成功的批次前缀可以索引和发布，失败 case 及尚未执行的 case 下批重试，不在本批重启常驻进程。
- 正常退出、crash 或 timeout 的 case，只要交互已启动且 profile 可用，都可纳入统计；进程退出类型单独记录。
- 批次结束时比较新旧 JSON summary 的已覆盖数量。仅执行次数增长不视为新增覆盖。
- 只要本批有成功采集的新 case，就保存完整累计 profdata 的 zlib 快照、完整 LLVM JSON，以及相邻元数据；无新增覆盖时贡献集合保持不变。
- `metadata.json` 保存本批 case ID、累计贡献 ID、覆盖增量、失败项、覆盖版 SUT 的身份信息、工具版本和构建指纹；身份信息使用路径和文件摘要，不持久化 PID。
- 通过临时目录、原子 rename 和 `latest.json` 发布完整批次，发布后才更新 LibAFL state metadata。`current.profdata` 是最新快照的可恢复副本。
- checkpoint 保留全部已成功采集 ID 和累计 raw 数据；旧快照保留压缩 profdata、JSON 和元数据，旧 raw checkpoint 可在新批次发布后删除。
- merge、export、快照发布或重放失败均告警并继续 fuzz。未成功发布的数据不推进已重放集合，下次 stage 从最后完整 checkpoint 重试。
- 输出目录加独占锁；覆盖二进制、AFL/LLVM runtime 版本或重放构建配置变化时使用新的构建指纹目录，保留旧数据。
- 覆盖结果、checkpoint 和覆盖版构建产物加入 Git 忽略规则。

## 5. 验收

- 用 LibAFL 当前版本的 `ForkserverExecutor` 驱动 forkserver-only 覆盖版，验证 runtime 握手、map-size 兼容、连续多轮控制消息、真实 SUT PID，以及正常/非零退出和信号终止的原始 wait status。
- 验证每轮为新 child、每批仅启动时 exec 一次；覆盖版没有 PCGUARD 调用点，forkserver-only 配置及兼容性 map 不影响主 fuzz 的 bitmap。
- 验证覆盖版启动失败、控制 EOF、fuzzer/父进程退出，以及活跃 SUT 被杀后的回收和状态回传；主 fuzz 和 coverage 会话同时存在时，清理不会误杀其他常驻进程。
- 验证 `A → B → A` 的在线累计：计数持续增加，已采集 case 不被 stage 重复执行。
- 验证正常退出、crash、SIGKILL timeout，以及 SUT 子进程退出后的 profile 可读取性。
- 验证连续多个批次重新启动 coverage forkserver，沿用累计 raw 后计数不丢失、不重复增加旧计数；验证父进程已建立 LLVM profile 映射时的失败回滚和快照发布顺序。
- 验证主 corpus 与 solutions 编号相同时仍能分别处理。
- 验证有覆盖增长和仅增加计数的批次；贡献集合与 `attribution: "batch"` 一致。
- 解压快照后核对其与对应 profdata 一致，JSON 可由匹配的 binary/profile 重新导出。
- 重启 Fuzzer 后续接 raw 和已重放集合，验证重新编号、未提交批次恢复及失败 case 重试。
- 注入重放、索引、导出和磁盘写入失败，确认继续 fuzz、保留上一完整快照且不重复累计。
- 用 LightFTP、OpenSSL 完成真实 generator 重放，确认主 fuzz 的 bitmap、进程、工作目录和 pcap 不受影响。
- 报告重放、索引、导出、压缩和持久化耗时，以及启用 stage 对 fuzz 吞吐量的影响。
