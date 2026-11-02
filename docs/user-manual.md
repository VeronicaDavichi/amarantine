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

#### 「人」是名词后缀，不是描述标记

`人` 曾经直接在描述标记集合里，于是**所有**带「人」的串都被当描述拦掉，
包括 `印度人`。后果很具体：`三哥就是印度人` 抽得出 claim、卡上也留着，
但建边时的 `_is_clean_name(canonical)` 把它丢了 —— **边一条建不出来，
且没有任何日志**。表现就是「这句话明明说清楚了，图上却什么都没有」。

区别不在「有没有人」，而在**「人」前面那个词是名词还是形容词**：

| 名词前缀 → 放行（是名字） | 形容词前缀 → 拦（是评价） |
|---|---|
| `印度人` `中国人` `上海人` `东北人` | `老实人` `聪明人` `坏人` `好人` |
| `外国人` `主持人` `负责人` `介绍人` | `懒人` `陌生人` `什么人` `那种人` |

判据是 `DESCRIPTIVE_BEFORE_REN`：只在 `人` 前面是形容词时才拦。

为什么不能干脆不拦：`小明就是老实人` 会建出
`(:Entity {name:"老实人"})` —— 评价词进了图，比漏掉一个名字脏得多。
这类 claim 仍然留在卡上（`uncertain`），等 LLM 层解析，只是不建边。

实测（真库）：修前 `_is_clean_name('印度人')=False` → 边 0 条；
修后 `True` → 图上出现
`(:Entity {name:"印度人"})-[:ALIAS_OF {label:"exact"}]->(:Alias {name:"三哥"})`，
查「三哥」能从 vector/bm25/graph 三条通道召回该事件。

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

#### 为什么宁可漏

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
