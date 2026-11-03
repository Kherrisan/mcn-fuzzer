# OpenSSL s_server 接入与实际 fuzz 验证

## 目标与分工

由同一 Herdr 面板中的 Grok 实施并验收，通过 [Herdr skill](/home/user/.codex/skills/herdr/SKILL.md) 协调。

按已选范围，使用 Python 生成有效 ClientHello 及其变异，测试 TLS 握手解析。默认 TLS 1.2、本机 TCP、每个 case 一个连接。

## 插桩与目标配置

- 固定 OpenSSL **3.5.9**，下载官方源码并校验 SHA256。[官方下载](https://www.openssl-library.org/source/)
- 使用现有 AFL++ PCGUARD 编译 `openssl`、`libssl` 和 `libcrypto`；采用 `no-shared no-module no-asm no-tests`，生成本地独立构建。
- 自动生成 RSA 2048 自签名证书、私钥及 OpenSSL 配置；生成物放入忽略目录。
- 在现有配置中增加 `target.args`，默认保持 LightFTP 的 `["@CONFIG@"]`；增加 `config_output`，默认保持 `fftp.conf`。
- 参数和环境变量统一支持端口、配置、证书、密钥等占位符。直接复用现有 ForkserverBackend，不增加 controller。
- OpenSSL 使用 `s_server -accept 127.0.0.1:@PORT@ -cert @CERT@ -key @KEY@ -tls1_2 -www -naccept 1 -no_cache -no_ticket`。连接关闭后回收当前 child；不额外建立 readiness 探测连接。[参数说明](https://docs.openssl.org/3.5/man1/openssl-s_server/)
- OpenSSL campaign 初始分配 1 MiB coverage map，继续使用握手长度检查。

## Python generator

- 保留 `generate(ctx, params)` 和现有原生 socket API。
- 可变参数包括协议版本、cipher、SNI、扩展字节和截断长度；正常构造时重算长度字段，对数值做有界转换。
- TLS 响应先读取 5 字节 record 头，再按声明长度读取正文，设置大小与时间限制。
- 正例必须收到 ServerHello；变异产生的 TLS Alert、预期 EOF 和拒绝连接分别记录，目标崩溃及 hang 仍由 executor 判断。
- 提供有效 ClientHello、非法协议参数及截断报文种子，通过种子中的 generator 路径指定 Python 脚本。

## 验收与交付

- 提供 OpenSSL 目标目录、构建和证书生成脚本、campaign、generator、种子及完整运行命令。
- 实际运行 release fuzzer，至少完成 **100 个 case**，记录执行数、吞吐量、覆盖、generator 状态和语料数量。
- 验证有效与非法握手产生不同覆盖，保存输入后能够 replay。
- 检查 case 之间进程、端口和文件描述符的回收。
- 运行相关 Rust/Python 测试及原有 LightFTP 验收，确认兼容。
- Grok 完成后，由 Codex 复核代码并执行独立启动验证，再报告实际结果。
