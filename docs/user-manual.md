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
- Docker（跑中间件：默认 3 个容器，rerank 是可选的第 4 个）
- `uv`（Python 包管理）
- **Windows 侧 Node ≥ 18**（只有导 QQ 聊天记录时用，见「QQ 聊天记录导入」）

### 1. 起中间件

```bash
cd /mnt/e/PycharmProjects/amarantine
docker compose up -d
docker compose ps          # 三个都要 healthy
```

预期看到：

| 服务 | 端口 | 用途 | 谁起 |
|---|---|---|---|
| weaviate | 8081（gRPC 50061） | 向量 + 关键词检索 | 容器 |
| embedding | 8082 | bge-m3 编码 | 容器 |
| neo4j | 7474（HTTP）/ 7687（bolt） | 实体关系图 | 容器 |
| reranker | 8083 | 精排，**可选** | 容器（`--profile rerank`） |
| admin HTTP | 8765 | 管理接口 + 浏览器管理台 `/ui` | `admin_http` 进程（WSL） |
| LLM relay | 3001 | Windows 侧 LLM 中继 | `harness/vpn_relay.py`（Windows） |

> **只有前三个是 `docker compose up -d` 起的。** 8083 要显式加 profile；
> 8765 是后端进程；3001 是 Windows 侧的 relay（`harness/dev-linux.sh` 会自动探活并按需拉起）。
> 3000 是**网关端口**、3001 才是 relay，填错的表现是「LLM 超时降级」而不是连接错误。

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

# LLM（切分 / 打标 / 别名推断用）
# 走 relay 时端口是 3001，不是网关的 3000；留空则由 dev-linux.sh 自动填
LLM_BASE_URL=
LLM_PROVIDER=anthropic
LLM_MODEL=deepseek-v4-flash
LLM_API_KEY=你的 key
# 代码默认 60000 —— 批量导入时必须调大：一片是「30 条只读上文 + 30 条本片」，
# 实测单片 20–80 秒，60 秒会让一部分片超时降级成原文卡
LLM_TIMEOUT_MS=180000
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

# 查当前词表 + 版本号（A23）；历史卡的 types 按哪版打的标签看这个
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/label/vocab

# 回溯某一版词表原文（版本号从上一个响应里抄）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/label/vocab?version=v1-a1b2c3d4"

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

#### 事件管理（ADR 4.4 / 4.6 / 4.7）

这五个接口是 ADR 承诺的管理面能力，2026-11-03 补齐：

```bash
# 把一条钉住（pinned=true 会自动带上 pinned_source=manual）
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pinned":true,"namespace":"work"}' \
  http://127.0.0.1:8765/admin/event/evt_xxx

# 改保留策略
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"retention":"long","namespace":"work"}' \
  http://127.0.0.1:8765/admin/event/evt_xxx

# 软删一条（Delete 的正式入口；/admin/forget 是同义的旧名）
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"retention_expired","namespace":"work"}' \
  http://127.0.0.1:8765/admin/event/evt_xxx

# 看有多少卡将进入冷区（只出数字，不搬）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"window_days":180,"namespace":"work"}' \
  http://127.0.0.1:8765/admin/archive

# 召回 + 上报引用（两个接口是配对的：前者给 trace_id，后者用它）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"上个月的 offer","namespace":"work"}' \
  http://127.0.0.1:8765/admin/recall

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"used_material_ids":["evt_xxx"],"namespace":"work"}' \
  http://127.0.0.1:8765/admin/recall/tr_xxxx/feedback
```

三条必须知道的行为：

| 行为 | 为什么 |
|---|---|
| `PATCH` 只认 `pinned` / `retention`，传 `summary` 会 **400 且一个字节都不写** | 这两个字段参与 `event_id` 的内容哈希，直接改会让卡与图/向量对不上 |
| `PATCH` 重复置同一值 → 200 但 `updated_at` **不变** | 否则审计里「这条被改过几次」永久失真 |
| `DELETE` 传 `hard=true` → **400**（不是「暂不支持」） | D7 + A9：事实层永不物理删，入口就关死 |
| `DELETE` 重复删 → 200 且 `deleted_at` **保持原值** | 重试不该刷新「什么时候删的」 |
| `archive` 的 `dry_run` 传 `false` 也 **`moved:0`** | P0 只出候选；误删率闸门没过之前不自动搬迁 |

> `DELETE` 的响应里有 `raw_quote_preserved: true`。它的字面含义是真的（事实层没删任何原文），
> 按 `quote_id` 查回原文的能力见下一节 —— 从 005 起**真的可用了**。

#### 导入历史对话（批量补数据）

LLM 的实时写入走 `memory_remember`；**历史对话批量补库**走这个接口：

```bash
# 提交一批对话 → 202 + job_id（后台切分 + 写卡）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "source":"chat",
        "session_id":"s_001",
        "dedupe_key":"s_001:2026-10-30",
        "namespace":"work",
        "turns":[
          {"text":"我上周决定接受那家公司的 offer 了","role":"user","ts":"2026-10-30T10:21:14+08:00"},
          {"text":"通勤要一小时，但薪资涨了不少","role":"assistant","ts":"2026-10-30T10:22:01+08:00"}
        ]
      }' \
  http://127.0.0.1:8765/admin/ingest

# 查进度（202 之后必须靠它，否则 job_id 就是个黑洞）
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/admin/ingest/job_xxxx
```

| 行为 | 说明 |
|---|---|
| `202 {job_id, status:"queued"}` | 收下了，后台切分。**原文在 202 返回时已经能查**，不等后台 |
| 同 `dedupe_key` 再提交 | 返回**同一个** `job_id`，不重跑（重跑会让同一批对话产出不同的卡） |
| 整批 `do_not_store` | **200** `{job_id:null, status:"rejected", reason:"do_not_store"}` —— 它压根不会执行 |
| 部分 `do_not_store` | 正常 202，`turns_skipped` 记下被跳过的条数 |
| 单条写卡失败 | 只跳过这一条，不整批失败。`events_created` 与 `turns_skipped` 的差值就是沉默损失 |

`status` 取值：`queued` → `running` → `done`；`rejected`（全批被拦）/ `failed`（连切分都做不了）。

**批次大不会静默丢数据（spec 036）**：切分撞到单批上限时，服务会**按行二分重切**，
而不是把尾部截掉。所以查进度的响应多了两个字段：

| 字段 | 含义 |
|---|---|
| `split_rounds` | 用掉了几轮二分。`1` = 没二分；`>1` 说明这批对话大到需要分片 |
| `dropped_events` | 到轮次上限（3 轮）仍超限、**真的丢掉**的条数。`0` = 一条没丢 |

`dropped_events > 0` 会同时打一条 WARNING（带 job 号 / 保留条数 / 丢弃条数 / 轮次）。
出现它只有两种原因：单行超长文本（没有行边界可切），或者一批里真塞了几百条对话 ——
前者应该改导入方式，把超长文本拆成多轮再提交，而不是调大上限。

