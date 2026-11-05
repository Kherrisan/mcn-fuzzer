# Generator 合成评估框架实施计划

## 目标与基本约定

新增独立 `mcn-synthesis-eval` binary，评估 agent 能否根据 FTP step trace 生成可被生产校验接受的 `generator.py`、`params.json` 和 `params.schema.json`，并通过直接运行 Python generator 与普通 LightFTP 进程完成运行检查。

- 直接支持 Codex、Claude、Pi CLI；不启动 HC。
- 每个 `profile × case × repeat` 使用独立 agent 会话和目录；同一实例先 init，再 generation。
- 不启动 fuzzer、forkserver、py server、namespace 或抓包组件。
- 默认每个实例一次、不自动修复、不自动重试、串行执行。
- 第一版检查产物和运行成功，不判断 trace 的协议语义覆盖。

结果字段区分 `artifact_pass`、`runtime_pass` 和 `passed`。`runtime_pass` 表示 generator 正常返回，SUT 启动且未异常崩溃，清理成功；不代表 trace 的所有 step 均已实现。schema 校验沿用生产代码支持的子集，并在报告中记作 `production_subset`。

## 目录与代码边界

```text
fuzz/src/evaluation/synthesis_eval.rs # CLI 参数及退出码
fuzz/src/evaluation/
  mod.rs config.rs task.rs agent.rs process.rs replay.rs report.rs
fuzz/src/engine/synthesis/                  # 抽取可共享的 prompt 与产物校验
fuzz/python/generator_server/standalone.py
fuzz/evaluation/lightftp.toml
fuzz/evaluation/fixtures/             # 离线校验用固定产物
```

新增评估模块只负责测试编排；生产 synthesis stage 继续负责 HC、LibAFL state 和 corpus。避免引入通用插件系统：provider 用 enum 分派，执行阶段用具体结构体建模。

将初始化提示词构造从 `GeneratorSynthesisStage` 提取为 `SynthesisPromptContext`，提供 `from_campaign`、`initialization_prompt` 和 `generation_prompt` 方法；它不得依赖 HC、worker 线程或 stage 状态。生产 stage 与评估 binary 共用该上下文、MiniJinja 模板和 `GeneratorRequest` 产物校验。将产物校验返回值整理为独立的 `ValidatedGenerator { input, origin }`，不携带 HC terminal 或 channel 状态。

提示词上下文允许附带可选的执行环境说明。生产模式说明 SUT 由 fuzzer 管理；评估模式说明 SUT 由评估框架启动。两者使用同一份模板。评估使用局部 campaign 配置覆盖输出目录、fixture 和 workdir，不修改用户 campaign 文件。

## Suite 与固定测试用例

`fuzz/evaluation/lightftp.toml` 指定 campaign 路径、party binding、SUT ready/cleanup 时限，以及有序 step 名列表。suite 相对路径相对于 suite 文件；campaign 路径仍按现有 TOML 规则解析。Agent profile 读取 campaign 的 `[agents.profiles]`。

公共登录前缀为：

```text
service greeting → send USER → USER intermediate → send PASS → login accepted
```

公共退出后缀为：

```text
send QUIT → ordinary command reply
```

首批用例：

| 名称 | 登录前缀后的步骤 |
|---|---|
| `login-quit` | 无 |
| `control-commands` | send PWD → pathname reply → send NOOP → ordinary command reply |
| `passive-list` | send PASV → PASV endpoint → send LIST → transfer preliminary → LIST data via passive → transfer completion |
| `passive-retr` | send TYPE → TYPE accepted → send PASV → PASV endpoint → send RETR → transfer preliminary → retrieve stream bytes via passive → transfer completion |
| `active-stor` | send TYPE → TYPE accepted → send PORT → PORT accepted → send STOR → transfer preliminary → store stream bytes via active → transfer completion |

所有用例按“公共前缀 + 用例步骤 + 公共后缀”构造。binding 将 `server-PI/server-DTP` 指定为 SUT，`user-PI/user-DTP` 指定为 generator，其余 party 为 unavailable。fixture 使用现有 `ubuntu/ubuntu` 账户、固定下载文件和实例专属上传文件。

