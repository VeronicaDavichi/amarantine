---
status: complete
created: 2026-11-04
priority: medium
tags:
- entity
- disambiguation
- backend
created_at: 2026-11-04T03:07:34.419433728Z
updated_at: 2026-11-17T03:24:51.765562627Z
completed_at: 2026-11-17T03:24:51.765562627Z
transitions:
- status: archived
  at: 2026-11-06T01:25:26.181576864Z
- status: in-progress
  at: 2026-11-17T03:24:36.705767437Z
- status: complete
  at: 2026-11-17T03:24:51.765562627Z
---
# 016 C.B3 三级实体消歧（大工程，先记录不实现）

> **状态：已归档（2026-11-06）。本文的 ② 级「向量近邻」方案已被实测否定，
> 由 `specs/026-entity-rule-disambiguation/` 取代。**
>
> 保留原因：这里记录了**两次**特征工程失败的数据（编辑距离、embedding 余弦），
> 是「为什么不能靠字面/向量判实体同一性」的重要实证，删掉会重犯第三次。
>
> 实测结论见 026 Overview：应合并最低 0.4806 < 不该合并最高 0.8801，
> 区间完全重叠，`ENTITY_ALIAS_SIM=0.92` 下 8 组应合并只过 2 组。

## Overview

014 审计把 `ENTITY_ALIAS_SIM` 当成「接个阈值」列进待办，实际读 ADR **C.B3** 后发现：
它要的是**整个三级消歧算法**，而当前实现只有第三级。

| 级别 | ADR 要求 | 现状 |
|---|---|---|
| ① 人工别名表精确匹配 | 命中即采用，**最高优先** | 有（`alias_resolver` / `alias_apply`） |
| ② 实体名 embedding 余弦 `≥ 0.92` | 采用 | **完全没有** |
| ③ LLM 判定 | **仅当 ② 落在 `[0.85, 0.92)` 灰区** | 有，但被当成唯一判据（confidence ≥ 0.5） |

### 与 ADR 的偏离不止一处

1. **没有向量近邻**：没有任何代码对**实体名**做 embedding
   （`embed_one` 只用于事件卡 summary）
2. **灰区逻辑不存在**：LLM 是「每次都问」，不是「只在灰区问」
3. **冲突规则未实现**：① 与 ②③ 冲突时 ① 赢；② 与 ③ 冲突时取 ② 标
   `disambiguation=low_confidence` —— 两条都没写
4. **`candidate_alias` 池没有**：ADR 要求「新实体不自动并入，创建新节点 +
   记 candidate_alias 进人工确认池」

所以这不是接线任务，是**新增一个消歧子系统**。

## 为什么先记录不实现

- 需要先决定**实体名向量存哪**：Weaviate 新 collection？还是复用 `MemoryEvent`？
  这会影响 D1 的存储模型，值得单独评审
- 需要**标定**：0.92 / 0.85 是 ADR 拍的初值，中文实体名的余弦分布未必适用，
  盲目接线可能比现在的 LLM 单级更容易误并
- 当前 LLM 单级是**能用**的（有 confidence + 审计痕迹），不是坏掉的

优先级低于 consolidation（P2 里有明确设计但完全没做）。

## Design

> 草案，待细化。

### D1 实体名向量存哪

倾向**独立 collection `MemoryEntity`**，与 010 的 chunk 层同样的理由：
不污染 `MemoryEvent` 的「全是事件卡」假设。

存的是 `(namespace, canonical_name, vector)`，`canonical` 去重。

### D2 三级判定的执行顺序

```
1. 查人工别名表 → 命中则用（最高优先，直接返回）
2. 算候选实体名的余弦
   - ≥ ENTITY_ALIAS_SIM     → 采用 ②
   - [GREY_LOW, SIM)        → 交给 ③ LLM
   - < GREY_LOW             → 认为是新实体
3. ② 与 ③ 冲突 → 取 ②，标 low_confidence
```

`GREY_LOW = 0.85` 现在也是硬编码在 ADR 里的魔数，应加配置项
（如 `ENTITY_ALIAS_GREY_LOW`），否则又是一次「假实现」。

### D3 `candidate_alias` 人工确认池

新实体先建节点，同时把「这个新实体可能与哪个已有实体是同一个」
记进池子。复用 003 的 alias-reject 表结构会更省事 —— 待定。

## Plan

- [x] ~~T1 决定 `MemoryEntity` collection 的 schema 与主键规则~~ —— **不做**：② 级不再用向量（见 026），没有实体名向量就不需要这个 collection
- [x] ~~T2 实体名 embedding 的写入时机~~ —— **不做**：同上，没有向量可写
- [x] T3 三级判定：① 别名表 / ② 确定性规则层 / ③ LLM 已实现（② 由 [026](../026-entity-rule-disambiguation/) 用规则替代向量，已 complete）
- [x] ~~T4 `ENTITY_ALIAS_GREY_LOW` 配置项~~ —— **不做**：灰区是向量方案的产物，规则层不需要灰区；省掉一个魔数配置
- [x] T5 `candidate_alias` 池：复用 `alias_reject`（reason=`candidate_ambiguous`），人可裁决 `accepted` 补建边（2026-11-17）

## Test

