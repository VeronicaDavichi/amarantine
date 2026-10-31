# ADR-001 长期记忆架构：事件卡 + 双库并行召回

- **状态**：待评审
- **版本**：v0.4.1
- **日期**：2026-10-30
- **决策人**：主人（待确认）/ 鱼鱼

---

## 1. 上下文

**目标**：让 agent 跨年记住对话，在需要时检索出相关材料，拼装进上下文。

**原始设想**：每日按「日期 + 事件」把聊天记录写入 Weaviate；每半年把上半年内容按「事情类型」迁入历史 class；对话时先经 Neo4j 扒出相关信息，再把结果丢进 Weaviate 二次查询，作为参考材料返回模型。

**本轮已拍板的四个约束**：

1. 本轮只交付架构决策文档，不写生产代码。
2. 数据后端从第一版就上双库：Neo4j + Weaviate。
3. 归档维度改为「时间」而非「事情类型」，且窗口期必须可配置。
4. 暂无历史聊天数据与标注集，先做架构与接口契约。

**核心判断**：「能记很多年」是伪需求，真需求是**该记的时候记得对 + 可溯源 + 可遗忘**。只堆记忆不控质量，得到的是记性极好、判断极差的 agent。本 ADR 的全部设计都服从这条。

---

## 2. 目标 / 非目标

### 目标

| ID | 目标 | 判定方式 |
|---|---|---|
| G1 | 写入：把对话流转成可检索的「事件卡」，保留原文引用 | 事件卡 schema 校验通过 |
| G2 | 召回：一次查询并行命中近期/全局/图谱三路（关键词并入 hybrid），融合后返回有界材料集 | `/recall` 契约测试通过 |
| G3 | 分层：热冷按时间切分，`ARCHIVE_WINDOW_DAYS` 可配 | 改配置后冷热标记随之变化 |
| G4 | 韧性：Neo4j 不可用时召回降级但可用 | 故障注入返回 200 |

### 非目标

- **N1** 不做模型微调。
- **N2** 不做实时流式写入，第一版按批次。
- **N3** 不做多用户/多租户隔离，列出为待定项。
- **N4** 不做自动**物理**遗忘：淘汰只作用于派生索引层，事件卡与原文永不物理删除，物理清理须人工闸门放行。

---

## 3. 关键决策

### D1 存储模型：单 collection + 多标签 + 时间过滤 + namespace 隔离

**放弃**「按事情类型建独立 class」。理由三条：

1. 事情类型是**开放类别**，不是封闭枚举，class 数量会无限膨胀。
2. 一件事天然跨类（「换工作」同时是工作 / 情绪 / 人际 / 财务），按类拆会导致同一段记忆被割裂，跨类召回直接丢失。
3. 半年一次「全量重分类」成本高，且 LLM 两次分类结果不一致，历史数据标签会漂移。

**采用**：单 collection `MemoryEvent`，元数据字段承载筛选能力，热冷用 `date_end` 过滤表达。

| 字段 | 类型 | 说明 |
|---|---|---|
| `namespace` | text | **agent 域隔离键**，由 `X-Agent-Key` 派生；写入必填，所有查询强制过滤 |
| `event_id` | text | 内容哈希主键，**含 namespace**，生成规则见下 |
| `date_start` / `date_end` | date / date(null) | `date_end=null` 表示持续中 |
| `types` | text[] | 多标签，受控词表 |
| `entities` | object[] | `[{name, kind}]`，`name` 规范化后与图节点对齐 |
| `summary` | text | 事件卡摘要（向量化字段）。**必须具体**：谁、做了什么、结果如何；禁止抽象概括，否则向量与其它卡糊在一起 |
| `conclusion` | text | 当时的结论 |
| `unresolved` | text[] | 未决事项 |
| `emotion` | text | 情绪标签 |
| `importance` | number | 0–1，只影响复核分桶，**不做一票豁免** |
| `raw_quote_ids` | text[] | 回指原文，溯源用 |
| `supersedes` | text(null) | 本卡取代的旧卡 id（指向旧版） |
| `observed_at` | date | 该事实首次被观察到的时点，D5 冲突消解用 |
| `retention` | text | `normal` / `sensitive`；`do_not_store` 在**入口拦截**，不产生卡 |
| `ingested_at` | date | 写入时间，用于对账 |
| `pinned` | boolean | 人工置顶，命中即硬保护 |
| `recall_count` | int | 有效召回次数（口径见 D7） |
| `last_recalled_at` | date(null) | 最近一次有效召回时间 |
| `confirmed_by_use` | int | 被有效引用次数 |

**namespace 隔离（v0.10）**

本机可能同时运行多个 agent，记忆必须按 agent 域隔离。

- **隔离方式：单 collection + `namespace` 字段过滤**，不按 agent 拆 collection
  - 拆 collection 会让 schema 变更成本 ×N、跨 agent 统计缺失；字段过滤只要**强制注入**就可靠
- **强制注入**：`namespace` 过滤由 **repository 层统一注入**，业务代码拿不到裸 Weaviate client。少写一次 filter 就串库，所以这是**代码结构约束**，不是编码规范
- **`namespace` 必须进 `event_id` 哈希**（见下）：否则两个 agent 记同一句话会撞 id，幂等机制把 A 的写入误判成 B 的重复
- `dedupe_key` 同理带 namespace 前缀

**`event_id` 生成规则（幂等地基）**

```
canonical  = namespace + "|"
           + date_start + "|" + (date_end ?? "") + "|"
           + sorted(types).join(",") + "|"
           + sorted(entity_names).join(",") + "|"
           + normalize(summary)
event_id   = "evt_" + sha256(canonical)[0:16]
normalize  = 去首尾空白、全角转半角、转小写、去标点
```

同一内容重复写入 → 同一 `event_id`，幂等成立。切分边界变化 → `event_id` 变化，这是**正确**的：边界变了就是不同事件，混用会导致检索串味。

幂等机制分工：`dedupe_key` 防「同一批 turns 重复提交」，`event_id` 防「同一内容重复落库」。二者缺一不可。

**`deleted_at` 不属事实层**：软删除标记只存在于派生片段层。事件卡与原文永不软删、永不物理删，由 D7 约束。D7 流程中的 `deleted_at` 一律指派生片段。

**归档语义**：`date_end` 为 `null`（持续中）或 `date_end >= now - ARCHIVE_WINDOW_DAYS` 为热，其余为冷。注意 Weaviate 的 date filter **不匹配 null**，热集查询必须写成 `date_end IS NULL OR date_end >= X`，否则所有持续事件会被误判为冷。P0 仅做**逻辑分层**（同一 collection 内 filter）；物理拆分（`MemoryEvent_Hot` / `_Cold`）列为 P1 可选项，由数据量或冷查询延迟触发，**不由日历触发**。

### D2 双库职责：单向派生，图可丢弃重建

这是本轮最重要的决策。既然第一版就上双库，就必须让图的失败**不阻断主链路**。

| 角色 | 组件 | 地位 |
|---|---|---|
| 事实源（source of truth） | Weaviate `MemoryEvent` | 不可丢 |
| 派生索引（可全量重建） | Neo4j 图谱 | 可丢弃，从事件卡重建 |

**写入顺序**：先写 Weaviate 成功 → 再发图更新（outbox + 幂等 `event_id`）。图写失败只告警，不回滚、不阻断。

**图 schema**：

- 节点：`Person` / `Event` / `Topic` / `Place` / `Org`；`Event` 节点携带 `weaviate_id` 回指。
- 边：`PARTICIPATED` / `OCCURRED_AT` / `ABOUT` / `CAUSED` / `SUPERSEDES` / `MENTIONED_IN`。

**namespace 隔离**：所有节点与边统一携带 `namespace` 属性，查询强制 `WHERE n.namespace = $ns`。与 Weaviate **同源**（同一 `X-Agent-Key` 派生），由 repository 层注入，业务代码不手写。图重建按 namespace 分片执行，**禁止跨域重建**。

**图只做两件事**：精确关系约束、实体消歧（把「上次说的那个项目」解析成具体节点）。**图不做召回主链路**。

**对账**：定时用 Weaviate 的 `event_id` 全集校验图，缺失重放。图重建作业是验收项之一。

### D3 召回编排：并行扇出 + RRF 融合 + 超时降级

原始设想的「Neo4j → Weaviate 串行两跳」**不采用**。两次网络往返叠加图遍历展开量，P99 不可控；更致命的是错误级联——图那跳漏了实体，第二跳必然查偏。

```
query
  ├─ time_resolver ──→ 时间意图 + 绝对区间
  └─ fan-out（并发，统一 budget）
       ├─ Weaviate 近期窗口（date filter = 区间）
       ├─ Weaviate 全局语义（无 date filter，意图为 recent 时可略）
       ├─ Neo4j 实体扩展（1–2 跳，只出候选 event_id 与约束）
       └─ 关键词/BM25（可与 hybrid 合并）
            ↓
        RRF 融合（k=60） → rerank（相关性 × 时效 × importance） → 去重 → 动态截断（预算驱动）
            ↓
        材料集：事件卡 + 原文引用 + 时间戳 + freshness
```

**超时降级矩阵**（硬约束）：

| 通道 | 超时 | 失败处理 |
|---|---|---|
| Weaviate | 800ms | 整体失败，返回 503 |
| Neo4j | 200ms | 丢弃该路，`degraded=true`，不重试 |
| 整体 | `budget_ms` | 返回已就绪的部分，标记 `partial=true` |

`degraded` 表示**某通道缺失**（图挂了），`partial` 表示**预算耗尽只拿到部分结果**；两者可同时为 true，必须在响应里分开上报，否则调用方无法区分「该重试」还是「该降级展示」。

**动态 top-K：K 是结果，不是入参**

