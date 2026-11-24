# AGENTS.md

## 项目

（在此描述项目的目标、结构与约定——这份文件会注入给每个 agent 作为工作区上下文。）

## 运行环境

- 项目的构建、运行、测试一律在容器里做，用 `docker exec atropos-libafl-nyx <cmd>` 进入执行（容器名是 `atropos-libafl-nyx`，没有叫 `atropos-libafl` 的容器）。
- **Agent 之间的交互直接使用宿主机上的 `hc`。** 查找 agent、分派任务、发送消息和查看进度都在宿主机执行。通过 `hc` 启动项目构建、运行或测试时，项目命令仍通过 `docker exec` 执行。
- 容器内工作目录是 `$HOME/mcn-fuzzer`，即 `/home/user/mcn-fuzzer`。
- 该路径与宿主机当前目录 `/data/exp/dkzou/atropos-libafl-nyx/mcn-fuzzer` 是同一份 bind mount（`/data/exp/dkzou/atropos-libafl-nyx` → `/home/user`），两边是同一批文件。
- 因此读写文件可以直接在宿主机上做（改完即对容器可见，无需同步）；只有构建、运行、测试需要走 `docker exec`。

## 文件、日志与分析

- **读文件、读日志不必进容器**：`read`/`grep`/`glob` 直接在宿主机当前目录用即可；产物（`rsc/runs/**`、`fuzz/targets/*/runs/**`、`events.jsonl`、`extract-checkpoint.json`、`*.log`）两边看到的是同一份内容。
- **写临时脚本也在宿主机，但放仓库内 `.tmp/`**：临时脚本、临时产物一律放 `<repo>/.tmp/`（已在 `.gitignore` 里），不要写宿主 `/tmp`——宿主根分区 `/` 只有 458G 且长期接近写满，`/tmp` 写满会让 shell 工具直接报 `ENOSPC` 连 `mkdtemp` 都起不来；`.tmp/` 在 `/data` 的大分区上，且位于工作区内，不受文件沙箱限制。用宿主机 python 直接运行脚本解析这些 JSON/JSONL/日志，例如：
  ```sh
  /data/exp/dkzou/anaconda3/bin/python3 .tmp/analysis.py
  ```
  宿主机上还有 `jq`、`zstd`（`/data/exp/dkzou/anaconda3/bin/`），可直接用于 jsonl / 压缩会话文件的统计。
- 需要**项目 venv 与依赖**（`rsc/.venv` 的 pydantic_ai、`fuzz` 的 Rust 工具链），或要跑 pytest/ruff/cargo、启动 campaign 与 `rsc extract` 时，进容器执行。
- 分析结论要写明数据来源路径，方便复现（同一路径容器内为 `/home/user/...`，宿主机为 `/data/exp/dkzou/atropos-libafl-nyx/...`）。

## 约定

- 改动前先阅读相关模块
- 保持与现有代码风格一致
