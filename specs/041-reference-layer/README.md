---
status: in-progress
created: 2026-11-06
priority: high
tags:
- graph
- reference
- semantics
- draft
depends_on:
- '038'
- '045'
created_at: 2026-11-06T08:48:18.266962385Z
updated_at: 2026-11-09T05:31:41.758535138Z
transitions:
- status: planned
  at: 2026-11-09T05:13:52.359717712Z
- status: in-progress
  at: 2026-11-09T05:31:41.758535138Z
---
# 指称层：字面 → 义项（行话 / 称呼 / 账号族都是它的一种）

> **Status**: planned · **Priority**: high · **Created**: 2026-11-06

## Overview

**执行顺序**：`043 身份 → 038 称呼 → 045 键 → 041 指称（本 spec）→ 042 账号族`。

**问题**：系统把「字面」直接当「指称」。

```
「鱼」这种单字概念进不了图（extract_entities 有 len < 2 的约束）
「王老师」与「王泳」靠 ALIAS_OF 连，但那条边没有 scope（全局生效）
同名多账号无法表达（通讯录里已有 3 个「智小安业务助手」、2 个「AI助理」）
```

**核心判断**：行话、称呼、账号族是同一个问题的三次应用 ——

| 类型 | 字面 | 指称 | 主要证据 |
|---|---|---|---|
| 称呼 | 王老师 | 王泳（人） | 同句 `@真名`、自称、`称呼X为Y` |
| 账号族 | `xxx的大肥鱼` | 鱼（某主体） | 账号名族 + 归属断言 |
| 行话 | 鱼 | deepseek agent | 称呼位 + 群内异常 + 定义句 |

差别只在**指称对象**和**证据来源**；`scope + 弱证据 + 留痕` 完全相同，所以做**一层**。

**目标**：让「任意名词」在**某个 scope 内**长出「义项」，带证据与强度，
可查询、可升级、可否认，且**永远不把弱证据用于破坏性操作**。

**非目标**

| 不做 | 理由 |
|---|---|
| 预置行话词典 | 行话是长出来的，不是配出来的 |
| 全量逐词检测 | 成本爆炸，绝大多数词义项没变 |
| 跨群自动泛化（cluster） | 需要群成员重叠数据；**v1 只做 session + global** |
| 用弱证据合并实体 / 去重 | 见「两条硬规则」 |

## Requirements

- **REQ-1** **WHEN** 一个名词在某 scope 内的触发分 ≥2 **THEN** SHALL 建 `Sense` 候选
  （`status=uncertain`），≥4 且无矛盾 **THEN** SHALL 升为 `probable`。
- **REQ-2** **WHEN** 文本里出现 `@w` / `w说` / `问w` / `w帮我` / `w在吗`
  **THEN** SHALL 记为「称呼位」证据（+3）。
- **REQ-3** **WHEN** 出现定义句（`w是X` / `w指X` / `我们说的w是X`）
  **THEN** SHALL 记为定义证据（+3）并填 `gloss`。
- **REQ-4** **WHEN** 一条 `Claim` 的 `mode=question`（「这个是鱼吧？」）
  **THEN** SHALL 只写 Claim、**不建边**、不计入升级阈值。
- **REQ-5** **WHEN** 同一 `Term` 在同一 scope 有多个义项 **THEN** SHALL 允许多个 `Sense` 共存；
  一条 `Mention` 最多指 2 个，超过 **THEN** `unresolved=true` 且不建边。
- **REQ-6** **WHEN** 查询带 `tier=strict` **THEN** SHALL 只用 `exact`；
  `default` **THEN** `exact + probable`（命中标注「疑似」）；`explore` **THEN** 含 `uncertain`。
- **REQ-7** **WHEN** 人否认一个 Sense **THEN** SHALL 置 `status=denied`（保留证据链，不删）；
  `strict/default` 查询不再命中，`explore` 仍可见。

## 不变行为

