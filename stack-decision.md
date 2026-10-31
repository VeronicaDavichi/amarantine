---
project: amarantine-memory
decidedAt: 2026-10-31
decidedBy: owner
backend:
  framework:
    choice: fastapi
    version: "0.128.0"
    alternatives: [django-rest-framework, flask]
    rationale: "async 原生匹配 D3 四通道并行；Pydantic v2 直接落地附录 B EventCard schema（A1）；自动 OpenAPI 产出契约文档"
    python: "3.12"
    pydantic: "2.11.7"
    router: "APIRouter 按模块分组（用户口径：blueprint）"
    deps: "uv"
  orm:
    choice: sqlalchemy
    version: "2.0.x"
    alternatives: [sqlmodel, tortoise-orm, raw-driver]
    rationale: "SQLite/PG 双方言一等公民 + Alembic 标配；EventCard 不在关系库，SQLModel 的模型复用优势不成立"
    scope: [raw_quote, vocab_version, review_record, ingest_log]
  db:
    choice: sqlite
    version: "3.45+"
    alternatives: [postgresql-16, minio, weaviate-only]
    rationale: "本机内部服务、离线、低频写 → SQLite 零运维；WAL 缓解多 agent 并发写；FTS5 为 D3 的 BM25 通道提供实现基础"
    configurable: [sqlite, postgresql]
    default_url: "sqlite:///./data/memory.db"
    postgres_version: "16"
    notes: "切换由 DATABASE_URL 驱动，业务代码不含方言 SQL"
  migration:
    choice: alembic
    version: "1.13+"
    alternatives: [yoyo-migrations, custom-sql, create-all]
    rationale: "SQLAlchemy 标配 + autogenerate；与 ORM 模型同源"
    sqlite_note: "render_as_batch=True（SQLite 无完整 ALTER TABLE）"
  auth:
    choice: api-key
    version: "n/a"
    alternatives: [none, jwt, mtls]
    rationale: "key → namespace 映射，业务入参不接受 namespace，从根上防越权"
    header: "X-Agent-Key"
    note: "认证即隔离载体；需回写 ADR D1 与 4.x 契约"
  ai:
    choice: openai-sdk
    version: "1.x"
    alternatives: [httpx-only, langchain, litellm]
    rationale: "chat + embedding 走 OpenAI 兼容协议；rerank 无标准协议，用 httpx 自写"
    abstraction: [LLMClient, EmbeddingClient, RerankClient]
    note: "D10 要求业务代码不直接依赖 SDK，一律经抽象层"
  test:
    choice: pytest
    version: "8.x"
    alternatives: [unittest, pytest-sync-testclient]
    rationale: "async 原生（pytest-asyncio）+ 参数化承载 29 条验收 + ASGITransport 免启端口"
    extras: [pytest-asyncio, httpx, jsonschema]
frontend:
  framework: { choice: react, version: "18.x" }
  ui:        { choice: mui, version: "6.x" }
  state:     { choice: zustand, version: "5.x" }
  test:      { choice: vitest, version: "2.x" }
backendDir: back_end
frontendDir: front_end
harnessDir: harness
---

# 技术栈决策 · amarantine 长期记忆服务

> 本文件由 stack-decision 流程**逐维度确认**生成。未确认的维度一律为 `null`，不预设值。

## 讨论背景

| 项 | 结论 | 对选型的影响 |
|---|---|---|
| 项目性质 | AI 应用 / 内部服务 | 无对外配额压力，优先开发效率与 NLP 生态 |
| 主力语言 | **Python** | 后端以 Python 为唯一主语言；P2 的 SetFit/BERTopic 可同进程，不必拆独立服务 |
| 硬约束 | 无特殊约束（可完全离线） | embedding 已走本地 docker middleware，天然满足离线 |
| **数据隔离** | **本机可能跑多个 agent，需按 agent 域隔离** | 影响 Weaviate / Neo4j 的 schema 与全部接口入参；**需回写 ADR D1** |
| 数据层现实 | Weaviate（向量）+ Neo4j（图），**无传统 RDBMS** | ORM 只管原文/元数据，事件卡走 Weaviate client |

## 架构形态（v1.1 澄清）

> 本项目**不是**给人手动调的 REST API，而是**交给大模型调用**的 MCP 工具集，同时保留人用的管理通道。