#### 溯源：一条记忆出自哪句话

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/raw/msg_xxxx?namespace=work"
```

返回 `{quote_id, text, role, ts, source, session_id, event_ids, scanned, truncated, lookup_ok}`：

- `text` 是**原文**，与导入时一字不差
- `event_ids` 是这条原文生成了哪些事件卡（反查出来的）
- **软删事件卡后仍返回 200**（ADR A32）—— `raw_quote` 没有删除路径，这是设计如此，不是漏了
- `scanned` 是反查时实际扫了多少张卡 —— `event_ids` 只在这个范围里成立
- `truncated: true` 表示扫描触到上限，`event_ids` **可能不全**（别当成「全都没有」）
- `lookup_ok: false` 表示反查失败，`event_ids` **不可信** —— 与「确定没有关联」（`lookup_ok:true` + `event_ids:[]`）不是一回事

> 三个字段一起看才有意义：`scanned` 说「基于多少条」，
> `truncated` 说「是不是没扫完」，`lookup_ok` 说「这次反查到底成没成」。
> 只看 `event_ids: []` 会把它误读成「这张记忆没有出处」——
> 一次 Weaviate 抖动就能让人据此删掉一条好记忆。

召回材料里也带 `raw_quote_ids`，所以「模型引用了这条记忆」和「这条记忆出自哪句话」串得起来。

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

### 一个人可能有多种称呼

`张三`、`小张`、`老张`、`张总` 如果各自变成一个节点，
「张三的所有事」就永远召回不全。所以别名单独建节点：

```
(:Person {name:"张三"})-[:ALIAS_OF {confidence, label, source, source_event_id}]->(:Alias {name:"小张"})
```

**方向**：边从 Person 指向 Alias，读作「张三的别名是小张」。
召回时反查 `(:Alias)<-[:ALIAS_OF]-(:Person)` 拿到主人，再去走 `INVOLVES`。

#### 置信度：这句话里有几分显式的身份锚定

| label | 例子 | 含义 |
|---|---|---|
| `exact` | `糖包就是豆包` | 用户直接给了等价关系，不用推理 |
| `uncertain` | `糖包就是那个AI里最弱智的` | 得先知道「最弱智的AI」是谁才能推出来，这一步会错 |

规则层只认**显式等价词**（`就是` / `也叫` / `又称` / `亦称` / `也就是` / `又叫`）。
`A 是 B` 不算 —— `张三是个好人` 不是别名声明。

#### 描述性的不建图边

`那个AI里最弱智的` 是描述不是名字。把它塞进 `(:Alias)` 节点，
以后查「糖包」会拉出一个叫「那个AI里最弱智的」的人。

所以这类声明**不建图边**，但 **claim 留在事件卡上**（`alias_claims` 字段），
供后续 LLM 层去解析出真正的等价对象。

#### 一词多义：一个别名可以对应多个人

`三哥` 既能指印度人，也能指家里排行第三的男性。所以别名**不是一对一**：
一个 `(:Alias)` 节点可以被多条 `ALIAS_OF` 边指向。

解析出多个主人时，`GraphHits.ambiguous_aliases` 会把那些称呼列出来。
**不替用户拍板**，如实报出来。

低置信度的会出现在 `GraphHits.uncertain_aliases`，召回结果该对用户标注 ——
不能让人以为那是板上钉钉的事实。

#### 可撤销

边带 `source_event_id`，能精确删掉「哪句话归的」。撤销时**只删边、
事件卡不动** —— 别名是派生关系，删了不丢事实。实测撤销后立刻不再解析。

#### 描述性声明交给 LLM 推断（P2）

`糖包就是那个AI里最弱智的` 规则层做不了，但 LLM 可以 ——
前提是**给它一份候选名单**，让它做选择题而不是自由填空。

```
POST /admin/aliases/resolve
{"event_id": "evt_xxx"}          # 单卡
{"limit": 20}                     # 扫命名空间
```

候选来自图上已有的 `(:Person)` / `(:Entity)` 名字。LLM 只能从里面挑，
挑不出来返回 `null`。返回的名字还要再过一遍白名单 —— **防它编一个新的**。

推论：
- **LLM 永远不给 `exact`**。`exact` 是「用户自己说的」，唯一的来源。
  推断最高只能到 `probable`，低于 `ALIAS_PROBABLE_MIN=0.8` 的标 `uncertain`。
- 推断出来的 claim 标 `source="llm_inferred"`，跟用户声明的分得开。
- 卡上两条**并存**：原来那条 `uncertain`（用户说的）不删，新的追加在后面。

**为什么手动触发不自动跑**：`remember` 是热路径，一次 LLM 要 1-2 秒。
同步塞进去会让「记一句话」变成「等两秒」。别名只是派生关系，
推断失败、延迟、重跑都不该影响记忆本身。

### 别名声明会把名字带进图

`糖包` 会被 jieba 标成普通名词 `n`，任何合并规则都收不到它 ——
光靠位置猜（句首、2-4 字）会把 `助手` `接口` `响应速度` 全收进来。

**别名声明是唯一不需要猜的场合**：用户说 `A 就是 B`，等于亲口断言
A 和 B 都是名字。所以只有这一类句子会补收两侧的名字。

```
糖包就是豆包            → 糖包(topic) + 豆包(topic)     都收
糖包就是那个AI里最弱智的  → 糖包(topic)                  描述性右值不收
豆包是我们组用的AI助手   → （空）                       故意漏，见下
```

标 `topic` 而不是 `person`：声明说的是「两个称呼等价」，分不出人还是物。
凭空把「豆包」认成一个人比漏掉更糟。

#### 两边都要像名字

判据 `_is_clean_name` 对**左右两侧**同样生效。

原来只查右侧，于是 `三哥主打的就是有声有色有味` 被匹配成
`三哥主打的 → 有声有色有味` —— 右边过了判据（6 个字、没描述标记），
`(:Alias {name:"三哥主打的"})` 就这样进了图。

**左侧才是最容易脏的一侧**：它紧挨等价词，正则 `{2,12}` 会一路吞到谓语为止。

拒收条件：

| 条件 | 例子 |
|---|---|
| 超过 6 个字 | `三哥主打的` |
| 含描述标记（`那个`/`这`/`最`/`东西`） | `那个东西` |
| 「X 人」里 X 是形容词（见下） | `老实人` / `聪明人` |
| 以助词结尾（`的`/`者`/`地`/`了`） | `前面说的` |
| 代词开头（`我`/`你`/`他`/`这`/`那`） | `我刚才说的` |

**不能用 `_has_stop_char` 全程扫**：那个函数会误伤名字内部的常用字
（「明」来自「证明」，「小明」会被拒）。只查首尾。

#### 单字只做位置检查，包含检查只用于多字词

这是全套判据的**统一原则**，也是 `印度人` 和 `科学家` 两个 bug 的共同源头。

汉语单字在构词里会换角色 —— 同一个字单用时和当零件时不是一类东西：

```
家   单用是量词  →  科学家 / 作家 / 国家   里是类词缀
人   单用是名词  →  印度人 / 主持人        里是类词缀
个   单用是量词  →  个别 / 个体            里是名语素
种   单用是量词  →  种类 / 种族            里是名语素
位   单用是量词  →  单位 / 位置            里是名语素
```

依据两条：中央研究院《詞類標記表》把词类分成封闭类（`D 副詞` /
`Nep 指代定詞` / `C 連接詞` / `P 介詞` / `T 語助詞`）与开放类
（`N 名詞` / `A 形容詞` / `Nf 量詞` / `V 動詞`）—— 封闭类不参与构词；
吕叔湘《說自由和粘著》用"能不能单用"区分自由语素与粘着语素 ——
`家`/`人`/`个`/`种`/`位` 全是**自由语素**，既能单用成词又能构词，
位置不固定。自由语素一旦进包含式匹配，必然在名词里误伤。

所以：

> **单字只能做位置检查（开头/结尾），包含式匹配只能用于多字词。**

代码里的四个落点：

| 落点 | 位置 | 例子 |
|---|---|---|
| `_LEADING_CHARS` | 开头 | `我们家`（代词）、`最近`（副词）、`个老实人`（量词） |
| `_DESCRIPTIVE_MARKERS` | 包含，**全是多字** | `那个` `这种` `一个` `各位` `家里` `之一` |
| `_DESCRIPTIVE_ENDINGS` | 结尾 | `三哥主打的`、`前面说的` |
| `DESCRIPTIVE_BEFORE_REN` | `人` 前的词 | `老实人` 降级，`印度人` 不降 |

`_DESCRIPTIVE_MARKERS` 里**一个单字都没有**，测试有断言锁死
（`test_no_single_char_markers`）。这条断言就是防止以后有人再往里塞
`家`/`个`/`位`。

单字量词换成具体双字词，不写通用规则：

| 原来 | 换成 |
|---|---|
| `家` | `家里` `家人` `家门口` `家庭` `家中` `家族` |
| `个` | `一个` `某个` `整个` `每个` |
| `种` | `这种` `那种` `某种` |
| `位` | `这位` `那位` `各位` `诸位` |

**为什么 `家里` 要写具体词而不是给 `家` 写位置规则**：`家` 打头的还有
`家乐福` 这种品牌名，一概拦掉会误伤真名字。具体词比规则可靠。

#### 「人」是名词性类词缀：降置信度，不拒收

`人` 曾经直接在 `_DESCRIPTIVE_MARKERS` 里，于是**所有**带「人」的串都被
当描述拦掉，包括 `印度人`。后果很具体：`三哥就是印度人` 抽得出 claim、
卡上也留着，但建边时的 `_is_clean_name(canonical)` 把它丢了 ——
**边一条建不出来，且没有任何日志**。

查汉典后确认：`人` 被列为**名词**，举证里明确有 `主持人` `介绍人`
`证人` `强人` `恩人` —— 说明 `X人` 是**能产构词**，造出来的是合法名词。

所以分成两档，**都不拒收**：

| 右值 | 建边 | 置信度 |
|---|---|---|
| `印度人` `中国人` `上海人` `主持人` `负责人` `介绍人` | ✅ | `exact` (1.0) |
| `老实人` `聪明人` `坏人` `懒人` `陌生人` | ✅ | `uncertain` (0.4) |
| `什么人` `那种人` `个老实人` | ❌ | 由多字标记 / 量词开头拦 |

**降级和拒收的区别很重要**：

- 硬拒 = 边建不出来，而且**没有任何日志** —— 正是 `印度人` 那个 bug 的形态
- 降级 = 边照建，只是置信度 0.4，召回不当强证据，`rebind`/撤销可清

判得宽容，可修；判得严，看不见。宁可要个低置信度实体，也不要再出现
「用户说清楚了、图上却没有、还查不出为什么」。

实测（真库）：

```
三哥就是印度人      → (印度人)-[:ALIAS_OF {label:exact}]->(:Alias 三哥)
老王就是科学家      → (科学家)-[:ALIAS_OF {label:exact}]->(:Alias 老王)
老张就是上海人      → (上海人)-[:ALIAS_OF {label:exact}]->(:Alias 老张)
小明就是老实人      → (老实人)-[:ALIAS_OF {label:uncertain, conf:0.4}]->(:Alias 小明)
小李就是家里        → 不建边
小明就是个老实人    → 不建边（捕获成「个老实人」，量词开头被拦）
```

#### 少了 `key` 会静默失败

别名的宿主节点是 `MERGE (p {namespace, key: item.key})`。

`append_alias_edges` 构造参数时漏了 `key`，于是 `{key: null}`，
Neo4j 抛：

```
Cannot merge the following node because of null property value for 'key'
```

而异常被 `except` 吞掉只留一行 warning —— **别名边一条都写不进去却看不出来**，
直到查某个别名查不到主人才会发现。

`write_event_node` 那条路径有 `key`，所以只有推断路径坏。
两条路径现在都有测试锁着。

#### 判据会说「为什么」了

名字判据原来只有布尔答案：`_is_clean_name("印度人")` 返回 `False`，
然后就没有然后了 —— 调用方拿不到任何理由，用户更拿不到。

现在收敛成**唯一实现** `_judge_name()`，返回一个 `NameVerdict`：

| 字段 | 含义 |
|---|---|
| `ok` | 放不放行 |
| `reason` | `empty` / `too_long` / `marker` / `leading` / `ending` |
| `detail` | 具体证据：命中的标记、开头的字、结尾的字、长度 |

检查顺序固定：`空 → 长度>6 → 描述标记 → 开头 → 结尾`。

`_is_clean_name()` / `_looks_like_name()` 都退化成它的布尔壳，
**判据只写一遍** —— 改一处不会漏掉另一处。

**`detail` 必须是确定性的。** `_DESCRIPTIVE_MARKERS` 是 `frozenset`，
直接 `next(iter(...))` 拿到哪个标记取决于字符串哈希种子（每个进程不同），
而 `detail` 要落库 —— 让它随机，等于「同一句话两次记下不同理由」，
排查时会对不上。所以 `_first_marker()` 的排序键写死为
**最早出现 → 最长 → 字典序最小**。

#### 被拒的声明会留痕

这是这一版最重要的改动：**判错了不要紧，判错了还看不见才要命。**

别名声明从原话到图上一条边，有三处会被拒，**三处都留痕**：

| 位置 | 典型输入 | 被拒后原来怎样 | 现在 |
|---|---|---|---|
| 左值 | `三哥主打的就是有声有色有味` | `continue`，**连声明都不生成** | `side=alias` 落库 |
| 右值 | `糖包就是这种人` | 声明在卡上，建边时静默跳过 | `side=canonical` 落库 |
| LLM 决议后 | resolver 改写过仍不合判据 | 完全无声 | `side=canonical` 落库 |

**左值最隐形**：它连 claim 都不生成，LLM 复核层根本看不到它。
`种师道也叫老种` 就是这样丢的。

留痕落在 SQLite 表 `alias_reject`，主键
`(namespace, event_id, alias, canonical, side)`，字段
`reason` / `detail` / `source_text` / `resolved_at` / `resolution`。

两个管理接口：

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/admin/alias-rejects` | 列待处理的拒绝，带 `by_reason` 分组计数 |
| POST | `/admin/alias-rejects/resolve` | 标记已裁决（`accepted` = 判据错了） |

