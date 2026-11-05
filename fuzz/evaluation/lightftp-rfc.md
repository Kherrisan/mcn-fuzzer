# FTP RFC synthesis evaluation cases

这组用例使用现有 FTP `protocol.json` 中的 step，套件为
`lightftp-rfc.toml`。每例包含公共登录前缀、表中列出的有序步骤、公共退出后缀。
每例使用独立 SUT work copy 和 agent 会话；generator 负责 user-PI 与 user-DTP，
真实 LightFTP 负责 server-PI 与 server-DTP。

## RFC 依据和目标

- [RFC 959 §3.2、§3.3](https://www.rfc-editor.org/rfc/rfc959.html#section-3.2)：数据传输和数据连接管理。
- [RFC 959 §4.1.1](https://www.rfc-editor.org/rfc/rfc959.html#section-4.1.1)：登录及目录切换。
- [RFC 959 §4.1.2](https://www.rfc-editor.org/rfc/rfc959.html#section-4.1.2)：TYPE、PORT、PASV。
- [RFC 959 §4.1.3](https://www.rfc-editor.org/rfc/rfc959.html#section-4.1.3)：文件操作、REST、RNFR/RNTO、APPE。
- [RFC 3659 §4](https://www.rfc-editor.org/rfc/rfc3659.html#section-4)：SIZE；[§5](https://www.rfc-editor.org/rfc/rfc3659.html#section-5)：流模式断点续传。

| 用例 | 核心序列 | 评估 generator 的能力 |
| --- | --- | --- |
| `admin-directory-lifecycle` | MKD → CWD → PWD → CDUP → RMD | 保存并复用目录参数，正确处理目录状态与回复 |
| `admin-rename-delete` | TYPE I → RNFR → RNTO → SIZE → DELE | 保持 RNFR/RNTO 连续，维护新旧文件名关联 |
| `passive-restart-retrieve` | TYPE I → SIZE → PASV → REST → RETR | 从回复提取长度和 endpoint，处理偏移与下载 EOF |
| `active-retrieve` | TYPE I → PORT → RETR | 主动建立 listener，处理 SUT 发起的数据连接 |
| `admin-passive-append-roundtrip` | TYPE I → PASV/STOR → PASV/APPE → SIZE → PASV/RETR | 三轮数据连接、上传 EOF、跨步骤文件与数据关联 |

这些是 agent 应实现的目标。当前框架仅以生产产物校验和 runtime_pass 为通过标准，
不自动验证以下语义断言，也不把 TOML 注释作为额外 prompt 字段注入。
agent 会收到完整的 step trace、receipt、RFC 路径和 SUT 配置上下文。

## 通用约束

- 登录先接收 220，USER/PASS 后接收 331/230；结束前完成数据传输与最终回复，再 QUIT。
- `admin-*` 用例使用配置中的完整权限账户：当前为 `webadmin`，密码 `ubuntu`。
  `ubuntu/ubuntu` 只有 upload 权限，无法成功执行 RMD、RNFR/RNTO 或 APPE。
  其余两例可使用 `ubuntu/ubuntu`。这是当前 LightFTP 配置事实，不是 RFC 规定的凭据。
- TYPE 使用 I，便于将 SIZE、REST 偏移和数据长度统一解释为字节。
- fixture 的 `hello.txt` 当前内容为 `hello from lightftp\n`（20 字节），生成器应检查实际文件/回复，
  不把这个大小当作跨目标固定常量。
- 动态控制端点来自 `ctx.host/ctx.port`；PASV endpoint 来自 227；PORT endpoint 来自实际 listener。
- 下载读到数据通道 EOF；上传关闭数据通道表示 EOF，然后读取控制通道完成回复。
- 处理 TCP 分片、控制回复缓冲以及 125/150 初步回复，避免把一次 recv 当作完整传输。
- 只通过 FTP 修改当前 SUT work copy；不直接修改宿主原始 fixture，也不假装替 SUT 执行文件操作。
- schema 使用生产支持的参数类型：字符串、整数、布尔值及其递归列表；字节负载可用文本/base64 参数表示。

## 每例实施说明

### 1. admin-directory-lifecycle

使用新的相对目录名；MKD 后读取 pathname reply，CWD 进入该目录，PWD 获取当前路径，
CDUP 返回父目录，最后 RMD 删除刚创建的空目录。

建议参数：`username`、`password`、`directory`。
语义检查参考：PWD 对应创建的目录，删除前已经返回父目录；不要删除 fixture 根目录。

### 2. admin-rename-delete

对当前 SUT work copy 的 `hello.txt` 执行 RNFR，接收 350 后紧接 RNTO 新文件名；
随后 SIZE 查询新文件名，最后 DELE 删除它。新旧文件名不得相同。

建议参数：`username`、`password`、`source_name`、`destination_name`。
语义检查参考：改名后文件长度不变，新文件名能够查询，删除得到成功回复。

### 3. passive-restart-retrieve

SIZE 查询 `hello.txt`，选择有效非零字节偏移（当前 fixture 可用 6）。先完成 PASV，
再 REST offset；接收 350 后下一个命令是 RETR，不能在两者间插入 PASV、SIZE 等命令。
连接 227 给出的数据端点，收取剩余字节，读到 EOF，最后消费控制通道完成回复。

建议参数：`username`、`password`、`filename`、`restart_offset`。
语义检查参考：接收长度等于 SIZE 减 offset，接收内容等于原始文件从 offset 开始的后缀。

### 4. active-retrieve

创建 loopback listener，使用动态分配的端口构造 PORT 的六个十进制数值。
得到 200 后发送 RETR；同时处理 SUT 发起的数据连接和控制通道初步回复，
接收完整文件并关闭数据连接，然后消费完成回复。

建议参数：`username`、`password`、`filename`。
语义检查参考：generator 真正承担 user-DTP 的监听角色，收取的内容与 fixture 一致。
不要将控制端口代入 PORT，也不要使用被动连接方式替代所选 active step。

### 5. admin-passive-append-roundtrip

STOR 新文件并发送第一段数据，关闭上传数据通道、读取完成回复；
重新 PASV，再 APPE 同一文件并发送第二段数据，完成同样的 EOF 与回复处理；
SIZE 查询结果，然后第三次 PASV 并 RETR 完整文件。

建议参数：`username`、`password`、`filename`、`initial_data`、`append_data`。
语义检查参考：SIZE 为两段编码后字节长度之和，回读内容为两段字节按顺序拼接。
每轮重新协商 PASV，不复用已经关闭的数据连接；APPE 必须作用于刚 STOR 的文件。

## 使用

在项目 `fuzz/` 目录中执行：

```sh
cargo run --offline --bin mcn-synthesis-eval -- prepare \
  --suite evaluation/lightftp-rfc.toml --profile codex \
  --out /tmp/ftp-rfc-eval-prepared
```

prepare 不调用 agent，仅生成五例 trace、receipt、提示词和配置快照。
执行真实 agent 合成与运行评估时，将 `prepare` 换成 `run`，并使用新的输出目录。
可通过 `--case active-retrieve` 只选择一例；`--repeat` 指定独立会话的重复次数。
第一次使用输出目录后，不要覆盖已有实例。