准备阶段以 step 名查找模型：名称缺失或不唯一就失败；保持 suite 中的顺序，允许重复引用同一模型 step；检查每步 sender、receiver、channel 均可由 binding 承担，且 trace 覆盖 SUT 和 generator 两侧。调用 `StepTrace::new` 和现有 receipt writer 生成 ID 与 receipt；不随机抽样、不闭包依赖、不重排步骤。

## CLI 与阶段输入输出

```text
mcn-synthesis-eval prepare  --suite ... --profile ... --out ...
mcn-synthesis-eval generate --task <task.json>
mcn-synthesis-eval validate --task <task.json>
mcn-synthesis-eval replay   --task <task.json>
mcn-synthesis-eval run      --suite ... --profile ... --out ...
```

`prepare/run` 支持重复指定 `--profile` 和 `--case`；`--repeat` 默认 1 且必须大于 0。profile 未指定时使用 campaign 的 `protocol_generation.profile`，若为空则报错。case 未指定时执行全部用例。评估不要求 `protocol_generation.enabled=true`。各命令使用 task snapshot；源 protocol、模板、fixture、binary 或 campaign 相关内容发生变化时，校验摘要并要求重新 prepare。

**prepare** 生成 `task.json`、`trace.json`、`prepared/receipt.json`、渲染后的 init/generation prompt、独立 fixture 和 SUT 配置快照。不启动 agent、Python 或 SUT。此时 receipt 放在 agent 不可读的 `prepared/` 下。

**generate** 为该实例启动独立逻辑会话，发送 init 并等待有效 `ready.json`；之后才创建 request 目录并放入 receipt，再以同一显式会话 ID 发送 generation。CLI 完成、会话标识匹配、完成文件存在均为必要条件。实例已有产物时禁止覆盖；重新合成必须创建新实例。

**validate** 调用生产嵌入的同一份 `skills/protocol-modules/scripts/validate_generator.py` 静态校验：result IDs 匹配且 status 为 ok、added_steps 是字符串数组、Python 可编译且同步顶层 `generate` 可接受运行时的两个位置参数、schema/params 属于生产子集、params 可转换为 `ParamValue`、远程 `$ref` 被拒绝。Agent 按 skill 先对 result.draft.json 调用脚本并增加隔离导入探测，通过后原子发布 result.json。静态通过不代表真实交互通过。框架验收后写入 `published/`、`input.json`、origin 和摘要。已有 published 只有在摘要匹配时允许幂等复用。

**replay** 只接受已验证的 published 输入。每次执行使用全新 replay 目录、fixture 和 workdir。

**run** 顺序执行 prepare → generate → validate → replay。某阶段失败后将后续阶段记为 `not_run`，继续其他实例。每个阶段结束立即落盘；第一版不提供失败自动重试或 agent 会话恢复。

## Agent CLI 会话

定义轻量 `DirectAgentSession`，对外提供有 deadline 的 `initialize(prompt)` 和 `generate(prompt)`；内部只管理 provider、显式会话 ID、cwd 和日志位置。

| Provider | 初始化与续接 |
|---|---|
| Codex | `exec --json` 获取 thread/session ID，后续 `exec resume <id>`；禁用共享 daemon |
| Claude | non-interactive print + JSON 输出，显式 session ID 续接 |
| Pi | JSON 模式，使用实例专属 session 文件续接 |

- CLI 每轮可启动独立 OS 进程，但两轮必须处于同一逻辑会话。
- 不使用 `--last`、全局最近会话或交互式 session picker。
- effort 分别按 Codex exec 配置、Claude `--effort`、Pi `--thinking` 映射；model 原样传入，不做 provider 间转换。
- 会话、输出模式和 cwd 等参数由适配器管理；profile args 不得覆盖它们。认证或权限拒绝作为失败记录，不能静默等待交互批准。
- stdout/stderr 持续排空并写入实例日志，避免管道填满造成假超时。每个 provider 单独解析会话 ID、完成和错误状态，不共享 JSONL 解析假设。
- 仅预检选中的 provider；executable 缺失、版本不兼容、认证失败均需带 provider 和阶段信息报错。未选中的 provider 不要求已安装。

## Python、SUT 与进程监督