**`by_reason` 是最有用的一栏**：某一类 `reason` 数量突然涨起来，通常意味着
判据误伤了一整类名字 —— `科学家` 就是这么该被发现的。

留痕是**旁路诊断**，不是主链路：`record_safe()` 外面包着 `except`，
它挂了绝不能让记忆写不进去（跟双跑复核一个态度）。
正常声明不写这张表 —— 否则它会被正常流量淹掉，反而看不见异常。

#### 罕见姓：量词字里也有姓氏

`种` 既是量词（`一种东西`）又是姓氏（`种师道`，读 **chóng**，宋代有种师道）。
这类字没法靠字本身区分：`小明就是个老实人` 会被捕获成 `个老实人`，
必须拦；而 `老种就是种师道` 是真名字。

两道闸，**先窄后宽**：

1. **量词字本身得是姓氏。** 只有 `种` 这一类有豁免资格。
   这道闸不能省 —— 实测 jieba 的 HMM 会把 `位小明` 也标成 `nr`
   （切分器对未登录词很激进），没有它，豁免就退化成
   「量词后面跟个像名字的就行」，`位小明` 会全部放进来。
2. 有资格之后，任一成立即放行：
   - jieba 把**整串**切成单一 `nr` 词条（`种师道` → `[('种师道','nr')]`）
   - 用户声明过这个姓，且名字 **≥3 字**（姓 + 名≥2）

用户声明从原话里解析：`姓X` 且 X **后面不接汉字**，再排掉 `名` / `氏`。
所以 `这人姓种` / `姓种，叫种师道` 认，`姓名` / `姓氏` / `老百姓` / `百家姓`
都不认。声明落 SQLite 表 `surname_decl`，启动时灌回进程。

**解析先于别名抽取。** `label_by_rules` 的顺序是
「解析姓氏 → 登记 → 抽别名」，所以 `这人姓种，种小明就是老种` 这一句里
的别名抽取**已经知道**「种」是姓，同一句内立刻生效。

```cypher
-- 声明之后可用的额外入口
GET  /admin/alias-surnames        -- 看已声明 + 进程内生效集合
POST /admin/alias-surnames        -- 声明（立即生效 + 落库）
```

实测（真 Weaviate + 真 Neo4j，`namespace=rejectfix*`）：

```
老种就是种师道              → (种师道)-[:ALIAS_OF {label:exact}]->(:Alias 老种)
三哥主打的就是有声有色有味   → 不建边；alias_reject: side=alias     reason=ending  detail='的'
糖包就是这种人              → 不建边；alias_reject: side=canonical reason=marker  detail='这种'
小李就是位小明              → 不建边；alias_reject: side=canonical reason=leading detail='位'
这人姓种                    → surname_decl: ['种']
```

**注意 `种` 只在「量词打头」这一条判据上被豁免**，别的判据一点没放宽。

#### 裁决之后会真的补边

「看得见」只是第一步。人看过原文、判定「判据错了」之后，如果还得手工去
Neo4j 补边，闭环就还差一口气。

所以 `POST /admin/alias-rejects/resolve` 传 `resolution=accepted` 时会
**顺手把边补出来**：

```
POST /admin/alias-rejects/resolve
{"event_id": "...", "alias": "老种", "canonical": "种师道",
 "side": "canonical", "resolution": "accepted"}
→ {"resolved": true, "resolution": "accepted", "repaired": true}
```

关键在 `repaired` 这个字段。`accepted` 的语义是「判据错了」，所以补建
**必须绕过判据**（内部走 `force=True`）—— 否则同一道判据会把边再拦一次：
人已经否决了判据，图上却仍然没边，而且这次连痕迹都不会再留。那正是这一版
要消灭的形态。

补建失败**不静默**：

| 情况 | `repaired` | `resolution` | 还在待办里吗 |
|---|---|---|---|
| 图写成功 | `true` | `accepted` | 否 |
| 图写失败 | `false` | `accept_pending` | **是**，可重试 |
| `dismissed` / `deferred` | `null` | 原样 | 否 |

**先补图、后落库**，所以「失败」本身是个可查状态。`accept_pending` 的行继续
出现在 `GET /admin/alias-rejects` 里 —— 列表带 `resolution` 字段，能分辨
「真没处理过」和「裁决了但没修成」。重试是幂等的，边用 `MERGE`。

如果失败也只打一行 warning、`resolution` 照写 `accepted`，那这行就从待办里
消失了：**人以为修好了，其实没有**。这一版不接受这种静默。

#### 为什么有些判据仍然偏保守

`X人` 之所以敢放行，是因为它只影响**置信度**。而下面这些判据影响的是
**能不能进图**，一旦错了就是永久垃圾 —— 所以它们继续保持保守。

**实体纠错机制目前约等于不存在**：

| 机制 | 能纠什么 | 能删错误实体吗 |
|---|---|---|
| `memory_forget` | 软删整张卡 | ❌ 节点残留 |
| `memory_feedback` | 只能写 `confirmed_by_use` | ❌ |
| `LABEL_REVIEW` | 只标记 `types` 分歧，不改卡 | ❌ |
| 别名撤销 | `DELETE ALIAS_OF` 边 | ❌ 只限别名 |

没有任何 API 能删掉一个错误的 `(:Entity {name:"助手"})` 节点。唯一手段是
连整张卡一起遗忘，**代价比错误本身还大**。

所以在加上实体纠错之前，规则层只补别名声明这一处，
`豆包是我们组用的AI助手` 这种照旧漏。

**但漏的方式变了**：现在它会进 `alias_reject` 表，带 `reason` 和 `detail`。
从「用户说清楚了、图上却没有、还查不出为什么」变成
「用户说清楚了、图上没有、但表里有记录、能裁决」。这是这一版的全部意义 ——
判据不可能没有盲区，所以盲区必须可见。

**升级路径**：先做实体纠错（删节点 / 改名 / 合并），再放开精度。

#### 一个已知的模型问题

同名名字可能同时存在 `:Person` 和 `:Entity` 两个节点 ——
`ALIAS_OF` 边为了有起点会 `MERGE (p:Person)`，而实体抽取又把同名名字
建成 `:Entity`。这是「别名的主人被硬绑成 Person」导致的，但「豆包」
显然不是人。候选列表里已用 `DISTINCT` 去重，图模型本身待改。

### 实体纠错：改 / 拆 / 合 / 解挂

**没有删除。** 删掉一个抽错的节点，事件卡里那句原文还写着它，下次重放还会
抽出来，而且删完就无从追溯「这里曾经抽错过」。这跟「事实层永不物理删除」
是同一条原则：**纠错是修正，不是抹除痕迹**。

### 为什么「存代号、读原文」

图里的节点有两个字段：

```
key    代号，创建后永不变   → 身份
name   原文，可以随便改      → 显示
```

`MERGE` 按 `key` 走，`MATCH ... WHERE n.name IN $names` 按 `name` 查。
用户只知道名字，所以查询用名字；系统内部认身份，所以建节点用代号。

**这带来一个直接后果：改名永不冲突。** 两个不同的实体都叫「豆包」是
合法状态，各有各的 key，查询时都返回、都显示原文。

### 四种操作

| 操作 | 什么时候用 | 端点 |
|---|---|---|
| 改 rename | 名字抽错了 | `POST /admin/entities/rename` |
| 拆 split | 一个节点其实是两个东西 | `POST /admin/entities/split` |
| 合 merge | 两个节点其实是同一个 | `POST /admin/entities/merge` |
| 解挂 rebind | **这一句的指向错了** | `POST /admin/entities/rebind` |

#### 痕迹可查

四种操作都会在图上留一条 `(:EntityEdit)`：

```
(:EntityEdit {namespace, op, target, args, reason, operator, created_at})
op ∈ {rename, merge, split, rebind}
```

原来只有写接口、没有读接口 —— 文档说「纠错是修正，不是抹除痕迹」，但痕迹
查不到，这话就只是口号。现在：

```
GET /admin/entities/edits?op=rename&target=助手&limit=50
→ {items: [{op, target, args: {...}, reason, operator, created_at}], total}
```

`args` 在图上是 **JSON 字符串**（Neo4j 属性不收嵌套 Map），出参已经解析成
对象 —— 查历史的人不该自己 `json.loads`。解析失败**不吞**：原样返回字符串
并标 `args_parse_error: true`，让你知道看到的是残缺数据。

`op` / `target` 是可选过滤，`namespace` 永远是必要条件（跨域查不到）。

#### 改

```
{"from_name": "助手", "to_name": "豆包"}
```

只动 `name`，`key` 和所有边都不受影响。目标名字已被占用也照改不误 ——
那就是「重名」，不是错误。

#### 合

```
{"from_name": "小张", "to_name": "张三"}
```

边搬过去（`INVOLVES` / `MENTIONS` 分别处理，Cypher 不支持动态关系类型），
被并的那个**降级成 `(:Alias)`**，节点留着。于是「小张」自动变成张三的
一个称呼，召回时都能找到。

#### 拆

```
{"from_name": "张三李四", "into": ["张三", "李四"]}
```

原节点标 `split_into` 留痕，**不删**。两个子实体各自建节点，原节点的边
**两边都连**并标 `from_split=true` —— 原文写的是「张三李四」，两个人都算
被提及是诚实的近似，但标出来让人知道这条关联不精确。

#### 解挂（最容易跟「改」混的一个）

```
{"event_id": "evt_xxx", "from_name": "三哥", "to_name": "印度人", "to_kind": "topic"}
```

`三哥主打的就是一个有声有色有味` —— 这个「三哥」其实是印度人，不是哥哥。

**名字没错**，别的句子里它确实指哥哥；**只是这一句的指向错了**。
所以只动这一条事件的边：

```
解挂前：事件A → 三哥      事件B → 三哥
解挂后：事件A → 三哥      事件B → 印度人    ← 事件A 一个字都没动
```

别名层（`三哥` 指向谁）完全不动 —— 一词多义本来就允许多条 `ALIAS_OF`。

**如果顺手用「改」会怎样**：把「三哥」这个名字整个改掉，那么别的句子里
真正指哥哥的部分就全错了。

`to_kind` 必须由调用方给。`三哥` 到底是人还是国家，**系统不猜** ——
猜错了比不改更糟。`person` 走 `INVOLVES`，其余走 `MENTIONS`。

旧节点不删。如果确实没人引用了，只记 `orphan_since` 标记。

### 留痕

每次操作在图里留一个 `(:EntityEdit)` 节点：

```cypher
(:EntityEdit {op, target, args, reason, operator, created_at})
```