固定 `RECALL_TOP_K` 有两个问题：短卡时 context 浪费，长卡时 12 条就超预算；而「难问题多给、简单问题少给」这种按查询难度驱动的逻辑不可靠——LLM 自评难度会系统性失真。

**正确做法**：外部只给 **token 预算**，条数由分数分布与预算共同决定。

```
候选（每路 top 50）→ RRF → rerank 打分
  1. 强制保留：pinned / unresolved 命中项（不参与截断，允许溢出）
  2. 绝对过滤：score < RECALL_COUNT_MIN_SCORE 直接丢弃
  3. 相对衰减：score < max_score × RECALL_DROP_RATIO 丢弃
  4. 断崖检测：降序相邻落差 > RECALL_GAP_RATIO 处截断
  5. token 预算：累计 token 超 CONTEXT_TOKEN_BUDGET 处截断
  6. 条数夹取：clamp(K, RECALL_TOP_K_MIN, RECALL_TOP_K_MAX)
```

优先级：**强制保留 > 绝对过滤 > 预算 > 条数夹取**。第 1 条可突破上限，此时标记 `overflow=true` 并上报，**绝不静默截断**——记忆安全高于 context 整洁。

**可审计**：`/recall` 必须返回 `trace.top_k_decision`，说明为什么是 K 条（触发的断崖、预算还是条数上限）。动态化离开可解释性就是黑箱，出了问题无法归因。

**对遗忘计数的连带修正**：动态 K 会让低分条目也进返回集。若沿用「进 top-K 就 +1」，垃圾条目会因被大量召回而获得豁免，正好破坏 A13 的初衷。因此 `recall_count` 追加分数门槛：`score >= RECALL_COUNT_MIN_SCORE` 才 +1。

**空结果必须显式，禁止静默**

触发条件（任一）：所有通道返回空，或最终最高分 < `RECALL_EMPTY_THRESHOLD`。

此时返回 `200` + `materials: []` + `trace.empty_reason: "no_candidate" | "below_threshold"`，**不得用低分材料填充凑数**。

prompt 组装侧必须注入显式标记（如「本次无相关长期记忆」），让模型知道「查不到」而不是「忘了」。否则模型会用当前对话编造记忆，或说「我不记得」——两者都是记忆系统的失败。

**不返回 404/204**：调用方必须能区分「检索正常但确实没有」与「检索失败」，这两种情况的处置完全不同。

### D4 时间解析必须独立成模块

query 里的「上次」「最近」「去年这时候」不解析成**绝对区间**，近期/全库的取舍就**没有依据**。`time_resolver` 输入 `query + now + timezone`，输出：

```json
{ "kind": "recent | specific | historical | ever | none",
  "ranges": [["2026-04-30", "2026-10-30"]],
  "confidence": 0.9 }
```

规则引擎兜底常见表达，LLM 处理模糊表达；两者冲突时以规则区间为下界、LLM 结果为扩展。`kind=none` 时并行全库，不设 date filter。

`now` 基准以**服务端时间**为准：客户端传入值与服务端偏差超过 5 分钟时忽略客户端值。否则时钟漂移或伪造会让「最近三个月」整体错位，而这类错误在结果里看不出来。

### D5 冲突与时效

同一实体的同属性出现多个版本（「我换工作了」说过三次）不合并成一条：保留 `observed_at` 时间序列，检索按 recency 加权，材料里**带时间戳与「可能已过期」标记**，由模型自行消解。新版事实通过 `supersedes` 指向旧版，供检索时判断权威性。

`possibly_stale=true` 的判定：该 `event_id` 位于某条 `supersedes` 链的非末端位置。不另设时间阈值，避免配置膨胀与「到底多久算旧」的无意义争论。

### D6 配置项（全部可配，不写死）

| 配置 | 默认 | 说明 |
|---|---|---|
| `ARCHIVE_WINDOW_DAYS` | `180` | 半年窗口，可配 |
| `RECALL_TOP_K_MIN` / `RECALL_TOP_K_MAX` | `3` / `30` | 动态条数夹取区间 |
| `RECALL_DROP_RATIO` | `0.6` | 相对最高分衰减阈值 |
| `RECALL_GAP_RATIO` | `0.15` | 断崖检测的相邻落差阈值 |
| `CONTEXT_TOKEN_BUDGET` | `3000` | 上下文 token 预算（动态 K 主驱动） |
| `RECALL_COUNT_MIN_SCORE` | `0.5` | 计入 `recall_count` 的最低分门槛 |
| `RECALL_EMPTY_THRESHOLD` | `0.35` | 空结果判定：最终最高分低于此值即返回空并标记 |
| `GRAPH_TIMEOUT_MS` | `200` | 图通道超时 |
| `VECTOR_TIMEOUT_MS` | `800` | 向量通道超时 |
| `RECALL_BUDGET_MS` | `1500` | 整体预算 |
| `TIMEZONE` | `Asia/Shanghai` | 时间解析基准 |
| `TYPES_VOCAB` | 受控词表 | 多标签词表，禁止自由生成 |
| `LABEL_DUAL_RUN` | `true` | 打标是否双跑校验（防漂移） |
| `LABEL_LLM_TEMP` | `0.2` | 打标 LLM 温度，低温度降漂移 |
| `LABEL_REVIEW_BATCH` | `50` | 周度标签复核批量 |
| `FORGET_SWEEP_DAYS` | `30` | 遗忘扫描周期 |
| `FORGET_IDLE_DAYS` | `180` | 低召回判定空闲期 |
| `FORGET_GRACE_DAYS` | `90` | 新条目保护期（防新条目饥饿） |
| `FORGET_MIN_RECALLS` | `1` | 低于此值视为低召回 |
| `PROTECT_IMPORTANCE_REVIEW` | `0.7` | 超此值进人工复核桶，**不是自动豁免** |
| `CONFIRMED_MIN` | `2` | 有效引用达此值即受保护 |
| `CHUNK_KEEP_PER_EVENT` | `2` | 同事件下保留的高分 chunk 数，其余可进淘汰候选 |
| `CONSOLIDATE_MIN_EVENTS` | `5000` | consolidation 触发：事件卡数下限 |
| `CONSOLIDATE_DUP_RATIO` | `0.4` | consolidation 触发：同实体重复率下限 |
| `CONSOLIDATE_LATENCY_MS` | `300` | consolidation 触发：召回 P95 延迟上限 |
| `GRAPH_HUB_DEGREE` | `8` | 图度数达此值视为枢纽节点 |
| `EMBEDDING_PROVIDER` | `local` | `local` / `openai-compatible`，统一走推理服务抽象 |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 默认选型，**可替换**；与索引元数据绑定 |
| `EMBEDDING_BASE_URL` | `http://127.0.0.1:8082` | 推理服务地址（宿主视角；代码跑宿主、中间件在 WSL docker） |
| `EMBEDDING_DIM` | `1024` | bge-m3 dense 维度；**变更须全量重建** |
| `RERANK_ENABLED` | `false` | P0 关闭，P1 开启 |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 可替换任意 cross-encoder |
| `RERANK_BASE_URL` | `http://127.0.0.1:8083` | 推理服务地址（宿主视角，同上） |
| `RERANK_CANDIDATES` | `50` | 参与 rerank 的候选上限，控延迟 |
| `RERANK_TIMEOUT_MS` | `300` | rerank 超时，超时退回 RRF 分数 |
| `RECENCY_HALF_LIFE_DAYS` | `180` | 时效半衰期，见 C.B1 |
| `W_TIME_BASE` | `0.7` | 时效影响力下限（时效最多影响 30%），见 C.B1 |
| `W_IMP_BASE` | `0.8` | importance 影响力下限（最多影响 20%），见 C.B1 |
| `TOKENIZER` | `cl100k_base` | token 计数口径，见 C.B2 |
| `ENTITY_ALIAS_SIM` | `0.92` | 实体消歧向量阈值，见 C.B3 |
| `TIME_CONF_MIN` | `0.5` | 时间解析信心下限，见 C.B9 |
| `LABEL_DUAL_RUN_JACCARD` | `0.8` | 双跑一致阈值，见 C.B10 |
| `CLUSTER_MIN_SIZE` | `15` | HDBSCAN 最小簇，见 C.B11 |
| `CLUSTER_MERGE_SIM` | `0.85` | 簇合并相似度，见 C.B11 |
| `COLD_QUERY_P95_MS` | `800` | 冷查询延迟目标，见 C.B12 |
| `USER_DIRECTIVE_DAILY_MAX` | `20` | 24h 内显式指令上限，超限降级，见 C.B13 |
| `DIRECTIVE_SCOPE_WINDOW` | ~~`1`~~ | **v0.11 废弃**：作用域由 LLM 决定的 `summary` 边界 |
| `DIRECTIVE_PENDING_TTL` | ~~`2`~~ | **v0.11 废弃**：服务端不再挂起指令 |
| `EMOTION_BOOST` | `0.15` | 情绪强度对 importance 的加成，见 C.B13 |
| `EXPLICIT_IMPORTANT_FLOOR` | `0.85` | 显式「重要」的 importance 下限，见 C.B13 |
| `DATABASE_URL` | `sqlite:///./data/memory.db` | 关系库连接串；可切 `postgresql://...` |
| `SQLITE_WAL` | `true` | WAL 模式，缓解多 agent 并发写 |
| `DB_BUSY_TIMEOUT_MS` | `5000` | SQLite 锁等待上限 |
| `AGENT_KEY_HEADER` | `X-Agent-Key` | 认证头名（4.0 通用约定） |
| `GUIDE_CHECK_CRON` | `0 9 * * 1` | 指南一致性校验的 cron 表达式（用户自行装 crontab，见 4.10） |
| `LOG_FILE` | `logs/memory.log` | 日志文件路径；**日志不走 stdout/stderr**（全局约定） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

