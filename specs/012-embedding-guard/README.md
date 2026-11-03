---
status: planned
created: 2026-11-03
priority: critical
tags:
- acceptance
- vector
parent: '001'
created_at: 2026-11-03T03:30:01.272668672Z
updated_at: 2026-11-03T03:30:01.272668672Z
---

# A25：换 embedding 模型必须拒绝写入

## Overview

ADR A25 原文：

> 把 `EMBEDDING_MODEL` 改为另一模型后向旧索引写入，返回**错误**（非告警），且索引未被污染

**现状：零校验。**

`EMBEDDING_MODEL` 只出现在一个地方 —— 请求体里发给 embedding 服务：

    payload = {"input": texts, "model": settings.EMBEDDING_MODEL}

没有人检查「索引里已有的向量是哪个模型产的」。

## 为什么这是硬伤

不同模型的向量**维度可能相同、空间完全不同**。
把 B 模型的向量写进 A 模型的索引，余弦相似度依然会算出数字 ——
**不会报错，只会给出错误的相似度**。

这是最危险的一类故障：**静默的、看起来正常的错误**。
检索质量慢慢烂掉，而所有监控都是绿的。

## 非目标

- 不做多模型共存（一个索引只服务一个模型）
- 不做自动重新 embed（换模型后要重建，那是 007 的 `rebuild_vectors.py`）
- 不改 embedding 服务本身

## Design

### 版本记在哪

两个候选：

1. Weaviate 每条数据带 `embedding_model` 字段 —— 冗余但自证
2. 单独一张元数据表/class，记录「本索引的模型」 —— 干净但多一次读

**推荐 1 + 2 并存**：
- 每条数据带 `embedding_model`（便于排查「哪批数据是旧的」）
- 索引级 meta 记录当前模型（写入前的快速校验）

只做 1 的话，空索引无法判断该用哪个模型（第一条数据进来时无从比对）。

### 校验时机

写入**之前**，在 `remember` 的最前面（embedding 调用之前）。
理由：embedding 调用要花钱/时间，先拦住。

### 报错形态

必须是**错误**（HTTP 4xx / 异常），不是 warning。
ADR 特意写了「非告警」—— 因为告警会被忽略。

建议：`EmbeddingModelMismatchError`，HTTP 映射 409（冲突，而非 400 参数错）。

## Plan

- [ ] T1 Weaviate schema 加 `embedding_model` 字段
- [ ] T2 索引级 meta：记录当前模型名（Weaviate class 描述或独立 SQLite 表）
- [ ] T3 `services/embedding_guard.py`：读当前模型 → 比对 → 不一致抛异常
- [ ] T4 `remember` 最前面接入校验
- [ ] T5 空索引时以第一条写入的模型为准（并记录）
- [ ] T6 测试：一致通过、不一致报错、空索引初始化

## Test

- [ ] D1 模型一致 → 正常写入
- [ ] D2 模型不一致 → 抛 `EmbeddingModelMismatchError`，且**没有调用 embedding 服务**
- [ ] D3 报错后索引条数不变（未被污染）
- [ ] D4 空索引首次写入 → 记录当前模型
- [ ] D5 换回原模型 → 又能写入（不是永久锁死）

## Notes

### D2 的「没有调用 embedding 服务」怎么测

用 spy 替身记录调用次数即可，不需要真服务。
这条很重要 —— 校验放在 embedding 之后也能「拦住」，但白花了一次钱。
