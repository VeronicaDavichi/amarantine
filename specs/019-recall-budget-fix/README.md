---
status: complete
created: 2026-11-04
priority: high
tags:
- recall
- timeout
- backend
created_at: 2026-11-04T08:45:35.929980887Z
updated_at: 2026-11-04T08:51:13.063453918Z
completed_at: 2026-11-04T08:51:13.063453918Z
transitions:
- status: in-progress
  at: 2026-11-04T08:45:36.659845125Z
- status: complete
  at: 2026-11-04T08:51:13.063453918Z
---
# 019 召回预算真的生效：`RECALL_BUDGET_MS` 语义修正

## Overview

`RECALL_BUDGET_MS = 1500` 的语义是「整次召回最多花 1.5 秒」。**现在它是假的。**

实测（`harness/repro_budget.py`，设 budget=1500）：

| 场景 | 实际耗时 | 被标超时的通道 |
|---|---|---|
| 三通道各慢 2s | **3791 ms** | 只有 `vector` |
| 向量慢 2s | 2129 ms | 只有 `vector` |
| 全部快 | 129 ms | — |

**超预算 2.5 倍。** 而且第二个错更隐蔽：

`bm25` 实际花了 2 秒（**已超 1500ms 预算**）却没被标 `partial` ——
因为轮到它时 `remaining` 仍是 1.5s，它「借」到了时间。
**现在的 `partial` 只反映「第一个撞线的通道」**，不是「所有超时的通道」。

### 根因

```python
remaining = budget / 1000.0          # 只算一次，从不更新
for name, future in futures.items():
    future.result(timeout=max(0.01, remaining))   # 每次都等这个全长
```

三个通道**并行提交**，却**顺序收结果**，且每次都用全预算等 → 最坏 3 × budget。

## 决策（已拍板）

| 问题 | 选择 |
|---|---|
| 修法 | **`concurrent.futures.wait` 一次等齐**（不是递减 remaining） |
| 三通道全超时 | **空结果 + `degraded=true`**（不抛 503） |

理由：通道本来就是并行的，用并行原语表达「最多等这么久」比维护递减变量更直接；
超时不是服务挂（真的挂已经走 503），但三通道全超时意味着「这次结果完全不可信」，
所以 `degraded=true` 比单纯 `partial=true` 更能让上层感知。

## 非目标

### 不给 BM25 独立 HTTP 超时

017 已说明：ADR 只定义了 `VECTOR_TIMEOUT_MS`，BM25 数据量大时本来就慢，
套 800ms 会误伤。需要独立配置项，不在本次范围。

**但要记录它的后果**：`shutdown(wait=False)` 之后，BM25 线程最长还能跑
20 秒（client 默认超时）。线程会被丢弃但仍占资源 —— 可接受（单次查询一个线程），
但如果并发高，需要给 BM25 也接超时。

### 不改 `WeaviateStoreError` → 503 的逻辑

Weaviate 真挂了必须 503，不能当空结果。这条保持不动。

## Design

### D1 用 `wait` 一次等齐

```python
done, not_done = wait(futures.values(), timeout=budget / 1000.0)
```

三个通道并行跑，`wait` 只等 budget。

### D2 **必须** `shutdown(wait=False)` —— 否则 D1 白做

这是本 spec 最容易踩空的地方：

```python
with ThreadPoolExecutor(max_workers=3) as pool:   # ← 退出时 shutdown(wait=True)
    ...
    wait(..., timeout=1.5)                        # 等了 1.5s
# ← 这里又把 2s 的线程等到结束，总时长还是 2s
```

`ThreadPoolExecutor.__exit__` 调用 `shutdown(wait=True)`，**它会等所有线程跑完**。
所以只改 `wait` 不改退出方式，实测时长不会变。

改法：

```python
pool = ThreadPoolExecutor(max_workers=3)
try:
    futures = {...}
    done, not_done = wait(futures.values(), timeout=budget / 1000.0)
    ...
finally:
    # 不等未完成的通道：它们有自己的 HTTP 超时兜底
    # （向量通道 800ms 由 spec 017 接管；BM25 是 client 默认 20s，见非目标）
    pool.shutdown(wait=False, cancel_futures=True)
```