### D7 遗忘策略：LRU 只作用于派生层

**分层原则**——LRU 不能一刀切，因为它会误杀三类记忆：

| 层 | 对象 | 策略 |
|---|---|---|
| 事实层 | EventCard、原文 `raw_quote` | **永不物理删**，只做冷热分层与压缩 |
| 派生层 | 向量片段、重复 chunk、consolidation 中间产物 | 可淘汰，因为**可重建 / 可去重** |
| 缓存层 | query→result 检索缓存 | 真 LRU，可随时丢弃 |

LRU 的三个盲区与对应修正：

1. **低频高价值**（一年一次的重大事件，recall 常年为 0）→ 靠「不可逆事件 + 图结构 + 独特性（实体重复度）」信号兜底。
2. **新条目饥饿**（刚写入 recall=0 就被淘汰）→ `FORGET_GRACE_DAYS` 保护期。
3. **溯源断裂**（删片段导致 `raw_quote_ids` 悬空）→ 事实层不删，溯源永远可解析。

**召回计数口径**：`recall_count` 仅在 material **进入 rerank 后 top-K 返回集**时 +1；只进候选池不计。计数写入 `recall_count` / `last_recalled_at`，按 60s 或 N 次批量 flush，避免写放大。

**三桶分流**（淘汰不是二值动作）：

| 桶 | 条件 | 处理 |
|---|---|---|
| `PROTECTED` | 命中任一硬信号 | 永不进淘汰候选 |
| `NEED_REVIEW` | 命中灰区信号（importance 偏高、同事件冗余片段） | 出周报，人工勾选 |
| `SAFE_TO_DELETE` | 不命中任何保护信号，且满足全部淘汰条件 | **人工确认后**才 soft delete |

淘汰条件（须同时满足）：空闲 ≥ `FORGET_IDLE_DAYS`、`recall_count < FORGET_MIN_RECALLS`、创建超 `FORGET_GRACE_DAYS`、`retention = normal`。

**五类硬信号（高价值界定 v0.1）**

| 信号 | 判定 | 为什么可靠 |
|---|---|---|
| `pinned` | 人工标记**或用户显式指令**（`pinned_source ∈ {manual, user_directive}`） | 最可靠，覆盖少但不可缺 |
| `unresolved` 非空 | 未闭环事项存在 | 二值可判，预言「未来必回查」 |
| `event_type ∈ IRREVERSIBLE_VOCAB` | 命中附录 A 词表 | 领域先验，不依赖模型自我打分 |
| 图结构位置 | `degree ≥ GRAPH_HUB_DEGREE`，或处于 `CAUSED` / `SUPERSEDES` 链关键节点 | 来自关系结构，独立于写入时的情绪 |
| `confirmed_by_use ≥ CONFIRMED_MIN` | 被有效引用次数达阈值 | 事后验证，最接近「真的有用」 |

**`pinned` 的两个来源**：`manual`（人工点选）与 `user_directive`（用户显式说「记住」）。**明确不允许 `llm_inferred`**——LLM 推断「这事好像重要」不能自动保护，否则就是把 D7 刚否定的不可靠信号换个名字放回来。用户指令的解析规则、作用域绑定与滥用护栏见 C.B13。

**importance 的降级**：`importance ≥ PROTECT_IMPORTANCE_REVIEW` **不自动豁免**，只进 `NEED_REVIEW` 桶。理由：importance 是写入时 LLM 的自我打分，缺乏全局参照、两次打分不一致、且会被「说得多」污染。用一个不自洽的信号做一票否决，等于把误删风险藏起来。

**灰区另含**：同一 `event_id` 下 chunk 数超过 `CHUNK_KEEP_PER_EVENT` 的冗余片段（去重优先，不做主题稀有性判定）。

**为什么移除「主题稀有性」信号**：话题集中时该信号恒定失效，且「主题」粒度本身缺乏定义——粗粒度永远集中，细粒度又回到 D1 拒绝的事先分类。改用**片段冗余度**：同一事件下被切碎的 chunk 才是可靠的淘汰依据，且删掉的向量可从事件卡重新 embed，成本最低、完全可逆。

**流程与闸门**

```
扫描（FORGET_SWEEP_DAYS） → 三桶分流
    → NEED_REVIEW 出人工周报 → 勾「保留」升 PROTECTED / 勾「可删」并入 SAFE
    → SAFE_TO_DELETE 人工确认 → soft delete（deleted_at）
    → 30d 冷静期 → 物理清理（仅派生层）
```

P0 只实现 `dry_run`：产出三桶报告，**不写删**。人工抽检误删率 < 5% 后才开自动。物理删除永不作用于事实层，由验收项 A9 硬断言守住。

### D8 Consolidation 派生层：主题簇 / 阶段摘要（P2）

**为什么需要这一层**：话题集中时，事件卡数量大但检索区分度低，召回容易返回一堆「同阶段、同主题」的碎片。consolidation 把散落事件压成中观叙事，提纯召回、降低检索成本——这是长期记忆该有的**抽象升级**，不是分类。

**严格边界（防止与 D7 职责重叠）**：

| 层 | 负责什么 | 不负责什么 |
|---|---|---|
| D7 遗忘 | 派生片段淘汰，控存储膨胀 | 不做抽象、不做压缩叙事 |
| D8 Consolidation | 抽象、压缩、中观检索路由 | **不做淘汰判定**，不决定删谁 |

**形态**：

```
EventCard（事实层，永不删）
   ↓ 派生（可全量重建）
TopicCluster（向量聚类 + LLM 命名，label 进受控词表）
   ↓ 派生
PhaseSummary（阶段摘要，可选）
```

| 对象 | 关键字段 | 说明 |
|---|---|---|
| `TopicCluster` | `cluster_id` / `label` / `member_event_ids[]` / `period_start` / `period_end` | 成员有序，可回溯到事件卡 |
| `PhaseSummary` | `phase_id` / `narrative` / `source_cluster_ids[]` | 叙事摘要，供模型直接消费 |

**硬约束**：

1. **可全量重建**：清空派生层后从事件卡重跑，同 seed 下结果一致。
2. **不污染事实源**：聚类只读事件卡，不回写、不改卡。
3. **label 进受控词表**，禁止自由生成（与 D1 同一立场）。
4. **簇的时间窗口与 `ARCHIVE_WINDOW_DAYS` 对齐**，否则热冷边界会把同一簇切成两半。
5. **P0/P1 不上线**，触发条件由数据决定，不由日历或直觉决定。

**触发条件**（任一满足，全部可配）：事件卡数 > `CONSOLIDATE_MIN_EVENTS`、或同 `entity_key` 重复率 > `CONSOLIDATE_DUP_RATIO`、或召回 P95 > `CONSOLIDATE_LATENCY_MS`。

**检索接入**：两级路由——先查簇定范围，再查簇内事件；簇命中时直接返回 `PhaseSummary` + 代表性事件卡，减少返回碎片条数。**簇只是更快找到事件，不能替代事件卡作为回答依据。**

### D9 标签管线：分层打标，词表收敛

**问题**：D1 只规定了 `types` 用受控词表，但没说谁打标、怎么防漂移、新类型从哪来。标签是长期记忆的**第一道质量闸门**——标错了，后面的检索、过滤、遗忘全错。

**核心原则：分层打标，越靠前越确定，越靠后越谨慎。**

```
事件卡 → ① 规则/词表（确定性，零漂移）
       → ② 受控词表分类器（候选集内选择）
       → ③ LLM 兜底（候选集外，必须进复核）
       → ④ 人工校准（独立复核队列）
```

| 层 | 输入 | 输出 | 确定性 | 何时启用 |
|---|---|---|---|---|
| ① 规则层 | 附录 A 词表、敏感词 | 命中标签，置信 1.0 | 完全确定 | 每次写入 |
| ② 分类器层 | 固定候选集 `TYPES_VOCAB` | 多标签 + 分数 | 稳定 | P2 起 |
| ③ LLM 层 | 开放候选 | 新标签**提议** | 不稳定 | 每次写入兜底 |
| ④ 人工层 | 复核队列 | 采纳 / 驳回 / 新增词 | 确定 | 周度 |

**防漂移三机制**（缺一不可）

1. **候选集约束**：②③ 只能从 `TYPES_VOCAB` 或「新标签候选池」中选，**禁止自由生成直接落 `types`**。
2. **双跑一致性**：同一事件打标两次（不同温度 / 不同 prompt），结果不一致 → 进标签复核队列。
3. **词表版本化**：`TYPES_VOCAB` 带 `vocab_version`；变更记入变更记录，历史卡保留旧版本号，可追溯、可重跑。

**新类型从哪来（词表演进闭环）**

```
未命中 → ③ LLM 提议 → 新标签候选池（不直接进词表）
       → ④ 人工周度审核 → 采纳则入词表（vocab_version +1）/ 驳回
```

**硬约束**：**未经人工审核的标签不得进入 `types` 过滤条件**。否则漂移标签会污染时间/主题过滤，污染面比打标本身更大。

**与 D7 的命名隔离**：D9 的复核队列叫 `LABEL_REVIEW`，与 D7 的 `NEED_REVIEW`（遗忘复核）**是两条独立队列，禁止复用**。两者混用会导致「遗忘复核的勾选被当成标签审核」。

**P0 / P1 / P2 落地形态**

