---
status: complete
created: 2026-11-03
priority: high
tags:
- admin
- ingest
- raw-quote
- observability
- backend
created_at: 2026-11-03T02:27:50.095049698Z
updated_at: 2026-11-03T02:33:25.166657897Z
completed_at: 2026-11-03T02:33:25.166657897Z
transitions:
- status: complete
  at: 2026-11-03T02:33:25.166657897Z
---
# 原文层：批量导入 + 原文溯源

> **Status**: in-progress · **Priority**: high · **Created**: 2026-11-03

## Overview

ADR 承诺了原文层，但代码里一个字都没有 —— `raw_quote_ids` 字段存在、
**从来没被填过一次**：

| ADR | 能力 | 现状 |
|---|---|---|
| 4.2 | `POST /admin/ingest`（批量导入历史对话） | ❌ 完全没有 |
| 4.8 | `GET /admin/raw/{quote_id}`（原文溯源） | ❌ 无数据源可查 |
| — | `GET /admin/ingest/{job_id}`（查导入进度） | ❌ ADR 未定义，但 202 之后**必须有**（本 spec 补） |

**目标（二值判定）**：A1–A14 全绿；`ingest` 一条真实对话后，
`GET /raw/{quote_id}` 能取回原文，且该 quote 能反查到它生成了哪些事件卡。

**非目标**

| 不做 | 理由 |
|---|---|
| ingest 里做自然语言指令解析 | ADR 4.2 明说：意图判断已上移给 LLM（C.B13） |
| 原文参与召回 | 原文是**溯源通道**，不是检索材料；进索引会污染召回评分的语义 |
| 删原文 | A32：软删事件卡后原文仍可读，所以 `raw_quote` 没有 delete 路径 |
| turn 级语义映射 | 需要 `split_events` 返回「哪几条属于哪个事件」，它现在只返回文本 —— 改契约是另一件事 |

**文档依据（docs/ 扫描）**

| 文件 | 用途 |
|---|---|
| `docs/ADR-001-memory-architecture.md` 4.2 | ingest 请求/响应、`dedupe_key` 幂等、`do_not_store` 语义 |
| `docs/ADR-001-memory-architecture.md` 4.8 + A32 | `GET /raw` 契约与「软删后仍可读」的硬约束 |
| `docs/ADR-001-memory-architecture.md` 4.7 | 软删语义（本 spec 的 raw 必须**不**跟着删） |
| `docs/user-manual.md` | 补「导入历史对话」与「溯源」两节 + changelog |

**本 spec 无 UI 需求** —— 纯管理接口 + 存储，不适用 E2E / 视觉对照。

## Design

### 两个新表（SQLite，与既有派生表同口径）

```
raw_quote(
  namespace, quote_id, text, role, ts, source, session_id, created_at,
  PRIMARY KEY (namespace, quote_id)
)
-- 只有 INSERT OR IGNORE，没有 UPDATE / DELETE。事实层不可变。

ingest_job(
  job_id PK, namespace, dedupe_key, status, source, session_id,
  turns_total, turns_skipped, events_created, error, created_at, updated_at
)
-- UNIQUE(namespace, dedupe_key)  ← 幂等的落点
```

**为什么不复用 `pending_task`**：`retry_pending()` 对**所有 kind** 都会
`write_event_node()` 重放。ingest 的载荷是对话原文，被当图写任务重放会
在图上刷出一堆垃圾节点。复用省不了几行，换来一个静默的破坏源。

### `quote_id` 生成

`msg_` + `sha256(namespace|source|session_id|ts|text)[:16]`

- 含 `namespace`：同一段对话导入两个域要各自可查，不互相覆盖
- 含 `text`：内容变了就是新 quote，符合「事实层不可变」
- 稳定：同一 turn 重复导入落到同一个 `quote_id`，天然幂等

### 异步流程

