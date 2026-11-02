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

# 裁决一条：留着 → PROTECTED（等价人工钉住）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"keep","reviewer":"owner","namespace":"work"}' \
  http://127.0.0.1:8765/admin/review/evt_xxx/resolve

# 裁决一条：可以删 → SAFE_TO_DELETE（**不会立即删**）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"delete","reviewer":"owner","namespace":"work"}' \
  http://127.0.0.1:8765/admin/review/evt_xxx/resolve

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
| 怎么清 | `POST /admin/review/{id}/resolve` | `POST /admin/label-review/{id}/resolve` |
| 清掉会怎样 | 留（→PROTECTED）或归入待删 | 只记「人看过了」，不动卡 |

**它们永远不合并**（ADR A24）。一个是存储策略，一个是标注质量，
混在一起会让人分不清「该删」和「标错了」。

### 裁决的两个语义

`POST /admin/review/{id}/resolve` 只有两个动作，都有讲究：

- **`keep`** → 进 `PROTECTED`，同时把卡置成 `pinned=true` / `pinned_source=manual`。
  不置 pinned 的话，下次扫描又会把它扫回 `NEED_REVIEW`，等于没裁。
- **`delete`** → 进 `SAFE_TO_DELETE`，但**不会立即删除**。真正删除还要过
  `POST /admin/forget/sweep` 那一关，两道缓冲都在，不靠单点决定。

两个安全边界：

1. **`delete` 不推翻硬保护**。一张 `pinned` 的卡，就算人点了删，仍然进
   `PROTECTED` —— 一次误点不该能删掉被钉住的东西。
2. **重复提交同一动作是幂等的**，返回 `unchanged=true` 说明这次没改动状态。
   提交相反动作**允许** —— 人会改主意，改主意不该被拒绝。

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

### 忘掉的事又被提起，会自己回来

用户说「忘掉 X」之后，如果把 X 原样又说了一遍，这条记忆会**自动复活**：

| 情况 | 结果 |
|---|---|
| 第一次说 | `created=true`，正常写入 |
| 说「忘掉 X」 | 软删，检索不到，原文保留 |
| 又说一遍 X | **`created=false` + `revived=true`**，重新可见 |

为什么必须这样：如果只看到「卡已存在」就静默返回，这条记忆会永远停在已忘状态
—— 用户说什么都没用，除非换措辞把 `event_id` 改掉。那是 bug，不是保守。

`deleted_at` 保留不删，作为「曾被忘过」的审计痕迹。

### 图上的四类节点

> 节点的原料是**从自然语言里抽出来的实体**。抽取质量直接决定图的质量 ——
> 抽错一个人，图上就多一个不存在的人；抽漏一个人，`RELATED` 边就少一条。
> 抽取用 jieba 分词 + 词性标注，细节见文末「实体抽取」一节。

| 节点 | 键 | 为什么单独建 |
|---|---|---|
| `(:Event)` | `event_id` | 事件本体 |
| `(:Person)` | `name` | **人是记忆的主语**，最常用的检索入口 |
| `(:Time)` | `YYYY-MM-DD` | **时间是共享维度**，属性没法被多张卡共用 |
| `(:Action)` | `verb` | **动作是共享谓词**，「哪些事被延过」需要跨事件聚合 |

剩下两类是辅助：`(:Entity)` 装 topic/place/org 这类宾语，
`(:Context)` 装当前话题。

**为什么人要从 Entity 里拆出来**：如果人只是 `kind="person"` 的一个取值，
就只能靠属性过滤找，没法作为图的枢纽去连。拆出来后
「张三的所有事」「张三和李四之间」都是沿边走，不用扫全表。

**为什么时间要建节点**：`date_start` 只是 Event 的属性，属性没法共享、
也没法做「同一时段」聚合。建成节点后按月查询只要：

```cypher
MATCH (e:Event)-[:AT]->(t:Time {month: "2026-06"}) RETURN e
```

实测：两条事件分别落在 2026-06 和 2026-07，按月聚合直接分组出来。

### 动作节点：谁对谁做了什么

