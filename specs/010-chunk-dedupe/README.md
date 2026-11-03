---
status: planned
created: 2026-11-03
priority: medium
tags:
- acceptance
- vector
parent: '001'
created_at: 2026-11-03T03:30:01.040870835Z
updated_at: 2026-11-03T03:30:01.040870835Z
---

# A16：冗余片段去重（chunk 层）

## Overview

ADR A16 原文：

> 同 `event_id` 下 chunk 数 > `CHUNK_KEEP_PER_EVENT` 时，仅保留 rerank 最高分的 N 个进检索引擎

ADR 的 D1 设计里，一条事件卡会被切成多个 chunk 各存一个向量，
检索命中的是 chunk，返回前聚合成卡。

**现状：项目里没有 chunk 层。**

- `CHUNK_KEEP_PER_EVENT = 2` 在 `config.py` 有默认值
- `src/` 里**零引用**
- 每张卡写入时一个向量（`embedding.py` 一次调用一条 summary）

所以 A16 不是「没写测试」，是**整个 chunk 层不存在**。

## 这条为什么最重

它是六个功能里唯一改**数据结构**的：

- Weaviate schema 要区分「card 向量」与「chunk 向量」
- 写入路径要 chunk 化（切分策略、每 chunk 的 `event_id` 外键）
- 检索路径要「chunk 命中 → 聚合到卡 → 再按 N 去重」

改错了会污染现有全部向量，所以**最后做**。

## 非目标

- 不做语义切分（先用长度滑窗，够用即可）
- 不改已有卡的向量（存量不迁移，新写入才 chunk 化）
- 不做 rerank 分数驱动（A27 还没实现，先用向量相似度排序）

## Design

### 先决条件：A27 必须先做

ADR 原文说「保留 **rerank 最高分**的 N 个」。没有 rerank 就只能按向量分排 ——
这会让 A16 与 A27 的验收互相依赖。**建议 A27 先落地**，A16 复用它的分数。

若 A27 因故不做，本 spec 需在 Notes 里明确「用向量分代替 rerank 分」的偏离。

### 存量怎么办

**不迁移。** 理由：
- 存量卡本来就没 chunk，硬切需要重新 embed 全部数据，成本高且可逆性差
- 新旧并存不影响正确性（旧卡退化为「1 个 chunk」）
- 真要统一，等 007 的 `rebuild_vectors.py` 成熟后跑一次

### 切分策略

长度滑窗：每 `CHUNK_SIZE` 字切一段，相邻重叠 `CHUNK_OVERLAP` 字。
配置项已存在则复用，不存在则新增（需在 config 里加并写注释）。

## Plan

- [ ] T1 先完成 013（A27 rerank），确认能拿到 per-chunk 分数
- [ ] T2 设计 schema 变更：chunk 与 card 的区分字段（如 `is_chunk: bool` + `parent_event_id`）
- [ ] T3 `core/chunker.py`：纯函数切分（可单测，无 IO）
- [ ] T4 写入路径：卡照写，额外写 chunk 向量
- [ ] T5 检索路径：chunk 命中 → 聚合到 `parent_event_id` → 每卡保留 N 个
- [ ] T6 测试：`core/chunker.py` 单测 + 聚合逻辑单测（不碰真库）

## Test

- [ ] D1 一张卡切出 5 个 chunk、`CHUNK_KEEP_PER_EVENT=2` → 检索引擎只进 2 个
- [ ] D2 被保留的 2 个是分数最高的 2 个（顺序可验证）
- [ ] D3 聚合后 `materials` 里该 `event_id` 只出现一次（chunk 不外泄给 LLM）
- [ ] D4 没 chunk 的旧卡仍能被检索（向后兼容）
- [ ] D5 切分纯函数：给定文本，输出确定（同输入同输出）

## Notes

### 为什么标「最后做」

它动的是**存储结构**，而其余五条动的是判定或接口。
存储结构一旦污染，回滚成本最高。

### 如果不想做

备选方案：**改 ADR**，明确写「P0 不做 chunk 层，每卡一向量；
`CHUNK_KEEP_PER_EVENT` 在 P0 不生效」。这比留一个没实现的配置项干净。

用户已选择「实现功能」，所以按上述执行。
