---
status: complete
created: 2026-11-03
priority: high
tags:
- admin
- event
- recall
- archive
- backend
created_at: 2026-11-03T01:38:40.986559465Z
updated_at: 2026-11-03T02:06:13.048497746Z
completed_at: 2026-11-03T02:06:13.048497746Z
transitions:
- status: complete
  at: 2026-11-03T02:06:13.048497746Z
---
# 事件管理面：五个 ADR 承诺但没实现的接口

> **Status**: in-progress · **Priority**: high · **Created**: 2026-11-03

## Overview

对照 ADR 第 4 章逐个核对管理面，五个接口「文档里有、代码里没有」：

| ADR | 接口 | 现状 |
|---|---|---|
| 4.6 | `PATCH /admin/event/{event_id}` | ❌ 完全没有 |
| 4.7 | `DELETE /admin/event/{event_id}` | ⚠️ `/admin/forget` 已能软删，但缺 `hard` 拒绝、缺 `retention_expired`、缺幂等 |
| 4.4 | `POST /admin/archive` | ❌ 没有 |
| 4.9 | `POST /admin/recall` | ❌ 没有（MCP 侧有 `memory_recall`） |
| 4.9 | `POST /admin/recall/{trace_id}/feedback` | ❌ `submit_feedback()` 已存在，只差 HTTP 入口 |

**目标（二值判定）**：A1–A12 全绿，且五个端点都能在真库上跑通。

**非目标**

| 不做 | 理由 |
|---|---|
| `POST /admin/ingest` + `GET /admin/raw/{quote_id}` | 原文从未落库（`raw_quote_ids` 只是 ID 列表），需新建 `raw_quote` 存储 + 异步 job，归 **005 原文层** |
| 真实冷热搬迁 | ADR 4.4 明确 P0 只返回候选，`moved: 0` |
| 物理删除 | ADR D7 + A9：**事实层永不物理删**，`hard=true` 必须 400 |
| 前端 | M5 |

**文档依据（docs/ 扫描）**

| 文件 | 用途 |
|---|---|
| `docs/ADR-001-memory-architecture.md` 4.4 / 4.6 / 4.7 / 4.9 | 五个接口的契约原文，1:1 实现 |
| `docs/ADR-001-memory-architecture.md` D7 + A9 + A32 | 「只软删」「原文可溯源」的硬约束 |
| `docs/user-manual.md` | 补两节操作说明 + changelog |
| `docs/memory-tool-guide.md` | 不含本次变更（管理侧接口不进 LLM 工具表） |

**本 spec 无 UI 需求** —— 全部是 HTTP 管理接口，不涉及页面/表单/按钮，故不适用 E2E 与视觉对照任务。

## Design

### 复用 vs 新增（先查清再说，不重复造）

| 项 | 可直接复用 | 必须新增 |
|---|---|---|
| PATCH | — | `WeaviateStore.update_fields()`（现在只有 `set_confirmed_by_use` / `set_review_verdict` 这类单字段方法） |
| DELETE | `forget_event()` 全套软删（`mark_forgotten` / `revive` / 图侧 retention） | `ALLOWED_REASONS` 增加 `retention_expired`；幂等短路 |
| archive | — | `WeaviateStore.count_older_than()` |
| recall | `recall()`，**已有 `trace_store` 参数** | — |
| feedback | `submit_feedback()` + `recall_feedback` 表 | — |

### 五个契约

#### 1 · PATCH /admin/event/{event_id}

```
PATCH /admin/event/{event_id}
{"pinned": true, "retention": "long", "namespace": null}
→ 200 {"event_id", "pinned", "pinned_source", "retention", "updated_at"}
```

- 可改字段**白名单**：`pinned` / `retention`。改 `summary` / `types` 必须走标签复核或重建 → **400**
- `pinned=true` 时**同时**写 `pinned_source="manual"`（ADR 4.6 要求，缺了就无法区分人工钉还是规则钉）
- `404` event_id 不存在
- **幂等**：值没变就**不更新** `updated_at`（ADR 4.6 明说「重复置 true 返回 200 且 updated_at 不变」）

`_now()` 只在真的要改时才取，否则幂等无从谈起。

#### 2 · DELETE /admin/event/{event_id}

```
DELETE /admin/event/{event_id}
{"reason": "user_forget | retention_expired", "hard": false, "namespace": null}
→ 200 {"event_id", "soft_deleted": true, "raw_quote_preserved": true, "deleted_at"}
```