`args` 是 JSON 字符串 —— Neo4j 属性只收基本类型，嵌套 Map 会直接
`CypherTypeError`。

留在图上而不是 SQLite：纠错本来就是图的操作，重建图时留痕不该先丢。

### 一个 key 只有一个节点

`MERGE` 一旦带上标签（`MERGE (p:Person {key})`），同一个 key 就能在图上
存在两次：别名声明建 `:Entity`，实体抽取建 `:Person`，两边各匹配各的，
永远合不到一起。

后果是「豆包」在 `list_known_names` 里出现两次、召回时各拿到一半的边。

**规矩：`MERGE` 不带标签，新建时才打标签。**

```cypher
MERGE (p {namespace: $namespace, key: item.key})
ON CREATE SET p.name = item.name, p.first_seen = $created_at
SET p:Person        -- 无条件：后来的判定更可靠
```

- `ON CREATE SET name` —— 重放不会把用户改过的名字顶回去
- `SET p:Person` 不带 `ON CREATE` —— 别名先把「豆包」建成 `:Entity`，
  实体抽取判定它是人之后要能补上 `:Person`，而不是另建一个

**`ALIAS_OF` 的宿主也不硬绑 `:Person`** —— 「豆包」是个产品，不是人。
别名只说明「这两个名字指同一个东西」，**不说明那个东西是什么**。

### 别名主人的召回：第 7 条路

别名解析出来的主人（`糖包` → `豆包`）可能**只有 `:Entity` 标签**：
别名声明不说明那个东西是什么，实体抽取也可能还没把它判定成人。

原来的做法是把它塞进 `person_names`，指望路 1（`(e)-[:INVOLVES]->(p:Person)`）
能命中 —— 纯 Entity 宿主直接落空。**别名查出来了，事件一条拿不到。**

现在单独一条路：

```cypher
MATCH (e:Event {namespace: $namespace})-[:INVOLVES|MENTIONS]->(host)
WHERE host.name IN $alias_hosts
```

- **终点不限定标签**，人还是物都认
- 用独立参数 `$alias_hosts`，**不混进 `person_names` / `entity_names`**
- **只直连，不扩展**

#### 为什么不塞进 `entity_names`

那样能命中路 2，但会连带走路 3（挖同僚人）和路 4（挖同动作）。别名大多是
`uncertain`，拿弱证据去扩散，噪声指数放大；而且别名声明**本来就不构成
「提到」**——它只说明「这两个名字指同一个东西」，不说明这句话跟那个东西有关。

所以别名只贡献「这条事件确实指向了主人」这一层，不参与动作扩展。

## 身份来源分层：uid > 备注 > 昵称（spec 043）

「这是不是一个真人、叫什么」**不从词形猜**，按权威程度取值：

| 优先级 | 来源 | 例 | 说明 |
|---|---|---|---|
| 1 | `uid` | `53660559` | 账号锚，跨群、改名都不变 |
| 2 | 备注 | `王晓虹 - 加班问题人事` | 拆成「姓名 + 说明」，说明不参与实体 |
| 3 | 通讯录真名 | — | 平台字段，可能为空 |
| 4 | 昵称 | `蓝网-韩晓迪` | 去公司前缀后入名册 |
| 5 | jieba 词形 | `周大福` | **仅在 1–4 全空时才用**；有名单时降级为 `topic` |

### 图上的形状

```
(:Account {uid, nick, nicks[], display_name}) --OPERATED_BY--> (:Person {name})
```

- `Account` 按 `(namespace, uid)` 合并 —— **同名不同 uid 是两个人**（通讯录里真有 3 个「智小安业务助手」）
- `nicks` 是数组：改名时只追加，旧昵称不丢（旧卡片上的人名还能对上）
- 写账号层失败**只告警**（图是派生层），不影响记忆写入

### 非汉族姓名：只检出，不解析

含 `·`、含字母数字、或「≥5 字且首字不在百家姓」→ **不进名册、不建 `Person`**，
只记日志（导出脚本里标 `non_han: true`）。

已知盲区：`买买提江` 这种「4 字、全汉字、首字非百家姓」判不出来 ——
汉族姓名最多 4 字（复姓 + 双名），门槛只能设 5，否则复姓名会被误伤。

### 已知偏差

改名后**会多出一个 `Person` 节点**（新旧名字各一个），但共用一个 `Account`。
`Person` 的键由名字派生是全局约定，改成 uid 派生要单开 spec。


## 一个人的身份键：uid，不是名字（spec 045）

`Person` 的键是 `uid:<uid>`，不是名字派生。好处：

| 场景 | 现在 |
|---|---|
| 同一个人改名 | 仍是**一个** `Person`；旧名进 `previous_names` |
| 两个同名的人 | 是**两个** `Person`（键不同） |
| 用旧名查 | 命中 —— 查询同时匹配 `name` 与 `previous_names` |

没有 `uid` 的写入（单条 `memory_remember`）仍按名字派生键，行为与以前一致。

**迁移**：`harness/migrate_person_keys.py` 把旧的名字键节点并到 uid 键上
（默认 dry-run，`--apply` 才改，`--backup` 可导出回滚）。

## 分词：称谓与自我学习

实体抽取的第一道关是分词。中文没有空格，切错了后面全错。

### 排行称谓

`三哥是我哥` 曾经**一个人都抽不出来** —— jieba 词典里没有「三哥」，
被切成 `三/m` + `哥/n`。

修法是把排行称谓灌进 jieba：

```python
RANK_DIGITS = "一二三四五六七八九十"
RANK_SUFFIXES = "哥姐弟妹叔伯婶舅姨"
```

10 × 10 = 100 个词，`三哥` `四妹` `三叔` 都能切对了。

**刻意不做「老X」「小X」**：`小鹏`/`小米`/`小说`/`老总` 全是坑，
姓氏表再全也躲不掉。排行模式没这个问题——「三哥」不可能是别的东西。

**词性仍然会错。** 「三哥」可能指印度人，也可能指家里排行第三的男性，
这是真歧义，规则层无解。抽出来交给 `rebind` 纠。

### 连词粘连

`老李和小王吵架` 会被切成 `老李/nr` + `和小王/nr` —— 「和」粘进了名字，
`和小王` 就这么进图了。

切分后剥掉开头的连词（`和`/`与`/`跟`/`及`/`同`），**但只在剥完仍是
合格名字时才动手**：否则 `和平` 会被剥成 `平`，`同事` 会被剥成 `事`。

### 用记忆反哺分词，但只在启动时

`豆包` `糖包` 这类名字 jieba 词典里永远没有。它们在图上出现过，
说明确实是名字——那就把它们灌进词典。

**必须只在启动时灌，不能每次写入后灌。**

因为 `event_id` 的哈希里有 `entity_names`：

```
event_id = hash(namespace | 日期 | types | entity_names | summary)
```

每写一条就改词典，下一句话的分词结果就变了 → `entity_names` 变了 →
**`event_id` 变了 → 一条记忆裂成两条**。实测：

```
同一句话写两次
  evt_aa1e78d36308780f   ← 第一次
  evt_520f8855d56ae47d   ← 第二次（词典已被反哺）
```

N4 的幂等性是地基，不能给分词让路。启动时灌一次没有这个问题：
**同一个进程内词典固定**，切分必然一致。

代价是新名字要等下次重启才切得对。可以接受。

### 已确认的专名可以单独成词

`nz`（其他专名）默认只在下述情况收：后面跟编号（`项目`+`A`）或
后缀（`腾讯`+`公司`）。孤零零一个 `nz` 不收——那条规矩挡住过
`助手`/`接口`/`响应速度` 这类噪声。

唯一的例外是**已经灌进词典的词**（也就是图上已有的实体）。
`豆包` 就靠这一条，第二次起能单独收下。

## 图上的关系只追加，不覆盖

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

## 图写失败不会静默：意图日志 + 启动补跑 + 每日对账

### 问题：进程的死法大多抓不到

写一条记忆要动三个地方：SQLite（计数器/词典）、Weaviate（**事实源**）、
Neo4j（**派生图**）。前两个写完了，第三个可能失败或半途被杀。

「那关掉的时候清理一下不就行了」——不行：

| 能抓到（但不可靠） | 抓不到 |
|---|---|
| stdin 关闭 | `kill -9` |
| `SIGTERM` | `taskkill /F` |
| `SIGINT` | **WSL 整个 VM 重启** |
| `atexit` | OOM killer / 断电 |

右栏这几种死法，`atexit` 和信号处理器**一次都不会执行**。所以正确性
不能押在「关闭时把没写完的刷下去」，只能押在**「死在任何一点，重启后
从盘上自证状态，接着走」**。

### 三层保险

| 层 | 管什么 | 代价 | 谁跑 |
|---|---|---|---|
| **意图日志** | 图写失败 | 每次写 +1 条 SQL 的 INSERT/DELETE | `memory_remember` 内联 |
| **启动补跑** | 进程死在中途 | 启动扫一次表 | 两个入口都做 |
| **每日对账** | 跨库毫秒窗口 | O(n)，所以每天一次 | `admin_http` 常驻循环 |

### 意图日志：先记账，再动手，成功销账

```
① 生成 event_id → ② 幂等检查 → ③ embed → ④ 写 Weaviate（事实源落盘）
                                              ↓
⑤ INSERT pending_task（记一笔「这张卡要写图」）  ← 动手之前
                                              ↓
⑥ 写 Neo4j（同步，返回前完成）
                                              ↓
⑦ 成功 → DELETE 那条账    失败 → attempts+1，排下次重试
```

**顺序不能反。**「动手之后、记账之前」崩溃 = 什么都没留下；
先记账的话，「记账之后、销账之前」崩溃 = 重启时补跑。

**happy path 一秒没变慢**：图写入仍然同步、仍然在 `memory_remember`
返回前完成，只是前后各多一条 SQLite 写入。实时性没有牺牲。

**幂等重报也记账**：同一条内容再提一次会走幂等短路（`created=false`），
那条分支原来不记账 —— 「第一次图挂了，再提一次」永远修不好，
只会一遍遍返回成功而图始终是空的。现在两条路径共用同一份逻辑。

### 失败了，调用方看得见

图挂了但事实源成功时，`memory_remember` 的返回会带上：

```json
{
  "event_id": "evt_3e42bb18f5ddef9f",
  "created": true,
  "graph_pending": true,
  "degraded": "graph_write_failed"
}
```

`graph_pending` 的意思是**「有欠账，且已经记在盘上，会补」**。
写成功时不会出现这两个字段。