| 期 | 数据条件 | ① 规则 | ② 分类器 | ③ LLM | 说明 |
|---|---|---|---|---|---|
| **P0** | 0 条标注 | 启用 | **不启用** | 结构化输出 + 候选集约束 + 双跑校验 | 只有 ①③④，先跑通链路 |
| **P1** | 100–500 条 | 启用 | 先跑无监督主题发现看分布，人工收敛词表 | 同 P0 | 先知道「话题到底怎么分布」 |
| **P2** | 1000+ 已标注 | 启用 | 少样本多标签分类器（如 SetFit 一类），LLM 降为 OOD 兜底 | 仅处理候选集外 | 稳定性与成本最优 |

**中文前置依赖**：embedding 选型（`bge-m3` / `m3e` / `text2vec` 一类多语言或中文模型）必须在 **P1 前**定，因为它同时影响 ② 与 D8 的聚类。**embedding 选错，后面全偏**。

**工具方案已联网核实（v0.6）**

| 工具 | 核实结论 | 落地要点 |
|---|---|---|
| SetFit | 多标签为**官方原生能力** | `multi_target_strategy` 三选一：`one-vs-rest` / `multi-output` / `classifier-chain`；无需改造，哪种最优待本地实测 |
| BERTopic | 中文可用，但**必须换分词器** | 默认英文分词器按空格切，中文整句变单 token，c-TF-IDF 主题词全废；须 `CountVectorizer(tokenizer=jieba.lcut)` + 中文 embedding |
| Label Studio | `choice="multiple"` 原生多标签 | 支持分组（Topics / Moods），适合分维度打标 |
| Argilla 2.x | `MultiLabelQuestion` + `Suggestion` | 可把模型打标结果**预填**进复核界面，更贴本设计的「提议 → 确认」流 |

**仍待本地实测**：哪种 `multi_target_strategy` 最优、UMAP/HDBSCAN 参数、以及我们语料上的真实效果。

### D10 模型供给：embedding / rerank 独立可配

**原则**：模型是**可替换的基础设施**，不是架构的一部分。业务代码只依赖「推理服务抽象」，不依赖具体模型名。所有模型调用统一走 **docker 中间件**出口。

**默认选型**

| 角色 | 默认模型 | 形态 | 何时必需 |
|---|---|---|---|
| embedding | `BAAI/bge-m3` | dense 1024 维 | **P0（必需）** |
| rerank | `BAAI/bge-reranker-v2-m3` | cross-encoder | P1（P0 关闭） |

**决策依据**：bge-m3 来自主人长期知识库调优的**一手经验**——中文语义与词法匹配质量最优，优于同期 Qwen embedding 档位。此依据优先级**高于 MTEB 榜单分数**。

**部署形态（docker middleware）**

```
docker: embedding-svc  →  POST /v1/embeddings    （bge-m3，CPU 可跑，常驻 ~1.1GB）
docker: rerank-svc     →  POST /v1/rerank        （P1 启用）
```

- 模型权重**挂卷**，不烤进镜像：换模型 = 换卷 + 改配置，不重构建镜像
- CPU 模式即可：summary 短、写入低频、查询单条，无需 GPU
- 推理服务与 Weaviate 分离，向量库只存向量与元数据

**换模型的硬约束（三条，违反即静默错误）**

1. 换 `EMBEDDING_MODEL` 或 `EMBEDDING_DIM` → **必须全量重建所有向量**。不同模型的向量空间不可混用。
2. 写索引时必须校验元数据 `{embedding_model, embedding_dim}`，与当前配置不一致则**拒绝写入**（不是告警）。
3. 换 `RERANK_MODEL` → 所有**绝对阈值**（`RECALL_COUNT_MIN_SCORE`、`RECALL_EMPTY_THRESHOLD`）必须重标；相对阈值（`RECALL_DROP_RATIO` / `RECALL_GAP_RATIO`）天然免疫。

**失败降级（不对称，有意为之）**

| 服务 | 不可用时 | 理由 |
|---|---|---|
| embedding | **写入失败，返回 503** | 没有向量就不能检索，宁可拒写也不落无向量的卡 |
| rerank | 退回 RRF 分数，标 `trace.rerank_skipped=true` | 只是排序差一点，不阻断主链路 |

**建卡约束（与 embedding 质量强相关）**：`summary` 必须写**具体**——谁、做了什么、结果如何。禁止「讨论了工作相关事宜」这类抽象概括，否则向量与其它卡糊成一团。这是数据问题，换任何 embedding 都救不回来。

---

## 4. 接口契约（本轮唯一可验收物）

### 4.0 双入口总览

本服务有**两个入口**，契约分开定义：

| 入口 | 调用方 | 传输 | 认证 | 覆盖小节 |
|---|---|---|---|---|
| **MCP** | 大模型 | stdio JSON-RPC | 环境变量 `MEMORY_NAMESPACE` | 4.3、4.9 |
| **Admin** | 人 / 运维 | HTTP（绑定 `127.0.0.1`） | Bearer token | 4.2、4.4–4.8 |

**共同约定**：

| 项 | 约定 |
|---|---|
| 隔离 | `namespace` 由入口凭证**派生**（MCP=env，Admin=token 映射）；两侧业务入参**一律不接受 namespace**，传了即 400 |
| 错误体（Admin） | `{ "error": { "code": "...", "message": "..." } }` + HTTP 状态码 |
| 错误体（MCP） | JSON-RPC `error`，或 tool result 带 `isError: true`，正文同上 |
| 幂等 | 按 `(namespace, dedupe_key)` 与 `event_id` 双重幂等 |
| 代码结构 | `core/` 领域逻辑被两入口共享；**入口层只做参数转换，不许写业务** |
| 日志 | 一律落 `LOG_FILE`；**stdout 只跑协议**，stderr 仅保留致命错误一行 |

### 4.1 事件卡 EventCard

```json
{
  "event_id": "evt_20261030_0001",
  "date_start": "2026-03-01",
  "date_end": null,
  "types": ["work", "emotion"],
  "entities": [{"name": "换工作", "kind": "topic"}, {"name": "李四", "kind": "person"}],
  "summary": "主人考虑接受新 offer，顾虑通勤与团队氛围",
  "conclusion": "暂不决定，等对方补充薪资方案",
  "unresolved": ["是否接受 offer"],
  "emotion": "焦虑",
  "importance": 0.7,
  "raw_quote_ids": ["msg_0001", "msg_0007"],
  "supersedes": null,
  "retention": "normal",
  "ingested_at": "2026-10-30T10:21:14+08:00"
}
```

正式 JSON Schema 见**附录 B**，A1 验收以附录 B 为准（上方示例只用于说明，不作为校验依据）。

> 上方示例**省略了 10 个可选字段**：`observed_at`、`pinned`、`pinned_source`、`resolves`、`importance_reason`、`emotion_intensity`、`user_directive`、`recall_count`、`last_recalled_at`、`confirmed_by_use`。完整字段与类型一律以附录 B 为准。

### 4.2 管理：批量导入（历史数据）

> **LLM 的实时写入不走这里**，走 4.9 的 `memory_remember`。本接口仅用于把历史对话批量补进库。

```
POST /admin/ingest
{ "source": "chat", "session_id": "s_001", "dedupe_key": "s_001:2026-10-30",
  "turns": [{ "role": "user", "text": "...", "ts": "2026-10-30T10:21:14+08:00" }] }
→ 202 { "job_id": "job_...", "status": "queued" }
```
`event_id` 由服务端在事件切分后生成；客户端用 `dedupe_key` 防重复提交。同一 `dedupe_key` 与同一 `event_id` 均幂等。

**本接口是管理侧批量导入，不做自然语言指令解析**（v0.11：意图判断已上移给 LLM，见 C.B13）。若某批 `turns` 标注 `do_not_store`，入口直接跳过不产卡；`pinned` 只能由管理侧显式传入。

**指令拦截的响应语义**：

| 情形 | 响应 |
|---|---|
| 整批 `turns` 全部命中 `do_not_store` | `200 { "job_id": null, "status": "rejected", "reason": "do_not_store", "events_created": 0 }` |
| 部分命中 | 正常 `202`；被拦截的 turn 在 `ingest_log` 记 `skipped=do_not_store` |
| 「忘了吧」 | **不走本接口**，走 4.7 的 `DELETE /admin/event/{event_id}` |

**事件卡字段归属（客户端不可直写）**：`ingest` 只接收原文 `turns`，事件卡由服务端生成——

| 字段 | 写入方 |
|---|---|
| `event_id` / `ingested_at` | 事件切分 |
| `importance` / `importance_reason` | 事件切分（LLM，须给档位+理由，见 C.B8） |
| `user_directive` / `pinned` / `pinned_source` | `memory_remember` 参数（C.B13） |
| `resolves` | 切分时的闭环检测（C.B7） |
| `recall_count` / `last_recalled_at` | 召回回路（D7 口径） |
| `confirmed_by_use` | 4.8 反馈接口（唯一通道，C.B6） |

客户端仅可经 4.6 的 `PATCH` 修改 `pinned` / `retention` 两个白名单字段。

### 4.3 MCP 工具：`memory_recall`

| 项 | 值 |
|---|---|
| tool 名 | `memory_recall` |
| namespace | 取自环境变量 `MEMORY_NAMESPACE` |
| 输入 | `query` 必填；`now` / `token_budget` / `top_k` / `budget_ms` 可选 |
| 输出 | 下述 JSON 作为 tool result 的 `content[0].text` |

```
memory_recall({ "query": "上次说换工作那事后来怎样了",
                "now": "2026-10-30T10:21:14+08:00",
                "token_budget": 3000,
                "top_k": null,
                "budget_ms": 1500 })
→ tool result
{ "time_intent": { "kind": "recent", "ranges": [["2026-04-30","2026-10-30"]] },
  "materials": [{
    "event_id": "evt_...", "date_start": "2026-03-01", "date_end": null,
    "summary": "...", "raw_quote_ids": ["msg_0001"],
    "types": ["work"], "score": 0.87, "freshness": 0.92,
    "possibly_stale": false, "source": "chat" }],
  "trace": { "vector_ms": 120, "graph_ms": 45, "fusion_ms": 8,
             "rerank_ms": 210, "rerank_skipped": false,
             "degraded": false, "partial": false, "overflow": false,
             "top_k_decision": { "k": 7, "reason": "score_gap",
                                 "score_gap_at": 7, "token_used": 1180 } } }
```