`standalone.py` 接收执行请求文件：generator 绝对路径、JSON params、host/port、连接与 I/O timeout、结果路径。它直接使用现有 `Registry`、`Session` 和 `Ctx`，原地调用 `generate(ctx, params)`，忽略返回值，并在 `finally` 执行 `ctx.close_all()`。完成后原子写入执行结果，包含 status、异常类别/traceback、耗时和 `Ctx` 字节数。stdout/stderr 单独记录；进程退出但结果文件无效视为失败。standalone 不导入 server、worker、CBOR 或 iceoryx2。

复用现有 fixture 复制和配置占位符逻辑。SUT 通过 `Command` 作为普通进程启动，使用独立 cwd/config；不创建 AFL map、控制管道或 forkserver executor，并清除继承的 AFL forkserver 变量及 FD。LightFTP 构建使用现有单连接补丁；没有 forkserver 父端时，AFL runtime 直接执行程序。

控制端口通过 loopback 临时 bind 获取，随后释放并由 SUT bind；若竞争导致启动失败，该例记为 `sut_start_failed`，实例内不偷偷重试其他端口。就绪检查每 10ms 检查进程存活，并通过 `/proc/<pid>/fd` socket inode 与 `/proc/net/tcp` LISTEN 状态匹配目标端口；不 connect 探测，以免消耗 LightFTP 唯一连接。默认 ready deadline 为 2 秒。

case deadline 默认沿用 campaign `case_ms`，从启动 Python 执行器前计算，覆盖 import、generate 和资源关闭。generator 成功后，SUT 最多等待 1 秒自然退出；仍运行则 TERM，等待 200ms 后 KILL，记录为框架清理。generator 失败或 SUT 崩溃立即清理对端；deadline 到期记录 `case_timeout`。SUT 正常先退出时，允许 Python 在剩余 deadline 内处理 EOF 并结束。

一个 supervisor 统一持有 agent、Python、SUT 的子进程状态、wait/reap 和进程组终止；设为 subreaper 收养孤儿。TERM/INT、初始化失败、超时和异常均走同一清理逻辑。仍有派生进程或无法回收时记 `cleanup_failed`，不能通过。

## 报告、目录及验收

默认结果根目录为 Git 忽略的 `test-results/synthesis/<run-id>/`：

```text
report.json
report.md
<profile>/<case>/<repeat>/
  task.json trace.json result.json
  prepared/{receipt.json,prompts/}
  workspace/{init/,requests/<id>/{receipt.json,产物,published/,input.json}}
  agent/ replays/<replay-id>/
```

每阶段记录 status、duration、failure kind/message 和产物路径。摘要记录 CLI 版本、model/effort、protocol/template 摘要、prepare/init/generation/validation/SUT ready/replay/cleanup 耗时。env 值不得进入快照；日志使用现有脱敏能力。汇总列出全部计划实例数、各阶段通过数、失败类别、`artifact_pass / 总实例`、`runtime_pass / 已进入 replay 实例`、`passed / 总实例`。基础设施失败计入计划实例，同时单列；`not_run` 不得伪装成成功或零耗时结果。

`Ctx` 字节数仅作诊断；原生 socket 或 Python modules 可能绕过它，因此零值不直接判失败，报告注明统计可能不完整。

开发顺序：

1. 提取共享 prompt/validation，保持生产 stage 行为不变。
2. 实现离线 prepare 和固定 trace。
3. 实现 standalone 与 replay，用手写 generator 验证五例。
4. 实现三种 agent CLI 适配，以可控 stub 验证会话续接和失败处理。
5. 实现 run、进程监督及汇总报告。
6. 对每个已安装且认证的真实 CLI 执行五例，stub 结果与真实 CLI 结果分开报告。

验收覆盖 trace 顺序/ID/错误 step、shared prompt/validator、会话隔离及续接、缺少 ready/result/错误 ID/无效 schema/params、CLI 提前退出/大量输出/认证失败/deadline、Python import 挂起/异常/缺少结果、SUT 崩溃/端口绑定失败/就绪超时/正常清理，以及 TERM/INT 后进程回收。还需确认完整流程未启动 HC、py server、forkserver、namespace 或 capture。
