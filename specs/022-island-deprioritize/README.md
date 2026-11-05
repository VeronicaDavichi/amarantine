---
status: complete
created: 2026-11-05
priority: medium
tags:
- graph
- forget
- scoring
- backend
created_at: 2026-11-05T01:17:13.621201714Z
updated_at: 2026-11-05T01:22:31.026630009Z
completed_at: 2026-11-05T01:22:31.026630009Z
transitions:
- status: in-progress
  at: 2026-11-05T01:17:14.475371889Z
- status: complete
  at: 2026-11-05T01:22:31.026630009Z
---
# 022 孤岛降权：孤立子图的记忆更快进入淘汰

## Overview

需求（用户原话）：

> 那种孤岛的数据可以降低重要性，就是这两三个 node 形成一个孤立的子图，
> 和别的 node 没有关系的时候，他应该重要性不高。

「重要性不高」在遗忘策略里的落地 = **更快达到淘汰线**，不是打分微调
（`importance` 在打分公式里权重只有 0.2，改动幅度用户感知不到）。

### 落点：遗忘策略，不是打分

现有 `forget_policy` 的五类硬信号里，`graph_degree >= GRAPH_HUB_DEGREE` 是
**枢纽保护**。本 spec 加的是它的**对称面**：小簇 → 更快淘汰。

这样架构一致，不引入新概念。

## Design

### D1 指标：`event_neighbors`，不是「邻居平均度数」

原方案（评审时定下的）是「邻居平均度数低 = 孤岛」。**实测不成立**：

```
张三事件  degree=5，邻居[3,1,1,1,1]，邻居平均 1.40
周会事件  degree=2，邻居[3,1]，      邻居平均 2.00
```

原因：邻居是 `Time` / `Entity` / `Action` 节点，它们**天然只连 1 个事件**。
所以「邻居平均度数」永远偏低，会把最重要的卡（张三的决定）误判成孤岛。

正确指标是**事件级邻域**：

```
event_neighbors = 通过共享 MENTIONS/INVOLVES 实体能到达的**其它 Event** 数量
```

只在 Event 之间计算，不把 Time/Entity/Action 的度数混进来。

```cypher
MATCH (e:Event {namespace:$ns, event_id: eid})
OPTIONAL MATCH (e)-[:MENTIONS|INVOLVES]->(shared)<-[:MENTIONS|INVOLVES]-(other:Event)
WHERE other IS NULL OR other <> e
RETURN eid, count(DISTINCT other) AS event_neighbors
```

**为什么这个指标对**：

- 张三的事件 → 共享「张三」/「项目A」→ 能到达所有提到张三或项目A的事件
- 一条真正孤立的记忆（只有它自己提到某个实体）→ `event_neighbors = 0`
- 连通大图里的叶子节点 → 通过 hub 能到达很多事件 → 不被误判

### D2 效果：`idle_days` 减半，不新增删除路径

```python
effective_idle_days = idle_days // 2 if island else idle_days
if idle >= effective_idle_days and ...:  # 其余四条件不变
```

**不新增独立的淘汰条件**，只放宽「多久算久」。理由：

- 现有四条件（`idle` / `recall_count` / `age` / `retention`）保持完整
- 孤岛不是「可以删」的理由，只是「更容易达到线」的加速器
- 隔离性最好：孤岛逻辑出 bug，最坏是「提前一点淘汰」，不是「绕过所有保护」

### D3 三重安全阀（这条是关键）

当前 `work` 只有 3 条真实记忆，**三个节点的 `event_neighbors` 全是 0**。
如果默认启用，会把全部 3 条都判成孤岛并加速淘汰 —— 那不是「孤岛降权」，
是「数据少时全部降权」。

所以：