> 这三个字段是后补的。在此之前 `RememberResult.graph_ok` 这个字段
> **全代码库没人读** —— 记忆进了 Weaviate、图关系缺失、调用方看到"成功"，
> 这就是静默降级。字段存在不等于有人在看。

### 重试与放弃

- 指数退避：30s → 60s → 120s → 240s …上限 1 小时
- 超过 **5 次**标 `status='dead'`，**停止重试但绝不删记录**
- 删了就真没痕迹了；留一行才知道「有过这件事、没办成」
- 人工排障后补成功，`dead` 的行仍然可以被销账

查欠账（就是普通 SQLite 文件，`DATABASE_URL` 指向的那个）：

```bash
sqlite3 back_end/data/memory.db \
  "SELECT id, kind, dedupe_key, attempts, last_error FROM pending_task
   WHERE status='pending' ORDER BY id"
```

### 对账：只能补一部分，所以要说清楚

对账比较「Weaviate 里有的事件」和「Neo4j 里的 `:Event` 节点」，
发现缺口就入队补图。它的**保真度低于意图日志**：

| 补什么 | 意图日志（payload 是完整卡片） | 对账（只能读 Weaviate） |
|---|---|---|
| `Event` 节点 | ✅ | ✅ |
| `AT` → Time | ✅ | ✅ |
| `MENTIONS` → Entity | ✅ | ✅（但 `kind` 只能是 `topic`） |
| `INVOLVES` → Person | ✅ | ❌ 补不回来 |
| `ACTION` → 动作 | ✅ | ❌ 补不回来 |
| `ALIAS_OF` 别名边 | ✅ | ❌ 补不回来 |

原因很直接：`SCHEMA_PROPERTIES` 里**没有** persons / actions / alias_claims
这些字段，Weaviate 里根本没有这些信息。所以对账补出来的是降级版，
用 `kind='graph_rebuild'` 标着，能被查出来：

```bash
sqlite3 back_end/data/memory.db \
  "SELECT dedupe_key, status FROM pending_task WHERE kind='graph_rebuild'"
```

**对账兜的是意图日志兜不住的那一段**：进程正好死在「Weaviate 写成功、
意图日志还没落」之间。Weaviate 和 Neo4j 之间没有跨库事务，那个毫秒级
窗口只能这样兜。

### 为什么不给 MCP 进程加 SIGTERM 处理器

加了也没用 —— 右栏那几种死法不执行它，而左栏那几种由 stdin 关闭触发，
本来也没什么要冲的（账已经落盘了，缓冲区里只有一行日志）。

**加了反而有害**：会让人以为「关闭时会保存」，于是别的地方开始偷懒。
正确性已经由「启动补跑」保证了，不需要任何退出钩子。

### 为什么维护循环可以放在后台，而图写入不行

判断标准不是「哪个更时髦」，是**「丢了会不会留下不可恢复的错误」**：

- `admin_http` 里的补跑循环：状态全在 `pending_task` 表里，循环丢了
  只是少跑一个 tick，下次启动接着做 —— **它只是触发器，不是状态持有者**
- 要是把「图写入」本身丢给 MCP 进程的后台线程：那条事件的关系就永久
  缺失，因为 MCP 进程由 host 控制，随时可能被 SIGKILL

一句话：**触发器可以丢，状态不能只存在于内存里。**

---

## 浏览器管理台（`/ui`）

不想敲 curl 的时候用这个。它是 `admin_http` 自带的一个静态页 —— 零构建、
零依赖，跟着后端一起起，不需要另开前端服务。

```
浏览器打开 http://127.0.0.1:8765/ui/
```

### 三步进入

1. 右上角 Token 填入 `ADMIN_TOKEN`（存在浏览器 localStorage，只需填一次）
2. 顶部「切换」输入记忆域（如 `work`），点「应用」
3. 页面顶部会一直显示**当前域** —— 它是这个系统里唯一的安全锚点

### 四个页面

| 页面 | 做什么 | 能不能改 |
|---|---|---|
| 复核队列 | 列 `bucket=NEED_REVIEW` 的卡，逐条接受/拒绝 | ✅ 裁决可写 |
| 误删率报告 | 跑 `forget/sweep` 的 `dry_run`，看分桶与抽检样本 | ❌ 只读 |
| 事件列表 | 分页列出当前域的卡，可点原文、可软删 | ✅ 软删可写 |
| 溯源（抽屉） | 点 `raw_quote_ids` 看原文与关联事件 | ❌ 只读 |

### 软删为什么要手输域名

「不做登录」意味着没有第二道防线。所以软删的确认框会要求你**把当前域名
再打一遍** —— 打错或取消就什么都不删。

这不是仪式感：它让「在 `work` 域手滑删掉 `personal` 的卡」在物理上做不到。
换域之后列表会**清空重拉**，上一域的行不会残留。

### 不给你的按钮

- **没有「物理删除」**：`DELETE /admin/event` 的 `hard=true` 在服务端就被
  400 关死了。页面上不给按钮，比给了再拦更省事。
- **没有「开自动遗忘」**：`dry_run` 在前端是**写死**的 `true`，不是默认值。
  按 ADR A12，误删候选率 < 5% 才允许开自动 —— 这个决定不该由一个按钮做出。

### 风险（明说）

- Token 明文存在 `localStorage`，`/ui` 的 HTML 本身不鉴权（它不含任何秘密，
  真正的边界在 `/admin/*` 上）
- 所以**只适合本机单人用**。要放到别人能访问的网络里，这个设计必须重做
  —— 先做登录，再谈别的。

---

## QQ 聊天记录导入（spec 040 / 046 / 047）

把一个 QQ 群的历史消息灌成记忆，要走四步：**导成员表 → 导消息 → 导库 → 校验**。
前两步在 Windows、后两步在 WSL，别在同一个 shell 里混着跑。

### 两条链路，两个系统

| 阶段 | 在哪跑 | 为什么 |
|---|---|---|
| 导出成员表 / 导出消息 | **Windows**（PowerShell） | 要直读腾讯 QQ 的 SQLCipher 库 `nt_msg.db`，Node 包和库文件都在 Windows 侧 |
| 导入 / 校验 | **WSL2 Ubuntu** | 服务、`.env`、SQLite 都在 WSL 里 |

产物目录用 `back_end/data/corpus/`：`data/` 和 `harness/corpus/` 都在
`.gitignore` 里 —— 真实聊天记录带人名、手机号、地址，**不准入库**。

一次性前置：

- 要导哪个账号的库，就**先登录那个账号**。库口令按账号推导，只从登录中的
  进程里拿得到，没登录的历史账号解不开。
- Windows 侧要能跑 `node`，并装好 `@powercess/qq-mcp`。不在默认路径时，
  改 `harness/export_qq.mjs` 顶部的 `PKG` 常量（`export_qq_members.mjs` 同一处）。
- 成员表**导一次就够**；但群名片被改过之后要重导（见「已知限制」）。

### 1. 导出群成员表

```powershell
cd E:\PycharmProjects\amarantine
node harness/export_qq_members.mjs --all --out-dir back_end/data/corpus/qq-identities
```

每个群一份 `group-<gid>.json`。预期输出：

```
  ✓ 485490399: 390 人 → back_end/data/corpus/qq-identities/group-485490399.json
  ✓ 1019369531: 500 人 → back_end/data/corpus/qq-identities/group-1019369531.json
  ...
导出 106/106 个群
```

文件长这样（真实片段）：

```json
{
 "groupQq": "485490399",
 "members": [
  {"uid": "u_oZvYzRujHn5KleWUi8o--Q", "qq": 3883585327, "nickname": "潮间带", "card": "往汐（北瑀家的鱼）"},
  {"uid": "u__2CM7A4NCleKM9ZrIm7zvg", "qq": 259538562, "nickname": "虚怀寄怀", "card": ""}
 ]
}
```

| 字段 | 是什么 | 会不会空 |
|---|---|---|
| `uid` | 账号的内部主键，跨群、改名都不变 | 不会 |
| `qq` | QQ 号，人看得懂的那串 | 偶尔空 |
| `nickname` | 账号昵称（好友看到的名字） | 偶尔空 |
| `card` | **群名片**（这个群里显示的名字） | **经常空** |

> **为什么必须单独导成员表**：消息导出里**没有群名片**，只有 `uid`。
> 只导消息的话，实测 1999 条里有 1040 条拿不到 `senderName` —— 别人的名片
> 在导入时解析不出来，发言人只能是一串数字或 `unknown`。

### 2. 导出消息

```powershell
node harness/export_qq.mjs --db "D:/TencentData/Chats/Tencent Files/505456846/nt_qq/nt_db/nt_msg.db" `
  --per-group-limit 1000 --groups 485490399 --out-dir back_end/data/corpus/qq-import
```

路径里的 `505456846` 是**登录账号的 QQ 号**（数据目录按账号分）。要导别的账号，
先登录它，再把路径换掉。

预期输出：

```
群 1 个，每群最多 1000 条
  ✓ 485490399 大肥鱼乐园: 1000 条
{ "groups": 1, "kept": 1000, "detail": [...] }
```

产出 `qq-<gid>.json`，每条消息带 `text` / `ts` / `type` / `sender` / `sender_uid` / `sender_name`：

| 字段 | 说明 |
|---|---|
| `sender` | 导出时的回落值：`senderName` → QQ 号 → `unknown` |
| `sender_uid` | **唯一可靠的锚**，导入时用它反查群名片 |
| `sender_name` | 源库里没有就是 `null`，别指望它 |

> **别用整库一次导出**（`--all-groups`）：它是「一次查 5000 条再按群分桶」，
> 2.6 GB 的库里前几个大群会把 5000 条吃光，其余群一条不剩。逐群
> `--per-group-limit` 才导得全。

### 3. 导入

```bash
cd /mnt/e/PycharmProjects/amarantine
./harness/dev-linux.sh python ../harness/import_chat.py \
  --corpus-dir data/corpus/qq-import \
  --namespace qq-505456846 --window 30 \
  --gate-marketing --gate-namespaces njzq-dingtalk \
  --prefix qq
