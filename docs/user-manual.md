# 用户手册

让你和你的 LLM 有一份能记很多年的记忆。**不占你的上下文窗口**，
按需召回，可溯源，可遗忘。

---

## 一、它解决什么问题

| 你的痛 | 它怎么解 |
|---|---|
| 每次对话都要重新交代背景 | 记忆存在外部，LLM 按当前问题召回 |
| 越聊越慢，上下文爆了 | 召回按 token 预算截断，常量级 |
| 记错了改不掉 | 可追溯 raw_quote，可遗忘 |
| 什么都往里塞，越记越乱 | 遗忘策略自动分流三桶，人工复核 |

**核心判断**：能记很多年不是重点，**该记的时候记得对**才是。

---

## 二、安装

### 前置

- **WSL2 Ubuntu 22.04+**（Windows 上请用 WSL 跑，不要在 PowerShell 里跑）
- Docker（跑三个中间件）
- `uv`（Python 包管理）

### 1. 起中间件

```bash
cd /mnt/e/PycharmProjects/amarantine
docker compose up -d
docker compose ps          # 三个都要 healthy
```

预期看到：

| 服务 | 端口 | 用途 |
|---|---|---|
| weaviate | 8081 | 向量 + 关键词检索 |
| embedding | 8082 | bge-m3 编码 |
| neo4j | 7474 / 7687 | 实体关系图 |

> 如果 `8080` / `50051` 被占，说明你机器上有别的 Weaviate
> （常见于 Dify）。本项目已改用 `8081` / `50061`，见 `docker-compose.yml`。

### 2. 装后端依赖

```bash
# WSL 内
export PATH="$HOME/.local/bin:$PATH"
cd /mnt/e/PycharmProjects/amarantine/back_end
export UV_PROJECT_ENVIRONMENT=.venv-linux
uv sync
```

> **为什么用 `.venv-linux` 而不是 `.venv`**：Windows 的 venv 用
> `Scripts/`，Linux 用 `bin/`，同一个目录不能混用。两个并存互不影响。

### 3. 配置

`back_end/.env`（不进 git）：

```ini
# 记忆域，必填，合法值 ^[a-z0-9][a-z0-9_-]{0,63}$
MEMORY_NAMESPACE=work

# 中间件地址
WEAVIATE_URL=http://127.0.0.1:8081
EMBEDDING_BASE_URL=http://127.0.0.1:8082
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=please-change-me

# 管理接口 token（不配则管理接口拒绝启动）
ADMIN_TOKEN=自己定一个够长的

# 数据库（默认 SQLite，可换 PG）
DATABASE_URL=sqlite:///./memory.db
```

**生产环境必须改** `NEO4J_PASSWORD` 和 `ADMIN_TOKEN`。

---

## 三、日常操作

### 跑测试

```bash
cd /mnt/e/PycharmProjects/amarantine
./harness/dev-linux.sh pytest -q
```

全绿应该是 `246 passed`。

### 起 MCP 服务（给 LLM 用）

由你的 MCP 宿主（Claude Desktop / Cherry Studio / DSH 等）拉起，
配置里让它执行：

```bash
wsl -d Ubuntu2204 --exec bash -c 'cd /mnt/e/PycharmProjects/amarantine/back_end && .venv-linux/bin/python -m memory.entrypoints.mcp_stdio'
```

宿主需要注入 `MEMORY_NAMESPACE`。**它不能由 LLM 传**（见工具指南）。

### 起管理接口（给人用）

```bash
cd /mnt/e/PycharmProjects/amarantine/back_end
LOG_FILE=/tmp/admin.log .venv-linux/bin/python -m memory.entrypoints.admin_http
```

监听 `127.0.0.1:8765`。常用调用：

```bash
TOKEN=$(grep '^ADMIN_TOKEN=' .env | cut -d= -f2)

# 健康检查（不用 token）
curl -s http://127.0.0.1:8765/health

# 看该人工复核的记忆
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/review?bucket=NEED_REVIEW&namespace=work"

# 看「打标打得对不对」的待办（注意：和上一条是**两条不同的队列**）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/label-review?namespace=work"

# 标掉一条打标分歧
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resolution":"kept_llm","namespace":"work"}' \
  http://127.0.0.1:8765/admin/label-review/evt_xxx/resolve

# 看即将被清理的记忆（只出报告，不删）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true,"namespace":"work"}' \
  http://127.0.0.1:8765/admin/forget/sweep

# 手工忘掉一条
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_id":"evt_xxx","reason":"user_forget","namespace":"work"}' \
  http://127.0.0.1:8765/admin/forget
```

### 冒烟验证

改完代码想快速确认没炸：

```bash
./harness/dev-linux.sh python ../harness/e2e_mcp.py     # MCP 协议层
./harness/dev-linux.sh python ../harness/e2e_admin.py   # 管理接口
```

---

## 四、理解遗忘策略

这是整套系统最反直觉的部分，看懂能省很多困惑。

