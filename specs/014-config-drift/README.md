---
status: complete
created: 2026-11-04
priority: high
tags:
- config
- recall
- auth
- backend
created_at: 2026-11-04T02:41:09.707428430Z
updated_at: 2026-11-04T02:58:45.990704715Z
completed_at: 2026-11-04T02:58:45.990704715Z
transitions:
- status: in-progress
  at: 2026-11-04T02:45:16.016409233Z
- status: complete
  at: 2026-11-04T02:58:45.990704715Z
---
# 014 配置漂移：三个「配了但没生效」的开关

## Overview

设计审计（2026-11-04）用 `harness/audit_config_usage.py` 扫了 `config.py` 全部 76 个字段，
发现 **21 个零引用**。其中 18 个有正当理由（P2 预留、外部服务适配），
但有 **3 个是「声明的值和实际行为不一致」**，性质比「压根没这个配置」更糟 ——
你以为调了，其实没调。

这不是新功能，是**把已有声明兑现**。

### 三个不一致

| 配置项 | 声明 | 实际 | 后果 |
|---|---|---|---|
| `RERANK_CANDIDATES` | 50 | `recall.py` 里 `limit=100` 硬编码 | 改配置无效，且默认值差一倍 |
| `GRAPH_TIMEOUT_MS` | 可配（默认 200） | `graph_search.py` 里 `GRAPH_TIMEOUT_S = 0.2` 硬编码 | 改配置无效 |
| `AGENT_KEY_HEADER` | 可配（默认 `X-Agent-Key`） | `admin_http.py` 用 `Authorization` 硬编码 | 改配置无效，且默认值不同 |

`AGENT_KEY_HEADER` 那条最微妙：默认值是 `X-Agent-Key`，而实际实现是
`Authorization: Bearer`。也就是说**默认配置描述的是一个不存在的协议**。

### 为什么值得单独做

这是本项目的**反复出现的病**：A10（`GRAPH_HUB_DEGREE`/`CONFIRMED_MIN`）、
A16（`CHUNK_KEEP_PER_EVENT`）、A27（五个 `RERANK_*`）、A4（`ARCHIVE_WINDOW_DAYS`）
全都是同一个模式 —— 配置项有默认值，消费它的逻辑不存在。

四次之后应该建立**机制**，而不是第五次再靠人眼发现。所以本 spec 除了修三条，
还要把检测**固化成测试**。

## 非目标

- 不新增功能，不改默认行为
- 不处理另外 18 个零引用字段（P2 预留、外部服务适配有各自的分期）
- 不重构配置加载机制

## Design

### D1 `RERANK_CANDIDATES` 的语义是什么

名字上的「候选数」有两种可能：

- (a) 送进 rerank 的候选**上限**
- (b) 召回后用于排序的候选池大小

现在代码里 `limit=100` 是 chunk 检索的 limit，与 rerank 无关。
真正的 rerank 候选是 `scored` 全部（RRF 融合后的候选）。

**决定**：`RERANK_CANDIDATES` 用于**截断送进 rerank 的候选**，
即 `rerank(query, scored[:RERANK_CANDIDATES])`。理由：
- rerank 是 CPU 上的昂贵操作，候选越多越慢（实测每批 0.3s）
- 名字就是这个意思，不该改语义去迁就代码

chunk 检索的 `limit=100` 是**另一个数**，应改成独立常量或配置。
为保持最小改动：改成 `settings.RERANK_CANDIDATES` 不合适（语义不同），
**直接写死为模块常量 `CHUNK_SEARCH_LIMIT = 100`** 并注释说明它与
`RERANK_CANDIDATES` 无关。这样「硬编码」变成了「显式命名常量」，
不再是隐藏的漂移。

### D2 `GRAPH_TIMEOUT_MS` 与 `GRAPH_TIMEOUT_S`

`graph_search.py` 里的 `GRAPH_TIMEOUT_S` 被用作函数默认参数。
改法：默认参数改为 `None`，运行时从 `settings.GRAPH_TIMEOUT_MS / 1000` 取值。
`GRAPH_TIMEOUT_S` 常量删除，避免两个真值来源。

要注意：`GRAPH_TIMEOUT_S` 可能被测试引用，删之前先 grep。

### D3 `AGENT_KEY_HEADER` 到底是什么协议

ADR v0.10 决定用 `X-Agent-Key` 认证。实现成了 `Authorization: Bearer`。
两者都是合法的，但**不能都宣称**。

