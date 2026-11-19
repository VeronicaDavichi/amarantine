# amarantine

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

**给大模型用的、自托管的长期记忆服务。**

大模型没有跨会话的记忆。这个项目给它一个：通过 MCP（stdio JSON-RPC）写入与召回，
人通过本地 HTTP 通道做复核、纠错、遗忘。记忆以「事件卡」为单位，
存在向量库里可语义召回，同时派生一张图回答「谁和谁什么关系」。

```
┌────────────────────────────────────────────────────┐
│ MCP Host（Claude Desktop / DSH / 其它）              │
│   └─ 拉起子进程  env: MEMORY_NAMESPACE=xxx           │
│        └─ memory-mcp（stdio JSON-RPC）               │
└────────────────────────────────────────────────────┘
                 ↓ 共用 service 层
┌────────────────────────────────────────────────────┐
│ FastAPI — 127.0.0.1:8765（管理通道 + /health）        │
└────────────────────────────────────────────────────┘
                 ↓
   Weaviate（事实源：卡片 + 向量）
   Neo4j（派生图：可整张重建）
   SQLite（原文 / job / 各类留痕）
```

### 它解决什么问题

| 问题 | 做法 |
|---|---|
| 模型记不住上次的决定 | 事件卡 + 语义召回，`recall` 带 token 预算与衰减 |
| 「他说过什么」查不到原话 | 原文进 SQLite，卡片带 `raw_quote_ids` 可溯源 |
| 同名多账号、群名片天天改 | 账号锚定（uid）> 备注 > 昵称 > 姓名，按 scope 查 |
| 一张卡说错话就永远错 | 事实层只软删；卡片有版本与冲突记录 |
| 记了一堆垃圾 | 遗忘扫描（保留期 + 硬信号保护）+ 营销闸门 |

### 30 秒快速开始

```bash
docker compose up -d            # weaviate + neo4j + embedding + admin
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# 期望 degraded=false，四依赖 ok
```

细节（`.env` 最少改哪三项、第一次写入/召回、接 MCP 客户端、常见卡点）见
**[冷启动文档](docs/quickstart.md)**。

### 实测数字（不是承诺，是某次真实测量的快照）

| 指标 | 值 | 说明 |
|---|---|---|
| 单测 | 1613 passed | `uv run pytest -q` |
| 召回 hit@5 | 0.50（60 样本） | 用**原文片段**当 query，不是拿摘要反向搜 |
| 召回延迟 P95 | 299 ms | 同一批样本 |
| 关系 precision | 0.95（19 条核对） | 机器边由人逐条判对错 |
| 关系 recall | 0.67（24 题） | 「这句该有几条」由人答 |

召回质量目前是**最真实的短板**：hit@5 只有一半。语料越多噪声越多，
所以下一步优先提质量而不是继续灌数据。

### 核心设计

| 决策 | 内容 |
|---|---|
| **事实源与派生层分离** | 卡片进 Weaviate 即事实；Neo4j 图可丢弃、可整张重建 |
| **写序固定** | 先写向量（成功才算成功），再异步更新图；图失败只告警、不回滚 |
| **namespace 隔离** | 只来自环境/配置，请求参数携带一律拒绝 |
| **原话与摘要分层** | 原文进 SQLite（可溯源），摘要进向量（可语义召回） |
| **遗忘不物理删** | 事实层软删（`is_deleted`），只有图节点会 `DETACH DELETE` |
| **弱关联可分层** | 关系带 `label` / `confidence` / 证据，查询分 strict / default / explore |

### 文档

| 文档 | 内容 |
|---|---|
| [冷启动](docs/quickstart.md) | clone → 起服务 → 第一次 remember/recall |
| [降级行为](docs/degradation.md) | LLM / 向量库 / 图库挂掉时会发生什么 |
| [配置项参考](docs/config-reference.md) | 86 项配置（自动生成，含默认值与源码注释） |
| [用户手册](docs/user-manual.md) | 导入、召回、复核、遗忘、关系层、实体抑制 |
| [设计规格](specs/) | 49 个 spec：每个决策与踩过的坑 |

### 质量与验收

```bash
bash harness/verify_all.sh --static     # 单测 + spec 骨架 + 文档一致 + 环境覆盖 + 隐私扫描
bash harness/verify_all.sh              # 再加真库验收（需容器与 LLM 网关）
```

CI（`.github/workflows/ci.yml`）跑 `--static` 那一半。

### 外部依赖

| 依赖 | 用途 | 必需 |
|---|---|---|
| LLM 网关（OpenAI 兼容 / Anthropic Messages） | 事件切分、标签复核、关系抽取 | **必需**（不可用时降级成原文卡） |
| Weaviate | 卡片与向量（事实源） | 必需 |
| Neo4j | 派生图 | 必需（可重建） |
| embedding / rerank | bge-m3 / bge-reranker | 必需 / 可选（挂了只降排序质量） |
| [nbnhhsh](https://github.com/itorr/nbnhhsh) | 拼音缩写猜全称（`lysk → 恋与深空`） | 可选，默认关 |

### 已知限制

**取舍（不是 bug）**

- 单机、单租户；`namespace` 是隔离粒度，**不是鉴权边界**；只有一个 `ADMIN_TOKEN`
- 中文群聊优先；英文语料效果未验证
- 关系层只保留陈述通道（行为通道因数据不足停用）；关系会猜错

**待办**

- 召回 hit@5 只有 0.50，提升质量是当前主线
- 部分能力的召回率未量化（只测了 precision）
- 数据导入按需进行，仓库不含任何语料

**许可**：见 [LICENSE](LICENSE)（待选定）。

---

## English

### What this is

A **self-hosted long-term memory service for LLMs**. The model writes and recalls
through MCP (stdio JSON-RPC); a human manages it through a local HTTP channel.

Memory is stored as *event cards* in a vector store (semantic recall), with a
derived property graph answering "who relates to what". Original quotes live in
SQLite so every card can be traced back.

### Quick start

```bash
docker compose up -d
curl -s http://127.0.0.1:8765/health | python3 -m json.tool   # expect degraded=false
```

See [quickstart](docs/quickstart.md) for `.env` minimums, first write/recall, and
MCP client wiring.

### Measured numbers (a snapshot, not a promise)

| Metric | Value |
|---|---|
| Unit tests | 1613 passed |
| Recall hit@5 | 0.50 (60 samples, queries are raw quotes) |
| Recall latency P95 | 299 ms |
| Relation precision / recall | 0.95 / 0.67 |

Recall quality is the honest weak spot right now.

### Docs

[quickstart](docs/quickstart.md) · [degradation](docs/degradation.md) ·
[config reference](docs/config-reference.md) · [user manual](docs/user-manual.md) ·
[specs](specs/)

### License

See [LICENSE](LICENSE) (pending selection).
