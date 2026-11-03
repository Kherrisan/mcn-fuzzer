# 协议交互模型编译设计

实现入口为 `rsc.protocol.compile_protocol`，CLI 仅转发参数和退出码。代码分为：

- `context.py`：完整批次预检、结构索引、本次输入集合内的检索与引用查询。
- `contract.py` / `schemas/protocol.schema.json`：输出契约及按阶段裁剪的 LLM 响应 Schema。
- `merge.py`：语义比较、Issue 的 description/object_names，以及只重写 step ID 的映射。
- `validate.py`：Schema 结构、step ID 唯一性和无连接信道角色；不把名称引用当外键。结构非法的对象会被剔除。
- `completion.py` / `history.py` / `proposals.py`：基础对象与关系多轮补全、问题追踪和逐项提交。
- `sessions.py`：列表持续会话、协调会话、阶段交接与三级上下文压缩。
- `compiler.py`：objects → objects link → objects complete → interactions → interactions link → interactions complete → 最终校验，顺序保存并发结果与阶段报告；两处补全各自默认最多 5 轮。

各阶段输入、输出、详细步骤和数据结构见 [当前处理流程](pipeline.md)。

文档身份来自 ingest 的 doc_id/source_id。跨 RFC 证据不使用裸 block_id。引用查询只访问调用者提供的集合，不自动下载外部 RFC，也不推断不同协议版本的覆盖关系。重复内容的 ingest 条目可以共享同一个文档；身份不一致时预检失败。

party、message、field 和 channel 不分配定义 ID。step ID 仍由分配器按协议、阶段和 local ID 生成，与线程完成顺序无关。step name 从抽取响应保留到最终模型，依赖用这个 name 而不是 step ID。名称相同本身不足以合并，程序也不因同名全局替换字符串。

依赖是发生实例间的前置关系，图不要求无环。一条依赖只记录前后 step name、短 condition 和字段名对。没有强度、替代组或字段算术类型。

模型验证不能证明自然语言蕴含关系。它保证来源在输入集合内、对象结构合法，以及 step ID 不重复。它不检查名称引用是否存在、唯一或属于预期消息。事实抽取质量通过独立人工标注子集评估。结构非法的对象不进入最终模型；语义问题可以留在 unresolved，每条 Issue 只有 description 和 object_names。

缓存键包含有效会话历史、当前指令、响应 Schema、provider/model、生成参数及 endpoint，使用 `RequestCache`。报告中的 calls 是逻辑调用数（含缓存），cache_hits 单列。失败不伪装成零对象成功。输出携带输入快照和摘要哈希，后续 validate 不依赖原始 ingest 目录的绝对路径。