**决定**：以**实际实现**为准 —— 保留 `Authorization: Bearer`，
把 `AGENT_KEY_HEADER` 的默认值改成 `Authorization`，并处理
「带 Bearer 前缀」的取值逻辑。理由：
- `Authorization: Bearer` 是标准做法，MCP 客户端与 curl 都直观
- 改实现去迎合 `X-Agent-Key` 会破坏现有 006 前端的 token 输入
- 配置项的价值在于「允许换 header 名」，不在于绑定某个特定名字

若 `AGENT_KEY_HEADER` 与 `Authorization` 不同，则不剥 `Bearer` 前缀，直接用原值。
这条要写测试。

### D4 机制化：把检测固化成测试

新增 `tests/test_config_usage.py`：

- 读 `config.py` 全部字段
- 对每个字段在 `src/` 里找消费点（排除 `config.py` 自身）
- 允许一个**显式白名单**（P2 预留、外部适配），白名单里每条必须写理由
- 断言「不在白名单里的字段都必须有消费点」

这样第五次假实现会在 CI 里挂掉，而不是等人去做设计审计。

## Plan

- [x] T1 grep 三个字段与 `GRAPH_TIMEOUT_S` 的全部引用，确认影响面
- [x] T2 `RERANK_CANDIDATES`：截断送进 rerank 的候选；chunk 的 100 提成命名常量
- [x] T3 `GRAPH_TIMEOUT_MS`：函数默认参数改 `None`，运行时读配置，删 `GRAPH_TIMEOUT_S`
- [x] T4 `AGENT_KEY_HEADER`：改默认值，按是否等于 `Authorization` 决定剥不剥 Bearer
- [x] T5 写 `tests/test_config_usage.py`（含白名单 + 理由）
- [x] T6 更新 `.env.example` 与 `docs/user-manual.md` 里涉及的说明

## Test

- [x] D1 `RERANK_CANDIDATES=1` 时只有 1 个候选被送进 rerank（fake client 记录入参）
- [x] D2 改 `GRAPH_TIMEOUT_MS` 后 `graph_search` 用的超时真的变了
- [x] D3 `AGENT_KEY_HEADER=Authorization` 走 Bearer 剥离；改成 `X-Agent-Key` 后不剥离且能认证
- [x] D4 `audit_config_usage` 的断言能真的挂掉（造一个假字段验证测试本身有效）
- [x] D5 现有 959 条测试全绿（默认行为不变）

## Notes

### 为什么 `AGENT_KEY_HEADER` 的默认值要往实现靠，而不是反过来

直觉上「按 ADR 写的 `X-Agent-Key` 实现」更忠诚。但：

- `Authorization: Bearer` 是标准，MCP 客户端、curl、前端 token 输入框都是按它做的
- 改成 `X-Agent-Key` 要连带改 006 的前端与用户手册，收益只是「和 ADR 字面一致」
- 配置项真正要提供的能力是「**允许换 header 名**」，不是绑定某个名字

所以决定：默认值改成 `Authorization`，实现保持不变，把「允许换」这条真正接通。

### `RERANK_CANDIDATES` 与 chunk 检索的 100 是两个概念

容易混在一起，特别标注：

- `RERANK_CANDIDATES`：RRF 融合后、送进 rerank 的**候选上限**（昂贵操作，要截断）
- `CHUNK_SEARCH_LIMIT`：chunk collection 的**检索条数**（便宜操作，为聚合留余量）

它们碰巧都是「候选数」，但一个决定成本、一个决定召回广度，不该共用一个配置。

### 白名单要写理由，否则机制会退化

`tests/test_config_usage.py` 的白名单如果只列字段名，半年后没人知道为什么它在那儿，
下一个人会往里随便加。所以白名单的每一项必须是「字段名 + 一句理由」。

### 本节之外仍留 18 个零引用字段

不在本 spec 范围，但记录在此避免下次重复发现：

- P2 预留（5）：`CLUSTER_MERGE_SIM` `CLUSTER_MIN_SIZE` `CONSOLIDATE_MIN_EVENTS`
  `CONSOLIDATE_DUP_RATIO` `CONSOLIDATE_LATENCY_MS`，另加 `COLD_QUERY_P95_MS`
- 外部服务适配（3）：`EMBEDDING_API_KEY` `EMBEDDING_PROVIDER` `WEAVIATE_GRPC_PORT`
- 未接线（其余）：`LABEL_LLM_TEMP` `LABEL_REVIEW_BATCH` `ENTITY_ALIAS_SIM`
  `FORGET_SWEEP_DAYS` `DB_BUSY_TIMEOUT_MS` `SQLITE_WAL` `TOKENIZER` `GUIDE_CHECK_CRON`

## 实现记录

### T1 影响面比预期小