```
POST /admin/ingest
  ├─ turns 全带 do_not_store ──→ job 记 rejected
  │                              200 {job_id:null, status:"rejected", events_created:0}
  ├─ dedupe_key 已存在 ────────→ 直接返回那个 job（**不重跑**）
  ├─ INSERT raw_quote（同步，快）
  ├─ INSERT ingest_job(queued)
  ├─ BackgroundTasks.add_task(run_ingest, job_id)
  └─→ 202 {job_id, status:"queued"}

run_ingest(job_id):
  status=running
  有效 turns 拼文本 → split_events() → N 条摘要（失败降级为整段一条）
  每条 remember_event(summary=…, raw_quote_ids=**整批** quote_ids)
  状态 → done；events_created / turns_skipped 落库
  异常 → failed + error（单条写卡失败只跳过，不整批崩）
```

### `GET /admin/ingest/{job_id}` —— ADR 之外必须补的

```
GET /admin/ingest/{job_id}
→ 200 {job_id, status, events_created, turns_total, turns_skipped, error, …}
→ 404
```

ADR 只定义了 `202 {job_id, status:queued}`。**没有查询接口，202 就是个黑洞** ——
调用方永远不知道那次导入成没成。这正是 002/003 一直在杀的那种静默失败，
只是换了个位置。

### `GET /admin/raw/{quote_id}`

```
GET /admin/raw/{quote_id}?namespace=
→ 200 {quote_id, text, role, ts, source, session_id, event_ids:[...]}
→ 404
```

`event_ids` = 扫 Weaviate 里 `raw_quote_ids` 含该 quote 的卡。
**不建反向索引**：P0 数据量是「一个人几年的记忆」，扫一遍可接受；
代价写在 Notes 里，不在 spec 里假装它是 O(1)。

### `remember_event` 增加 `raw_quote_ids`

透传到 `EventCard.raw_quote_ids`（字段早就有，一直是空列表）。

## Plan

- [x] T1 `raw_quote` 表 + store（`insert_or_ignore` / `get` / `quotes_for_events`）
- [x] T2 `ingest_job` 表 + store（`create` / `get` / `set_status` / `find_by_dedupe`）
- [x] T3 `quote_id` 生成 + `do_not_store` 过滤
- [x] T4 `remember_event(raw_quote_ids=…)` 透传
- [x] T5 `POST /admin/ingest`（202 / rejected / dedupe 幂等）
- [x] T6 `GET /admin/ingest/{job_id}`
- [x] T7 `run_ingest` 后台任务（切分 + 逐条写 + 状态机）
- [x] T8 `GET /admin/raw/{quote_id}`（含 `event_ids` 反查）
- [x] T9 测试 A1–A14
- [x] T10 文档：user-manual「导入历史对话」「溯源」+ changelog v0.18

## Test

- [x] A1 `raw_quote` 落库：同 `quote_id` 重复写只留一行，`text` 不变
- [x] A2 `quote_id` 稳定：同 turn 算两次得到同一个 id
- [x] A3 `quote_id` 含 namespace：同 turn 导进两个域得到两个不同 id
- [x] A4 `POST /admin/ingest` 正常 → **202**，`job_id` 非空、`status="queued"`
- [x] A5 `raw_quote` 在 202 返回时**已经可读**（原文同步落库，不等后台）
- [x] A6 同 `dedupe_key` 再提交 → 返回**同一个** `job_id`，且不新建 job
- [x] A7 全部 `do_not_store` → **200**，`status="rejected"`、`job_id=null`、`events_created=0`
- [x] A8 部分 `do_not_store` → 202，`turns_skipped` 等于被跳过条数
- [x] A9 后台跑完 → job `status="done"`，`events_created >= 1`
- [x] A10 生成的卡上 `raw_quote_ids` **恰好是本批那些 quote**
- [x] A11 `GET /raw/{quote_id}` → 200 且 `text` 与导入原文一致，`event_ids` 非空
- [x] A12 `GET /raw/不存在` → 404
- [x] A13 软删事件卡后 `GET /raw/{quote_id}` **仍 200**（A32 硬约束）
- [x] A14 `GET /admin/ingest/{job_id}` 查得到；不存在 → 404

## Notes

### 为什么 202 要有配套的查询接口

`202 Accepted` 的含义是「收下了，稍后告诉你」。ADR 只给了这一半 ——
没有 `GET /admin/ingest/{job_id}`，调用方拿到 `job_id` 之后无处可用。
所以本 spec 加了这个 ADR 没写的接口，并把「为什么加」写在这里，
而不是悄悄扩契约。