- `hard=true` → **400**。这不是「暂时没实现」，是**设计上关死**：事实层永不物理删（D7 + A9）
- `reason` 只认 `user_forget` / `retention_expired`。「忘了吧」走前者
- **幂等**：软删前先 `get_by_event_id()`；已删则直接返回**原来的** `deleted_at`，不再 PATCH。否则每次重试都会刷新时间，审计就废了
- `raw_quote_preserved` 恒 `true`，**但要诚实标注**：事实层确实零改动，可原文层还没实现（005），所以这个字段目前只表示「没删任何原文」；「真能查到原文」要等 005

#### 3 · POST /admin/archive

```
POST /admin/archive
{"window_days": 180, "dry_run": true, "namespace": null}
→ 200 {"window_days": 180, "candidates": 1234, "moved": 0, "mode": "logical"}
```

- `dry_run` 默认 `true`；P0 **无论传什么**都 `moved: 0`、`mode: "logical"`（ADR 4.4：P0 只返回候选，不搬迁）
- `candidates` = `observed_at < now - window_days` 的卡数
- 与 `forget` 的职责分离（ADR 4.7 硬约束）：`archive` 是热冷逻辑分层**按时间**，`forget` 是淘汰三桶**按空闲+召回收**。两者不可混用，也不共用一个 service

#### 4 · POST /admin/recall

```
POST /admin/recall
{"query": "...", "namespace": null, "budget_ms": null}
→ 200 {"materials": [...], "trace": {...}, "trace_id": "tr_..."}
```

**这一条必须传 `trace_store`** —— 不传就不产 `trace_id`，下一项的 feedback 永远关联不上。
这是 4.9 两个接口唯一的耦合点，也是「写了但串不起来」的典型陷阱。

`RecallUnavailableError` → **503**（事实源不可用，不是 400）。

#### 5 · POST /admin/recall/{trace_id}/feedback

```
POST /admin/recall/{trace_id}/feedback
{"used_material_ids": ["evt_..."], "namespace": null}
→ 200 {"trace_id", "accepted", "updated"}
```

复用 `submit_feedback()`：

- `trace_id` 不存在 / 不属该 namespace / 出现不属于该 trace 的 id → `FeedbackError` → **400**
- trace 不存在单独给 **404**，便于调用方区分「传错了域」和「根本没有」

## Plan

- [x] T1 `WeaviateStore.update_fields()` —— 白名单 + PATCH 单对象
- [x] T2 `WeaviateStore.count_older_than()` —— `observed_at` 截止计数
- [x] T3 `forget.ALLOWED_REASONS` 增加 `retention_expired`
- [x] T4 `PATCH /admin/event/{event_id}`（白名单校验 + pinned_source + 幂等）
- [x] T5 `DELETE /admin/event/{event_id}`（hard 400 + 幂等短路）
- [x] T6 `POST /admin/archive`（dry_run 恒不搬）
- [x] T7 `POST /admin/recall`（传 trace_store）与 `.../feedback`
- [x] T8 测试：A1–A12
- [x] T9 文档：user-manual 补「事件管理」一节 + changelog v0.17

## Test

- [x] A1 `PATCH` 改 `pinned=true` → 200，`pinned_source="manual"`，Weaviate 里真的变了
- [x] A2 `PATCH` 传白名单外字段（`summary`）→ 400，且**没有任何写入**
- [x] A3 `PATCH` 重复置同一值 → 200 且 `updated_at` **不变**（幂等）
- [x] A4 `PATCH` 不存在的 event_id → 404
- [x] A5 `DELETE` 软删 → 200，`is_deleted=true`、`deleted_at` 有值，`raw_quote_preserved=true`
- [x] A6 `DELETE` `hard=true` → **400**，图上/索引上零改动（证明它被拒绝在入口，不是静默降级）
- [x] A7 `DELETE` 重复删 → 200 且 `deleted_at` **与原值相同**
- [x] A8 `DELETE` reason 不在枚举 → 400；`retention_expired` 可用
- [x] A9 `POST /admin/archive` 默认 → `dry_run:true`、`moved:0`、`mode:"logical"`；`candidates` 与种子数据一致
- [x] A10 `POST /admin/archive` 传 `dry_run=false` → **仍然** `moved:0`（P0 不搬）
- [x] A11 `POST /admin/recall` → 200，`materials` 非空、`trace_id` **非空**
- [x] A12 `POST /admin/recall/{trace_id}/feedback` → 200 且 `used_material_ids` 真的落到 `recall_feedback`；错 trace_id → 404

## Notes

### 为什么 `DELETE /admin/event` 和 `/admin/forget` 并存

`POST /admin/forget` 是先实现的，名字是历史产物；ADR 4.7 定义的是
`DELETE /admin/event/{event_id}`。两者**语义相同**（都是软删），不打算删掉旧的
（MCP 侧和现有测试依赖它），但新入口是 ADR 的那一个。

实现上不复制逻辑：两个端点都调 `forget_event()`，差异只在参数校验
（新入口多一道 `hard` 拒绝）。