```

两点说明：

- `dev-linux.sh` 会把 cwd 切到 `back_end/`，所以语料路径相对它写
  `data/corpus/qq-import`。
- **别用裸 `python3`**：依赖在 `.venv-linux` 里，裸解释器会直接
  `ModuleNotFoundError: No module named 'pydantic'`。

| 参数 | 作用 | 建议值 |
|---|---|---|
| `--corpus-dir` | 目录下所有 `*.json` | 导出目录；只导一个群改用 `--corpus <文件>` |
| `--namespace` | 落到哪个记忆域 | QQ 单独一域（`qq-<群号>`），别混进工作域 |
| `--window 30` | 每片多少条 | 30（与 046 的回溯窗口对齐） |
| `--gate-marketing` | 营销闸门 | 群里有广告就开 |
| `--gate-namespaces` | 拿哪些域的记忆做词面比对 | 你真正在用的域（`work` / `njzq-dingtalk`） |
| `--prefix` | `dedupe_key` 前缀 | `qq`（默认 `dt`，键会写成 `dt:qq-...`，以后分不清来源） |
| `--identity-dir` | 成员表目录 | 默认 `back_end/data/corpus/qq-identities`，导到别处才用传 |
| `--resume` | 跳过已 `done` 的片 | 中断后重跑必备 |
| `--retry-failed` | `failed` 的片换后缀重提 | 服务端对 `failed` 也做幂等，不改键不会重跑 |
| `--wait 300` | 每片最多等几秒 | 单片实测 20–80 秒，别调小 |
| `--report` | 报告路径 | 默认 `back_end/data/import_report.json` |
| `--selftest` | 灌完自动跑召回自测 | `back_end/data/corpus/selftest_040.json` |

预期输出（真实：`qq-485490399.json`，907 条有效消息。前几行来自干跑，
`--dry-run` 只切不提交，可以先拿它核对人名解析）：

```
加载记忆词库: ['njzq-dingtalk']
  词库 9038 字符
  群成员表: 390 人 → 可用显示名 377（排除 12）
  营销闸门: 识别 2 条 → 保留 2 / 丢弃 0 (缓存命中 1)

=== qq-485490399.json | cid=qq-485490399 | turns=907 | 片=31 | 过滤=93
  [1] 提交 30 条 | qq:qq-485490399:2026-10-23:2026-10-23:c01
    status=running
    status=done
  ...
  名字标记(scope group:485490399): 50 条

汇总：片 31（跳过 0）| turn 907 | 卡 ~480 | 丢 0
报告 → /mnt/e/PycharmProjects/amarantine/back_end/data/import_report.json
```

卡数由模型切分决定（实测密度约 15 张/片），四行里各看一件事：

| 行 | 看什么 | 不对时说明 |
|---|---|---|
| `群成员表: 390 人 → 可用显示名 377（排除 12）` | 可用名数量 | 0 → 成员表没找到：`--identity-dir` 不对，或文件名不是 `group-<gid>.json`，或 `cid` 里的群号和文件名对不上 |
| `营销闸门: 识别 N 条 → 保留 / 丢弃` | 广告判定 | 丢弃 ≈ 识别 → 词库是空的，见下一节 |
| `turns=907` | 过滤了多少 | `过滤=93` 里含图片/链接/隐私/过短，正常 |
| `丢 0` | `dropped_events` | >0 说明单行超长或一批太满，改小 `--window` |

> 导入期间**不要**往同一个域并发灌。片与片串行等 `done`，并发提交只会让
> LLM 排队更久，`--resume` 的语义也会变模糊。

### 4. 校验

四件事过一遍才算灌完。

**(1) 报告**

```bash
python3 -c "import json;d=json.load(open('back_end/data/import_report.json'));print(d['totals']);print(d['files'][0].get('display_names'))"
```

`totals` 五个数：`chunks` / `skipped` / `turns` / `events` / `dropped_events`。
`files[].marketing_gate` 是闸门统计，`files[].display_names.written` 是名字标记写了几条。

**(2) 卡数 + 时间来源**

```bash
curl -s -X POST http://127.0.0.1:8081/v1/graphql -H "Content-Type: application/json" \
  -d '{"query":"{ Aggregate { MemoryEvent(where: {path: [\"namespace\"], operator: Equal, valueText: \"qq-505456846\"}) { meta { count } } } }"}'
# {"data":{"Aggregate":{"MemoryEvent":[{"meta":{"count":1659}}]}}}

curl -s -X POST http://127.0.0.1:8081/v1/graphql -H "Content-Type: application/json" \
  -d '{"query":"{ Aggregate { MemoryEvent(where: {path: [\"namespace\"], operator: Equal, valueText: \"qq-505456846\"}, groupBy: [\"time_source\"]) { groupedBy { value } meta { count } } } }"}'