| 字段 | 语义 |
|---|---|
| `token_budget` | 动态 K 主驱动，缺省取 `CONTEXT_TOKEN_BUDGET` |
| `top_k` | 调试用硬上限覆盖；`null` 表示走动态截断 |
| `trace.top_k_decision.reason` | `score_gap` / `token_budget` / `top_k_cap` / `overflow` 之一 |
| `trace.overflow` | 强制保留项突破上限时为 true，此时 `k` 可大于 `RECALL_TOP_K_MAX` |
| `trace.empty_reason` | 空结果时为 `no_candidate` / `below_threshold`，非空结果为 null |
| `trace.rerank_ms` | rerank 实际耗时；未启用或降级时为 0 |
| `trace.rerank_skipped` | rerank 服务不可用/超时时为 true，此时 `score` 为 RRF 分数而非语义分 |

### 4.4 管理：归档

```
POST /admin/archive
{ "window_days": 180, "dry_run": true }
→ 200 { "window_days": 180, "candidates": 1234, "moved": 0, "mode": "logical" }
```
`dry_run` 默认 `true`；P0 只返回候选，不搬迁。

### 4.5 管理：标签复核

```
GET /admin/label/queue?status=pending&limit=50
→ 200 { "items": [{ "event_id": "evt_...", "summary": "...",
                    "suggested_types": ["work"],
                    "reason": "dual_run_mismatch | no_type_hit | new_type_proposed",
                    "vocab_version": 3 }] }

POST /admin/label/review
{ "event_id": "evt_...", "decision": "accept | reject | new_type",
  "types": ["work", "emotion"], "new_type": null, "reviewer": "owner" }
→ 200 { "event_id": "evt_...", "types": ["work", "emotion"],
        "vocab_version": 3, "queued": false }

GET /admin/label/vocab?version=3
→ 200 { "vocab_version": 3, "types": ["work", "emotion", "..."],
        "changed_at": "2026-10-30T12:00:00+08:00" }
```

| 字段 | 语义 |
|---|---|
| `reason` | `dual_run_mismatch` / `no_type_hit` / `new_type_proposed`，P0 至少支持前两种 |
| `decision=accept` | 把 `suggested_types` 落进 `types` |
| `decision=new_type` | 触发词表变更，`vocab_version` +1，需二次确认 |
| `reviewer` | 记录审核人，审计留痕 |

### 4.6 管理：事件与复核

```
PATCH /admin/event/{event_id}
{ "pinned": true }
→ 200 { "event_id": "evt_...", "pinned": true,
        "pinned_source": "manual", "updated_at": "2026-10-30T12:00:00+08:00" }
```

| 约束 | 说明 |
|---|---|
| 可改字段白名单 | 仅 `pinned` / `retention`；改 `summary` / `types` 必须走标签复核或重建 |
| 幂等 | 重复置 `true` 返回 200 且 `updated_at` 不变 |
| 错误 | `404` event_id 不存在；`400` 试图改白名单外字段 |

```
GET /admin/review?bucket=NEED_REVIEW&limit=50&cursor=...
→ 200 { "items": [{ "event_id": "evt_...", "summary": "...",
                    "bucket": "NEED_REVIEW",
                    "reason": "importance_high | chunk_redundant",
                    "importance": 0.82, "chunk_count": 3,
                    "idle_days": 210, "recall_count": 0 }],
        "next_cursor": null }
```

- `bucket` 枚举：`NEED_REVIEW` / `SAFE_TO_DELETE`（P0 只出报告，不写删）
- **与 D9 的 `/label/queue` 是两条独立队列，禁止混用**（D9 硬约束，A24 验收）

```
POST /admin/review/{event_id}/resolve
{ "action": "keep | delete", "reviewer": "owner", "note": null }
→ 200 { "event_id": "evt_...", "bucket_before": "NEED_REVIEW",
        "bucket_after": "PROTECTED | SAFE_TO_DELETE", "deleted": false }
```

- `keep` → 升 `PROTECTED`，等价 `pinned_source=manual`，记录 `reviewer`
- `delete` → 并入 `SAFE_TO_DELETE`，**不立即删**；真正删除走 4.7
- 幂等：同 `action` 重复提交不改变状态

### 4.7 管理：遗忘与扫描

```
POST /admin/forget/sweep
{ "dry_run": true }
→ 200 { "dry_run": true, "scanned": 12345,
        "buckets": { "PROTECTED": 800, "NEED_REVIEW": 120, "SAFE_TO_DELETE": 45 },
        "candidates": [{ "event_id": "evt_...", "reason": "idle",
                         "idle_days": 210, "recall_count": 0 }],
        "deleted": 0 }
```

- P0 强制 `dry_run=true`；传 `false` 返回 **400**（D7：人工抽检误删率 < 5% 才开自动）
- **与 4.4 `archive` 职责不同**：`archive` 是热冷逻辑分层（按 `window_days`），`forget` 是淘汰三桶分流（按空闲 + 召回收）——两者不可混用
- 三桶数量之和 = `scanned`

```
DELETE /admin/event/{event_id}
{ "reason": "user_forget | retention_expired", "hard": false }
→ 200 { "event_id": "evt_...", "soft_deleted": true,
        "raw_quote_preserved": true, "deleted_at": "2026-10-30T12:00:00+08:00" }
```

- **只允许软删**（`deleted_at` 落派生层）；`hard=true` 返回 **400**——事实层永不物理删（D7 + A9）
- 软删后：向量移出索引、`/recall` 检索不到，但 `GET /raw/{quote_id}` **仍可溯源**（A32）
- 幂等：重复删除返回 200 且 `deleted_at` 不变
- 「忘了吧」指令走本接口（C.B13）

### 4.8 管理：原文读取

```
GET /admin/raw/{quote_id}
→ 200 { "quote_id": "msg_0001", "text": "...", "role": "user",
        "ts": "2026-10-30T10:21:14+08:00", "source": "chat",
        "event_ids": ["evt_..."] }
```

- 事实层原文，**即使其事件卡已软删也仍可读**（A32 溯源依赖此点）
- `404` 表示 quote_id 不存在
- 引用反馈在 MCP 侧，见 4.9 的 `memory_feedback`

### 4.9 MCP 工具总表（LLM 侧）

| tool | 用途 | 定义位置 |
|---|---|---|
| `memory_recall` | 召回相关记忆 | 4.3 |
| `memory_remember` | 写入一条记忆 | 本节 |
| `memory_forget` | 遗忘（软删） | 本节 |
| `memory_feedback` | 回报本次引用了哪些材料 | 本节 |

**`memory_remember`**

```
memory_remember({ "summary": "主人上个月决定接受新 offer，顾虑通勤",
                  "date_hint": "上个月",
                  "pinned": false,
                  "important": false,
                  "emotion_intensity": 0.0 })
→ tool result { "event_id": "evt_...", "created": true }
```

| 参数 | 语义 |
|---|---|
| `summary` | **必填**，事件摘要：谁做了什么、结果如何（禁止抽象概括，见 D1） |
| `date_hint` | 自然语言时间提示，服务端走 time_resolver（C.B9） |
| `pinned` | 由 **LLM 判断**是否属于「明确要求记住」；服务端**不解析自然语言**（C.B13 已瘦身） |
| `important` | 重要性宣告；`importance` 兜底至 `max(x, EXPLICIT_IMPORTANT_FLOOR)` |
| `emotion_intensity` | 0–1；只参与 `EMOTION_BOOST` 加成，**永不构成保护**（C.B13） |

**`memory_forget`**

```
memory_forget({ "event_id": "evt_...", "reason": "user_forget" })
→ tool result { "event_id": "evt_...", "soft_deleted": true,
                "raw_quote_preserved": true }
```

- 只做**软删**；事实层原文保留（A9 / A32）
- `reason` 枚举：`user_forget` / `outdated`

**`memory_feedback`**

```
memory_feedback({ "trace_id": "tr_...", "used_material_ids": ["evt_..."] })
→ tool result { "accepted": 1, "deduped": 0,
                "confirmed_by_use_incremented": ["evt_..."] }
```

- **幂等键 `(trace_id, event_id)`**；重复上报计入 `deduped`，不累加
- 只接受该 `trace_id` 实际返回过的 `event_id`，否则 `isError: true`
- `confirmed_by_use` 的**唯一写入通道**（C.B6 明令禁止自动判定）

### 4.10 工具使用指南（LLM 侧）

MCP 的 `initialize` 响应可带 `instructions` 字段，运行时直接投给 LLM——这是教模型**何时调、怎么填参数**的官方位置。

| 约定 | 内容 |
|---|---|
| **单一源头** | [`docs/memory-tool-guide.md`](./memory-tool-guide.md)，构建时生成 `instructions`，代码里**不另写一份** |
| **一致性校验** | `harness/check-guide.py` 比对「工具 inputSchema」与「指南文档」，不一致即非零退出 |
| **定时** | 由用户 crontab 驱动，表达式放 `.env` 的 `GUIDE_CHECK_CRON`；**不内置调度器**（省一个守护进程） |
| **失败可见** | 校验失败写日志并非零退出，由 cron 的日志/邮件暴露 |

> 为什么值得单独定：MCP 形态下工具调不调、参数填得对不对，**全靠这段文本**。它和代码一样需要版本管理与回归校验。

---

## 5. 验收标准（二值判定）

