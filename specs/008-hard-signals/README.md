---
status: planned
created: 2026-11-03
priority: critical
tags:
- acceptance
- forget
parent: '001'
created_at: 2026-11-03T03:30:00.834463670Z
updated_at: 2026-11-03T03:30:00.834463670Z
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

- [ ] T1 `ForgetCandidate` 加 `graph_degree` / `confirmed_by_use`（默认 0）
- [ ] T2 `classify()` 加两条判定 + 独立 reason（`graph_hub` / `confirmed_by_use`）
- [ ] T3 `services/graph.py` 加 `event_degree(event_ids, namespace)` 批量查
- [ ] T4 `services/feedback.py` 加 `confirmed_counts(event_ids, namespace)` 批量聚合
- [ ] T5 `/forget/sweep` 接线：填字段 + 图失败降级 + `graph_degraded` 标记
- [ ] T6 测试：5 组夹具各命中一类，断言 reason 互不相同

## Test

- [ ] D1 五类信号各构造一条，`bucket == PROTECTED`
- [ ] D2 五条的 `reason` 两两不同（能区分是哪类保护）
- [ ] D3 `graph_degree=8` 命中、`=7` 不命中（边界二值可判）
- [ ] D4 `confirmed_by_use=2` 命中、`=1` 不命中
- [ ] D5 图查询失败时 sweep 仍 200，且响应含 `graph_degraded=true`
- [ ] D6 新字段默认 0 时，现有 18 条 forget_policy 测试全绿（零回归）

## Notes

### 这条为什么排在新功能第一个

改动面最小（纯函数加两个分支 + 两处数据填充），但**风险最高** ——
它直接决定「什么永不被删」。所以先做，做完再动 chunk 和 rerank。

### 与 007 的关系

`graph_degree` 的正确性依赖图能重建（007 的 T1）。
但本 spec 不**阻塞**于 007：degree 查询走现有图，不依赖重建能力。
只是等 007 做完后，能额外验证「重建后 degree 不变」。