# msg_ts 1656 / relative 3（imported 0）
```

上面这组是 10 群重灌后的真实值。**`imported` 应该为 0 或接近 0**；
它占比高说明这批卡的时间只能靠导入时间兜底 —— 要么源消息没有 `ts`，
要么切分降级成了原文卡（见「排障」）。

**(3) 发言人解析**

```bash
cd /mnt/e/PycharmProjects/amarantine/back_end
TOKEN=$(grep '^ADMIN_TOKEN=' .env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/whois?name=%E5%BE%80%E6%B1%90&scope=group:485490399&base=true&namespace=qq-505456846"
```

返回 2 个账号且 `ambiguous:true` 就对了（完整返回见下文「名字标记」）。

**(4) 召回自测（可选但推荐）**

```bash
./harness/dev-linux.sh python ../harness/import_chat.py \
  --corpus-dir data/corpus/qq-import --namespace qq-505456846 --resume \
  --selftest ../back_end/data/corpus/selftest_040.json
```

`--resume` 会跳过已 `done` 的片，所以这条重跑只花自测那几秒。不加它也行 ——
`dedupe_key` 幂等会返回原 job，只是要多等一遍。

```
✓ 前端部署在哪台机器 → 材料 1 条
✓ ESB 查客户信息返回什么字段 → 材料 1 条
✓ 中登接口用什么报文格式 → 材料 1 条
✓ 广州营业部合规对接人是谁 → 材料 1 条
✓ 报表导出积压告警阈值 → 材料 1 条
  命中率 5/5 = 1.0
```

判据是「任意材料摘要里出现任意 expect 关键词」—— 测的是**灌进去的查得回来**，
不测语义正确性。

### 5. 营销闸门：广告先比对，无关就丢

`--gate-marketing` 对识别成营销的消息（广告/补款/图片刷屏）抽出**术语**
（品牌、作品名，已去掉 URL、图片哈希、模板词），拿去和已有记忆做**词面**比对：
命中一个就保留，一个都没有就丢弃。统计写进报告 `marketing_gate`。

> **为什么不用 `/admin/recall` 判相关性**：召回分数是 RRF 归一化后的**相对值**，
> top-1 恒为 1.0 —— 拿它判「相关」等于恒真。实测 289 条广告**全部**被判相关。
> 词面命中没这个问题：广告里的品牌名要么在记忆里出现过，要么没有。

两个已知坑：

| 坑 | 表现 | 怎么办 |
|---|---|---|
| 词库读的是 **Neo4j HTTP 7474**（`neo4j` / `password`），不是 `/admin/recall` | 改过 `NEO4J_PASSWORD` 后词库为空 → **广告全丢** | 让 7474 的账号密码与脚本一致；日志里会出现「读取记忆词库失败（按空处理）」 |
| 术语抽不出来 | 该条判 `marketing_no_terms` 直接丢 | 纯图/纯表情的广告没有术语，属预期 |

### 6. 指代回溯：30 条上文，最多 120

片首的「那个多少钱」「他说的那个」没有上文，模型只能瞎猜。所以每片建卡时：

1. 带上**同会话**、当前片之前最多 **30 条**消息作为**只读上文**，并明确告知模型「不要为上文建卡」；
2. 模型返回 `needs_more_context` 时，回溯到最多 **120 条**重试一次；
3. 模型输出 `source_indices`（本片内的 1-based 序号），`date_start` 取这些消息里**最早**的 `ts`；越界索引丢弃并计数。

失败时的降级顺序：多给的上文超时 → **截短上文（30→10）重试一次** → 仍失败才降级成整段原文卡。

> **单片 30 条实测 20–80 秒**，这就是 `LLM_TIMEOUT_MS` 要写成 `180000` 的原因。
> 用 60 秒的默认值，一部分片会超时降级，卡上 `time_source` 就会变成 `imported`。

### 7. 名字标记：昵称 / 群名片 / 备注都是「名字」

**身份只有一个（`uid`），名字可以有很多个；差别只在「这个标记在哪个范围生效」。**

| 标记 | 来源 | 默认 scope | 谁看得到 |
|---|---|---|---|
| 账号昵称 | `nick` | `global` | 好友 / 资料页 |
| 群名片 | `group_card` | `group:<gid>` | 该群所有人 |
| 好友备注 | `remark` | `friend:<uid>` | 只有备注的人自己 |
| 通讯录真名 | `real_name` | `global` | 平台字段，可能为空 |

**没有优先级，只有生效范围。** 查询发生在哪个 scope，就取那个 scope 的标记：
本 scope 有 → 用；没有 → 回落 `global`；全都没有 → 用 `uid` 短形式占位并标 `name_missing`。

#### 五分类：名片不是都能当名字用

实测群 `1019369531`（500 人）同名组 10 个，形态完全不同 ——
**不分类直接按名字关联，会把 21 个人并成 1 个人**：

| 分类 | 长什么样 | 实测号数 | 处理 |
|---|---|---|---|
| `moderation` | `合并转发存在广告,禁言15分钟`、`文本存在广告,禁言15分钟` | 23 + 13 | **不是名字**，排除 |
| `blank_ws` | 全角空格、控制字符组成的空名片（整活人） | 3 + 2 | **不是名字**，排除 |
| `blank_punct` | `……`、`.` | 2 | 回落昵称 |
| `alt` | `往汐…复活版`、`小号`、`二号机` | 1 | 同主人候选 |
| `normal` | `秋`、`咸鱼`、`黑崎一护` | 各 2 | 真实同名 |

另有第 6 类 `spoofed`（伪装名），**不在这五类里**，规则见下一节。

判定顺序固定：处罚关键词 → 全空白/控制字符 → 括号先剥再删标点 → 小号后缀 → 其余 `normal`。

> **括号必须「先剥再删标点」**：否则 `往汐（北瑀家的鱼）复活版` 会变成
> `往汐北瑀家的鱼`（括号被当标点删掉，括号里的字粘进名字）。

#### 整活阈值 3

| 组规模 | 条件 | 结论 |
|---|---|---|
| ≥ 3 | — | `impersonation`（整活），**不建任何关联边** |
| = 2 | 恰好一个带小号后缀 | `same_owner`，可建同主人边 |
| = 2 | 都无后缀 | `ambiguous`（同名但无证据），只留痕 |

阈值定 3 的依据：实测整活组 13～23 个号，真实同名多为 2 个号 ——
定 3 既不误伤「秋×2」，又拦得住大组。

#### 伪装名：一律不作数

含双向控制符（U+202A–202E、U+2066–2069、U+200E–200F）或零宽字符
（U+200B–200D、U+FEFF、U+2060）的名字，渲染出来像干净名，但字节不同。
这类名字常是**复制粘贴别人的模板**，两个号即使只差一两个字也可能毫无关系。所以：

- 判成 `spoofed`，**不参与任何关联**（既不跟干净名合并，也不跟其它伪装名合并）；
- 进审计报告（带 `uid` / `qq` / `card` / `kind` / `reason`），不静默丢弃。

**近似名也不合并**：`往汐` 与 `往汐酱`、`往汐` 与 `往汐。` 都各算两个独立名字。
判定只用「基础名完全相同」这一个条件 —— **宁可漏关联，不可错合并**。

#### 写与查

导入时会自动把该群的名片写成 `group:<gid>` 那一层（日志里的
`名字标记(scope group:...)` 就是它）。要补昵称（`global` 层）就手动发一次：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"namespace":"qq-505456846","scope":"global","records":[
        {"uid":"u_oZvYzRujHn5KleWUi8o--Q","name":"潮间带","source":"nick"}]}' \
  http://127.0.0.1:8765/admin/display-names
# → {"scope":"global","written":1,"failed":0}
```

图上落成 `(:Account {namespace, uid})-[显示名边 {scope, source}]->(:Alias {name})`：
同一个 uid 多名片共存、互不覆盖；名片改名时旧名字不删。

查：

```bash
# 精确查（字节完全相同）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/whois?name=往汐（北瑀家的鱼）&scope=group:485490399&namespace=qq-505456846"
# → 1 个账号，ambiguous:false

# 按基础名查（去括号 + 去小号后缀）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/admin/whois?name=往汐&scope=group:485490399&base=true&namespace=qq-505456846"
```

真实返回（`base=true`）：

```json
{"name":"往汐","scope":"group:485490399","namespace":"qq-505456846","base":true,
 "accounts":[
   {"uid":"u_Z0cEqOYSuNMwk8uyXXFgtQ","name":"往汐（北瑀家的鱼）复活版","source":"group_card"},
   {"uid":"u_oZvYzRujHn5KleWUi8o--Q","name":"往汐（北瑀家的鱼）","source":"group_card"}],
 "ambiguous":true,"name_missing":false}
```

| 字段 | 含义 |
|---|---|
| `accounts` | 全部命中。`ambiguous=true` 时**必须全列**，不替你挑 |
| `ambiguous` | 同一个名字在同一个 scope 下对应多个 uid |
| `name_missing` | 一条都没命中 —— 不是「这个人不存在」，是「这个 scope 里没登记过这个名字」 |

> `base=true` 只放宽「去括号 + 去小号后缀」，**不做编辑距离/相似度合并**：
> 查 `往汐` 不会带出 `往汐酱`。

### 8. 时间从哪来：`time_source`

| 取值 | 什么时候 | 例子 |
|---|---|---|
| `msg_ts` | 原话没给时间 → 用消息时间 | 消息 10-30 → 事件 10-30 |
| `relative` | 原话是相对词 | 「昨天」+ 消息 10-31 → 事件 10-30 |
| `stated` | 原话给了明确日期 | 「10月28日」 |
| `imported` | 连消息时间都没有 → 用导入时间 | 无 `ts` 的语料 |

`date_start` 由 `source_indices` 指向的**最早**那条消息的 `ts` 决定。
校验口径只有一句：**`source_indices` 生效之后，`time_source=imported` 的占比应该接近 0**。

---

## 五、FAQ

**Q：换 `EMBEDDING_MODEL` 之后写入报错「索引由 X 生成，当前是 Y」？**

这是 A25 的保护，**不是 bug**。不同模型的向量可能维度相同、空间完全不同，
混写不会报错，只会让相似度静默变错 —— 所以系统选择直接拒绝。

处理方式二选一：

1. 把 `EMBEDDING_MODEL` 改回原来那个（数据没坏，能立刻恢复写入）
2. 确实要换模型 → 先重建向量索引（`harness/rebuild_vectors.py`，007 待做），
   重建期间不要写入

错误信息里会同时给出两个模型名，照着改即可。每个记忆域（namespace）
各自记录自己的模型，互不影响。

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

**Q：导入后发言人全是 `undefined` / `unknown` / 一串数字？**

三种回落，从差到好：`undefined`（导出时字段缺失）→ `unknown`（`senderName`
和 QQ 号都没有）→ 一串 QQ 数字。根因是同一个：**没把 `sender_uid` 解析成群名片**。

按这个顺序查：

1. 成员表在不在：`back_end/data/corpus/qq-identities/group-<gid>.json`，
   文件名里的 gid 必须和语料的 `cid` 对得上；
2. 导入日志里有没有 `群成员表: N 人 → 可用显示名 M`，`M=0` 就是没读到；
3. 语料里 `sender_uid` 是不是 `null` —— 那说明导出脚本没带这个字段
   （旧版导出脚本没有它），重导一次即可。

> 名片可用却显示成昵称，是**名片被分类排除了**（处罚标记 / 空白 / 伪装名），
> 此时回落昵称是正确行为，不是 bug。

**Q：为什么「往汐」能查出两个号？**

因为名片被两个号共用：主号 `3883585327`（昵称潮间带，名片 `往汐（北瑀家的鱼）`）
和复活版 `2154245492`（昵称琴酒拜松年，名片 `往汐（北瑀家的鱼）复活版`）。
精确查只命中主号；加 `base=true` 去掉小号后缀后命中两个，并标 `ambiguous:true`。

这是**要的行为**，不是数据脏：它们确实是两个号，系统不替你合并，也不替你做主。

**Q：为什么广告一条都搜不到？**

大概率是被营销闸门丢了，不是没灌进去。证据在两处：报告的
`marketing_gate.dropped`，以及 `files[].dropped` 里 `reason` 为
`marketing_unrelated` / `marketing_no_terms` 的条目。

如果 `dropped` 几乎等于 `marketing`（几乎全丢），先看导入日志前两行：
`词库 N 字符` 很小、或出现「读取记忆词库失败（按空处理）」，说明词库没读到
（Neo4j 7474 的账号密码与脚本不一致），这时**所有广告都会被判无关**。
要保留它们：清掉这批 `dedupe_key`（或换一个空 namespace）后不加
`--gate-marketing` 重跑。

**Q：导入要跑多久？**

LLM 切分是**串行**的，一片 30 条，单片实测 20–80 秒。106 个群 × 每群 1000 条
≈ 10.6 万条 ≈ 3500 片，按 18 秒/片算约 **18 小时**。

想快只有两条路：少灌（`--per-group-limit` 调小）或分批（按群分几天跑，
`--resume` 会跳过已完成的片）。**并发提交不会更快** —— 服务端同一时刻只吃
有限个 LLM 请求，多开的只会一起排队。

**Q：我只想导一个群？**

导出用 `--groups <gid>`（逗号分隔可多个），导入把 `--corpus-dir` 换成
`--corpus <那一个 json>`：

```powershell
node harness/export_qq.mjs --db <nt_msg.db> --per-group-limit 1000 `
  --groups 485490399 --out-dir back_end/data/corpus/qq-import
```

```bash
./harness/dev-linux.sh python ../harness/import_chat.py \
  --corpus data/corpus/qq-import/qq-485490399.json \
  --namespace qq-505456846 --window 30 --prefix qq
```

单群同样会读 `--identity-dir` 里的成员表（按 `cid` 找 `group-<gid>.json`）。

**Q：灌错了怎么回滚 / 清空某个 namespace？**

先把三层分清楚，**只有一层是唯一真相**：

| 层 | 删了会怎样 | 能重建吗 |
|---|---|---|
| 图（Neo4j） | 实体/关系没了 | ✅ 能从事件卡重建（`rebuild_graph.py`） |
| 向量（Weaviate 的 `MemoryEvent`） | **事实层，删了就没了** | ❌ 只能重灌 |
| SQLite（`ingest_job` / `raw_quote`） | 原文和 job 记录没了 | ❌ 只能重灌 |

`harness/purge_namespaces.py` **只适合清测试域**：它清的是**所有**非
`work` / `personal` 的 namespace，没有「只清一个」的开关。别在有真数据的
机器上随手 `--apply`。

只清一个域，就手写三条（顺序：图 → 向量 → SQLite）。先取口令：

```bash
cd /mnt/e/PycharmProjects/amarantine/back_end
export NEO4J_PASSWORD=$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2-)
```

```bash
# 1) 图（派生层，可重建）
curl -s -u neo4j:$NEO4J_PASSWORD -H 'Content-Type: application/json' \
  -d '{"statements":[{"statement":"MATCH (n) WHERE n.namespace = $ns DETACH DELETE n","parameters":{"ns":"qq-505456846"}}]}' \
  http://127.0.0.1:7474/db/neo4j/tx/commit

# 2) 向量（事实层，删了只能重灌）
curl -s -X DELETE http://127.0.0.1:8081/v1/batch/objects -H 'Content-Type: application/json' \
  -d '{"match":{"class":"MemoryEvent","where":{"path":["namespace"],"operator":"Equal","valueText":"qq-505456846"}},"output":"minimal"}'

# 3) SQLite：job 记录 + 原文
sqlite3 back_end/data/memory.db \
  "DELETE FROM ingest_job WHERE namespace='qq-505456846';
   DELETE FROM raw_quote WHERE namespace='qq-505456846';"
```

> **向量删了必须连 `ingest_job` 一起删。** 只删向量、留着 `done` 的 job，
> 下一次 `--resume` 会把所有片当「已完成」跳过 —— 卡永远回不来，而且不报错。
> 这是「回滚」最容易踩的坑。
>
> 反过来说，**只想重灌某几片**就别清库：用 `--suffix r2` 换一批 `dedupe_key`
> 重提即可（`failed` 的片用 `--retry-failed`）。

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
| **QQ 全量 106 群 ≈ 18 小时** | LLM 串行切分，单片 20–80 秒；中途要能续跑 | 暂不优化（并发收益被 LLM 排队吃掉）；用 `--resume` 分批 |
| **群名片随时可能被改** | 管理员可以把名片改成处罚标记；导入按当时快照取名，之后不自动更新 | 重导成员表 + 重跑一次导入（名字标记幂等覆盖） |
| **不做跨群指代** | 「上次那个群说的」回溯不到 | 设计边界（spec 046 只做同会话） |
| **伪装名不关联** | 含双向/零宽控制符的名字不参与任何关联 | 设计如此（宁可漏） |
| **近似名不合并** | `往汐` 与 `往汐酱` 是两个独立名字 | 设计如此（宁可漏） |

---

## 七、排障

### 派生层重建（图 / 向量）

事实层（事件卡）是唯一真相源，图与向量都可以从它重建。
三个脚本都在仓库根目录跑，**默认 dry-run**，先看数字再加 `--apply`。

```bash
# 1. 看会重建多少（不动图）
./harness/dev-linux.sh python ../harness/rebuild_graph.py --namespace work

