---
status: complete
created: 2026-11-03
priority: critical
tags:
- acceptance
- vector
parent: '001'
created_at: 2026-11-03T03:30:01.272668672Z
updated_at: 2026-11-03T04:36:39.221807477Z
completed_at: 2026-11-03T04:36:39.221807477Z
transitions:
- status: in-progress
  at: 2026-11-03T04:32:35.253007045Z
- status: complete
  at: 2026-11-03T04:36:39.221807477Z
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
2. 单独一张元数据表，记录「本索引的模型」 —— 干净但多一次读

**实际实现：只做 2（`embedding_index_meta` 表）。**

原计划 1+2 并存，实施时否掉了 per-row 字段：

- 本 spec 前提是「一个 namespace 只服务一个模型」，per-row 字段就是
  **同一个值重复 N 遍**，没有新增信息
- 空索引时 meta 必须能读（第一条数据进来前就要判断），只有 2 满足
- 加字段要动 schema + `to_properties` + `_FIELDS` + 回填，为冗余值不值

真做多模型共存时再加 per-row 字段，那时 meta 也要改成 `(namespace, model)` 复合键。

### 校验时机

写入**之前**，在 `remember` 的最前面（embedding 调用之前）。
理由：embedding 调用要花钱/时间，先拦住。

### 报错形态

必须是**错误**（异常），不是 warning。ADR 特意写「非告警」—— 因为告警会被忽略。

**落地差异**：spec 原写「HTTP 409」，但**没有单条记忆写入的 HTTP 端点**
（只有 `/admin/ingest` 批量），所以 409 无处映射：

| 入口 | 落地方式 |
|---|---|
| MCP `memory_remember` | `isError: true` + 含两个模型名的消息 |
| `/admin/ingest` | 该条进 `errors`，其它 turn 继续 |

两者都是显式错误，不会被误读成成功。409 等有单条写入端点时再启用。

## Plan

- [x] T1 （实现时否决：per-row 字段冗余，理由见 Design）
- [x] T2 索引级 meta：`embedding_index_meta` 表（namespace 主键）
- [x] T3 `services/embedding_guard.py`：读当前模型 → 比对 → 不一致抛异常
- [x] T4 `remember` 最前面接入校验
- [x] T5 空索引时以第一条写入的模型为准（并记录）
- [x] T6 测试：一致通过、不一致报错、空索引初始化

## Test

- [x] D1 模型一致 → 正常写入
- [x] D2 模型不一致 → 抛 `EmbeddingModelMismatchError`，且**没有调用 embedding 服务**
- [x] D3 报错后索引条数不变（未被污染）
- [x] D4 空索引首次写入 → 记录当前模型
- [x] D5 换回原模型 → 又能写入（不是永久锁死）

## Notes

### D2 的「没有调用 embedding 服务」怎么测

用 spy 替身记录调用次数即可，不需要真服务。
这条很重要 —— 校验放在 embedding 之后也能「拦住」，但白花了一次钱。

### 实现记录

**1 · 加了一个 spec 里没写的参数。**
`remember_event` 新增 `meta: EmbeddingIndexMeta | None = None`，为让测试注入
临时库。缺省时按 `_db_path(settings)` 自建 —— 生产路径零改动。

**2 · 测试暴露了写入路径的真实顺序。**
第一次写 `test_remember_registers_model_on_first_write` 时挂了：
`_BoomStore` 缺 `get_by_event_id`。这说明真实顺序是

    校验 → 打标 → 算 event_id → **查幂等** → embed → 写库

而不是「校验 → 写库」。这个顺序对 A25 是好事：幂等查询比 embed 便宜，
校验排在它前面，被拦住时连一次数据库读都不用。

**3 · 一个没做的判断，而不只是没写的代码。**
原设计要给每条数据加 `embedding_model` 字段。真写的时候发现：
在「一个域一个模型」的前提下，这个字段永远是同一个值。
**所以删掉了这个计划**，而不是「实现了但没用」。理由写在 Design 里。

## 验收证据

- 单测：`pytest tests/test_embedding_guard.py -q` → **8 passed**
- 全量：`pytest tests -q --tb=line` → **879 passed**（871 + 8）
- 零污染：`back_end/data/memory.db` 时间戳未变（测试全用 tmp_path）
- 守门：`check_guide.py` / `check_spec.py` exit 0
