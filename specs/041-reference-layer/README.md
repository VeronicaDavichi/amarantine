---
status: draft
created: 2026-11-06
priority: high
tags:
- graph
- reference
- semantics
- draft
depends_on:
- '038'
created_at: 2026-11-06T08:48:18.266962385Z
updated_at: 2026-11-06T08:48:18.266962385Z
---

# 指称层：字面 → 义项（行话 / 称呼 / 账号族都是它的一种）

> **Status**: draft · **Priority**: high · **Created**: 2026-11-06

## Overview

**执行顺序**：`043 身份来源 → 038 称呼归一 → 041 指称层（本 spec）→ 042 账号族`。
依赖 038：它先把「称呼 → 真人」跑通并留下带 scope 的边形状，本 spec 再把「称呼」
泛化成「指称（scope 内的义项）」。

**问题**：系统现在把「字面」直接当「指称」。

```
Person 里既有 wanglaoshi 这种称呼节点，也有真人节点，之间无边
「鱼」这种单字概念根本进不了图（len < 2 被丢）
同名多账号无法表达（通讯录里已有 3 个「宁小智业务助手」、2 个「AI助理」）
```

**核心判断**：行话、称呼、账号族是同一个问题的三次应用 ——

| 类型 | 字面 | 指称 | 证据 |
|---|---|---|---|
| 称呼 | 王老师 | 王勇（人） | 同句 `@真名`、自称 |
| 账号族 | `xxx的大肥鱼` | 鱼（bot 主体） | 账号名族 + 归属断言 |
| 行话 | 鱼 | deepseek agent | 称呼位 + 群内异常 + 定义句 |

差别只在**指称对象**和**证据来源**；`scope + 弱证据 + 留痕` 这一层完全相同。
所以做**一层**，不做三个 spec。

**目标（粗）**：让「任意名词」在**某个会话范围内**能够长出一个「义项」，
并带着证据和强度，可被查询、可被升级、可被否认。

**非目标**

| 不做 | 理由 |
|---|---|
| 预置行话词典 | 行话是长出来的，不是配出来的 |
| 全量逐词检测 | 成本爆炸，且绝大多数词义项没变 |
| 跨群自动泛化 | 需要群簇（成员重叠）工程，先做会话级 |
| 破坏性操作（合并实体/去重）用弱证据 | 见「两条硬规则」 |

## Design

### 1. 数据模型

```
(:Term    {text:"鱼"})                                   ← 字面，全局
(:Sense   {term:"鱼", scope:"qq:123", gloss:"deepseek agent",
           status:"probable", confidence:0.6})            ← 义项，scope 内
(:Mention {surface:"鱼", session:"qq:123", ts, quote_id}) ← 一次出现
(:Claim   {subject, predicate, object, mode, speaker, ts}) ← 一次断言

(:Term)-[:HAS_SENSE]->(:Sense)
(:Mention)-[:USES_SENSE {label:"probable", confidence:0.6}]->(:Sense)
(:Claim {mode:"assert|question"})-[:SUPPORTS]->(:Sense)
```

- `Term` 管字面；`Sense` 管**某 scope 内的义项**；同一 Term 同 scope 可有多个 Sense
- 选不了就 `unresolved`，留痕，不猜
- `mode=question`（「这个是鱼吧？」）**只贡献证据，不建边**

### 2. 触发式发现（不做全量）

| 信号 | 分值 | 例 |
|---|---|---|
| 出现在称呼位：`@w` / `w说` / `问w` / `w帮我` / `w在吗` | +3 | `@鱼 帮我查下` |
| 定义句：`w 是/指/就是 X`、`我们说的 w 是 X` | +3 | `鱼就是那个 deepseek 的 bot` |
| 群内存在同名账号 | +2 | 账号 `xxxx的大肥鱼` |
| 归属断言：`X 账号是谁的 w` | +2 | 主体待填槽 |
| 群内频次 / 群外频次 异常（TF-IDF） | **+1（仅候选）** | 本群高频 |
| 宾语位 + 食物/动作语境（`吃w`/`w刺`） | −2（反向） | 指原义 |

阈值：**≥2 建 Sense 候选；≥4 且无矛盾 → probable**。
`接口` 这类词频次极高但指称没变 —— **频次只能当候选，不能当判据**。

### 3. scope 三级

```
session（一个群）            ← 行话默认只在这里有效
cluster{群A, 群B}            ← 成员重叠 ≥ 60% 的群簇，行话共享
global                       ← 仅「用户明确说完」才升级
```

查询：**同 scope 查 Sense，查不到退回字面**；跨 scope 不自动传播。

### 4. 三档查询

| 档 | 边集 | 用途 |
|---|---|---|
| `strict` | 只 `exact` | 破坏性操作：合并、去重、写回 |
| `default` | `exact + probable` | 日常问答；命中标注「疑似」 |
| `explore` | 全含 `uncertain` | 「有哪些可能」；结果必须带证据 |

### 5. 两条硬规则

1. **弱边不传递**：传递闭包只在 `exact` 上做，深度 ≤1
2. **弱边不参与破坏性操作**：合并实体 / 删重复 / 改归属，一律只认 `exact`

### 6. 升降级

```
≥2 条 assert 且无矛盾        → probable
人确认                        → exact（记 operator + ts）
出现矛盾断言                  → label=conflict，双方降级，进人工队列
人否认                        → status=denied（不删，保留证据链）
```

## Plan

- [ ] T1 `Term / Sense / Mention` 节点与约束（唯一键含 scope）
- [ ] T2 触发信号打分器（正则 + 计数，不调 LLM）
- [ ] T3 `Claim` 写入与 `mode` 区分（question 不建边）
- [ ] T4 三档查询开关（默认 default）
- [ ] T5 升降级接口（确认 / 否认 / 冲突）
- [ ] T6 与 038（称呼）、042（账号族）对接
- [ ] T7 评测：候选召回率 + 误绑率

## Test

- [ ] A1 `@鱼 帮我查下` → 建 `Sense(鱼, bot)` 候选，score ≥3
- [ ] A2 `这鱼新鲜吗` → 反向信号生效，不建 Sense
- [ ] A3 `xxx账号是鱼吧？` → 只写 Claim(mode=question)，不建边
- [ ] A4 同一 Term 同 scope 两个 Sense（bot / 动物）可共存
- [ ] A5 跨 scope 查询不串（群 A 的 Sense 在群 B 查不到）
- [ ] A6 弱边不参与传递：A~B、B~C 不产出 A~C
- [ ] A7 人否认后 `status=denied`，查询 `strict` 不再命中

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 038 称呼归一 | 本 spec 的一个**应用**；038 的 `ALIAS_OF` 应带 scope |
| 039 关系谓词 | 用本 spec 的强度语义（label / confidence / evidence） |
| 040 导入工具 | 分组粒度必须是「会话 + 账号」，否则 scope 无从谈起 |
| 042 账号族 | 本 spec 的另一个应用（Account / Bot / Person 三层） |

### 诚实边界

- 能给出**候选 + 证据链**，不能保证 100% 正确
- `ds` 既可能指 deepseek 也可能指「大神」→ 只能进候选
- 「鱼是我」（真人自称）与 bot 撞名 → 靠账号类型区分
- 跨群实体归并需要全局 id，本 spec 不做

### 未定

- `cluster` 的成员重叠阈值定多少（60% 是拍的）？
- 证据数组的存储上限（边属性会变长）？
- 一条 Mention 能否同时指向两个 Sense（多义句）？