# 2. 真写图节点
./harness/dev-linux.sh python ../harness/rebuild_graph.py --namespace work --apply

# 3. 重算向量（先小步试 2 条）
./harness/dev-linux.sh python ../harness/rebuild_vectors.py --namespace work --limit 2
./harness/dev-linux.sh python ../harness/rebuild_vectors.py --namespace work --apply

# 4. 验证双向可解析
./harness/dev-linux.sh python ../harness/check_rebuild.py --namespace work
```

**风险提示**

- `--apply` 会真写。重建图**不会删**已有节点（`MERGE` 语义），
  但重建向量会覆盖现有向量 —— 如果 `EMBEDDING_MODEL` 改过，
  必须先删掉旧 collection 再重建，否则两种模型的向量会混在一起
  （维度可能相同、空间不同，检索会静默变错；`rebuild_vectors.py` 会先拦一次）
- 重建期间不要同时写入
- `check_rebuild.py` 报「图有、未删除卡没有 N 个」是**正常**的 ——
  软删的卡在 Weaviate 里被过滤，但图节点还在

### rerank：可选增强，没有也能跑

rerank 是**精排**，让返回顺序更准。它**不是必需品**：
关掉或服务挂掉时，召回会自动退回 RRF 排序，
仍然返回 200 和非空材料集，只是顺序粗一点（`trace.rerank_skipped=true`）。

**两种部署形态，代码都支持：**

| 形态 | 要做什么 | 效果 |
|---|---|---|
| **不要 rerank**（默认） | 什么都不做，`RERANK_ENABLED=false` | 召回正常，排序 = RRF |
| **要 rerank** | 下面四步 | 召回正常，排序 = rerank 分 |

```bash
# 1. 下模型（ModelScope，仓库里直接有 safetensors，不用转换）
bash harness/fetch-reranker.sh
#   默认 BAAI/bge-reranker-v2-m3（568M）。想省内存：
#   bash harness/fetch-reranker.sh bge-reranker-base     # 278M

# 2. 起 reranker 容器（profile 隔离，不影响默认 up）
docker compose --profile rerank up -d reranker

# 3. 打开开关
#   在 back_end/.env 里设 RERANK_ENABLED=true

# 4. 重启后端
```

验证：

```bash
curl -s -X POST http://127.0.0.1:8083/rerank \
  -H "Content-Type: application/json" \
  -d '{"query":"项目进度","texts":["项目A延期到七月","今天天气不错"]}'
# 期望 [{"index":0,"score":...},{"index":1,"score":...}]，第一条分更高
```

**为什么不能拿 embedding 容器当 reranker**：
TEI 只会用它 `--model-id` 加载的那**一个**模型做 rerank。
拿 `bge-m3`（embedding 模型）调 `/rerank` 会返回
`424 model is not a re-ranker model` —— 代码把它归成「不可用」并降级，
所以不会崩，但也不会有精排效果。

**开 rerank 的一个副作用**（值得知道）：阈值 `RECALL_COUNT_MIN_SCORE`
之后是在 **rerank 分**的尺度上判断，不再是 RRF 分。两套分数不能混用于同一个
排序，只能整套换掉。

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
Invoke-WebRequest "http://198.51.100.58:3000/" -TimeoutSec 10 -UseBasicParsing
```

```bash
# WSL 侧直连
curl -o /dev/null -w "%{http_code}\n" http://198.51.100.58:3000/
```

**Windows 通、WSL 不通** → 是 NAT 不继承 VPN 路由，不是代码问题。
网关挂在 aTrust 这类**用户态虚拟网卡**上，WSL2 默认 NAT 网络看不到那条路由：

```
Windows: 198.51.100.129  ──(aTrustVPNIC 2.0.0.1)──> 198.51.100.58:3000   ✅
WSL2:    172.20.192.x    ──────────────────────────> 198.51.100.58:3000   ❌
```

**解法**（不需要管理员、不需要重启）：

```powershell
# Windows 侧起转发（relay 监听 3001，转发到网关的 3000）
python harness\vpn_relay.py --listen 3001 --target 198.51.100.58:3000
```

`harness/dev-linux.sh` 会**自动探活并按需拉起** relay，再把 `LLM_BASE_URL`
填成 `http://<WSL默认网关>:3001`，所以 WSL 这边**什么都不用改**。

> **端口别搞混**：3001 是 relay，3000 是网关。填错端口的表现是
> 「LLM 切分 60 秒后超时，降级成整段原文卡」——不会报连接错误，只会变慢变差。

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

#### 拉丁词形：jieba 之外的第二条通道

jieba 把 `ESB` 标成 `eng`，而抽取器只认 `nr`/`ns`/`nt` —— 于是**英文缩写从来
进不了图**。所以加了一条独立通道（纯正则，不看分词）：

```
ESB接口返回字段的数据字典   → ESB（kind=topic）
用 PyCharm 调一下          → PyCharm
Kafka 那边堆积了           → Kafka
API 网关限制了模型大小      → （空，停用词拦住）
```

判据：长度 2–10、不在停用词表、不是纯小写、**不是 hex/UUID 片段**
（`B8BB6E6F52`、`F439` 这类来自图片 id，必须挡掉），且满足「全大写 / 含 ≥2 个大写 /
含数字 / 首字母大写的 CamelCase」之一。

两条约束值得记住：

- **名册里的人名不会被建成 topic**：`Veronica Davichi` 是发言人，抽取器不会
  额外产出一个 `Veronica` 的实体节点 —— 人名归名册通道管。
- **全小写的系统名收不到**：`dsh-music-insight` 会被拒。这是刻意的取舍 ——
  放开纯小写会让 `and`/`for`/任意英文词涌入，噪声不可控。宁可漏。

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
| v0.4 | 2026-11-02 | 补别名章节（一个人多种称呼、置信度分级、一词多义、可撤销） |
| v0.5 | 2026-11-02 | 别名 LLM 推断（描述性声明 → 候选选择 → probable/uncertain）；补 upsert 回填说明 |
| v0.6 | 2026-11-02 | 别名声明带出名字；实体纠错现状与「宁可漏」的取舍；同名双节点问题 |
| v0.7 | 2026-11-02 | 实体纠错（改/拆/合/解挂，无删除）；实体代号 key 化；解挂与改名的区别 |
| v0.8 | 2026-11-02 | 一个 key 只有一个节点（MERGE 不带标签）；ALIAS_OF 宿主不再硬绑 Person |
| v0.9 | 2026-11-02 | 别名主人走第 7 条路：终点不限标签、只直连不扩展 |
| v0.10 | 2026-11-02 | 分词：排行称谓词典、连词剥离、启动时 warm-up（不自动反哺） |
| v0.11 | 2026-11-02 | 别名声明左值也要校验；修 `append_alias_edges` 漏传 key 导致别名边静默丢失 |
| v0.12 | 2026-11-02 | 图写不再静默：意图日志（写前记账）+ 启动补跑 + 每日对账；`graph_pending` 透传给调用方 |
| v0.13 | 2026-11-02 | `人` 不再一刀切当描述标记：`印度人`/`主持人` 放行，`老实人`/`聪明人` 仍拦；修「三哥就是印度人」建不出别名边 |
| v0.14 | 2026-11-02 | 判据统一成「单字只做位置检查，包含检查只用于多字词」：`家`/`个`/`种`/`位` 换成具体双字词，单字副词/量词走开头检查，`X人` 改降置信度；修 `科学家`/`上海人` 被误伤 |
| v0.15 | 2026-11-03 | 判据带理由（`reason`/`detail`，`detail` 确定性）；别名声明的拒绝两侧都落 `alias_reject` 表 + 两个管理接口；罕见姓豁免（量词字是姓氏 + jieba `nr` 或用户 `姓X` 声明，≥3 字），修 `种师道` 被量词判据误杀 |
| v0.16 | 2026-11-03 | 裁决闭环：`accepted` 后绕过判据补建别名边，失败记 `accept_pending` 留在待办可重试；纠错痕迹可查（`GET /admin/entities/edits`，`args` 解析成对象）；新增 `harness/check_spec.py` 校验 spec 骨架 |
| v0.17 | 2026-11-03 | 事件管理面补齐五个 ADR 接口：`PATCH /admin/event`（白名单 + 幂等 + `pinned_source`）、`DELETE /admin/event`（只软删，`hard=true` 拒绝）、`POST /admin/archive`（P0 不搬）、`POST /admin/recall` + `.../{trace_id}/feedback`（带 `trace_id` 才能关联）；`retention_expired` 成为独立审计原因 |
| v0.18 | 2026-11-03 | 原文层落地：`POST /admin/ingest`（异步导入 + `dedupe_key` 幂等 + `do_not_store` 拦截）、`GET /admin/ingest/{job_id}`（`202` 的配套查询接口）、`GET /admin/raw/{quote_id}`（原文溯源，软删事件卡后仍可读）；`raw_quote_ids` 第一次真的被填上，召回材料也带出来了 |
| v0.19 | 2026-11-03 | 浏览器管理台（`/ui`）：零构建单页，复核队列可裁决、事件列表可软删（**手输域名**二次确认）、误删率报告只读（`dry_run` 写死）、点 `raw_quote_ids` 看原文；新增 `GET /admin/events`（分页列表，只读）；`GET /raw` 补 `scanned` / `truncated` / `lookup_ok` 三个诚实字段 |
| v0.20 | 2026-11-07 | 批量导入不再静默丢尾部（spec 036）：撞单批上限时按行二分重切（最多 3 轮），`ingest_job` 增 `dropped_events` / `split_rounds` 两列并在 `GET /admin/ingest/{job_id}` 返回；仍超限时打 WARNING 并计数，日志带 job 号 |
| v0.21 | 2026-11-12 | 补 QQ 导入全流程（导成员表 → 导消息 → 导库 → 校验，含真实预期输出）、营销闸门与词库口径、指代回溯（30→120）、群名片五分类与 scope 体系（整活阈值 3、伪装名、近似名不合并）、`/admin/display-names` 与 `/admin/whois`、`time_source` 四取值；安装章节补 8083/8765/3001 端口与 `LLM_TIMEOUT_MS=180000`；补 6 条 QQ FAQ 与 5 条限制 |
