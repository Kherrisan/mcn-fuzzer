# OpenSSL TLS 1.2 generator synthesis evaluation

本套件固定 5 例，每例一次独立 Agent 会话，直接对真实 OpenSSL `s_server`
合成并回放。使用 `openssl-tls12-campaign.toml`，不修改正常 fuzz campaign 的协议绑定。
模型是人工裁定的测试子集，不是 RSC 自动编译结果，也不是完整 TLS 模型。
`fixtures/tls12/protocol.json` 的文档引用指向仓库已有的原始 RFC HTML，文档哈希
记录该原始文件；不将它冒充 RSC 的解析文档快照。

| 用例 | 目标 |
| --- | --- |
| server-flight | 发送有效 TLS 1.2 ClientHello，解析服务端完整首轮握手至 ServerHelloDone |
| handshake-close | 完成 ECDHE-RSA TLS 1.2 握手，双向 close_notify 关闭 |
| https-roundtrip | 完成握手，加密发送 HTTP GET，接收 `-www` 响应及关闭通知 |
| unsupported-cipher-alert | 仅提供 TLS_RSA_WITH_NULL_MD5，验证没有共享 cipher 时的 fatal handshake_failure |
| duplicate-extension-alert | 重复 supported_groups 扩展，验证当前 OpenSSL 的 fatal illegal_parameter |

握手消息顺序、记录层、Finished、关闭与告警参考
[RFC 5246](https://www.rfc-editor.org/rfc/rfc5246.html) §§6、7、10。
ECDHE-RSA 扩展与编码可按目标源码及已安装 Python ssl 实现核对。
重复扩展的具体告警以本地 OpenSSL 3.5.9 `ssl/statem/extensions.c` 为依据，
不宣称所有 TLS 实现都发送同一告警。

正常会话可使用 Python ssl/MemoryBIO 实现密钥协商和加密；错误输入需要真实发送
相应 ClientHello，不能用本地 SDK 拒绝构造输入代替服务端响应。
SUT 每例只接受一次连接，准备检查不应建立额外探测连接。
测试模型的 step conditions 会交付给 Agent；TOML 注释不会作为额外指令交付。

```sh
cargo build --offline --bin mcn-synthesis-eval
./target/debug/mcn-synthesis-eval run \
  --suite evaluation/openssl-tls12.toml --profile codex \
  --repeat 1 --out test-results/openssl-tls12-UNIQUE
```

评估沿用生产产物检查和运行检查，不自动证明完整的 trace 语义覆盖。
预期 fatal TLS alert 属于用例目标；进程异常、超时或 Python 未处理异常仍计为失败。
真实评估保留首次产物，不自动改写 Agent 的实现来提高通过率。