| 阀 | 作用 |
|---|---|
| **默认关闭**（`ISLAND_MAX_EVENT_NEIGHBORS=0`） | 与 `GRAPH_HUB_DEGREE` 的 `0=关闭` 惯例一致；不改变默认行为 |
| **数据量门槛**（`ISLAND_MIN_NAMESPACE_EVENTS=50`） | namespace 事件数低于阈值时**不判孤岛** —— 数据少时的孤立不是孤岛，是没积累 |
| **访问历史合取** | 仍要求 `recall_count < min_recalls`：孤立但被反复召回的记忆（婚礼会被查很多次）不加速 |

第二条是本 spec 唯一「额外加的复杂度」，但它防的是真实风险：
没有它，这个功能在冷启动阶段会变成「全员加速淘汰」。

### D4 配置

```
ISLAND_MAX_EVENT_NEIGHBORS=0     # 0 = 关闭（默认）。建议 50+ 条数据后设为 2
ISLAND_MIN_NAMESPACE_EVENTS=50   # 低于此事件数不做孤岛判定
```

与 `GRAPH_HUB_DEGREE` 对称：`0` = 关掉这条信号，而不是「所有卡都 >= 0 于是全被处理」。

### D5 图不可用降级

`event_neighbors` 查不到（`None`）时：
- **不判孤岛**（`island = False`）
- 在 `ForgetReport` 里标 `graph_degraded=True`

不能把「查不了」当成「孤岛」—— 那会在图抖动时把全库加速淘汰。
与 `event_degrees` 的 `None` 语义保持一致。

## Plan

- [x] T1 `graph_search.event_event_neighbors()`：批量查事件级邻域
- [x] T2 `ForgetCandidate.neighbor_events: int | None`（`None` = 图不可用）
- [x] T3 `forget_policy.sweep()` 加 `island_max_neighbors` / `island_min_events` 参数
- [x] T4 `sweep()` 内部算 `island` 并只改 `effective_idle_days`
- [x] T5 `config.py` 加两个字段，`.env.example` 补说明
- [x] T6 `admin_http` 两处调用点接线（查图 + 传参 + 降级标记）

## Test

- [x] D1 `event_neighbors=0` 且数据量足够 → 孤岛
- [x] D2 `event_neighbors=3` → 不是孤岛
- [x] D3 **默认 `0` 时完全不生效**（回归保护：不改变默认行为）
- [x] D4 namespace 事件数 < 门槛 → 不判孤岛（即使 `event_neighbors=0`）
- [x] D5 图不可用（`None`）→ 不判孤岛 + 标 `graph_degraded`
- [x] D6 孤岛 + `recall_count` 高 → **仍不加速**（访问历史合取生效）
- [x] D7 孤岛让 `idle_days=60` 的卡在第 30 天就进淘汰桶
- [x] D8 非孤岛同条件卡不受影响（对照组）
- [x] D9 孤岛不绕过任何硬信号（pinned / permanent / unresolved 仍然 PROTECTED）
- [x] D10 现有 1030 条测试全绿

## Notes

### 为什么现在写逻辑而不是等数据

用户明确要求「先写逻辑」。而且：

- 指标（`event_neighbors`）和数据量无关，可以先定型
- 默认关闭 + 数据量门槛让它在冷启动时**零副作用**
- 等数据 50+ 条再回头做，会忘掉「邻居平均度数为什么不成立」这个结论

### 两次实测修正（都改变了设计）

1. **「邻居平均度数」不成立** —— 被 Time/Entity 的低度数污染，
   核心事件反而平均最低。改成事件级邻域。
2. **当前数据三个节点全 `event_neighbors=0`** —— 证明「默认开启」在冷启动时
   是危险的，促成了数据量门槛和默认关闭。

如果只按评审时的结论写，会做出一个「把最重要的卡误判成孤岛」的功能。

### 这仍然不是 C.B3

C.B3 是实体消歧（同名不同人）。本 spec 是**图结构**层面的价值判断。
两者都在「用图结构辅助判断」，但对象不同。

### 一个待观察的问题

