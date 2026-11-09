# Generator synthesis 配置改名与额外参考文件

## 目标与确定的选择

将 campaign TOML 中的 `[protocol_generation]` 改名为 `[generator_synthesis]`，并增加额外参考文件列表。synthesizer 在初始化和后续生成任务中读取这些文件，获取账号、权限、证书、目录等运行环境事实，避免猜测通用默认参数。

- 额外文件以路径提供给 agent，由 agent 按需读取；不把完整内容注入 prompt。
- 同时修改 synthesis init 和 generation 两个 prompt；evaluation 复用相同能力。
- 此计划只要求实现以下配置和 prompt 改动，不改变 coverage proposal 的选择方式，也不新增 MCP 工具。

## 配置接口与迁移

目标配置示例，路径均相对于 campaign TOML 所在目录：

```toml
[generator_synthesis]
enabled = true
model = "protocol.json"
profile = "codex"
output = "synthesis"
init_timeout_secs = 300
task_timeout_secs = 600
extra_files = ["fftp.conf"]
```

1. 将 Rust `Campaign.protocol_generation` 字段改名为 `generator_synthesis`，对应配置类型改名为 `GeneratorSynthesisConfig`，统一修改所有运行时、coverage analysis、synthesis、evaluation、测试和配置示例中的引用。
2. 六个现有配置项的含义和默认值不变：enabled=false、model/profile 为空、output="synthesis"、init_timeout_secs=300、task_timeout_secs=600。
3. 新增 `extra_files: Vec<PathBuf>`，使用 serde 默认空列表。支持相对路径和绝对路径，沿用 campaign 现有路径解析规则，保留列表顺序。
4. 不做旧表名兼容。显式拒绝 `[protocol_generation]`，错误说明改用 `[generator_synthesis]`；同时出现两个表也报错。不能让旧表被忽略而导致 synthesis 静默关闭。
5. 更新已跟踪的 campaign/evaluation TOML、README、代码生成的测试配置文本，以及活跃脚本中的配置访问。更新现有 bindings/trace selection/hc_executable 的弃用报错前缀为 generator_synthesis。
6. protocol model 仍由 generator_synthesis.model 配置，coverage analysis 和 parties 解析继续读取该字段。即使 generator_synthesis.enabled=false，启用 coverage analysis 时依然可以提供该 model；此次不迁移模型所有权。
7. 已产生的 campaign/evaluation 结果目录、日志、旧 protocol model 和历史方案不批量重写；用户复用旧 campaign 时按报错迁移表名。
8. LightFTP 的已跟踪 campaign 添加 `extra_files = ["fftp.conf"]`。其他 campaign 保持默认空列表，除非其现有运行确实需要明确的额外文件。

## 文件路径与共享 prompt context

1. 在配置加载时解析 extra_files 的路径，在构建 `SynthesisPromptContext` / `CampaignContext` 时转换为绝对路径，并校验每项是存在且可读取的普通文件；符号链接可指向普通文件。
2. 出错时包含 `generator_synthesis.extra_files`、列表索引和路径，作为明确的初始化/上下文构造错误返回。未启动 synthesis 且没有构造其 prompt context 时，不新增额外的文件读取要求。
3. 共享 context 中新增 extra_files 列表，使 init、generation 和 evaluation 通过已有模板渲染路径获得相同文件信息；Rust 不解析 FTP 配置语法，也不复制文件内容到 prompt。
4. 提供路径意味着读取原文件。将其作为只读参考，不允许 agent 修改它们。执行中的文件视为 campaign 提供的稳定输入，此次不引入文件快照或动态重载机制。

## 两个 synthesis prompt 的要求

1. 增加明确的额外参考文件清单，逐项列出绝对路径。列表为空时说明未提供额外文件。
2. 初始化时要求读取这些文件并识别与协议生成有关的运行环境事实；每次 generation 时再次列出清单，要求核对与当前 proposal 目标相关的配置，不能仅凭通用默认值生成参数。
3. 调整当前“solely from proposal/RFC”的措辞以消除冲突：协议消息语义与顺序遵循 proposal/RFC；账号密码、访问权限、证书、目录和其他部署事实可以且应当来自这些显式提供的参考文件。
4. 初始 params.json 应使用与目标条件相符的实际配置值。例如 FTP STOR proposal 要求上传权限时，从配置中选择具备相应权限的账号及匹配密码；不能在配置已有凭据的情况下猜测 ftpuser/ftppass。
5. 文件中的占位符结合现有 campaign 上下文解释。实际默认网络端点仍使用 ctx.host/ctx.port，不能将配置模板端口或某次运行的临时端口写死到 generator 中。
6. 保留不读取 SUT 源码、不逆向二进制、不探测实现能力、不根据实现是否支持功能删减 proposal 行为的约束；额外配置文件的读取许可应明确写出，避免被误认为违反源码读取限制。
7. 如相关部署事实确实缺失，明确记录假设或失败原因；不能把未给定凭据描述为已经验证有效。
8. 生成器将所需事实落实到 params.json 或代码中，不在每次 testcase 运行时依赖 extra_files 路径或请求目录；参数与资源使用仍遵守已有 generator 运行契约。

## 验证与验收

- 配置测试：缺省 generator_synthesis 和 extra_files、启用新表、相对/绝对文件路径、旧表拒绝、两个表同时出现，以及未知配置项报错。
- 文件检查：正常文件、指向文件的符号链接，以及不存在、目录和不可读文件的明确错误。权限测试使用非 root 或确定性的读取失败注入，避免 root 下误判。
- Prompt 测试：两个模板均输出额外文件的绝对路径、配置事实使用要求、实际参数选择要求；空列表可正常渲染；保留源码访问限制和 ctx 端点约定。
- 更新现有结构体构造和 evaluation fixtures，执行相关定向测试与编译检查，确认 coverage analysis 在 synthesis 关闭但 model 已配置时仍能正常加载。
- 做一次独立目录的 LightFTP synthesis 验证，确认 agent 实际读取 fftp.conf，生成参数采用满足上传权限要求的实际账号，不再使用 ftpuser/ftppass；记录 request ID、产物和实际运行反馈。静态校验通过与真实 SUT 执行结果分别如实报告。
- 当前工作区有另一项 coverage replay 日志改动正在进行。实现此计划时保留并合并那些改动，不覆盖或回退；同样保留原有未跟踪的 SVF/OpenSSL 实验文件和运行产物。
- 本计划的验收不要求运行 1 小时 campaign，不自行 commit 或 push。
