---
status: complete
created: 2026-11-04
priority: high
tags:
- config
- recall
- timeout
- backend
created_at: 2026-11-04T07:33:05.824299321Z
updated_at: 2026-11-04T07:42:25.310903855Z
completed_at: 2026-11-04T07:42:25.310903855Z
transitions:
- status: in-progress
  at: 2026-11-04T07:33:06.594964380Z
- status: complete
  at: 2026-11-04T07:42:25.310903855Z
---
# 017 向量通道超时接线：`VECTOR_TIMEOUT_MS` 真正生效

## Overview

`VECTOR_TIMEOUT_MS = 800` 在 `config.py` 有默认值，`src/` 里**零引用**。
ADR 第 265–266 行的原意很明确：

| 配置 | 值 | 含义 |
|---|---|---|
| `VECTOR_TIMEOUT_MS` | 800 | **向量通道超时** |
| `RECALL_BUDGET_MS` | 1500 | 整体预算 |

且 C.B12 补了一句：`COLD_QUERY_P95_MS = 800`「与 `VECTOR_TIMEOUT_MS` 对齐 ——
冷查询不应比单通道超时还慢」。也就是说 `VECTOR_TIMEOUT_MS` 是一个**被认真设计过**的数，
不是随手填的。

### 现状：实际超时是 20 秒

`WeaviateStore.__init__` 里 `httpx.Client(timeout=20.0)` 写死。
所以「向量通道 800ms 超时」这件事从来没有发生过 —— 单次 GraphQL 最长可以拖 20 秒。

外层有 `future.result(timeout=remaining)`（`remaining = RECALL_BUDGET_MS/1000 = 1.5s`）
兜一层，但那个只能让**调用方**不再等，取消不了已经跑起来的线程，
HTTP 连接照样挂着 20 秒。所以：

- 调用方看到的是「3 个通道各等 1.5 秒」
- 实际后端是「最多 3 个请求各自挂 20 秒」

**`VECTOR_TIMEOUT_MS` 接线后，向量通道才会真的在 800ms 放弃。**

## 非目标

### 不改 `httpx.Client` 的全局超时

`self._http` 被三类调用共用：

| 调用 | 走哪 |
|---|---|
| `search_near_vector` / `search_bm25` | `_graphql` |
| `list_all`（管理接口全量列举） | `_graphql` |
| `insert_event` / 软删 / 复活 | `/v1/objects` 等 REST |

把 client 全局改成 800ms 会让 `list_all`（可能几千条）和写入也变 800ms ——
那是另一个 bug。所以必须**按请求传超时**（httpx 支持 `post(..., timeout=...)` 覆盖）。

### 不给 BM25 通道加同样的超时

ADR 只定义了「向量通道超时」，没有 BM25 的。BM25 查询在数据量大时本来就慢，
盲目套 800ms 会误伤。BM25 继续由外层 budget 兜底。

要加的话应该有一个独立配置项，不是复用 `VECTOR_TIMEOUT_MS` ——
名字不匹配的配置比没有配置更容易误导。

### 不修 `remaining` 不递减的问题（记录，另开）

`recall.py` 的通道等待循环里：

```python
remaining = budget / 1000.0
for name, future in futures.items():
    channel_results[name] = future.result(timeout=max(0.01, remaining))
```

`remaining` 在循环里**从不递减**。所以「整体预算 1500ms」实际是
「每个通道各 1500ms」，最坏 4500ms —— **`RECALL_BUDGET_MS` 的语义是假的**。

这是独立的一处 bug，修法是每轮减去 `time.perf_counter()` 的差值。
不在本 spec 范围（本 spec 只接一个配置项），但**必须记录**，因为它决定
`VECTOR_TIMEOUT_MS` 的效果如何被观察到。

## Design

### D1 `_graphql` 支持按请求覆盖超时

