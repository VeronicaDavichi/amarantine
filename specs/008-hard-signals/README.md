---
status: complete
created: 2026-11-03
priority: critical
tags:
- acceptance
- forget
parent: '001'
created_at: 2026-11-03T03:30:00.834463670Z
updated_at: 2026-11-03T04:49:12.261109352Z
completed_at: 2026-11-03T04:49:12.261109352Z
transitions:
- status: in-progress
  at: 2026-11-03T04:44:55.658813482Z
- status: complete
  at: 2026-11-03T04:49:12.261109352Z
---
# A10：五类硬信号只实现了三类

## Overview

ADR D7 定义五类硬信号，命中任一即**永不进入淘汰候选**：

| # | 信号 | 现状 |
|---|---|---|
| 1 | `pinned`（`manual` / `user_directive`） | ✅ 已实现 |
| 2 | `unresolved` 非空 | ✅ 已实现 |
| 3 | `event_type ∈ IRREVERSIBLE_VOCAB` | ✅ 已实现（以 `retention` 形式） |
| 4 | 图结构位置：`degree ≥ GRAPH_HUB_DEGREE` | ❌ **零实现** |
| 5 | `confirmed_by_use ≥ CONFIRMED_MIN` | ❌ **零实现** |

`GRAPH_HUB_DEGREE = 8` 与 `CONFIRMED_MIN = 2` 在 `config.py` 里有默认值，
但 `forget_policy.py` **从未引用**，`ForgetCandidate` 也**没有对应字段**。

这是「配置项在、逻辑不在」的典型假实现 —— 审计初版还把它标成了 🟡。

## 非目标

- 不做 `CAUSED` / `SUPERSEDES` 链的关键节点判定（ADR 标为可选增强，P2）
- 不改已实现的三类判定，不动它们的 reason 字符串

## Design

### 为什么 degree 和 confirmed_by_use 优先

这两类都有**现成数据源**，不需要新写入路径：

- `graph_degree`：Neo4j 里该 `:Event` 节点的入边 + 出边数
- `confirmed_by_use`：`recall_feedback` 表按 `event_id` 聚合 `used_material_ids` 次数

另外三类里，`pinned` 和 `unresolved` 是卡上的字段（已在），`retention` 也是字段。
所以补齐第 4、5 类后，五类就全了（链式关键节点那条不在本次范围）。

### 字段怎么加

`ForgetCandidate` 加两个带默认值的字段：

    graph_degree: int = 0
    confirmed_by_use: int = 0

默认 0 保证现有调用方（测试、旧数据）不炸 —— 0 永远不触发保护，
行为与现在完全一致。

### 判定放在哪

放在现有三类**之后**、`manual_keep` **之前**。理由：
硬信号的语义是「绝对保护」，人工 `delete` 裁决不能推翻它（现有注释已写明这个原则）。
顺序：三硬信号 → 新增两硬信号 → 人工 keep → 人工 delete → 宽限期 → ...

### 数据从哪填

- `admin_http` 的 `/forget/sweep`：拿到候选后，查一次 Neo4j degree + 一次
  `recall_feedback` 聚合，填进候选
- 不要放进 `classify()`（它是纯函数，不该有 IO）
- 图查询失败时**降级为 degree=0**（宁可不保护，也不能让整个 sweep 500）——
  但要在响应里带 `graph_degraded=true`，否则就是静默降级

## Plan

- [x] T1 `ForgetCandidate` 加 `graph_degree` / `confirmed_by_use`（默认 0）
- [x] T2 `classify()` 加两条判定 + 独立 reason（`graph_hub` / `confirmed_by_use`）
- [x] T3 `services/graph.py` 加 `event_degree(event_ids, namespace)` 批量查
- [x] T4 `services/feedback.py` 加 `confirmed_counts(event_ids, namespace)` 批量聚合
- [x] T5 `/forget/sweep` 接线：填字段 + 图失败降级 + `graph_degraded` 标记
- [x] T6 测试：5 组夹具各命中一类，断言 reason 互不相同

## Test

- [x] D1 五类信号各构造一条，`bucket == PROTECTED`
- [x] D2 五条的 `reason` 两两不同（能区分是哪类保护）
- [x] D3 `graph_degree=8` 命中、`=7` 不命中（边界二值可判）
- [x] D4 `confirmed_by_use=2` 命中、`=1` 不命中
- [x] D5 图查询失败时 sweep 仍 200，且响应含 `graph_degraded=true`
- [x] D6 新字段默认 0 时，现有 18 条 forget_policy 测试全绿（零回归）

## Notes

### 这条为什么排在新功能第一个

改动面最小（纯函数加两个分支 + 两处数据填充），但**风险最高** ——
它直接决定「什么永不被删」。所以先做，做完再动 chunk 和 rerank。

### 与 007 的关系

`graph_degree` 的正确性依赖图能重建（007 的 T1）。
但本 spec 不**阻塞**于 007：degree 查询走现有图，不依赖重建能力。
只是等 007 做完后，能额外验证「重建后 degree 不变」。

### 实现记录

**1 · 一个好消息：`confirmed_by_use` 早就在 schema 里了。**
实到一半发现 `to_properties()` 已经在写 `confirmed_by_use`，Weaviate 属性表里也有它。
所以第 5 类硬信号**不需要新写入路径**，只要在 `_load_all` 里把它搬进
`ForgetCandidate` 即可。比预估省了一整块。

**2 · 阈值 0 是「关掉」，不是「全保护」。**
这是个容易写反的地方：`candidate.graph_degree >= 0` 恒真。
所以判定写成 `graph_hub_degree > 0 and degree >= graph_hub_degree`。
单测 `test_zero_threshold_disables_the_signal` 专门钉这一条 —— 否则
有人把阈值配成 0 想「临时关闭」，结果全部卡变成永久保留。

**3 · 图降级有三个语义，不能混。**
`event_degrees()` 返回三种：

- `{...}`：正常
- `{}`：调用方传了空列表（短路）
- **`None`**：图不可用

第三种必须让调用方知道，所以端点标 `graph_degraded=true`。
如果返回 `{}` 假装正常，「图枢纽」这一整类保护会静默失效 ——
而报告上看起来一切正常。

**4 · 一次取舍：图查不到时按 degree=0 继续，不 503。**
sweep 是「出报告」，不是「做删除」。图挂了就少一类保护 + 显式标降级，
比整个报告失败有用。真正做删除（P0 不允许）时才该是硬失败。

## 验收证据

- 单测：`pytest tests/test_hard_signals.py -q` → **18 passed**
- 回归：`test_forget_policy` / `test_admin_http` / `test_acceptance_gaps` → **76 passed**
- 全量：`pytest tests -q --tb=line` → **897 passed**（879 + 18）
- 边界：`degree=8` 保护 / `=7` 不保护；`confirmed_by_use=2` 保护 / `=1` 不保护
- 信号：五条 reason 两两不同（`pinned` / `retention=permanent` / `unresolved` /
  `graph_hub` / `confirmed_by_use`）
- 降级：图不可用 → 200 + `graph_degraded=true`；图正常 → `false`