`types` 只表达粗分类（`decision` / `work`），回答不了
「张三对项目A做了什么」。动作单独建成节点：

```
(Event)-[:ACTION {created_at, primary, patient}]->(:Action {verb})
```

**按 verb 唯一，不按 (verb, patient) 配对**。`(:Action {verb:"延期"})`
全库只有一个节点，这样「哪些事被延期过」是一次走边就能答的聚合：

```cypher
MATCH (e:Event)-[r:ACTION]->(a:Action {verb:"延期"})
WHERE r.patient = "项目A"
RETURN e
```

如果按配对建节点，`延期+项目A` 和 `延期+项目B` 就是两个孤立节点，
永远聚合不起来。

**同义词必须归并**：`推迟` / `延后` / `顺延` 都归一化成 `延期`。
不归并的话图上就是几个isolated 节点，聚合查询直接废掉。

**一句话可有多个动作**：`张三决定项目A延期到六月` 有「决定」和「延期」，
全部保留，最外层那个 `primary=true`。图上表现为：

```
evt_xxx ─ACTION(primary=true,  patient=项目A)─→ (决定)
        └ACTION(primary=false, patient=项目A)─→ (延期)
```

**宾语放在边上，不能放在节点上**。这是踩过的坑：Action 节点是全库共享的，
如果把宾语建成 `(Action)-[:PATIENT]->(Entity)`，所有事件的宾语会挤进同一个
集合 —— `李四推迟了项目B` 和 `张三延期项目A` 会互相污染，
查「项目A 被延期过吗」会答出项目B 那条事件。`ACTION` 边带事件上下文，
`patient` 放这里才不会串。

实测：三条事件写入后 `Action` 节点只有 `决定` / `取消` / `延期` 三个，
`决定` 被两条事件共享仍是 1 个节点，查 `项目A 被延期` 只命中正确的那一条。

### 召回时人跟实体走不同的路

图通道不是「一个实体名列表一视同仁地查」，而是按类型分流：

| 查询里抽到 | 怎么查 | 为什么 |
|---|---|---|
| **人**（张三） | `(e)-[:INVOLVES]->(p)` 直连命中，**到此为止** | 人是稀有枢纽，名字出现本身就是强信号；再查动作只会引入噪声 |
| **实体**（项目A） | 命中后**继续挖动作和相关人**，再扩一层 | 实体常被反复提到，光看字面提到它的那些事件，召回不了「本该相关但没提它」的事 |

实体的两层扩展：

```
项目A ─MENTIONS→ 事件1 ─INVOLVES→ 张三 ─INVOLVES→ 事件2   （事件2 也召回）
                    └ACTION→ (决定/延期) ─ACTION→ 事件3    （同动作且共享人才召回）
```

**同动作扩展必须同时要求共享人**。`延期` 是个高频动词，只按动作匹配
会让库里所有「延期」事件涌进来，跟查询意图未必相关。

实测：库里三条事件
```
事件1 张三决定项目A延期到六月
事件2 张三批评了李四          ← 没提项目A
事件3 王五延期项目C
```
查「项目A」→ 召回 事件1 + **事件2**（经张三扩过来，这正是分流的价值）
查「张三」→ 召回 事件1 + 事件2，**不会**因为「延期」把事件3 拉进来

召回结果里的 `routed` 字段可以直接看到走了哪几条路，
查人时它一定不含 `entity_to_action`。

### 图上的关系只追加，不覆盖

五类边，**全部用 `CREATE` 而不是 `MERGE`**：

```
(Event)-[:INVOLVES {created_at}]->(Person)                    事件涉及哪些人
(Event)-[:AT       {created_at}]->(Time)                      事件发生在哪天
(Event)-[:MENTIONS {created_at}]->(Entity)                    事件提到什么项目/地点
(Event)-[:ACTION   {created_at, primary, patient}]->(Action)  事件做了什么
(Event)-[:RELATED  {kind, created_at}]->(Event)               两个事件共享人或实体
(Context)-[:RELATES_TO {created_at}]->(Event)                 当前话题挂到事件上
```