```python
def _graphql(
    self,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    ...
    resp = self._http.post(
        f"{self._base}/v1/graphql",
        json={"query": query, "variables": variables or {}},
        timeout=timeout_s,   # None 时用 client 的默认值
    )
```

**不要**写成 `post(..., timeout=timeout_s)` —— 实测 httpx 把 `None` 展开成
`{"connect": None, "read": None, ...}`，即**禁用超时**，比原来的 20 秒更糟。
`None` 时必须**不传这个 kwarg**（见实现记录）。

### D2 `search_near_vector` 透传

```python
def search_near_vector(
    self, vector, namespace, *, limit=50, date_from=None, timeout_s=None
) -> list[dict[str, Any]]:
    ...
    data = self._graphql(query, {"vector": vector}, timeout_s=timeout_s)
```

### D3 `recall` 的向量通道用配置值

```python
def run_vector():
    return active_store.search_near_vector(
        query_vec, namespace,
        limit=candidates_limit,
        date_from=date_from,
        timeout_s=settings.VECTOR_TIMEOUT_MS / 1000.0,
    )
```

800ms → 0.8 秒。

### D4 `<= 0` 表示不限（沿用项目约定）

`VECTOR_TIMEOUT_MS <= 0` 时传 `None`（用 client 默认 20s），而不是传 0
（httpx 会把它当成立即超时）。这是项目里「0 = 关信号」约定的延续。

必须写测试，否则 0 会被误读成「所有请求立刻失败」。

## Plan

- [x] T1 `_graphql` 加 `timeout_s` 参数（默认 None）
- [x] T2 `search_near_vector` 透传
- [x] T3 `recall.run_vector` 传 `VECTOR_TIMEOUT_MS / 1000`
- [x] T4 `<= 0` → None
- [x] T5 `.env.example` 补该项（与 `GRAPH_TIMEOUT_MS` 一起，成对出现）

## Test

- [x] D1 `VECTOR_TIMEOUT_MS=800` → httpx 收到 `timeout=0.8`
- [x] D2 不传 `timeout_s` → httpx 收到 `timeout=None`（沿用 client 默认）
- [x] D3 `VECTOR_TIMEOUT_MS=0` → 传 `None`，不是 0
- [x] D4 `list_all` 不受影响（仍是 client 默认）
- [x] D5 BM25 通道不受影响
- [x] D6 现有 986 条测试全绿
- [x] D7 `test_config_usage` 白名单里 `VECTOR_TIMEOUT_MS` 必须删掉（防腐测试会抓）

## Notes

### 为什么这个接线值得单独一个 spec

它只有三行改动，但它揭示了一个**测量盲区**：
我们在 `docs/acceptance-audit.md` 里说「召回预算 1500ms 生效」，
而实际上通道级超时是 20 秒，且 budget 不递减。

也就是说：**之前所有关于召回延迟的说法都没有依据。**
这次接线让「向量通道 800ms」第一次成为可观测的事实。

### 接线后能观察到什么

接线之后，如果 Weaviate 变慢，trace 里会出现 `errors.vector`，
而不是整条 recall 卡住 20 秒。这是「降级路径可见」的一部分（与 013 的
rerank 降级同构：**不可用的通道要能说出自己不可用**）。

### `remaining` 不递减：建议的修法（未做）

```python
deadline = time.perf_counter() + budget / 1000.0
for name, future in futures.items():
    remaining = max(0.01, deadline - time.perf_counter())
    ...
```

用绝对 deadline 而不是递减变量，比「每轮减差值」更不容易出错 ——
循环里有 `continue` 或异常分支时不会漏算。

建议作为独立 spec，因为它会改变「部分通道超时」的判定行为，
需要单独定验收标准（三个通道都超时时应该返回什么）。

## 实现记录

### 关键发现：httpx 的 `timeout=None` 不是「沿用 client」

第一版 `_graphql` 写成：

```python
self._http.post(url, json=payload, timeout=timeout_s)   # timeout_s=None 时
```