### `raw_quote_preserved: true` 是一个不能夸大的字段

ADR A32 要求「软删后 `GET /raw/{quote_id}` 仍可溯源」。但原文从来没落过库
（`raw_quote_ids` 只是 ID 列表），所以今天无法真的溯源。

这个字段仍然返回 `true`，因为它的**字面含义**（「没有删掉任何原文」）是真的 ——
事实层零改动。但如果有人据此以为「能查到原文」，那就是在骗人。
所以文档和本 spec 都写明：真正的溯源能力在 005。

宁可字段带一句注释，也不要让人踩空。

### 为什么 archive 一条都不搬

不是因为懒。ADR 4.4 定的就是 P0 `dry_run` 默认 true、只出候选。
先让「有多少卡进入冷区」这个数字可见，再由人决定何时开搬 ——
自动搬迁是「人工抽检误删率 < 5%」之后的下一阶段，和 `forget` 的
`dry_run=true` 强制是同一套谨慎逻辑。

### 实现记录：四个「不写下来下次会踩」的地方

**1 · `extra="ignore"` 会让 A2 变成假绿。**
Pydantic 默认丢弃未知字段，所以 `{"summary": "..."}` 会**静默通过**：
请求 200、什么都没改，调用方以为改成功了。改成 `extra="allow"` +
端点手工检查 `model_extra` → 400，才真正满足「白名单外一律拒绝」。
没这一步，A2 只在测「模型能反序列化」，不是在测「拒绝了」。

**2 · `FeedbackError` 有四种原因，只有一种该 404。**
「trace 不存在」「不属于当前记忆域」「id 不在返回集」「参数非法」都抛同一个
`FeedbackError`。管理面必须区分 404 和 400，否则调用方不知道是重试还是改参数。
一开始想用 `str(exc)` 里有没有「不存在」来分 —— 改一个字就静默降级成 400。
最后加了 `TraceNotFoundError(FeedbackError)` 子类，用类型而不是文案判断。

**3 · `count_older_than` 不是精确全量，所以响应多了 `scanned`。**
Weaviate 的 `Aggregate` 在本机版本没验证过，宁可用已知可用的 `Get` 拉回来数。
代价是只覆盖最近 `limit` 条。与其把可能不全的计数当全库报出去，
不如把 `scanned` 一起返回 —— 调用方看得到这个数字建立在多少条之上。
ADR 的响应体因此多一个字段（向后兼容）。

**4 · 新加了 Weaviate 属性 `updated_at`，靠 `ensure_collection` 增量补齐。**
老 collection 不会自动获得新属性，`ensure_collection()` 会对比远端 schema
做 `POST /v1/schema/{class}/properties`，所以不需要重建、不丢数据。
旧对象读新字段得到 `None`，语义正好是「从没被管理面改过」。

**5 · 入口收窄：`ALLOWED_REASONS` 扩了，MCP 不能跟着放宽。**
T3 把 reason 扩成三个（管理面 `DELETE` 要用 `retention_expired`），
但 `mcp_stdio._tool_forget` 原本**完全不校验** reason —— 于是 MCP 侧也
静默接受了 `retention_expired`。而 LLM 没有能力判断「保留期到了」，
放开它等于给模型一个会误删的理由。

同一次改动里必须把三个入口的子集分开：

| 入口 | 可用 reason | 为什么 |
|---|---|---|
| MCP `memory_forget` | `user_forget` / `outdated` | ADR 4.9；LLM 只该说这两种 |
| `DELETE /admin/event` | `user_forget` / `retention_expired` | ADR 4.7；`outdated` 反而该拒 |
| `POST /admin/forget` | 三个都认 | 管理面历史入口，保持兼容 |

**这是自审时发现的**，不是测试报出来的 —— 单测当时全绿，因为没有任何
一条断言「MCP 不该接受第三个 reason」。扩枚举时顺手检查「谁在共用它」，
比多写十行测试更管用。

### 验收证据

- 单测：`pytest tests -q` → **827 passed**（805 + 22 新增），零回归
- 真库：15/15 全绿（真 Weaviate + 真 Neo4j + TestClient，namespace `event0d8fcf`）
  - A1 `raw.pinned=True raw.source=manual`
  - A3 两次 `updated_at` 完全相同 → 幂等成立
  - A7 `deleted_at` 两次相同 → 重复删不刷新时间戳
  - A5b 软删后 `/admin/recall` 命中 0 条 → 检索侧真的过滤掉了
  - A11 `trace_id=tr_977a60dd2a49` 非空 → feedback 链路串得起来
- 守门：`check_guide.py` / `check_spec.py` / `e2e_mcp.py` 均 exit 0