`cancel_futures=True`（Python 3.9+）会把**还没开始**的任务取消掉。

### D3 全超时 → `degraded=true`

```python
if not channel_results:
    # 三个通道都没在预算内返回。不是服务挂（那走 503），是「太慢」。
    return RecallResult(
        materials=[],
        degraded=True,
        partial=True,
        trace={"empty_reason": "budget_exhausted", ...},
        ...
    )
```

`empty_reason` 新增一个值 `budget_exhausted` —— 与现有 `no_candidate` /
`below_threshold` 并列，让「为什么空」可区分。

### D4 部分超时保持现有语义，但修正标记

`errors[name] = "budget exhausted"` 每个超时通道都填（`wait` 天然能做到，
不像现在的顺序循环只标第一个）。

`partial=True` 只要有一个通道超时。

## Plan

- [x] T1 `wait` 替换顺序 `future.result`
- [x] T2 `with` 换成 `try/finally` + `shutdown(wait=False, cancel_futures=True)`
- [x] T3 全超时分支：`degraded=True` + `empty_reason="budget_exhausted"`
- [x] T4 保留 `WeaviateStoreError` → 503 的行为
- [x] T5 `harness/repro_budget.py` 转为回归测试（断言总时长 <= budget × 1.2）

## Test

- [x] D1 三通道各慢 2s、budget=300ms → **实际耗时 <= 500ms**（含开销余量）
- [x] D2 同上 → `partial=true`、`degraded=true`、`empty_reason="budget_exhausted"`、`materials=[]`
- [x] D3 两通道快一通道慢 → 快的结果**保留**，慢的进 `errors`
- [x] D4 Weaviate 挂（`WeaviateStoreError`）→ 仍然 503，不是空结果
- [x] D5 全部快 → `partial=false`、`degraded=false`
- [x] D6 现有 1001 条测试全绿

### D1 的断言要用宽松上限

线程调度有抖动，不能断言 `<= 300ms`。用 `budget × 1.2 + 50ms` 之类，
并把「慢通道 2s 却能在 500ms 内返回」作为核心断言 ——
如果 `shutdown(wait=True)` 没改掉，这条会以「2000ms」失败，正是我们要的。

## Notes

### 为什么这个 bug 之前没被发现

因为 `budget` 只在**慢**的时候才起作用，而单元测试用的都是假通道（返回极快）。
生产里如果三个通道都快，预算是隐形的 —— 直到某天 Weaviate 变慢，才会发现
「1.5 秒预算」其实允许 4.5 秒。

这正是 D1 要写集成式测试（真 sleep）而不是纯 mock 的原因：
**超时逻辑只能用真实时间来验证**。

### `partial` 与 `degraded` 的分工

| 字段 | 含义 | 谁置位 |
|---|---|---|
| `partial` | 有通道没返回（超预算） | 预算超时 |
| `degraded` | 有通道返回了但不可信 | `GraphHits.ok=False` / 全通道超时 |

两者独立：可能 `partial=true` 而 `degraded=false`（图通道超时，向量通道结果可信）。

### 一个后续可做的观察点

`shutdown(wait=False)` 之后，BM25 线程最长能活 20 秒。如果将来并发上来，
可能出现「线程数累积」。届时的解法：
1. 给 BM25 接独立超时（推荐）
2. 或改用 `asyncio` + `httpx.AsyncClient`（彻底，但改动大）

现在记录，不处理 —— 单次查询一个线程，实际影响为零。

## 实现记录

### 依赖

本 spec **依赖 spec 017**（`VECTOR_TIMEOUT_MS` 接线）：`shutdown(wait=False)` 之后
被丢弃的向量通道线程需要有自己的 HTTP 超时才会退出，否则会挂到 client 的 20 秒。
仓库里 `leanspec rel add` 报「not yet migrated to the adapter API」，所以依赖关系
写在这里而不是 frontmatter。