### 三条不可让的原则

1. **事实层永不物理删除**。原文和事件卡一直在，只影响索引层。
2. **不自动删**。P0 阶段所有删除都要人点头（`dry_run=false` 直接 400）。
3. **不可靠信号不做一票否决**。LLM 写的 `importance` 只能进复核队列。

### 三桶分流

一条记忆会落进以下之一：

| 桶 | 含义 | 你怎么处理 |
|---|---|---|
| `PROTECTED` | 不该动 | 不用管 |
| `NEED_REVIEW` | 可能该删，拿不准 | 看一眼，决定留还是删 |
| `SAFE_TO_DELETE` | 满足所有淘汰条件 | 抽检确认没问题再放行 |

### 什么会被保护

满足任一即进 `PROTECTED`：

- `pinned = true`（用户明确要求记住）
- `retention` 是 `protected` 或 `permanent`
- 有未解决的问题挂在这条记忆上
- **创建不满 90 天**（宽限期，新记忆不该被扫走）
- 最近还在被召回（活跃）

### 什么才算「可以删」

四个条件**必须同时**满足：

- 闲置 ≥ 180 天
- 有效召回 < 1 次
- 创建超过 90 天
- `retention = normal`

少一个都不删。

### 为什么 importance 高不自动保护

因为 `importance` 是写入时 LLM 自己打的分。它有三个问题：
没有全局参照、两次打分不一致、会因「说得多」被污染。

拿它做一票否决，等于把误删风险藏起来。所以它只能让一条记忆
**进人工复核**，不能让系统默默放过它，也不能让它被默默删掉。

### 两条复核队列，别再搞混了

系统里有**两个**「人工看一眼」的队列，它们问的是完全不同的问题：

| | `NEED_REVIEW` | `LABEL_REVIEW` |
|---|---|---|
| 问什么 | 这张卡**该不该留** | 这张卡**打得对不对** |
| 什么时候进 | 闲置很久但 importance 高 | 规则层和 LLM 打标分歧 |
| 怎么看 | `GET /admin/review?bucket=NEED_REVIEW` | `GET /admin/label-review` |
| 怎么清 | （待实现） | `POST /admin/label-review/{id}/resolve` |
| 清掉会怎样 | 留 / 删 | 只记「人看过了」，不动卡 |

**它们永远不合并**（ADR A24）。一个是存储策略，一个是标注质量，
混在一起会让人分不清「该删」和「标错了」。

### 打标分歧是怎么来的

每次写入都会跑两层：

1. **规则层**按关键词匹配，命中不了就给 `general`
2. **LLM 层**自己判断

两层结果用 Jaccard 相似度比较，低于 `LABEL_DUAL_RUN_JACCARD`（默认 0.8）
就进 `LABEL_REVIEW`。

实测例子（真跑出来的）：

| 事件 | 规则层 | LLM | 相似度 | 结果 |
|---|---|---|---|---|
| 张三决定项目A延期到六月 | `decision, work` | `decision, work` | 1.0 | 不入队 |
| 今天背了五十个单词 | `general` | `study` | 0.0 | 入队 |
| 老板批评进度太慢 | `general` | `work` | 0.0 | 入队 |

规则层对没有明显关键词的句子只能给 `general`，这正是 LLM 能补的地方。

> **写卡的 `types` 永远用规则层结果**，不用 LLM 的。
> 因为 `types` 参与 `event_id` 哈希 —— 让 LLM 决定 types 会导致
> 同一条事件重写时 `event_id` 变化，**幂等直接失效**。
>
> 实测：带 LLM 和不带 LLM 写同一条，`event_id` 完全一致，第二次 `created=false`。

关掉双跑：`LABEL_DUAL_RUN=false`（省一次 LLM 调用）。

---

## 五、FAQ

**Q：LLM 说记得某件事，但召回是空的？**

先看 `empty_reason`：

- `no_candidate` → 真的没写过，是 LLM 在编
- `below_threshold` → 有候选但分数低，换个更具体的问法
- 响应是 503 → 检索服务挂了，不是没有

**Q：写入后立刻召回不到？**

写入是两步：先写 Weaviate（同步），再写图（异步告警）。向量索引有
几百毫秒的可见性延迟。等一下再查。

**Q：为什么 `degraded` 老是 true？**

大概率是图通道超时（200ms 预算）。这是设计好的降级：图挂了照样能用
向量结果，只是少了实体扩展。不用管。

**Q：第一次调用特别慢（1.4 秒）？**

`/mnt/e` 是 WSL 跨 Windows 文件系统，冷启动慢。热起来是 160ms 左右。
MCP 是长驻进程，只在启动时冷一次。

**Q：记忆越存越多会不会变慢？**

召回是常数级：每个通道取固定候选数（默认 50），RRF 合并，再动态截断。
不会随总量线性变慢。遗忘扫描是全量扫，但它是周期任务，不在请求路径上。

