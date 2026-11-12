# RSC JSON Schemas

`document` / `batch` 描述源文档预处理产物，`index` 描述结构索引，`manifest` 描述通用运行信息。`common` 保留源跨度、章节路径与置信度定义。

`protocol.schema.json` 描述协议交互模型：角色、消息、信道、步骤、依赖及 unresolved。Step 以 `action` 区分 `send`、`listen`、`connect`、`close`。角色、消息、字段、信道与步骤使用名称；依赖没有名称或 ID，以步骤名称引用两端。对象不包含生成的 ID 或摘要。验证保留结构、引用、名称唯一性和通信角色检查。

发布模型的 `meta` 仅含 `protocol`、`complete`、`documents`。每个来源仅含 `doc_id`、`rfc`、`path`，其中 `path` 指向模型目录内的源文档快照。应用产物不包含 `schema_version`、`input_sha256`、`document_sha256` 或哈希标识。JSON Schema 的 `$schema` / `$id`、软件包版本、源文档与块定位信息及协议自身的版本/哈希字段保留。

模型不包含 evidence、aliases、effects、alternative group 或 ObjectReuse。associated_fields 中每项仅含 name。来源证据由工作流检查点保留；发布模型的来源由 `meta.documents[].path` 指向的快照追溯。

```bash
rsc validate protocol work/model/protocol.json
```

必须保留模型目录中的源文档快照。只支持新格式；带旧版本或哈希元数据的产物会明确报错，需从 RFC 重新生成。不会静默转换旧格式。
