---
status: draft
created: 2026-11-27
priority: high
tags:
- memory
- graph
- forget
- attention
created_at: 2026-11-27T04:21:47.963026300Z
updated_at: 2026-11-27T04:22:09.429563030Z
---
# 实体关注度：attention 0–1 控制摘要详略、遗忘与弱边召回

## Overview

**主人提的想法**：给指定的人/事物一个 0–1 的关注度，交给 LLM 输入；
不输入就是日常记忆。数值越大，摘要越长、内容越丰富。

**用户的补充澄清（决定了本 spec 的边界）**：

> attention，核心是关注度，关注多的人记住他的细节越多，重要性越高，
> 内容越丰富，不仅仅是要不要遗忘，更是他的**弱边也会被更多的考虑进来**。

所以 attention 有**三层作用**，不是单纯的「摘要长度开关」：

| 层 | 作用 | 现状 |
|---|---|---|
| ① 写入 | 摘要详略分档（一句话 / 两三句 / 完整段落） | ❌ 全是「一句话」 |
| ② 遗忘 | 关注对象更难被忘 | ⚠️ 只有 `importance`，与「是谁」无关 |
| ③ 召回 | **弱边（probable/低 confidence）被更多考虑** | ❌ 图通道不看 attention |

### 与现有 `importance` 的区别

`importance`（0–1，`core/guards.merge_importance`）是**单条记忆**的属性，
影响排序与遗忘，**不影响摘要长度**（那是切分提示词决定的）。

`attention` 是**实体的属性**（"小张有多重要"），跨记忆持久存在。
「小张换了工作」这件事只发生一次，但我对小张的关注是长期的 ——
这就是两者不能合并的原因（用户明确选了新增 `attention`）。

## Design

### 存储：SQLite 表（不挂图上）

```
entity_attention(
  namespace TEXT, name TEXT,
  attention REAL,          -- 0.0–1.0
  note TEXT DEFAULT '',    -- 为什么关注（人可读）
  decided_by TEXT,         -- 'manual' | 'llm'
  updated_at TEXT,
  PRIMARY KEY (namespace, name)
)
```

**为什么不用图节点属性**：图是派生层、可整张重建（ADR D2）；
关注度是**人给的意愿**，不能因为重建图就丢。与 `entity_suppress` 同思路
（也是 SQLite，理由相同）。

### 写入路径：attention 影响摘要详略

**循环依赖问题**：摘要 → 提实体 → 查 attention → 改摘要长度。

**解法**：用**原文**粗提实体，不用摘要。原文里出现「小张」就能匹配到 attention：

```
ingest 原文
  → extract_entities(原文)（jieba + 名册，已有）
  → 查 entity_attention，取 max
  → 分档决定提示词要求
  → split_with_sources(..., detail_level=...)
  → 逐条 remember_event
```

分档（用户选定「改提示词分档」）：

| attention | 摘要要求 | 预期长度 |
|---|---|---|
| 未配置 / < 0.3 | 一句话事件描述（现状） | ~17 字 |
| 0.3–0.7 | 两三句：含时间、人物、动作、结果 | ~50 字 |
| ≥ 0.7 | 完整段落：再加背景、原因、后续影响 | ~120 字 |

一条记忆涉及多个实体时取 **max**：只要有一个高关注对象，这条就值得记细。

### 遗忘路径：attention 抬高保护

`forget` 的保留判定里，卡片涉及高 attention 实体时提升保护等级
（不替换 `pinned` 语义，只是把「可忘」降级为「观察」）。
与 `importance` 独立：一条 `importance` 低但涉及关注对象的卡不该被清掉。

### 召回路径：弱边按 attention 放宽

这是用户最强调的一层。现状：关系边有 `label`（exact/probable）和
`confidence`，但图通道**不看 attention**，弱边一律同等对待。

改动：图通道查询时，**若 query 涉及高 attention 实体**，
把 `probable` / 低 confidence 边的阈值放宽：

