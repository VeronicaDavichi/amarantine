# amarantine

> 给大模型用的长期记忆：**MCP 工具（LLM 侧）+ 本地管理通道（人侧）**。
> 事件卡片进向量库与图库双写，支持召回、遗忘、复核、反馈的完整闭环。

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

一个自托管的长期记忆服务。大模型通过 MCP（stdio JSON-RPC）写入与召回记忆；
人通过本地 HTTP 管理通道做复核、纠错、遗忘和归档。

```
┌──────────────────────────────────────────┐
│ MCP Host（Claude Desktop / DSH / ...）    │
│   └─ 拉起子进程 env: MEMORY_NAMESPACE=xxx │
│        └─ memory-mcp  (stdio JSON-RPC)    │
└──────────────────────────────────────────┘
              ↓ 共享 service 层
┌──────────────────────────────────────────┐
│ FastAPI — 127.0.0.1:8765 管理通道          │
└──────────────────────────────────────────┘
              ↓
   Weaviate（事实源）· Neo4j（派生图）· SQLite（原文/计数）
```

### 核心设计

| 决策 | 内容 |
|---|---|
| **事实源与派生层分离** | 事件卡写进 Weaviate 即为事实；Neo4j 图是可丢弃、可全量重建的索引 |
| **写序固定** | 先写向量（成功才算写成功），再异步更新图；图失败只告警，不回滚 |
| **namespace 隔离** | 只来自环境变量，入参携带一律拒绝；向量与图同源派生 |
| **原话与摘要分层** | 原文进 SQLite（可溯源），摘要进向量（可语义召回） |
| **遗忘不物理删** | 事实层只软删（`is_deleted` + `deleted_at`），图节点才会 `DETACH DELETE` |
| **弱关联可分层** | 关系带 `label` / `confidence` / 证据，查询分 strict / default / explore 三档 |

### 特性

- **双入口**：MCP 工具给 LLM，Admin HTTP 给人，共用 service 层
- **批量导入**：`POST /admin/ingest` 把历史对话切分成事件卡（LLM 切分 + 幂等 `dedupe_key`）
- **动态召回预算**：token 预算 + 相对衰减 + 断崖 + 强制保留溢出
- **别名与称呼**：规则层 + LLM 复核，拒绝也留痕（`alias_reject`）可复查
- **可观测**：召回带 `trace_id`，反馈能回流；健康检查覆盖数据一致性

### 快速开始

前置：WSL2 + Docker（中间件）、Python 3.12。

```bash
# 1. 中间件（WSL 内执行）
docker compose up -d          # weaviate / embedding / reranker / neo4j

# 2. 后端
cd back_end
uv sync --python 3.12
cp .env.example .env          # 填 LLM_API_KEY / ADMIN_TOKEN
uv run pytest -q

# 3. 起管理通道
uv run python -m memory.entrypoints.admin_http   # http://127.0.0.1:8765

# 4. MCP 冒烟
MEMORY_NAMESPACE=agent-a uv run memory-mcp
```

### 目录

```
back_end/         Python 3.12 · FastAPI · MCP stdio · SQLite
harness/          夹具生成、开发脚本、一致性校验、导入工具
specs/            LeanSpec 规格（43 个，含需求/设计/任务/验收）
docs/             ADR、P0 任务、用户手册、工具指南、审计记录
docker-compose.yml
```

### 文档

| 文档 | 内容 |
|---|---|
| [ADR-001 架构](docs/ADR-001-memory-architecture.md) | 决策 D1–D10、契约 4.0–4.10、验收 A1–A34 |
| [用户手册](docs/user-manual.md) | 安装、配置、操作、FAQ、限制 |
| [工具使用指南](docs/memory-tool-guide.md) | 给 LLM 的调用说明（MCP instructions 单一源头） |
| [验收审计](docs/acceptance-audit.md) | 34 条验收标准逐条核对 |
| [规格目录](specs/) | 每个 spec 有需求、设计、任务、验收 |

### 质量

```bash
cd back_end && uv run pytest -q        # 1223 passed
python3 ../harness/check_spec.py       # spec 骨架一致（43 个）
python3 ../harness/check_guide.py      # 文档与代码一致
```

### 已知限制

- 单机自托管，无多租户；隔离粒度是 `namespace`
- 依赖外部 LLM 做对话切分与标签复核，不可用时降级但质量下降
- 中文人名抽取依赖权威名册；无名册时退化到分词，会误判品牌名等
- 事件卡的 `date_start` 目前取写入日期（待修，见 specs/040）

### License

[MIT](LICENSE)

---

## English

**amarantine** is a self-hosted long-term memory service for LLMs.

- **MCP tools** for the model side (stdio JSON-RPC), **HTTP admin channel** for humans
- **Dual store**: event cards in Weaviate (source of truth) + Neo4j graph (derived, rebuildable)
- **Batch ingest** of historical conversations with idempotent dedupe keys
- **Forget without deleting**: fact layer is soft-deleted only
- **Weak relations**: edges carry `label` / `confidence` / evidence, queries have three strictness tiers

```bash
docker compose up -d
cd back_end && uv sync --python 3.12 && cp .env.example .env && uv run pytest -q
uv run python -m memory.entrypoints.admin_http   # http://127.0.0.1:8765
```

Docs are in Chinese under `docs/`. MIT licensed.