- **WHEN** 事件卡写入 **THEN** 现有的 `MENTIONS` / `INVOLVES` 语义 SHALL CONTINUE TO 不变
- **WHEN** 同名字符串出现在普通文本 **THEN** SHALL CONTINUE TO 只当字面，不建 Sense
- **WHEN** `event_id` 计算 **THEN** SHALL CONTINUE TO 不包含任何 Sense/Mention 数据
- **WHEN** `extract_entities` 的 `len < 2` 约束 **THEN** SHALL CONTINUE TO 保留
  （指称层不靠放宽它来收单字，而是靠 `@`/定义句等**触发信号**单独建 Sense）

## Design

### 1. 数据模型

```
(:Term    {text:"鱼"})                                   ← 字面，全局
(:Sense   {term:"鱼", scope:"session:qq:123", gloss:"deepseek agent",
           status:"probable", confidence:0.6, evidence:[]})   ← 义项，scope 内
(:Mention {surface:"鱼", scope:"session:qq:123", ts, quote_id})
(:Claim   {mode:"assert|question|deny", speaker, ts})

(:Term)-[:HAS_SENSE]->(:Sense)
(:Mention)-[:USES_SENSE {label:"probable"}]->(:Sense)
(:Claim)-[:SUPPORTS|DENIES]->(:Sense)
```

- **唯一键**：`Sense(term, scope, gloss)`；`Term(text)`；`Mention(quote_id, surface)`
- `scope` 取值：`session:<cid>` / `global`（v1 不建 cluster）
- 选不了就 `unresolved`，**留痕不猜**

### 2. 触发式发现（不做全量）

| 信号 | 分值 | 例 |
|---|---|---|
| 称呼位 `@w` / `w说` / `问w` / `w帮我` / `w在吗` | +3 | `@鱼 帮我查下` |
| 定义句 `w是X` / `w指X` / `我们说的w是X` | +3 | `鱼就是那个 deepseek 的 bot` |
| 群内存在同名账号（`Account.nick` 含 w） | +2 | 账号 `xxxx的大肥鱼` |
| 归属断言 `X账号是谁的w` | +2 | 主体待填槽 |
| 群内/群外频次异常（TF-IDF） | +1 | **仅候选**，不单独建 Sense |
| 宾语位 + 食物/动作语境（`吃w` / `w刺`） | −2 | 反向，指原义 |

阈值：**≥2 候选；≥4 且无矛盾 → probable**。
`接口` 这类高频词指称没变 —— **频次只能当候选，不能当判据**。

### 3. 三档查询

| 档 | 边集 | 用途 |
|---|---|---|
| `strict` | 只 `exact` | 破坏性操作：合并、去重、写回 |
| `default` | `exact + probable` | 日常问答；命中标「疑似」 |
| `explore` | 全含 `uncertain` | 「有哪些可能」；必须带证据 |

### 4. 两条硬规则

1. **弱边不传递**：传递闭包只在 `exact` 上做，深度 ≤1
2. **弱边不参与破坏性操作**：合并实体 / 删重复 / 改归属，一律只认 `exact`

### 5. 升降级

```
≥2 条 assert 且无矛盾   → probable
人确认                  → exact（记 operator + ts）
出现矛盾断言            → status=conflict，双方降级，进人工队列
人否认                  → status=denied（不删，保留证据链）
```

### 6. 落点（要改哪些代码）

| 位置 | 改动 |
|---|---|
| `src/memory/services/reference.py`（新增） | 触发打分、Sense/Mention/Claim 写入、三档查询 Cypher（纯函数可单测） |
| `admin_http.py` | `/admin/recall` 加 `tier` 参数（默认 `default`）；新增 `/admin/sense/{id}/confirm|deny` |
| `harness/build_references.py`（新增） | 离线扫描会话 → 候选 Sense（`--dry-run` / `--apply`） |
| `graph_outbox.py` | **不改**：Mention 由离线批处理写，避免拖慢事件写入 |
| `labeler.py` | **不改** `len < 2`；单字靠触发信号建 Sense |

## Plan

- [ ] **M1 最小可用**
  - [x] T1 `Term / Sense / Mention` 节点与唯一键（含 scope）
  - [x] T2 触发信号打分器（正则 + 计数，不调 LLM）
  - [x] T3 离线建候选：`harness/build_references.py --dry-run/--apply`
  - [x] T4 查询接 `tier`（默认 default），命中标「疑似」
- [ ] **M2 升降级**
  - [ ] T5 `Claim` 写入（`assert/question/deny` 三态，question 不建边）
  - [ ] T6 确认 / 否认 / 冲突接口
