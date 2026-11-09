---
name: protocol-step-graph
description: 根据 protocol.json 绘制协议交互依赖图，用 step 作节点、step dependency 作有向边，并显示消息、角色和 channel。用于查看或导出协议模型的交互图。
---

# Protocol step graph

读取用户指定的 `protocol.json`，使用本 skill 的 `scripts/render_graph.py` 输出两种形式：`steps.dot` 是可编辑的 Graphviz 图定义，`steps.png` 是直接生成的彩色框图。另存同一框图的 `steps.svg`，用于缩放和查看悬浮提示。脚本使用 Python 标准库，布局需要系统中的 `dot`（Graphviz）。

从项目根目录运行：

```bash
python3 skills/protocol-step-graph/scripts/render_graph.py /path/to/protocol.json \
  --output-dir /path/to/output
```

可选 `--rankdir TB` 将默认从左到右的图改为从上到下；`--edge-labels` 在边上显示依赖条件，默认将完整条件与字段关联放在 SVG 悬浮提示中，避免文字遮挡。

绘图语义：

- 每个 step 独立保留，节点显示 step 名称、短 ID、action、msg 和通信角色。同一 message 在不同 step/channel 中出现时不合并节点。
- send 节点的 msg 使用原始 `message_name`；listen/connect/close 节点显示 `msg: —` 和执行 party，不为网络操作编造消息。
- 每条 dependency 从 `prior_step` 指向 `dependent_step`，用 step 名称解析引用，不将消息名当作 step 引用。保留孤立节点、重复边、环和条件边；图描述依赖，不能据此认定所有边都必须同时执行。
- 按 channel 分组；消息节点按发送 party 着色，网络操作节点用单独颜色和六边形区分，跨 channel 的依赖显示为虚线。缺失引用、重复 step 名称/ID 等错误须报告并修正输入，不通过删边获得“成功”图。

运行后检查脚本输出的节点数、边数是否等于输入的 steps、step_dependencies 数量。直接展示 PNG 彩色框图，提供 DOT 和 SVG 的文件链接，并说明网络操作节点和跨 channel 边的含义。输入模型不完整时保留该状态，不用绘图推断或补写协议行为。
