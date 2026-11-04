# amarantine · 长期记忆服务

给大模型用的长期记忆：**MCP 工具（LLM 侧）+ 本地管理通道（人侧）**。

```
┌──────────────────────────────────────────┐
│ MCP Host（Claude Desktop / DSH / ...）    │
│   └─ 拉起子进程 env: MEMORY_NAMESPACE=xxx │
│        └─ memory-mcp  (stdio JSON-RPC)    │
└──────────────────────────────────────────┘
              ↓ 共享 service 层
┌──────────────────────────────────────────┐
│ FastAPI — 127.0.0.1 管理通道 + 前端        │
└──────────────────────────────────────────┘
              ↓
      Weaviate / Neo4j / SQLite
```

## 文档

| 文档 | 内容 |
|---|---|
| [ADR-001 架构](docs/ADR-001-memory-architecture.md) | 决策 D1–D10、契约 4.0–4.10、验收 A1–A34 |
| [P0 任务清单](docs/P0-task-plan.md) | 40 任务 / 7 阶段 / 里程碑 |
| [技术选型](stack-decision.md) | 后端 7 维 + 前端 4 维 + 工程约束 |
| [工具使用指南](docs/memory-tool-guide.md) | 给 LLM 的调用说明（MCP instructions 单一源头） |
| [验收审计](docs/acceptance-audit.md) | ADR 34 条验收标准逐条核对（已全绿） |
| [设计审计](docs/design-audit.md) | 配置项 × 消费点扫描、七起「假实现」记录、剩余待办 |

## 结构

```
back_end/    Python 3.12 · FastAPI · MCP stdio · SQLite/PG
front_end/   管理前端（React 18 + MUI 6 + Zustand 5，阶段 F）
harness/     夹具生成、开发脚本、一致性校验
docs/        ADR / 任务 / 指南
```

## 快速开始

```bash
# 1. 中间件（WSL 内执行）
./harness/dev.sh up

# 2. 后端
cd back_end
uv sync --python 3.12
cp .env.example .env
uv run pytest -q

# 3. MCP 冒烟
MEMORY_NAMESPACE=agent-a uv run memory-mcp
```

## 环境

- 代码跑 **Windows 宿主**（Python 3.12.10）
- 中间件跑 **WSL2 docker**（Weaviate / embedding / Neo4j）
- 端口映射：`8081` Weaviate、`8082` embedding、`7687` Neo4j、`8765` admin

## 进度

| 里程碑 | 状态 |
|---|---|
| M1 地基（配置 / 模型 / 夹具 / compose / MCP 框架） | ✅ |
| M2 可写入 | ⏳ |
| M3 可召回 | ⏳ |
| M4 闭环完整 | ⏳ |
| M5 管理可用 | ⏳ |
| M6 验收全绿 | ⏳ |

## 约定

- **日志落盘**（`LOG_FILE`），stdout / stderr 都不承载日志，失败靠退出码
- **namespace 只来自环境变量**，入参携带一律拒绝
- EventCard 改动必须同步 ADR 附录 B，否则契约测试会红