**Q：能换数据库吗？**

`DATABASE_URL` 换成 PG 即可（SQLAlchemy + Alembic 已配 `render_as_batch`）。
但向量和图的存储不能换，那两套是独立中间件。

**Q：忘掉的记忆还能找回吗？**

技术上能（对象还在，`is_deleted=true`），但**目前没有恢复接口**。
所以 `memory_forget` 别乱用。

**Q：多个项目怎么隔离？**

用不同的 `MEMORY_NAMESPACE`。换 namespace 要重启 MCP 进程（它读环境变量）。
管理接口支持 `?namespace=` 参数跨域查看。

---

## 六、已知限制

| 限制 | 影响 | 计划 |
|---|---|---|
| 中文 BM25 召回弱 | 关键词通道常为空，只靠向量 | ✅ 已修（bigram 分词） |
| 无关查询返回高分 | 归一化把 top-1 变 1.0 | ✅ 已修（绝对余弦闸门） |
| `memory_feedback` 未实现 | 无法主动标注「这条有用」 | E2 |
| 没有恢复接口 | 忘掉只能靠数据库回滚 | P1 |
| 单 namespace 每进程 | 多域要起多进程 | 暂不解决 |
| 全量扫描 O(n) | 记忆上万条后变慢 | 上万分页 |
| 图通道 200ms 常超时 | `degraded` 频繁为 true | 可调 |

---

## 七、排障

### 起不来，只有 exit code 1

日志在 `LOG_FILE` 指向的文件里（**stderr 没有日志**，这是刻意的）。
常见原因：

- `MEMORY_NAMESPACE` 没配或格式不合法
- `ADMIN_TOKEN` 没配（管理接口）
- 中间件没起来

```bash
tail -50 /tmp/admin.log
```

### 召回 503

```bash
curl -s http://127.0.0.1:8081/v1/.well-known/ready   # weaviate
curl -s http://127.0.0.1:8082/health                  # embedding（200 空 body 是正常）
docker compose ps
```

### 管理接口 401/403

- 401 → 没带 token 或 scheme 不是 Bearer
- 403 → token 不对
- 503 → 服务端没配 `ADMIN_TOKEN`

### 升级 schema 后报 GraphQL 错误

Weaviate 的 collection schema **不会自动更新**。改了
`SCHEMA_PROPERTIES` 之后必须删库重建：

```bash
curl -X DELETE http://127.0.0.1:8081/v1/schema/MemoryEvent
# 然后重新写入数据
```

> 这条坑踩过两次：一次是 `deleted_at` 的 `indexNullState` 被静默忽略，
> 一次是给 schema 加字段后没重建。改 schema 一定要重建。

### LLM 调用一直超时

**症状**：`LLM 请求失败: timed out`，打标/切分全部回退，日志里一串
`LLM 打标失败，回退规则层`。

**先分清是哪一层**：

```powershell
# Windows 侧直连（走 VPN）
Invoke-WebRequest "http://192.168.0.58:3000/" -TimeoutSec 10 -UseBasicParsing
```

```bash
# WSL 侧直连
curl -o /dev/null -w "%{http_code}\n" http://192.168.0.58:3000/
```

**Windows 通、WSL 不通** → 是 NAT 不继承 VPN 路由，不是代码问题。
网关挂在 aTrust 这类**用户态虚拟网卡**上，WSL2 默认 NAT 网络看不到那条路由：

```
Windows: 192.168.31.129  ──(aTrustVPNIC 2.0.0.1)──> 192.168.0.58:3000   ✅
WSL2:    172.20.192.x    ──────────────────────────> 192.168.0.58:3000   ❌
```

**解法**（不需要管理员、不需要重启）：

```powershell
# Windows 侧起转发（3000 端口防火墙已放行，所以直接用它）
python harness\vpn_relay.py --listen 3000 --target 192.168.0.58:3000
```

`harness/dev-linux.sh` 会自动把 `LLM_BASE_URL` 填成
`http://<WSL默认网关>:3000`，所以 WSL 这边**什么都不用改**。

> **网关 IP 会变**：每次 `wsl --shutdown` 之后默认网关可能换一个，
> 所以别把 IP 写死进 `.env`，交给脚本动态取。
> 想临时切回直连排查：`MEMORY_KEEP_LLM_BASE_URL=1 ./harness/dev-linux.sh ...`

**长期方案**：`%USERPROFILE%\.wslconfig` 里加 `networkingMode=mirrored`
（需 WSL 2.0+ / Win11 22H2+，本机 2.7.3 + 22631 满足），WSL 会直接继承
Windows 网络接口包括 VPN。代价是得 `wsl --shutdown` 一次，容器会停、要重起。

---

## 八、变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-10-31 | 首版：安装、配置、日常操作、遗忘策略、FAQ、排障 |
| v0.2 | 2026-10-31 | 补 LLM 超时排障（WSL 不继承 VPN 路由）+ vpn_relay 用法 |