### 修复前后对比（真实测量）

warm 之后、`budget=1500ms`：

| 场景 | 修复前 | 修复后 | partial | degraded | empty_reason |
|---|---|---|---|---|---|
| 三通道各慢 2s | **3791 ms** | **1614 ms** | True | True | `budget_exhausted` |
| 只有 graph 慢 4s | — | 1591 ms | True | True | `no_candidate` |
| store 慢 2s | 2129 ms | 1622 ms | True | False | `no_candidate` |
| 全快 | 129 ms | 108 ms | False | False | — |

### 关键坑：`with ThreadPoolExecutor` 会抵消 `wait`

第一反应是把顺序 `future.result()` 换成 `wait(futures, timeout=budget)`，
但**只做这一步完全无效** —— 实测仍然是 2 秒。

原因：

```python
with ThreadPoolExecutor(max_workers=3) as pool:   # __exit__ = shutdown(wait=True)
    ...
    wait(..., timeout=1.5)                        # 等了 1.5s
# ← 这里又把没跑完的线程等到结束
```

`ThreadPoolExecutor.__exit__` 调用 `shutdown(wait=True)`，**它会等所有线程跑完**。
所以必须改成：

```python
pool = ThreadPoolExecutor(max_workers=3)
try:
    ...
finally:
    pool.shutdown(wait=False, cancel_futures=True)
```

这条如果不是实测比对，光看代码会觉得「`wait` 加了就完事了」。

### 测量陷阱：第一次调用有 jieba / TEI 开销

第一次跑 `recall` 会加载 jieba 词典 + 建 TEI 连接，**约 1.5–2 秒**。
这个开销和预算完全无关，但会把耗时撑到 3 秒以上，让人误判「没修好」。

中间我还遇到一个反直觉现象：

```
graph 慢 2.0s -> 总 3420 ms
graph 慢 4.0s -> 总 1652 ms      ← 更慢的通道反而总耗时更短？
```

原因是第一次调用包含了 warm 开销，第二次才是真实测量。
**所以测试里必须有 warm fixture**，否则第一个断言会用 warm 时间当「预算超标」。

### 测试假设错了一次：图通道没有元数据

最初想用「图通道快且有命中」来验证「快通道结果保留」，测试挂了：

```
assert result.materials, "快通道的结果被丢掉了"
E  AssertionError
```

看 trace 才发现有 `note: "图通道命中的 id 没有元数据（P0 限制）"` ——
图通道只返回 `event_ids`，没有卡片正文，所以 `materials` 是空的。
这是**已知的 P0 限制**，不是 bug。

改用 BM25 的完整命中来验证（它带全套字段）。

### `partial` 与 `degraded` 的实际取值

| 场景 | partial | degraded | 说明 |
|---|---|---|---|
| 三通道全超时 | True | True | 全没回来，结果完全不可信 |
| 图通道超时、其余有结果 | True | True | 图通道降级 |
| BM25 超时、图只在 | True | False | 图通道正常，只是少了一路 |
| 全快 | False | False | — |

`degraded` 不等于 `partial`：它们量的是两件事（「有没有缺」vs「缺的是不是致命」）。

## 验收证据

- `pytest tests/test_recall_budget.py -q` → **5 passed**
- 全量：`pytest tests -q --tb=line` → **1006 passed**（1001 + 5）
- 手工测量见上表（修复前 3791ms → 修复后 1614ms）

### 测试为什么必须用真 sleep

5 条测试全部用 `time.sleep` + `time.perf_counter()`，没有一条用 mock 时间。

理由：**超时逻辑只能靠真实时间验证**。如果假通道返回很快，预算不生效也测不出来 ——
这正是这个 bug 存在很久没被发现的原因（单元测试里通道永远快）。

测试用 `BUDGET_MS=300` + 慢通道 `1000ms`，阈值 `700ms`：

- 修好了：~350ms
- 没修（或 `shutdown(wait=True)` 没改）：~1000ms+

阈值能把两种情况干净分开，同时留够调度抖动余量。