```
┌──────────────────────────────────────────┐
│ MCP Host（Claude Desktop / DSH / ...）    │
│   └─ 拉起子进程 env: MEMORY_NAMESPACE=xxx │
│        └─ memory-mcp  (stdio JSON-RPC)    │
└──────────────────────────────────────────┘
              ↓ 共享 service 层
┌──────────────────────────────────────────┐
│ FastAPI                                   │
│   ├─ entrypoints/mcp_stdio.py   ← LLM 侧 │
│   └─ entrypoints/http_admin.py  ← 人侧   │
│        绑定 127.0.0.1 + 前端静态资源       │
└──────────────────────────────────────────┘
              ↓
      Weaviate / Neo4j / SQLite
```

| 项 | 决定 |
|---|---|
| 传输 | **stdio（MCP）+ HTTP（管理）并存** |
| 写入触发 | **只有 LLM 主动调 `memory_remember`**；批量导入历史走管理侧离线脚本 |
| namespace 注入 | MCP 走**环境变量** `MEMORY_NAMESPACE`；HTTP 管理侧走 token |
| 代码结构 | `core/` 放领域逻辑，两个入口只做**薄封装**，不许各自写业务 |
| LLM 可见工具 | `memory_recall` / `memory_remember` / `memory_forget` / `memory_feedback` |
| LLM 不可见 | 复核裁决、dry_run、归档、pin、词表 |

## 工程约束

| 约束 | 说明 |
|---|---|
| **MUI 样式** | 所有自定义风格**必须查 MUI 手册**，走 `sx` / `theme` / `styled` 官方 API；**禁止自己拍脑袋覆盖** |

## 决策过程

### 1. backend.framework

**已定：FastAPI 0.128.0 / Python 3.12 / Pydantic 2.11.7 / uv**

| 候选 | 结论 |
|---|---|
| **FastAPI 0.128.0** | ✅ 采用 |
| Django REST Framework 5.x | ❌ admin/ORM 优势因无关系库而失效 |
| Flask 3.x | ❌ 并行扇出、schema 校验、OpenAPI 都要自拼 |

- **决策理由**：async 原生匹配 D3 四通道并行；Pydantic v2 直接复用附录 B；自动 OpenAPI 即契约文档
- **接口组织**：`APIRouter` 按模块分组（主人原话「blueprint」，FastAPI 等价物为 `APIRouter`）
- **依赖管理**：`uv`（`pyproject.toml` + `uv.lock`）

### 2. backend.db

**已定：默认 SQLite（WAL + FTS5），可配置切换 PostgreSQL 16**

| 候选 | 结论 |
|---|---|
| **SQLite** | ✅ 默认 |
| PostgreSQL 16 | 🔁 可配置切换（`DATABASE_URL`） |
| MinIO / 对象存储 | ❌ 元数据仍需另找库，多一层 |
| 全塞 Weaviate | ❌ 原文是 write-once 大字段，向量库不适合 |

- **决策理由**：本机内部服务 + 离线 + 低频写 → SQLite 零运维最合适
- **并发缓解**：WAL 模式，缓解多 agent 同时写
- **附加收益**：FTS5 为 D3 的「关键词/BM25」通道提供实现基础
- **可配置**：`DATABASE_URL` 默认 `sqlite:///./data/memory.db`，可切 `postgresql://...`
- **切换保障**：业务代码只走 SQLAlchemy 方言抽象，不写方言 SQL；迁移由 Alembic 覆盖

### 3. backend.orm

**已定：SQLAlchemy 2.0.x + Alembic**

| 候选 | 结论 |
|---|---|
| **SQLAlchemy 2.0.x** | ✅ 采用 |
| SQLModel | ❌ 模型复用优势因主数据不在关系库而失效 |
| Tortoise ORM | ❌ 迁移工具 aerich 小众 |
| 裸 driver | ❌ 方言差异要手维护 |

- **职责边界**：ORM 只管 `raw_quote` / `vocab_version` / `review_record` / `ingest_log`；**事件卡走 Weaviate client，不进 ORM**
- **理由**：双方言一等公民、Alembic 标配、生态成熟

### 4. backend.migration

**已定：Alembic（batch mode）**