- [ ] **M3 扩展**
  - [ ] T7 与 038（称呼带 scope）、042（账号族）对接
  - [ ] T8 评测：候选召回率 + 误绑率（人工标 20 条）

## Test

- [x] A1 `@鱼 帮我查下` → `Sense(鱼, session:X)` 候选，score ≥3
- [x] A2 `这鱼新鲜吗` → 反向信号生效，不建 Sense
- [ ] A3 `xxx账号是鱼吧？` → 只写 `Claim(mode=question)`，无 `USES_SENSE` 边
- [ ] A4 同 Term 同 scope 两个 Sense（bot / 动物）可共存
- [x] A5 跨 scope 不串：群 A 的 Sense 在群 B 查不到
- [x] A6 `tier=strict` 只返回 exact；`default` 含 probable 并标「疑似」
- [ ] A7 人否认后 `status=denied`，`strict` 不再命中、`explore` 仍可见
- [x] A8 弱边不传递：A~B、B~C 不产出 A~C
- [ ] A9 全量 pytest 无回归

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 038 称呼归一 | 本 spec 的一个应用；`ALIAS_OF` 应带 `scope`（038 已留口） |
| 039 关系谓词 | 共用 `label / confidence / evidence` 强度语义 |
| 040 导入工具 | 分组粒度「会话 + 账号」是 scope 的来源 |
| 043 身份来源 | `Account.nick` 是「同名账号」信号的数据源 |
| 045 Person 键 | 指称解析出的人名按 `uid:` 键落，不新建名字键节点 |
| 042 账号族 | 本 spec 的另一个应用（Account / Bot / Person 三层） |

### 已定的三个未定项（2026-11-09）

| 未定 | 决定 |
|---|---|
| `cluster` 成员重叠阈值 | **v1 不做 cluster** —— 只做 `session` + `global`；等 042 把群成员关系建起来再议 |
| 证据数组上限 | **20 条，保留最新**（按 ts 截断），丢弃数记 `truncated` 属性 |
| 一条 Mention 多义 | **最多 2 个**；超过则 `unresolved=true` 不建边 |

### 诚实边界

- 能给出**候选 + 证据链**，不能保证 100% 正确
- `ds` 既可能指 deepseek 也可能指「大神」→ 只能进候选
- 「鱼是我」（真人自称）与 bot 撞名 → 靠账号类型区分
- 跨群实体归并需要全局 id，本 spec 不做

### 2026-11-09 · M1 落地（Term / Sense / 触发 / tier 查询）

**已交付**

| 文件 | 内容 |
|---|---|
| `src/memory/services/reference.py` | 触发信号（jieba 词边界）、候选聚合、阈值/状态、tier 过滤、`pick_sense`、Cypher |
| `harness/build_references.py` | 扫 `raw_quote` → 候选 Sense（dry-run / `--apply`） |
| `admin_http.py` | `GET /admin/senses?term=&scope=&tier=` |
| `tests/test_reference.py` | 15 条 |

**真库结果**（`njzq-dingtalk`，294 条原文）

```
候选义项 31 个；写入 Sense 31 个
default 查询「刘阅」(session:77744779002) → 1 条 probable，evidence 2
strict 查询同条件 → 空（probable 不外泄给破坏性操作）
未知 tier → 400
```

**判据收紧过程**（三轮，都是实测驱动的）

1. 裸正则抓字面 → 抽出 `19852220`（`@uid`）、`题`、`请`、`还` 这类噪声
2. 改用 **jieba 词边界** → 噪声大幅下降，但 `问鱼` 被切成一个词 → 加「去动词前缀」
3. 补「纯标点不是字面」「单字只在 at/define 收」→ 191 → 47 → 31 个候选

**已知边界**

- 单字（`鱼`）只认 `@鱼` 与定义句，`鱼好吃` 这类靠 jieba 词性，可能漏
- 英文词（`id` / `check`）会被当成字面，是否要收进候选待定（当前保留为 uncertain）
- 证据只写到 Sense 上，还没写 `Mention` 节点（M2 补）

**M2/M3 未开工**：Claim 三态、确认/否认、cluster 与 042 对接。