以为 `None` 会沿用 client 的超时。实测**不是** —— httpx 把它展开成
`{"connect": None, "read": None, "write": None, "pool": None}`，也就是**禁用超时**，
比原来的 20 秒更糟。

修法是 `None` 时**不传这个 kwarg**：

```python
kwargs = {"json": {...}}
if timeout_s is not None:
    kwargs["timeout"] = timeout_s
resp = self._http.post(url, **kwargs)
```

这条是被 `test_list_all_is_not_affected` 抓出来的 —— 它断言 `list_all` 仍拿到
client 默认的 5s，而实际拿到了 `None`。

**如果只写「传了 0.8 就生效」的测试，这个 bug 会漏过去**（0.8 那条是过的）。
必须有「不传时不受影响」的反向断言。

### 为什么测不到 `request.timeout` 而是 `request.extensions["timeout"]`

httpx 的超时是 client/transport 层的东西，不进 `httpx.Request` 的公开属性。
MockTransport 里能读到的是 `request.extensions["timeout"]`（一个
`connect/read/write/pool` 的 dict）。测试就断言这个 dict 的 `read` 值。

### 白名单防腐第三次生效

`VECTOR_TIMEOUT_MS` 接上后，`test_whitelist_entries_are_still_unused` 立刻挂掉 ——
这正是 D7 预测的。已从 `WHITELIST` 删掉。

三次了（`RERANK_CANDIDATES` / `TIME_CONF_MIN` / `VECTOR_TIMEOUT_MS`），
每次都是它先报，不是我记性好。**这个测试的价值已经用三次实践证明。**

### 测试替身要跟着签名走

`FakeStore.search_near_vector` 加了 `timeout_s` 参数之后，`test_recall.py` 里
19 个测试全挂 `unexpected keyword argument 'timeout_s'`（因为 recall 现在总会传）。

这不算 bug，是**接口变更的正常信号**：替身不跟着改，说明它和老接口绑定了。
`tests/test_acceptance_gaps.py` 里的另一个替身也一起补上了。

### 范围外的一处 bug（记录，未修）

`recall.py` 通道等待循环：

```python
remaining = budget / 1000.0
for name, future in futures.items():
    channel_results[name] = future.result(timeout=max(0.01, remaining))
```

`remaining` **从不递减**，所以「整体预算 1500ms」实际是「三通道各 1500ms」，
最坏 4500ms。`RECALL_BUDGET_MS` 的语义目前是假的。

建议修法（用绝对 deadline 而不是递减变量，避免 `continue`/异常分支漏算）：

```python
deadline = time.perf_counter() + budget / 1000.0
for name, future in futures.items():
    remaining = max(0.01, deadline - time.perf_counter())
```

需要单独的 spec，因为它会改变「部分通道超时」的判定行为，要重新定
「三个通道都超时时返回什么」的验收标准。

## 验收证据

- `pytest tests/test_vector_timeout.py -q` → **9 passed**
  - store 层 4 条（向量传 0.8 / 向量不传用默认 / list_all 不受影响 / BM25 不受影响）
  - recall 层 5 条（800→0.8、1200→1.2、50→0.05、0→None、默认值生效）
- 全量：`pytest tests -q --tb=line` → **995 passed**（986 + 9）
- `audit_config_usage.py`：零引用 **16**
- `test_config_usage.py` → 9 passed（白名单已收缩）

### 接线前后能观察到什么

接线**前**：Weaviate 变慢 → 单次 GraphQL 可拖 20 秒 → recall 调用方等到
budget 超时才返回 `partial`，但后端连接还在挂着。

接线**后**：向量通道 800ms 放弃 → `errors.vector` 出现 → 调用方 0.8 秒就知道
「向量通道不行了」，剩下两个通道的结果照样返回。

这才是「通道级超时」的意义：**不可用的通道要能说出自己不可用**（与 013 的
rerank 降级同构）。