`idle_days // 2` 这个系数是拍的（无数据支撑）。设计成常数而非配置项，
理由是：多一个配置项就多一个「配了但没人调」的字段。
如果将来发现问题，一次改动比一个永远用默认值的配置更诚实。

## 实现记录

### 实测修正一：不能用「邻居平均度数」

评审时定的方案是「邻居平均度数低 = 孤岛」。**实测不成立**：

```
张三事件  degree=5  邻居度数 [3,1,1,1,1]  邻居平均 1.40
周会事件  degree=2  邻居度数 [3,1]        邻居平均 2.00
```

张三的事件是**最核心**的记忆，但它的邻居平均只有 1.40，比周会事件还低。
原因是邻居都是 `Time` / `Person` / `Entity` / `Action` 节点，它们**天然只连 1 个事件**
—— 它们的度数反映的是「这个实体被提到几次」，不是「这个事件孤不孤立」。

换成**事件级邻域**：

```
event_neighbors = 通过共享 MENTIONS/INVOLVES 实体能到达的**其它 Event** 数量
```

Cypher 只在 Event 之间算，不把 Time/Entity/Action 的度数混进来。

### 实测修正二：默认必须关闭

现有 3 条真实记忆，`event_neighbors` **全是 0**（语义正确 —— 这三件事确实互不相关）。

如果默认开启，冷启动阶段**每一条都是孤岛**，等于「全员加速淘汰」。
所以加了三重阀：

| 阀 | 配置 | 作用 |
|---|---|---|
| 默认关闭 | `ISLAND_MAX_NEIGHBORS=0` | 与 `GRAPH_HUB_DEGREE` 的 `0=关` 一致 |
| 数据量门槛 | `ISLAND_MIN_EVENTS=50` | namespace 事件数不够时**不判孤岛** |
| 访问历史合取 | 现有 `recall_count < min_recalls` | 孤立但被反复召回的（婚礼）不加速 |

第二条是本 spec 唯一新增的复杂度，但它防的是真实风险：
没有它，这个功能在冷启动阶段就是「全员降权」。

### 落点：只放宽门槛，不新增删除路径

```python
effective_idle_days = idle_days // 2 if island else idle_days
```

步骤 6 的四个条件（`idle` / `recall_count` / `age` / `retention`）**一个都没少**。
孤岛只是让 `idle` 更快达标。

隔离性好：孤岛逻辑出 bug，最坏是「提前一点淘汰」，不是「绕过所有保护」。

### 顺手修了一个既有 bug

`/admin/review` 端点**从来没查过图**：

```python
# 改之前
rows = _load_all(cfg, ns)
report = sweep(rows, ..., )          # 没传 graph_hub_degree / confirmed_min
```

于是 `graph_degree` 全是默认 0，`GRAPH_HUB_DEGREE` 的枢纽保护在这个端点
**从来没有生效过**，而 `/admin/sweep` 是查图的 —— **两个端点分桶结果不一致**。

孤岛逻辑必须在两边都生效，否则同样的数据两个端点给出不同答案。
所以这次一并修了（属于本 spec 的必要部分，不是加戏）。

### 真库验证（3 条数据）

```
neighbors    = {evt_44d7…: 0, evt_f55b…: 0, evt_afec…: 0}
total_events = 3

① 默认关闭（island_max=0）           → 判 0/3，reason=idle_and_unrecalled
② 开启但门槛 50（数据只有 3 条）     → 判 0/3，reason=idle_and_unrecalled
③ 开启且门槛降到 1                   → 判 3/3，reason=island_and_unrecalled
```

第 ③ 条是关键：如果它也不判，说明是接线断了，不是门槛在工作。
三条同时成立，证明「默认关、门槛挡、逻辑通」。

**验证时踩到的坑**：第一版脚本传 `created_at=None`，导致 `age=0` →
所有卡被宽限期保护（`grace_period`），孤岛判定根本走不到。
这不是 bug：宽限期在孤岛之前是**正确顺序**（新卡不该被加速淘汰）。