为什么关系不能 `MERGE`：`MERGE ... SET r.at = $now` 会抹掉上一次的时间。
**同一条记忆被反复提起，恰恰是最有价值的信号** —— 覆盖掉历史等于把信号删了。

实测：同一条内容写三次，`INVOLVES` 边有 3 条、3 个不同时间戳；
两个共享「张三」的事件重复写入，`RELATED` 边从 1 条变 2 条。

**「不同 event 之间必有关系」的落法**：两个 Event 共享至少一个 Person
**或** Entity 才连 `RELATED` 边。共享同一个人的两件事、共享同一个项目的两件事，
才是真的有关系；两两相连会让边数平方级爆炸。

**Neo4j 不做忘却处理**：删卡时图上的边留在原地，历史不抹。

### 用 context 把记忆挂到话题上

`memory_remember` 新增可选参数 `context`：

```json
{"summary": "张三决定项目A延期到六月", "context": "A股"}
```

传了就会建 `(Context {key:"A股"})-[:RELATES_TO]->(Event)`，
将来按话题回溯能串起来。不传则不建话题关联。

召回时图通道走三条路：实体边直连（准）→ `RELATED` 一跳扩展（广）→
`summary CONTAINS` 兜底（兼容加边之前的旧数据）。

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

### 实体抽取：jieba 分词 + 词性标注

图上的 Person / Entity 节点从自然语言里抽。抽取器是 `services/labeler.py`。

**主路径是 jieba 词性标注**，用它给的词性直接映射：

| 词性 | 含义 | 映射成 |
|---|---|---|
| `nr` | 人名 | `(:Person)` |
| `ns` | 地名 | `(:Entity {kind:"place"})` |
| `nt` | 机构 | `(:Entity {kind:"org"})` |
| `n` / `nz` | 普通名词 | `(:Entity {kind:"topic"})`，需配合后缀或编号 |
| `v` | 动词 | `(:Action)` |

**为什么要分词**：中文没有空格，**没有词边界就没有实体边界**。
早期用「任意字 + 后缀」的正则，`张三决定项目A延期到六月` 会从「张三」
一路吞到「项目」，抽出 `张三决定项目` —— 一个根本不存在的东西。

jieba 切成 `张三/nr 决定/v 项目/n A/x 延期/v 到/v 六月/t`，
`张三` 和 `项目A` 各自独立。正则做不到的还有：

```
张三让李四去做项目A   → 张三 + 李四 + 项目A     （正则只能抽到「张三让李四」）
告诉张三这个月要交报告  → 张三                    （人名不在句首，正则抓不到）
王五和赵六在北京见面    → 王五 + 赵六 + 北京       （裸城市名没有「市/区」后缀）
```

**三条后处理规则**（jieba 也会犯错，得兜）：

1. **单字人名丢掉**：`周会上` 会被切成 `周/nr 会上/t`，`周` 不是人。
2. **裁掉尾部函数词**：`张三让` 会被标成一个 `nr`，得裁回 `张三`。
3. **名词 + 编号合并**：`项目` + `A` 两个 token，要合并成 `项目A`。

**降级路径**：没装 jieba 时自动退回正则抽取。能力明显弱（抽不全多个人名、
抓不到裸城市名），但**必须可用** —— 抽取失败会直接阻断写卡。
降级路径有单测守着。

**首次调用有 1 秒延迟**：jieba 第一次 import 要加载词典。所以探测是
**懒加载 + 结果缓存**，放在第一次抽取时发生，而不是模块导入时 ——
否则每个冷启动的进程都要付这 1 秒。

---

## 八、变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-10-31 | 首版：安装、配置、日常操作、遗忘策略、FAQ、排障 |
| v0.2 | 2026-10-31 | 补 LLM 超时排障（WSL 不继承 VPN 路由）+ vpn_relay 用法 |
| v0.3 | 2026-11-02 | 补图结构章节（Event/Person/Time/Action 四类节点、关系只追加、人/实体分流召回）；实体抽取改用 jieba |