数据未到位，先用**合成夹具 + 契约测试**建立基线；真实数据到位后补测并回填基线值。

| ID | 验收项 | 通过条件 |
|---|---|---|
| A1 | `memory_recall` 契约 | tool result 通过 JSON Schema 校验，缺字段即失败 |
| A2 | 时间锚定 | 「最近三个月」召回事件的时间区间与查询区间**有交集**（30 条合成夹具，含 `date_end=null` 的持续事件） |
| A3 | 图故障降级 | kill Neo4j 后 `memory_recall` 正常返回，`degraded=true`，单次耗时 ≤ 1200ms |
| A4 | 窗口可配 | `ARCHIVE_WINDOW_DAYS=90` 与 `=365` 下，同一批事件的冷集/热集划分不同且符合 D1 定义 |
| A5 | 溯源 | 对每条 material 的每个 `raw_quote_ids` 调用 `GET /admin/raw/{quote_id}`，均返回 200 且 `text` 非空 |
| A6 | 冲突不合并 | 同实体两版本事实返回 2 条、各带时间戳、不合并 |
| A7 | 写入幂等 | 连续两次 `memory_remember` 相同内容，库内仅 1 条（`event_id` 相同）；管理侧同 `dedupe_key` 重放同样幂等 |
| A8 | 图可重建 | 清空 Neo4j 后从事件卡重建，`Event` 节点覆盖 Weaviate 全部 `event_id`（100%），`weaviate_id` 双向可解析 |
| A9 | 事实层零误删 | 遗忘扫描前后，EventCard 与 `raw_quote` 的条数与内容哈希完全一致 |
| A10 | 硬信号豁免 | 构造 5 组夹具逐一命中五类硬信号，均不出现在 `SAFE_TO_DELETE` 桶 |
| A11 | 新条目保护 | 创建 ≤ `FORGET_GRACE_DAYS` 且 `recall_count=0` 的片段，不进入任何删除桶 |
| A12 | 误删率闸门 | `POST /admin/forget/sweep` 的 `dry_run` 报告人工抽检 100 条，误删候选率 < 5% 才允许开自动；P0 传 `dry_run=false` 返回 **400** |
| A13 | 召回计数口径 | 仅进候选池、未进 rerank 后 top-K 的片段，`recall_count` 不增加 |
| A14 | 派生层可重建 | 清空全部向量片段后从事件卡重新 embed，20 条固定 query 的 top-K `event_id` 覆盖率 100%（顺序可不同） |
| A15 | consolidation 不污染事实源 | 跑完聚类后事件卡条数与内容哈希不变；清空派生层后 `/recall` 仍返回 200 |
| A16 | 冗余片段去重 | 同 `event_id` 下 chunk 数 > `CHUNK_KEEP_PER_EVENT` 时，仅保留 rerank 最高分的 N 个进检索引擎 |
| A17 | 动态截断可解释 | 任意 query 的响应含 `trace.top_k_decision`，`k` 落在 [`RECALL_TOP_K_MIN`, `RECALL_TOP_K_MAX`] 内（或 `overflow=true`），`reason` 为四个合法值之一 |
| A18 | 保护条目溢出 | 构造 40 条 `pinned` 夹具，返回 `k > RECALL_TOP_K_MAX` 且 `overflow=true`，且全部 pinned 条目在结果中 |
| A19 | 计数分数门槛 | `score < RECALL_COUNT_MIN_SCORE` 的 material 即使被返回，`recall_count` 也不增加 |
| A20 | 空结果显式化 | 无相关记忆的 query 返回 200、`materials=[]`、`trace.empty_reason` 非空，且 prompt 注入「无相关记忆」标记；不得用低分材料凑数 |
| A21 | 候选集约束 | LLM 输出含词表外标签时，写入必须失败或转候选池，`types` 中**不出现**未审核标签 |
| A22 | 双跑一致性 | 同事件双跑一致则直接落库；不一致则进 `LABEL_REVIEW` 且 `types` 暂不写入 |
| A23 | 词表版本化 | 词表变更后 `vocab_version` 递增，历史卡保留旧版本号，`GET /label/vocab` 可按版本回溯 |
| A24 | 复核队列隔离 | `LABEL_REVIEW` 与 D7 的 `NEED_REVIEW` 数据互不串流：审核标签不改变遗忘桶归属，反之亦然 |
| A25 | 换模型拒绝写入 | 把 `EMBEDDING_MODEL` 改为另一模型后向旧索引写入，返回**错误**（非告警），且索引未被污染 |
| A26 | embedding 不可用 | kill embedding 服务，写入返回 **503**，且不产生无向量的卡 |
| A27 | rerank 降级 | kill rerank 服务，`POST /admin/recall` 仍返回 200，`trace.rerank_skipped=true`、`trace.rerank_ms=0`，材料集非空 |
| A28 | 时效不主导 | 一条 3 年前高相关（rel=0.95）与一条昨天低相关（rel=0.5）同场检索，前者 `final` 更高或至少入选 |
| A29 | 有效引用回路 | 调用 `memory_feedback` 上报 `used_material_ids` 后 `confirmed_by_use` +1；同 `trace_id` 重复上报计入 `deduped` 且不累加 |
| A30 | 双跑阈值 | `types` Jaccard=0.85 直接落库；=0.75 进 `LABEL_REVIEW` 且 `types_pending=true` |
| A31 | 显式记住生效 | LLM 调 `memory_remember(pinned=true)` 后，该卡 `pinned=true` 且 `pinned_source=user_directive`，且 `GET /admin/review?bucket=NEED_REVIEW` 中**不出现**该卡 |
| A32 | 遗忘通道对称 | 调用 `memory_forget` 后 `memory_recall` 检索不到该卡，但 `GET /admin/raw/{quote_id}` 仍返回 200 且 `raw_quote_preserved=true` |
| A33 | 指令滥用降级 | 24h 内第 21 次「记住」不再自动 pin，降级为 `explicit_important` 并记 `anomaly=directive_abuse` |
| A34 | 越权隔离 | 以 `MEMORY_NAMESPACE=A` 启动的 MCP 进程调 `memory_recall`，检索不到 namespace=B 的卡；入参若携带 `namespace` 返回 `isError` |

---

## 6. 风险与取舍（双库的真实代价）

| 风险 | 影响 | 缓解 |
|---|---|---|
| 双写漂移 | 图与向量不一致，召回自相矛盾 | Weaviate 为事实源；outbox + 幂等 + 定时对账；图可全量重建 |
| 图遍历爆炸 | 延迟抖动，P99 失控 | 跳数上限 2、节点度数上限、200ms 超时直接丢弃 |
| 标签漂移 | 半年后同类事件标签不一致 | 写入时打标 + 受控词表 `TYPES_VOCAB` + 归档**不重分类** |
| 摘要失真 | 事件卡丢细节，模型答偏 | 卡片与原文双层；材料始终附 `raw_quote_ids` |
| 无真实数据 | 验收结论不可外推 | 合成夹具 + 契约测试打底；真实数据到位后补基线并回填 |
| 运维复杂度 | 两套 schema / 查询 / 监控 | 图侧只保留最小能力集；监控只盯「图降级率」与「对账差异数」 |

**取舍声明**：第一版上双库而非单库，代价是周期与调试面变大。缓解手段是把图严格限制为**可丢弃的派生索引**——任何图侧故障都退化为「召回质量下降」，而不是「服务不可用」。

---

## 7. 分期

| 期 | 范围 | 出口条件 |
|---|---|---|
| **P0** | embedding docker 中间件（bge-m3）、namespace 隔离与 API Key 认证、事件卡写入、指令解析、标签管线（规则 + LLM + 双跑 + 复核队列）、时间解析、并行召回（双库）、动态 K、空结果显式化、遗忘 `dry_run` 报告、13 个接口契约、合成夹具 | A1–A13、A17–A19、A20–A26、A28–A34 全绿（30 条） |
| **P1** | rerank 中间件接入与绝对阈值重标、图重建作业、热冷物理拆分（按阈值触发）、主题发现 | A14–A16、A27 全绿；冷查询 P95 ≤ 目标 |
| **P2** | consolidation 摘要层、标签分类器（少样本）、人工复核 UI 与自动淘汰开启、多用户隔离 | 待定项收敛后评审 |

---

## 8. 待定项

> v0.10 已定多 agent 域隔离（`namespace`）与原文存储（SQLite / PG 可配），下列仅剩**真正需要外部信息或实测定夺**的项。

- [ ] P1 是否真的做物理冷热拆分（等数据量与延迟数据）
- [ ] `types` 受控词表的初版清单
- [ ] 不可逆事件词表需按个人经历校准（附录 A 是通用初版，私人语义须另测）
- [ ] `recall_count` 的落点（Weaviate 字段 vs 独立计数表，涉及写放大与事务）
- [ ] 动态 K 对下游 prompt 稳定性的影响（是否需要「至少包含 N 条」以保证叙事连贯）
- [ ] SetFit 三种 `multi_target_strategy` 在自有语料上哪种最优
- [ ] BERTopic 中文参数（UMAP / HDBSCAN）在自有语料上的实测调优

---

## 附录 A：不可逆事件词表 v0.1

> 命中即进 `PROTECTED`，不参与任何淘汰。词表是**受控枚举**，禁止自由生成。

| 类别 | 标签 |
|---|---|
| 健康 | 手术、住院、确诊、怀孕、流产、慢性病 |
| 关系 | 结婚、离婚、分手、亲人去世、搬家 |
| 职业 | 离职、入职、创业、被裁、转行 |
| 财务 | 买房、卖房、大额投资、负债、借贷 |
| 教育 | 入学、毕业、考试、录取、退学 |
| 法律 | 诉讼、签约、签证、移民 |

