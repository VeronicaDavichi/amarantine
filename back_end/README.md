# back_end — 记忆服务

长期记忆服务。**两个入口**：

| 入口 | 调用方 | 传输 | 启动 |
|---|---|---|---|
| MCP | 大模型 | stdio JSON-RPC | `MEMORY_NAMESPACE=agent-a uv run memory-mcp` |
| Admin | 人 / 运维 | HTTP `127.0.0.1` | 待实现（阶段 D） |

架构与契约见 [`docs/ADR-001-memory-architecture.md`](../docs/ADR-001-memory-architecture.md)。

## 环境

- Python **3.12**（`uv` 管理）
- Docker（Weaviate + embedding + Neo4j，跑在 WSL2）
- 代码跑 Windows 宿主，中间件端口已映射到 `127.0.0.1`

## 起步

```bash
# 1. 中间件（WSL 内执行）
./harness/dev.sh up

# 2. 依赖
cd back_end && uv sync --python 3.12

# 3. 配置
cp .env.example .env      # 按需改

# 4. 测试
uv run pytest -q

# 5. MCP 冒烟
MEMORY_NAMESPACE=agent-a uv run memory-mcp
```

## 目录

```
src/memory/
  config.py            配置加载（env > .env > yaml > 默认）
  models/event_card.py EventCard 25 字段（与附录 B 对拍）
  core/tools.py        MCP 工具契约（给模型读的 description）
  entrypoints/
    mcp_stdio.py       MCP stdio 入口
fixtures/events.json   33 条合成夹具（harness/gen_fixtures.py 生成）
tests/                 契约测试 + 协议冒烟
```

## 关键约束

- **stdout 只写协议消息**，日志一律落 `LOG_FILE`；stderr 也不承载日志，失败看退出码
- **namespace 只来自环境变量**，任何工具入参携带 `namespace` 直接报错
- EventCard 改动必须同步 ADR 附录 B，否则 `test_event_card.py` 会红
