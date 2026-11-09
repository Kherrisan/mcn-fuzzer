# RSC JSON Schemas

`document` / `batch` 描述 parse 产物，`index` 描述结构索引，`manifest` 保留通用运行元数据。`common` 只保留源跨度、章节路径与置信度定义。

`protocol.schema.json` 是协议交互中间模型的版本化契约（8.0.0）：角色、消息、信道、步骤、依赖及 unresolved。Step 以 `action` 区分 `send`、`listen`、`connect`、`close`。party、message、field、channel、dependency 和 Issue 没有定义 ID。只有 step 保留 Python 分配的 ID，并另有持久化的 name。引用使用协议名称。Python 不校验这些名称是否存在或唯一。

模型 response 不包含定义 ID、evidence、aliases、effects、alternative group 或 ObjectReuse。associated_fields 中每项只含 name，不再包含 purposes。来源由模型目录中的 `sources/`、持续会话和请求日志追溯。

```bash
rsc validate protocol work/model/protocol.json
```

必须保留模型目录中的 `sources/` 快照。candidate、draft 和 unit 契约已删除，旧 protocol 1.x–6.x 与 units.jsonl 需要从 RFC 重新抽取。