```
低 attention（<0.3）：只用 exact + confidence >= 0.6
中（0.3–0.7）：      exact + confidence >= 0.4
高（>=0.7）：        exact + probable（confidence 不设门槛）
```

同时在图通道返回的 event_id 上加权（RRF 里给更高位次）——
因为他关注这个人，与他的间接关联也更有价值。

### 输入方式

| 入口 | 形态 |
|---|---|
| 管理接口 | `PUT /admin/entities/{name}/attention` body `{"attention": 0.8, "note": "..."}` |
| 查询 | `GET /admin/entities/attention` 列出已设置的 |
| MCP 工具 | `memory_attention(name, attention)` —— LLM 可主动说「我很关注小张」 |
| 默认 | 不存在该实体的记录 = 日常（<0.3 档），**不预先写库** |

### 幂等坑（必须写明）

`event_id` 的哈希里含 `summary`。attention 变了 → 摘要长度变 → `summary` 变
→ **event_id 变** → 同一条记忆重报会变成两张卡。

**处理**：attention 变更**只影响之后新写入**的记忆，不回改历史卡。
在文档里写清；不提供「按新 attention 重写历史摘要」的批量操作
（那是另一个量级的重灌，需要单独评估）。

## Plan

- [ ] T1 `services/entity_attention.py`：表 + CRUD + `max_attention(names)`
- [ ] T2 管理接口三件（GET 列表 / PUT 设置 / DELETE 取消）
- [ ] T3 `llm_split`：`detail_level` 参数 → 提示词三档
- [ ] T4 `ingest` 接线：原文粗提实体 → 查 attention → 传 detail_level
- [ ] T5 `graph_search`：按 attention 放宽弱边阈值
- [ ] T6 `forget`：attention 抬高保护等级
- [ ] T7 MCP 工具 `memory_attention`
- [ ] T8 文档：user-manual「关注度」一节 + 三档说明；degradation 不涉及

## Test

| 编号 | 验收 | 手段 |
|---|---|---|
| D1 | 未配置时摘要长度与今天一致（不回归） | 真库：同一段原文前后摘要比对 |
| D2 | attention=0.8 时摘要明显更长，且含时间/人物/动作 | 真库：写入 5 条对比字数分布 |
| D3 | 多实体取 max：A=0.9 B=0.1 → 走高档 | 单测 |
| D4 | 空/未配置的名字不报错，按日常档 | 单测 |
| D5 | 图通道：高 attention 时 probable 边被召回，低时不被召回 | 单测（fake driver）+ 真库抽查 |
| D6 | 遗忘：涉及高 attention 实体的卡不进入可忘桶 | 单测 |
| D7 | attention 变更不改历史卡的 summary/event_id | 真库：改 attention 后查旧卡哈希不变 |
| D8 | 存量单测零回归 | pytest |

## Notes

### 为什么「弱边」这层最重要

前两层（摘要长度、遗忘）是**让记忆更细**；
第三层是**让检索更愿意联想**。用户原话：

> 关注多的人记住他的细节越多……更是他的弱边也会被更多的考虑进来

对一个你在意的人，系统应该努力把间接的、置信度不高的关联也翻出来 ——
即使会多几条噪声，也比漏掉强。这是**产品取向**，不是纯技术优化，
所以阈值放宽只在 attention 高时生效，不影响日常查询的精度。

### 不做什么

| 不做 | 理由 |
|---|---|
| 合并 `importance` 与 `attention` | 一个是单条属性、一个是实体属性，语义不同（用户明确选择） |
| attention 变更后重写历史摘要 | 会改 `event_id`，一条记忆裂成两条；要重灌，属另一件事 |
| attention 自动学习（从交互频次推断） | 先做显式输入；自动推断需要行为数据，目前没有 |
| 让 attention 影响 `event_id` | 哈希已含 summary，再加 attention 会让幂等更脆 |