### 测试

新增 26 条：

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `test_island_policy.py` | 20 | 三重阀、门槛减半、不绕过硬信号、参数校验 |
| `test_island_graph.py` | 6 | 图查询语义（None vs {}、缺 id、不关别人的 driver） |

关键几条：

- `test_disabled_by_default_changes_nothing` —— **回归保护**：默认参数下行为不变
- `test_small_namespace_is_never_island` —— 冷启动时数据量门槛挡住
- `test_graph_unavailable_is_never_island` —— 图不可用不能当「没有邻居」
- `test_island_with_many_recalls_is_not_accelerated` —— 访问历史合取
- `test_island_never_bypasses_hard_signals` —— 四个硬信号逐一验证

全量：**1056 passed**（1030 + 26）。

### 这个功能现在处于什么状态

**逻辑可用，默认不启用。**

要开启：

```bash
ISLAND_MAX_NEIGHBORS=2    # 事件邻居 <= 2 算孤岛
ISLAND_MIN_EVENTS=50      # 且 namespace 至少 50 条记忆
```

建议等 `work` 有 50+ 条真实记忆后再开。现在只有 3 条，
开启会把全部判成孤岛（虽然会被数据量门槛挡住）。

### 待观察

`idle_days // 2` 这个系数是拍的（没有数据支撑）。设计成常数而非配置项，
理由写在 spec 里：多一个配置项就多一个「配了但没人调」的字段。


## 修复：`idle_days // 2` 在极小配置下会把门槛归零

用户问「`idle_days // 2` 是什么意思」时顺手验了边界，发现一个真 bug。

### 复现

```
idle_days=1  →  1 // 2 == 0  →  effective_idle_days = 0
                              →  idle >= 0 永远成立
```

后果：**连刚刚被召回过的孤岛卡都会被判 SAFE_TO_DELETE**。

```
刚刚被召回过的孤岛卡（idle=0），idle_days=1
  修之前 -> SAFE_TO_DELETE  island_and_unrecalled   ← 明显错误
  修之后 -> PROTECTED       active
```

默认 `FORGET_IDLE_DAYS=180` 不会触发（180//2=90），但配置成 1 或 2 就会。
而且它是**静默的**：报告里看起来一切正常，只是淘汰得比预期激进。

### 修法

```python
if island and idle_days > 0:
    effective_idle_days = max(1, idle_days // 2)
else:
    effective_idle_days = idle_days
```

两个细节：

1. **`max(1, ...)`** —— 门槛至少 1 天。`idle_days=1` 时无法减半，
   就保持 1（不加速），而不是归零（立即）。
2. **`idle_days == 0` 保持原样** —— 它现在是既有的「立即」语义
   （ADR 没定义 0 的含义，但既成事实）。孤岛不该顺手改它，
   要重新定义得单独议。

### 补的测试

- `test_tiny_idle_days_does_not_zero_out_the_threshold` —— 刚召回过的卡不淘汰
- `test_tiny_idle_days_with_one_day_idle_is_still_eligible` —— 闲置 1 天仍淘汰
- `test_idle_days_zero_keeps_existing_semantics` —— 0 的语义没被偷偷改
- `test_two_days_halves_to_one` —— 2 → 1 的减半正常

### 一个没修的既有问题（记录，不顺手改）

`idle_days == 0` 的语义**在 ADR 里没有定义**，实际行为是「立即淘汰所有满足
条件的卡」。它可能被误当成「关闭淘汰」的哨兵值（毕竟 `GRAPH_HUB_DEGREE=0`
就是关闭）。

现在没改，理由是：改它要动既有行为，超出本 spec 范围。
但值得单独议 —— 一个能静默触发全库淘汰的配置值，不该没有文档。

### 修正后的计数

- `test_island_policy.py`：20 → **24** 条
- 全量：1056 → **1060 passed**