**维护规则**：每季度人工校准一次；新增词条必须附带一句「为什么不可逆」，否则拒绝入库；词表变更记入第 9 节。

---

## 附录 B：EventCard JSON Schema（draft-07）

> A1 验收以本 schema 为准。`format` 需校验器启用（date / date-time）。

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "EventCard",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "namespace", "event_id", "date_start", "types", "entities",
    "summary", "retention", "ingested_at"
  ],
  "properties": {
    "namespace":  { "type": "string", "minLength": 1,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$" },
    "event_id":   { "type": "string", "pattern": "^evt_[0-9a-f]{16}$" },
    "date_start": { "type": "string", "format": "date" },
    "date_end":   { "type": ["string", "null"], "format": "date" },
    "types":      { "type": "array", "minItems": 1, "items": { "type": "string" } },
    "entities": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "kind"],
        "properties": {
          "name": { "type": "string", "minLength": 1 },
          "kind": { "type": "string",
                    "enum": ["person", "topic", "place", "org", "event"] }
        }
      }
    },
    "summary":          { "type": "string", "minLength": 1 },
    "conclusion":       { "type": ["string", "null"] },
    "unresolved":       { "type": "array", "items": { "type": "string" } },
    "resolves":         { "type": "array", "items": { "type": "string", "pattern": "^evt_[0-9a-f]{16}$" } },
    "emotion":          { "type": ["string", "null"] },
    "emotion_intensity":{ "type": ["number", "null"], "minimum": 0, "maximum": 1 },
    "importance":       { "type": "number", "minimum": 0, "maximum": 1 },
    "importance_reason":{ "type": ["string", "null"] },
    "user_directive":   { "type": ["string", "null"],
                          "enum": ["remember", "important", null] },
    "raw_quote_ids":    { "type": "array", "items": { "type": "string" } },
    "supersedes":       { "type": ["string", "null"] },
    "observed_at":      { "type": ["string", "null"], "format": "date-time" },
    "retention":        { "type": "string", "enum": ["normal", "sensitive"] },
    "ingested_at":      { "type": "string", "format": "date-time" },
    "pinned":           { "type": "boolean" },
    "pinned_source":    { "type": ["string", "null"],
                          "enum": ["manual", "user_directive", null] },
    "recall_count":     { "type": "integer", "minimum": 0 },
    "last_recalled_at": { "type": ["string", "null"], "format": "date-time" },
    "confirmed_by_use": { "type": "integer", "minimum": 0 }
  }
}
```

**说明**：`do_not_store` 不在 enum 内——它在写入入口拦截，不会产生卡，因此没有合法实例需要校验。

---

## 附录 C：判定方法定义（B1–B12）

> 本附录补齐 v0.6 之前「有承诺、无方法」的 12 处判定点。**所有条目均可被代码直接实现，不含「酌情」「视情况」这类不可执行措辞。**

| 编号 | 判定点 | 对应决策 | 关键配置 |
|---|---|---|---|
| B1 | 时效与最终排序公式 | D5 / D3 | `RECENCY_HALF_LIFE_DAYS`、`W_TIME_BASE`、`W_IMP_BASE` |
| B2 | token 计数口径 | D3 | `TOKENIZER` |
| B3 | 实体消歧算法 | D2 | `ENTITY_ALIAS_SIM` |
| B4 | RRF 各通道权重 | D3 | （默认等权，无配置） |
| B5 | `pinned` / `NEED_REVIEW` 写接口 | D7 | — |
| B6 | `confirmed_by_use` 回写与「有效引用」 | D7 | `CONFIRMED_MIN` |
| B7 | `unresolved` 闭环判定 | D7 | — |
| B8 | `importance` 评分 rubrics | D7 | `PROTECT_IMPORTANCE_REVIEW` |
| B9 | 时间解析冲突合并 | D4 | `TIME_CONF_MIN` |
| B10 | 双跑一致性判定 | D9 | `LABEL_DUAL_RUN_JACCARD` |
| B11 | 聚类算法与参数 | D8 | `CLUSTER_MIN_SIZE`、`CLUSTER_MERGE_SIM` |
| B12 | 冷查询 P95 目标 | P1 出口 | `COLD_QUERY_P95_MS` |

### C.B1 时效与最终排序公式

**问题**：D5 说「按 recency 加权」，D3 说「rerank（相关性 × 时效 × importance）」，但没给公式。纯乘法会让任一项为 0 时整条归零，且时效会主导排序——「一年一次的不可逆事件」永远被昨天的小事压过。

```
rel        = rerank 归一化分数 ∈ [0,1]
freshness  = 2 ** (-Δdays / RECENCY_HALF_LIFE_DAYS)     # Δdays = now - date_end（持续中取 date_start）
imp_factor = W_IMP_BASE + (1 - W_IMP_BASE) * importance # importance ∈ [0,1]

final = rel × (W_TIME_BASE + (1 - W_TIME_BASE) × freshness) × imp_factor
```

- `W_TIME_BASE = 0.7`：时效**最多影响 30%**，不主导排序
- `W_IMP_BASE = 0.8`：importance **最多影响 20%**
- **`pinned` / `unresolved` 非空 / 不可逆事件不参与衰减**，按 D3「强制保留」直接置顶
- 各因子归一化到 `[0,1]`，`final` 仍在 `[0,1]`，可直接供断崖检测使用

**为什么不用纯乘法**：纯乘法下旧事件即使 `rel=1.0` 也会被 `rel=0.6` 的新事件压过，长期记忆退化成短期记忆。加保底项后，**相关性仍是主因子**，时效只做修正。

**验收**：A28。

### C.B2 token 计数口径

**定义**：按**目标对话模型的 tokenizer** 计。P0 用 `tiktoken` 的 `cl100k_base` 近似（与 DeepSeek 系偏差 ±10% 内可接受）。

```
tokens(text) = cl100k_base(text)
降级估算（无 tiktoken 时）：ceil(字符数 / 1.5)
逐条累计：sum(tokens(card.summary + raw_quote_snippet))
```

**为什么不用字符数**：中英混排下字符数与 token 数非线性，「3000 字符」可能对应 1200 也可能 3500 token，预算会失控。

### C.B3 实体消歧算法

**三级判定，优先级从高到低**：

| 级别 | 方法 | 采用条件 |
|---|---|---|
| ① | 人工别名表精确匹配 | 命中即采用，**最高优先** |
| ② | 向量近邻（实体名 embedding） | 余弦 `≥ ENTITY_ALIAS_SIM(0.92)` |
| ③ | LLM 判定 | 仅当 ② 落在 `[0.85, 0.92)` 灰区 |

- ① 与 ②③ 冲突 → **① 赢**
- ② 与 ③ 冲突 → 取 ②，标 `disambiguation=low_confidence`
- 新实体**不自动并入**已有节点，创建新节点 + 记 `candidate_alias` 进人工确认池

**为什么不让 LLM 直接判**：LLM 对「同名不同人」的判断不稳定，且不留审计痕迹；别名表是可复查的事实，模型推理不是。

### C.B4 RRF 各通道权重

**定义**：默认**等权**（RRF 标准做法，`k=60`），不引入手工权重。

- 某通道失败 → **直接移除该路**，不补偿、不放大其他路
- 理由：RRF 的设计前提正是「不需要知道各路可靠度」；若某路挂掉就把权重分给其他路，单通道故障会被放大成系统性偏差
- 仅当 P2 观测到某路**系统性偏噪**，才引入 `RRF_WEIGHT_{GRAPH,VECTOR,BM25}`（默认全 `1.0`）

### C.B5 `pinned` / `NEED_REVIEW` 写接口

**问题**：D7 五类硬信号里，`pinned` 与 `NEED_REVIEW` 勾选**完全依赖人工通道**，没有接口它们永远为 `false` / 空桶，A12/A13 无法验收。

| 接口 | 方法 | 体 |
|---|---|---|
| `PATCH /admin/event/{event_id}` | PATCH | `{ "pinned": true }`（幂等） |
| `GET /admin/review?bucket=NEED_REVIEW&limit=50` | GET | — |
| `POST /admin/review/{event_id}/resolve` | POST | `{ "action": "keep" \| "delete" }` |

### C.B6 `confirmed_by_use` 回写与「有效引用」

**定义**：**调用方显式上报**，系统不猜。

```
POST /admin/recall/{trace_id}/feedback
{ "used_material_ids": ["evt_...", "evt_..."] }
→ 对每个 id：confirmed_by_use += 1
```

- **幂等**：以 `(trace_id, event_id)` 为去重键，重复上报只计一次
- **为什么不能自动判定**：模型「提到」某材料 ≠「用了」它；系统无法可靠区分「引用」与「复述」。自动判定会把噪声当信号，直接污染 A13 的保护逻辑

### C.B7 `unresolved` 闭环判定

**只有两种方式置空**：

1. 后续事件卡带 `resolves: [event_id]` 显式指向
2. 人工接口标记

**不做语义自动闭环**：LLM 判断「这事办完了吗」可靠性不足，误判会让未闭环事项失去保护——而这正是最不该丢的一类记忆。

### C.B8 `importance` 评分 rubrics

| 档位 | 含义 | 示例锚点 |
|---|---|---|
| 0.9–1.0 | 不可逆 / 不可替代 | 手术、离职、亲人去世 |
| 0.7–0.9 | 需长期跟踪的进行中事项 | 求职中、治疗中、项目推进中 |
| 0.4–0.7 | 一般日常 | 一次普通聚餐、一次短途出行 |
| 0.0–0.4 | 琐碎 / 一次性信息 | 天气闲聊、临时问路 |

- 写入时 LLM 必须给出**档位 + 一句理由**，理由存 `importance_reason`（便于事后审计）
- **importance 仍只进 `NEED_REVIEW` 桶**，不自动豁免（D7 立场不变）

### C.B9 时间解析冲突合并

规则区间 `R`、LLM 区间 `L`：

| 情形 | 处理 |
|---|---|
| 有交集 | 取**交集** |
| 无交集 | 取 `R`，标 `time_conflict=true`，`confidence` 降为 `min(conf_R, conf_L) × 0.5` |
| `confidence < TIME_CONF_MIN(0.5)` | **不设 date filter**（退回全库），标 `time_unresolved=true` |

**为什么规则优先**：规则可复现、可审计；LLM 只负责补规则覆盖不到的模糊表达。

### C.B10 双跑一致性判定

```
一致 ⟺ Jaccard(types_A, types_B) ≥ LABEL_DUAL_RUN_JACCARD(0.8)
     且 Jaccard(entities_A, entities_B) ≥ 0.6