| 候选 | 结论 |
|---|---|
| **Alembic** | ✅ 采用 |
| yoyo-migrations | ❌ 与 ORM 模型脱钩，无 autogenerate |
| 自研 SQL 脚本 | ❌ 维护成本高 |
| 不做迁移 | ❌ 无法支持切 PG |

- **必须开启** `render_as_batch=True`：SQLite 无完整 `ALTER TABLE`，否则改列直接失败
- 迁移脚本纳入版本控制，与 ORM 模型同源

### 5. backend.auth

**已定：双入口分别认证**

| 入口 | 认证方式 | namespace 来源 |
|---|---|---|
| **MCP stdio** | 环境变量 `MEMORY_NAMESPACE`（host 配置里写死） | 同左，进程级固定 |
| **HTTP admin** | Bearer token + **绑定 127.0.0.1** | token → 全库管理权限 |

| 候选（HTTP 侧） | 结论 |
|---|---|
| **Bearer token** | ✅ 采用 |
| 不认证 | ❌ 本机仍可能有其他进程，且防手滑 |
| JWT | ❌ 内部单机过度设计 |
| mTLS | ❌ 过重 |

- ⚠️ **stdio 没有 HTTP header**：原 `X-Agent-Key` 方案在 MCP 侧不成立，改为**进程级环境变量**
- **核心约束**：两个入口的业务入参**一律不接受 namespace**（MCP 从 env 取、HTTP 从 token 推），传了即 400
- 理由：若 namespace 可入参，agent A 传 B 的值即可读别人的记忆

### 6. backend.ai

**已定：openai SDK（chat + embedding）+ httpx（rerank）**

| 候选 | 结论 |
|---|---|
| **openai SDK + httpx** | ✅ 采用 |
| 纯 httpx | ❌ chat/embedding 的流式与重试要手写 |
| LangChain | ❌ 抽象重、链路黑盒 |
| LiteLLM | ❌ 多一层依赖，本项目不需要多 provider 路由 |

- **抽象层**（D10 要求）：`LLMClient` / `EmbeddingClient` / `RerankClient`，业务代码只调抽象，不碰 SDK
- **协议**：chat 与 embedding 走 OpenAI 兼容；rerank 无标准，按推理框架格式用 httpx 直调
- **模型名全部来自配置**（D10：可替换基础设施）

### 7. backend.test

**已定：pytest + pytest-asyncio + httpx + jsonschema**

| 候选 | 结论 |
|---|---|
| **pytest 8.x** | ✅ 采用 |
| unittest | ❌ async 与参数化写法冗长 |
| pytest + 同步 TestClient | ❌ 测不到真正的 async 并行路径 |

- **async 测试**：`httpx.AsyncClient(transport=ASGITransport(app=app))`，不启真实端口
- **验收承载**：29 条 A 项用 `@pytest.mark.parametrize` 参数化
- **A1 契约**：`jsonschema` 库对附录 B 做校验

### 8-11. frontend.*

**已定：React 18 + MUI 6 + Zustand 5 + Vitest 2**

| 维度 | 选择 | 理由 |
|---|---|---|
| `frontend.framework` | **React 18 + Vite** | 管理 UI 组件化需求明确 |
| `frontend.ui` | **MUI 6** | 表格/弹窗/表单齐全 |
| `frontend.state` | **Zustand 5** | 极轻、无 Provider 包裹、用法接近 Pinia 体验 |
| `frontend.test` | **Vitest 2** | Vite 原生、与后端 pytest 解耦 |

⚠️ **Pinia 是 Vue 专属，React 不可用**（依赖 Vue 响应式系统），已改为 Zustand。
⚠️ **MUI 自定义风格必须查手册**：一律走 `sx` / `theme` / `styled` 官方 API，禁止自创覆盖写法。

**P0 管理前端最小功能集**：复核队列（列表 + 裁决）、遗忘 dry_run 报告、事件卡浏览与 pin。

### 12-14. 目录布局

**已定：`back_end` / `front_end` / `harness`**

| 键 | 值 | 说明 |
|---|---|---|
| `backendDir` | `back_end` | 全部服务代码 |
| `frontendDir` | `front_end` | 占位，P0 不开发 |
| `harnessDir` | `harness` | 脚本（dev / test / migrate） |