- [x] ~~D1 别名表命中时向量与 LLM 都不被调用~~ —— **不做**：无向量可调
- [x] ~~D2 余弦 ≥ 0.92 直接采用~~ —— **已被 026 实测否定**（区间完全重叠）
- [x] ~~D3 灰区调 LLM~~ —— **不做**：规则层取代灰区
- [x] D4 判为新实体 → 进 candidate 池（已实现，`test_016_candidate_pool.py`）
- [x] D5 冲突取规则层：`resolve_one` 规则命中即 `return`，不再问 LLM

## Notes

### 为什么不在 014 里一起做

014 只修「配置项与行为不一致」，都是小改动。C.B3 是新增子系统，
绑在一起会让 014 的验收标准模糊（到底算「配置生效」还是「新功能」）。

### 当前 LLM 单级的真实风险

`alias_resolver` 用 `confidence >= 0.5` 采信 LLM 的判断。
ADR C.B3 明确说「**为什么不让 LLM 直接判**：LLM 对『同名不同人』的判断不稳定，
且不留审计痕迹」。

所以现在的实现**违背了 ADR 写的理由**，只是恰好因为 `confidence` 阈值保守而
没有造成明显误并。这不是「能用就行」，是有明确已知缺陷 —— 但它的修复成本
远高于优先级，所以先记录。

### 中间态：编辑距离方案已被实测否定（2026-11-04）

原计划「用编辑距离挡住张三/张三丰」。**实测证明这条路走不通**：

| pair | 编辑距离 | 相似度 | 真实关系 |
|---|---|---|---|
| 豆包 / 豆包AI | 2 | **0.50** | 同人 |
| 张三 / 张三丰 | 1 | **0.67** | 不同人 |
| 微信 / WeChat | 6 | 0.00 | 同物 |

**该合并的相似度更低，该区分的更高。** 任何单一阈值都是反向的：

- 阈值 0.6 → 张三丰(0.67) 放行=误并，豆包AI(0.50) 挡住=误挡
- 阈值 0.4 → 两者都放行 = 等于没做
- 阈值 0.7 → 同物也被挡

而且跨语言别名（微信/WeChat 相似度 0.00）字面距离完全无效。

**结论**：编辑距离做不到它声称的事。硬做会产出「把该合并的挡住、
该区分的放行」的反向功能。

### 真正落地的中间态：spec 020

改用**中文人名结构**判定（`姓 + 单字名`）而不是字符串距离：

> `short` 是「姓+单字名」且 `long` 只比它多一个字 → 判为不同实体。

| pair | 判定 | 期望 |
|---|---|---|
| 张三 / 张三丰 | 挡住 | ✅ 不同人 |
| 豆包 / 豆包AI | 放行（豆非姓） | ✅ 同人 |
| 李小狼 / 小狼 | 放行（非前缀） | ✅ 同人 |

只覆盖一种可判定情形，**不是 C.B3**。本 spec 保持 `planned`。
详见 `specs/020-entity-single-char-prefix/`。

### 2026-11-17 · T5 完成；T1/T2/T4 与 D1–D3 明确「不做」

**结论先行**：这个 spec 只有一项值得做 —— **T5 candidate 池**。
其余任务都是「② 级用向量近邻」方案的附属物，而那个方案已被
[026](../026-entity-rule-disambiguation/) 实测否定（应合并最低 0.4806 <
不该合并最高 0.8801，区间完全重叠）。删掉不算损失，留着会诱导重犯。

**做了什么（T5）**

以前 `resolve_one` 判「有候选但不合并」时，只写一行 `logger.info` ——
**人永远不知道机器看过这两个名字、没敢合**。现在：

| 环节 | 变化 |
|---|---|
| `resolve_one(declined=...)` | 三种拒绝原因入池：`single_char_prefix` / `llm_undecided` / `llm_low_confidence` |
| `resolve_claims(declined=...)` | 批量透传 |
| `alias_apply` | 收集后 `record_safe(reason="candidate_ambiguous", side="candidate")` |
| 管理接口 | 复用 `GET /admin/alias-rejects` 列出、`POST /admin/alias-rejects/resolve` 裁决 |
| 裁决 `accepted` | `repair_accepted_edge(force=True)` 补建边 —— 闭环 |

**真库验证**（`harness/verify_016.py`）

```
写入 1 条（此前待办 0）
待办 1 条：
  张三丰 vs 张三 | side=candidate | reason=candidate_ambiguous | detail=single_char_prefix
池子写入+可列出: ✓
裁决后移出待办: ✓
```

**为什么复用 `alias_reject` 而不是新建表**

同一张表已经在装「判据说不行」的记录，字段（alias/canonical/side/reason/detail/
resolution）完全够用；新建表意味着第二套管理接口、第二个待办列表，
人要在两个地方看同一件事。

**测试**：`test_016_candidate_pool.py`（3 条）；既有别名测试 44 条不回归。

**不做清单（不是遗漏，是决定）**

| 任务 | 不做的理由 |
|---|---|
| T1/T2 `MemoryEntity` collection + 实体名 embedding | 没有向量需求了 |
| T4 `ENTITY_ALIAS_GREY_LOW` | 灰区是向量方案产物，规则层不需要 |
| D1–D3 向量相关断言 | 同 026 实测结论 |
