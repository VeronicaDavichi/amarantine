---
status: complete
created: 2026-11-03
priority: medium
tags:
- acceptance
- vector
parent: '001'
created_at: 2026-11-03T03:30:01.040870835Z
updated_at: 2026-11-04T02:15:03.226749105Z
completed_at: 2026-11-04T02:15:03.226749105Z
transitions:
- status: in-progress
  at: 2026-11-04T02:05:07.423588568Z
- status: complete
  at: 2026-11-04T02:15:03.226749105Z
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

- [x] T1 先完成 013（A27 rerank），确认能拿到 per-chunk 分数
- [x] T2 设计 schema 变更：chunk 与 card 的区分字段（如 `is_chunk: bool` + `parent_event_id`）
- [x] T3 `core/chunker.py`：纯函数切分（可单测，无 IO）
- [x] T4 写入路径：卡照写，额外写 chunk 向量
- [x] T5 检索路径：chunk 命中 → 聚合到 `parent_event_id` → 每卡保留 N 个
- [x] T6 测试：`core/chunker.py` 单测 + 聚合逻辑单测（不碰真库）

## Test

- [x] D1 一张卡切出 5 个 chunk、`CHUNK_KEEP_PER_EVENT=2` → 检索引擎只进 2 个
- [x] D2 被保留的 2 个是分数最高的 2 个（顺序可验证）
- [x] D3 聚合后 `materials` 里该 `event_id` 只出现一次（chunk 不外泄给 LLM）
- [x] D4 没 chunk 的旧卡仍能被检索（向后兼容）
- [x] D5 切分纯函数：给定文本，输出确定（同输入同输出）

## Notes

### 为什么标「最后做」

它动的是**存储结构**，而其余五条动的是判定或接口。
存储结构一旦污染，回滚成本最高。

### 如果不想做

备选方案：**改 ADR**，明确写「P0 不做 chunk 层，每卡一向量；
`CHUNK_KEEP_PER_EVENT` 在 P0 不生效」。这比留一个没实现的配置项干净。

用户已选择「实现功能」，所以按上述执行。

## 实现记录

### 关键设计决策：chunk 用**独立 collection**，不是同表加标记

spec 的 T2 原本写「chunk 与 card 的区分字段（如 `is_chunk` + `parent_event_id`）」，
即塞进 `MemoryEvent`。实施时改成**独立的 `MemoryChunk` collection**。

理由：现有代码里到处都是「`list_all` 拿全部卡」的假设 ——
计数、软删、归档、`check_rebuild.py`、`forget` 扫描。
往同一个 collection 混入 chunk 会让**每一处**都得加 `is_chunk == false` 过滤，
漏掉一处就静默算错。

独立 collection 的代价是「检索多查一次」，收益是**现有路径一行不用改**。
对一个已经在跑的系统，后者重要得多。

结果：`weaviate_store.py` **零改动**，A16 全部逻辑在 `chunk_store.py` 里。

### 开关默认 false，零风险上线

`CHUNK_ENABLED=false` 时：
- `remember` 不写 chunk
- `recall` 不查 chunk（连 `ChunkStore` 都不会被实例化，有测试钉住）
- 现有 959 条测试全绿

### 真 e2e 抓到的 bug：`date_start` 是 `date` 对象

713 字的卡、切 4 块、`embed_texts` 批量编码都正常，但 chunk 一块都没落库：

```
chunk 写入失败（已忽略，卡已写成功）: Object of type date is not JSON serializable
```

`EventCard.date_start` 是 `date`，直接塞进 JSON body 序列化失败，
而 `remember` 里的 `except` 把它吞成 warning —— **表现为静默的「0 块」**。

修在服务层（`insert_chunks` 里统一 `isoformat()`），调用方不用关心。
这类 bug 单测测不出来（fake store 不序列化），只有真跑才会暴露。

### 第二个坑：幂等短路导致 e2e 假绿

第一次修完 date 后重跑 e2e，chunk 还是 0 块 —— 因为同一条文本的
`event_id` 是确定的，复用 namespace 会命中**幂等短路**，
`insert_event` 都不执行，更不会写 chunk。

e2e 脚本改成每次用新 namespace。**幂等是特性，但它会让「重跑 e2e」变成假绿。**

### 测试隔离：`.env` 的开关会污染不相关的测试

主人把 `.env` 的 `RERANK_ENABLED` 打开后，`test_conflict_versions`
里一条「三条不同实体不应被折叠」的用例挂了 —— 因为 `_run()` 用
`load_settings()` 读到 true，真的去调了 8083，rerank 把不相关的卡
判成低分砍掉。

修法：`tests/test_recall.py` 的 `_run()` 里**默认关掉 rerank 和 chunk**，
需要的用例显式传自己的 settings。

**教训**：测试不能依赖 `.env`。环境一变测试就漂移，而且崩的是
看起来毫不相干的用例。

## 验收证据

- 纯函数：`pytest tests/test_chunker.py -q` → **18 passed**
- 接线：`pytest tests/test_chunk_layer.py -q` → **6 passed**
- 全量：`pytest tests -q --tb=line` → **959 passed**（935 + 18 + 6）
- 真 e2e（`harness/e2e_chunk.py`，711 字）：
  - 切分 4 块，落库 **4 块**
  - chunk 检索 4 命中，score 0.7033 / 0.6833 / 0.6706 / 0.4917
  - 召回 `chunk_hits=4`、`chunk_parents=2`（`CHUNK_KEEP_PER_EVENT=2` 生效）
  - `materials` 里该 `event_id` **只出现一次**（D3 聚合正确）
- 测试数据已清理（`DELETE /v1/batch/objects` on `chunke2e*`）
