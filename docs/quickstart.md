# 冷启动：从 clone 到第一次「记住 + 回忆」

目标：**一条命令起依赖，几条命令起服务，然后能存能取**。
适用环境：Windows + WSL2(Ubuntu) 或原生 Linux。macOS 未验证。

## 0. 前置

| 需要 | 版本 | 说明 |
|---|---|---|
| Docker | 24+ | 跑向量库 / 图库 / 向量模型 / 重排 |
| Python | 3.10+ | 后端（`uv` 管依赖） |
| `uv` | 最新 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| LLM 网关 | OpenAI 兼容 **或** Anthropic Messages 协议 | 不配也能跑，但写入会降级成原文卡 |

内存建议 ≥ 8 GB（两个模型服务常驻）。

## 1. 起依赖（Compose）

```bash
git clone <repo> amarantine && cd amarantine
docker compose up -d          # weaviate + neo4j + embedding + rerank + admin
docker compose ps             # 五个都 healthy/running 再继续（首次 3–5 分钟）
```

**Docker Hub 拉不动时**（国内常见）指到镜像站：

```bash
DOCKER_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  docker compose up -d
```

> `ghcr.io` 在很多国内镜像站**不被代理**（403），所以 uv 是从 PyPI 装的，
> 镜像不依赖 ghcr。

端口：`8081` 向量库 · `8082` 向量模型 · `8083` 重排 · `7474/7687` 图库。

## 2. 装后端

```bash
cd back_end
uv sync --frozen
cp .env.example .env          # 再按下面改最少的三项
```

**最少要改的**：

```dotenv
ADMIN_TOKEN=换成你自己的随机串
LLM_BASE_URL=http://<你的网关>:<端口>
LLM_API_KEY=<key>
```

其余 80+ 项都有默认值，含义见 [配置项参考](config-reference.md)。

## 3. 起服务

```bash
uv run python -m memory.entrypoints.admin_http      # 默认 127.0.0.1:8765
```

自检：

```bash
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# 期望 degraded=false，deps 四项（neo4j/embedding/weaviate/rerank）都 ok:true
```

> 容器里 `ADMIN_HOST=0.0.0.0`（否则端口发布不生效），
> 并以宿主 UID 运行（`user: ${HOST_UID:-1000}:${HOST_GID:-1000}`），
> 否则挂载的 `data/` 写不进去。这两条 compose 已经配好，不用你改。

## 4. 第一次写入和召回

**写入有两条路**：MCP 客户端用 `memory_remember`（正常用法），
命令行验手用 `POST /admin/ingest`（批量入口，也是导入工具的接口）。

```bash
TOKEN=<你的 ADMIN_TOKEN>

# 4a. 写入：一条对话（原文先落 SQLite，202 立刻可查）
curl -s -X POST http://127.0.0.1:8765/admin/ingest \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "namespace": "demo",
        "dedupe_key": "demo-001",
        "source": "chat",
        "turns": [{"role": "user", "text": "我在做一个自托管的长期记忆服务"}]
      }'
# → 202 {"job_id": "job_...", "status": "queued"}

# 4b. 等它 done（切分要调 LLM，30 条一片通常 20–80 秒）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/ingest/<job_id>" | head -c 300

# 4c. 召回
curl -s -X POST http://127.0.0.1:8765/admin/recall \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"namespace": "demo", "query": "长期记忆"}' | head -c 400
```

请求/响应模型以 `back_end/src/memory/entrypoints/admin_http.py` 为准
（`IngestRequest` / `RecallRequest`）。

## 5. 接 MCP 客户端

`harness/` 下有 MCP 入口脚本；Claude Desktop / 其它客户端的 `mcpServers` 配置示例见
[用户手册](user-manual.md) 的「MCP 接入」一节。

## 6. 常见卡点

| 现象 | 原因 | 处理 |
|---|---|---|
| `degraded=true`，`down:["weaviate"]` | 容器没起来 | `docker compose logs weaviate` |
| 写入很慢、耗时 60s+ | LLM 网关连不上（超时在等） | `curl` 一下 `LLM_BASE_URL`；或临时调小 `LLM_TIMEOUT_MS` |
| `recall` 返回 503 | embedding / 向量库不可用 | 这是**故意的**（空结果会误导） |
| 召回结果排序怪但能用 | rerank 挂了 → 退化成第一阶段排序 | `/health` 看 `rerank` |
| 「今天/昨天」解析错 | 时区 | 设 `TIMEZONE=Asia/Shanghai` |

依赖不可用时的完整行为见 [降级行为](degradation.md)。