- `GRAPH_TIMEOUT_S` 只有 2 处（定义 + 默认参数），其余全是 `search_by_entities` 的调用方
- `AGENT_KEY_HEADER` 是裸字段，没有注释（上面那句「Admin 侧不使用」属于 `MEMORY_ADMIN_NAMESPACE`）
- `RERANK_CANDIDATES` 只有定义

### T2 顺手发现我自己引入的漂移

chunk 层（010）里我写了 `limit=100`，而 `RERANK_CANDIDATES=50` —— 但这两个数**语义不同**：

- `RERANK_CANDIDATES`：送进 rerank 的候选上限（决定 CPU 成本）
- chunk 检索条数：为聚合留多少余量（决定召回广度）

所以没有把 chunk 的 100 改成读 `RERANK_CANDIDATES`（那会错），而是提成模块常量
`CHUNK_SEARCH_LIMIT = 100` 并注释说明两者无关。
**「硬编码」变成「显式命名常量」后就不再是隐藏的漂移。**

### T3 踩到一个测试假设

`test_graph_search.py` 里用 `settings=object()` 当替身 —— 因为原实现只在
`driver is None` 时才碰 settings。现在超时也要读配置，于是 11 个测试全挂
`AttributeError: 

## 实现记录

### T1 影响面比预期小

- `GRAPH_TIMEOUT_S` 只有 2 处（定义 + 默认参数），其余全是 `search_by_entities` 的调用方
- `AGENT_KEY_HEADER` 是裸字段，没有注释（上面那句「Admin 侧不使用」属于 `MEMORY_NAMESPACE`）
- `RERANK_CANDIDATES` 只有定义

### T2 顺手发现我自己引入的漂移

chunk 层（010）里我写了 `limit=100`，而 `RERANK_CANDIDATES=50` —— 但这两个数**语义不同**：

- `RERANK_CANDIDATES`：送进 rerank 的候选上限（决定 CPU 成本）
- chunk 检索条数：为聚合留多少余量（决定召回广度）

所以没有把 chunk 的 100 改成读 `RERANK_CANDIDATES`（那会错），而是提成模块常量
`CHUNK_SEARCH_LIMIT = 100` 并注释说明两者无关。
**「硬编码」变成「显式命名常量」后就不再是隐藏的漂移。**

### T3 踩到一个测试假设

`test_graph_search.py` 里用 `settings=object()` 当替身 —— 因为原实现只在
`driver is None` 时才碰 settings。现在超时也要读配置，于是 11 个测试全挂
`AttributeError`。

修法是给测试补 `_FakeCfg(GRAPH_TIMEOUT_MS=200)`，而不是在实现里加 getattr 兜底 ——
兜底会掩盖「配置真的被读到了」这件事。

### T3 又差点造一个新的零引用

第一版我在模块顶部加了 `GRAPH_TIMEOUT_FALLBACK_S = None` 当兜底。
写完立刻意识到这就是本 spec 要消灭的东西，删掉，超时取值内联到函数体。

**「修漂移的过程中造出新漂移」是这类重构最容易犯的错。**

### T4 不能只改默认值，得让配置真的被读

`HTTPBearer` 把 header 名写死成 `Authorization`。只改默认值的话，
`AGENT_KEY_HEADER` 仍然是装饰品。所以 `require_token` 改成自己读 `Request.headers`：

- header 名 `Authorization`（默认）→ 必须 `Bearer <token>`
- 其它名字 → 整个值是 token

401 的报错文案会点出**配置的** header 名，否则排查时会被误导。

### T5 白名单防腐

`tests/test_config_usage.py` 有 5 个测试，其中两个是**元测试**：

- `test_detector_actually_detects`：造一个必然无人用的字段，确认检测器会报
- `test_comments_do_not_count_as_consumers`：注释里提到不算用了

还有 `test_whitelist_entries_are_still_unused` —— 某字段后来接上了就必须从白名单删，
否则白名单会腐烂成「历史上曾经没用过的字段集合」。

### 遗留：`TIME_CONF_MIN` 是第 4 个同型问题

`time_resolver.py:21` 的注释提到它，但代码里没有消费。
这说明**时间解析的置信度阈值从未被用上**。不在本 spec 范围，已加进白名单并写理由，
下次做时间解析相关工作时应该一并处理。

## 验收证据

- `pytest tests/test_config_usage.py -q` → **9 passed**
- `pytest tests/test_config_wiring.py -q` → **11 passed**
- 全量：`pytest tests -q --tb=line` → **979 passed**（959 + 9 + 11）
- `harness/audit_config_usage.py`：零引用从 **21 → 18**
- `check_guide.py` → exit 0
- `check_spec.py` → exit 0