```

- 不一致 → 进 `LABEL_REVIEW`，**`types` 暂不写入**，标 `types_pending=true`
- **为什么用 Jaccard 而非完全相等**：完全相等过于严格，标签顺序差异或词表统一前的近义写法会造成大量伪不一致；Jaccard 只关心集合重叠

### C.B11 聚类算法与参数

**HDBSCAN**（不预设簇数——「经历了几段」本身是未知量，KMeans 要预设簇数，方向反了）：

```
CLUSTER_MIN_SIZE = 15
metric = cosine
cluster_selection_epsilon = 0.05
簇合并：两簇质心余弦 ≥ CLUSTER_MERGE_SIM(0.85)
噪声点（label = -1）→ 归入「未归簇」，不强行分配
```

### C.B12 冷查询 P95 目标

`COLD_QUERY_P95_MS = 800`，与 `VECTOR_TIMEOUT_MS` 对齐——冷查询不应比单通道超时还慢。

- 超此值 → 触发 D8 的 consolidation 条件之一
- 为什么是 800：D3 整体预算 1500ms，冷查询占一半是上限；再慢说明物理拆分或聚类缺失

### C.B13 语言信号：从「服务端解析」改为「LLM 填参数」

> **v0.11 变更**：本项目改为 MCP 工具形态后，自然语言意图由 **LLM 判断**，服务端不再用正则解析。

**问题**：用户语言里藏着比 LLM 自评更可靠的记忆信号。但捕获位置变了——不是服务端解析文本，而是 **LLM 调 `memory_remember` 时填对应参数**。

**三类信号 → 三个工具参数**

| 类型 | 用户怎么说 | LLM 填什么 | 服务端落点 | 是否保护 |
|---|---|---|---|---|
| 显式记忆指令 | 「帮我记一下」「记住」 | `pinned=true` | `pinned_source=user_directive`、`user_directive=remember` | ✅ 进 PROTECTED |
| 重要性宣告 | 「这很重要」「绝不能忘」 | `important=true` | `importance=max(x, EXPLICIT_IMPORTANT_FLOOR)` | ⚠️ 仅进 NEED_REVIEW |
| 情绪强度 | 「气死我了」 | `emotion_intensity: 0–1` | `importance += EMOTION_BOOST × x`（封顶 1.0） | ❌ 不保护 |

**为什么把判断上移给 LLM**：MCP 形态下 LLM 本就理解用户意图，服务端再正则解析属于重复劳动，且正则永远覆盖不全。**但服务端保留全部校验**，不信任 LLM 的自觉。

**服务端仍必须做的四件事**

| 校验 | 规则 |
|---|---|
| 参数类型 | `pinned` / `important` 必须布尔；`emotion_intensity` 必须 0–1，越界返回 `isError` |
| 保护语义 | 只有 `pinned=true` 进 PROTECTED；`important` 与 `emotion` **永不**升 PROTECTED |
| 滥用护栏 | 见下 |
| `llm_inferred` 禁令 | `pinned_source` 不接受 `llm_inferred`（D7 硬约束） |

**护栏（防滥用）**

```
同一 namespace 24h 内 pinned=true 次数 > USER_DIRECTIVE_DAILY_MAX(=20)
  → 降级：pinned 改为 false，important 置 true
  → 记 anomaly=directive_abuse
```

理由：若「记住」可无限使用，`PROTECTED` 桶会被灌爆，保护机制自我废止。

**作用域**：由 LLM 决定的 `summary` 边界即作用域。原 `DIRECTIVE_SCOPE_WINDOW` / `DIRECTIVE_PENDING_TTL` 两项配置**废弃**（服务端不再做指令绑定）。

**反向指令（对称设计）**

| 表达 | LLM 应做的 | 服务端落点 |
|---|---|---|
| 「这个别记」「不用记」 | **不调用** `memory_remember` | 无卡产生 |
| 「忘了吧」「删掉那条」 | 调 `memory_forget` | 软删 + 移出索引；事实层**不物理删**（D7） |

**没有「忘」的通道，「记」的通道迟早没人用**——若「忘了吧」无响应，用户不会再相信「帮我记」。

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v0.1 | 2026-10-30 | 初稿：确认单 collection + 时间分层、双库单向派生、并行召回、契约与验收标准 | 鱼鱼 |
| v0.2 | 2026-10-30 | 新增 D7 遗忘策略（LRU 限于派生层、三桶分流、五类硬信号、importance 降级为复核）；N4 改写；补遗忘配置项、附录 A 词表、A9–A13 验收 | 鱼鱼 |
| v0.3 | 2026-10-30 | 移除「主题稀有性」信号（话题集中时恒定失效），淘汰依据改为片段冗余度；新增 D8 consolidation 派生层（P2、数据触发、可重建、不参与淘汰）；补 A14–A16 验收 | 鱼鱼 |
| v0.4 | 2026-10-30 | D3 引入动态 top-K：token 预算主驱动 + 断崖检测 + 强制保留溢出 + 可审计 trace；`RECALL_TOP_K` 拆为 MIN/MAX 等 5 项配置；`/recall` 契约改 `token_budget` 驱动；补 A17–A19 验收 | 鱼鱼 |
| v0.4.1 | 2026-10-30 | 修 6 处自相矛盾（entities 类型、observed_at 缺失、deleted_at 归属、do_not_store、版本号、分期）；补 3 条地基（event_id 生成规则、JSON Schema 附录 B、空结果显式化 D3）；补 A20 验收；分期与 P0 出口对齐 | 鱼鱼 |
| v0.5 | 2026-10-30 | 新增 D9 标签管线（四层打标、防漂移三机制、词表演进闭环、与 D7 队列隔离、P0/P1/P2 形态）；新增 4.5 标签复核接口、3 项配置、A21–A24 验收；分期表同步 | 鱼鱼 |
| v0.6 | 2026-10-30 | 新增 D10 模型供给（embedding/rerank 独立可配、docker 中间件、bge-m3 默认、换模型三硬约束、不对称降级）；D6 补 10 项模型配置；D1 补 summary 具体性约束；D9 待实测项改为联网核实结论；补 A25–A27 验收 | 鱼鱼 |
| v0.7 | 2026-10-30 | 新增附录 C「判定方法定义」B1–B12，一次清完 B 级：时效最终排序公式、token 口径、实体消歧三级、RRF 等权、三个遗忘写接口、有效引用回路、unresolved 闭环、importance rubrics、时间冲突合并、双跑 Jaccard、HDBSCAN 参数、冷查询目标；D6 补 10 项配置；附录 B 补 `resolves` 字段；补 A28–A30；待定项收缩至 9 条 | 鱼鱼 |
| v0.8 | 2026-10-31 | 新增 C.B13「用户指令解析与作用域绑定」：三类语言信号分级（显式记住→pin、重要性宣告→importance 下限、情绪→仅加成不保护）、作用域绑定、滥用护栏、反向指令对称；D7 `pinned_source` 扩为 manual/user_directive 且禁 llm_inferred；4.2 写入加指令解析前置；D6 补 5 项配置；附录 B 补 4 字段（含 v0.7 遗漏的 `importance_reason`）；补 A31–A33 | 鱼鱼 |
| v0.9 | 2026-10-31 | 全文契约审计，补齐 12 处「附录/正文有承诺、第 4 节无契约」：新增 4.6（置 pin / 复核列表 / 复核裁决）、4.7（遗忘扫描 `dry_run` / 软删除）、4.8（引用反馈 / 原文读取）；4.3 补 `trace.rerank_ms` + `trace.rerank_skipped`；4.2 补指令拦截响应语义；4.1 标注示例省略 10 个可选字段；A5/A12/A27/A29/A31/A32 改为对具体接口断言 | 鱼鱼 |
| v0.10 | 2026-10-31 | 按 `stack-decision.md` 回写：D1 新增「namespace 隔离」（单 collection + repository 强制注入 + **`event_id` 含 namespace** 防撞 id）；D2 图节点/边带 namespace 且禁止跨域重建；附录 B 补 `namespace` 并列入 required；新增 4.0 通用约定（`X-Agent-Key` 认证、namespace 不可入参）；D6 补 4 项存储/认证配置；待定项移除「原文存储」「多用户隔离」；补 A34 | 鱼鱼 |
| v0.11 | 2026-10-31 | **架构澄清：本服务是给 LLM 用的 MCP 工具集，不是人工 REST API**。重写 4.0 为双入口总览（MCP stdio=LLM / Admin HTTP=人）；4.2 改为管理侧批量导入；4.3 改为 `memory_recall` 工具；4.4–4.8 标为管理接口、统一 `/admin` 前缀；新增 4.9 MCP 工具总表（remember/forget/feedback）；C.B13 瘦身（意图判断上移给 LLM，服务端只留参数校验与滥用护栏，废弃 2 项配置）；A1/A3/A7/A29/A31/A32/A34 改为对 tool 调用断言 | 鱼鱼 |
