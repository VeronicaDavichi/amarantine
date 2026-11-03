---
status: complete
created: 2026-11-03
priority: high
tags:
- acceptance
- recall
parent: '001'
created_at: 2026-11-03T03:30:00.940830072Z
updated_at: 2026-11-03T05:36:38.279917582Z
completed_at: 2026-11-03T05:36:38.279917582Z
transitions:
- status: in-progress
  at: 2026-11-03T05:33:34.180795500Z
- status: complete
  at: 2026-11-03T05:36:38.279917582Z
---
# A6：同实体两版本事实必须都返回，不许合并

## Overview

ADR A6 原文：

> 同实体两版本事实返回 2 条、各带时间戳、不合并

场景：主人两次说项目A的进度 —

- 2026-08：「项目A延期到六月」
- 2026-10：「项目A又延期到七月」

这是**两张卡**（`event_id` 不同），但 `entities` 里都有 `项目A`。
召回时如果把「同实体」当成同一件事去重，只会返回一条，
主人就看不到「变更」这个信息 —— 而变更恰恰是记忆最该记住的东西。

## 现状

- 召回层的 RRF 合并按 `event_id` 做（`rrf.py`），**没有按实体合并的逻辑**
- 所以**行为上大概率是对的**，但**没有任何测试钉住它**
- 一旦有人为了「减少重复」在召回层加一个「按实体去重」的优化，
  这个语义就静默消失了

**结论：先核实是否已正确；若正确，本 spec 退化为「补测试 + 写清约束」。**

## 非目标

- 不做「冲突检测」（判断两版本哪个对）—— 那是主人的判断，不是系统的
- 不做版本链（`SUPERSEDES` 边）—— 属 P2
- 不改 RRF 的算法

## Design

### 为什么不合并才是对的

记忆系统的价值一半在「当时是怎么说的」。
把两版本压成一条，等于丢失时间维度 —— 而时间维度正是 ADR D3 反复强调的。

所以约束要写死在两个地方：
1. 代码注释（说明这是**故意不合并**，不是漏了去重）
2. 测试（两版本都返回，且各自带自己的 `date_start`）

### 怎么构造这个测试

不需要新服务：
- 两张卡，`entities` 都含 `项目A`，`date_start` 相差两个月
- 同一个 query
- 断言 `materials` 里两条都在，且 `date_start` 不同

关键是**断言两条都在**，而不是断言「返回了 2 条」——
如果去重逻辑把别的也合并了，数字对得上但内容错了。

## Plan

- [x] T1 核实 `rrf.py` / `recall.py` 是否有任何按 entity 去重的路径
- [x] T2 若无：补 `tests/test_conflict_versions.py`（两版本都返回 + 时间戳不同）
- [x] T3 在 `rrf.py` 加一句注释：按 `event_id` 合并是**故意**的，不许改成按实体
- [x] T4 （不适用：核实后确认 rrf_fuse 按 event_id 合并、recall 零去重，无需删任何东西）

## Test

- [x] D1 两条同实体不同版本 → `materials` 含 2 条
- [x] D2 两条的 `date_start` 不同且都非空
- [x] D3 两条的 `event_id` 不同（证明不是同一张卡被返回两次）
- [x] D4 加一条不相关实体 → 返回 3 条，确认没有按实体折叠

## Notes

### 为什么这条可能只是补测试

ADR 写 A6 的时候，防的是「未来的自己为了性能做合并优化」。
现阶段实现如果没有合并逻辑，那么 A6 是**尚未被违反**，不是**已被实现**。
这两者在审计表里都该算「需要一条断言」，但性质完全不同 ——
所以 T1 的第一步是核实，不预设立场。

### 核实结果（T1）

**结论：本来就对，一行实现都不用改。**

- `rrf.py::rrf_fuse` 的合并键是 `event_id`：`scores: dict[str, float]`，
  按 id 累加分值
- `recall.py` 里 `grep -nE "dedup|seen|by_entity|merge"` → **零命中**
- `materials` 的构造是 `[_material(meta[eid], by_id[eid]) for eid in decision.selected]`
  —— 按 id 逐个展开，没有任何折叠

所以本 spec 从「补功能」退化为「补断言 + 写死约束」，这与 spec 里
预设的 B 方案一致。

### 做了什么

1. `tests/test_conflict_versions.py`（5 条）：两版本都返回、是两张不同卡、
   各带自己的时间戳、不相关实体不被折叠、摘要不合并
2. `rrf.py` 的 `rrf_fuse` 里加了一段注释，明确写「合并键是 event_id，
   **这是故意的**，不要加按 entity 去重的优化」，并指向测试文件

第 2 条比第 1 条更重要 —— 测试能发现回归，注释能阻止回归被写出来。

### 一个测试细节

query 特意去掉了时间 hint（写成「项目A的进度」而不是「上个月项目A的进展」）。
否则 `date_range` 过滤会把「六月」那个旧版本筛掉，测的就变成
「时间过滤」而不是「版本不合并」了。

## 验收证据

- 单测：`pytest tests/test_conflict_versions.py -q` → **5 passed**
- 全量：`pytest tests -q --tb=line` → **915 passed**（910 + 5）
- 核实：`rrf_fuse` 合并键 `event_id`；`recall.py` 零去重逻辑