### 为什么 `raw_quote` 没有删除路径

A32 要求「软删事件卡后 `GET /raw/{quote_id}` 仍可溯源」。如果 raw 会跟着
事件卡一起删，这条验收就永远过不了。所以 `raw_quote` 只增不改不删，
连管理接口都不提供 —— **不给**，比「给了但藏着」更难被误用。

### 为什么 `event_ids` 靠扫描而不是反向索引

反向索引要多一张 `(quote_id, event_id)` 表 + 写入时双写，还得处理
「事件卡被重建后旧关联失效」。P0 数据量小，扫一遍换零一致性负担。
真到扫不动的时候（上万卡），再加索引并回填 —— 那时也知道真实基数了。

### 为什么单条写卡失败不整批 failed

一批 200 条 turn，第 50 条因 LLM 超时失败，不该让前 49 条白干。
所以单条 catch → `turns_skipped += 1`，整批继续。只有「连切分都做不了」
这种批级故障才置 `failed`。`events_created` 与 `turns_skipped` 的差值
就是「既没成功也没被跳过」的沉默损失 —— 所以两者都要落库，别只报成功数。

### 实现记录：四个「不写下来下次会踩」的地方

**1 · `raw_quote_ids` 不只要能存，还要能从召回材料里出来。**
字段早在 `EventCard` 里，但 `to_properties()` / `SCHEMA_PROPERTIES` / 查询
`_FIELDS` 三处都没有它 —— 只加存储的话，`ingest` 写进去了、
`GET /raw` 却查不到「哪些卡引用了这句」，因为召回材料里根本没有这个键。
三处一起补才算通。

**2 · `remember_event` 的 `raw_quote_ids` 绝不能进 `event_id` 哈希。**
`event_id` 是 `namespace|日期|类型|实体|摘要` 的规范化哈希。如果把 quote id
也算进去，同一句摘要挂在不同原文上就会变成两个事件 —— **幂等直接失效**，
同一批对话重复导入会无限产卡。

**3 · TestClient 会同步跑完 BackgroundTasks，生产不会。**
单测里 `POST /admin/ingest` 返回时 job 可能已经是 `done`，而真库跑出来是
`queued`。所以 A4 只硬断言「202 + `job_id` 非空」，状态放开成
`queued/running/done`；「跑完是 done」由 A9 单独验。不这么拆，
测试要么在 CI 里随机红，要么反过来把生产的 `queued` 当 bug。

**4 · 整批 `do_not_store` 要 200，不是 202。**
ADR 4.2 明确：这是「拒绝」，不是「收下了」。FastAPI 的 `status_code=202`
是路由级常量，所以端点签名要注入 `Response` 手动改回 200。
改不动响应码的话，调用方会把「被拦下」当成「已排队」。
（`job_id` 也必须是 `null` —— 它压根不会执行。）

### 依赖关系

本 spec 的 A13 用到 004 的 `DELETE /admin/event`，属于**软依赖**（只为验证
「软删后原文仍可读」）。`leanspec link` 在 0.3.0 里没有该子命令、
`rel` 报 `not yet migrated`，所以依赖关系只能在这里文字声明。

### 验收证据

- 单测：`pytest tests -q` → **847 passed**（827 + 20 新增），零回归
- 真库：11/11 全绿（真 Weaviate + 真 Neo4j + 真 LLM 切分，ns `raw73c6a7`）
  - A4 `job=job_8c9a06282fb5209f status=queued` —— 生产语义是真的异步
  - A9 `status=done events=3` —— 切分出 3 条摘要，各成一张卡
  - A11 `ev=[evt_3c62…, evt_b699…, evt_e7ed…]` —— 3 张卡都带回同一个 quote，
    证明 `raw_quote_ids` 真的进了 Weaviate 且反查可用
  - A13 `del=200 raw=200` —— 软删事件卡后原文不可删
  - A7 `status=rejected job=None` —— 全批拦截走 200 且不发 job_id
- 守门：`check_guide.py` / `check_spec.py` exit 0

