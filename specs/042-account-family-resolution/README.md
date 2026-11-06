---
status: draft
created: 2026-11-06
priority: medium
tags:
- graph
- entity-resolution
- accounts
- draft
depends_on:
- '041'
created_at: 2026-11-06T08:49:25.130796513Z
updated_at: 2026-11-06T08:49:25.130796513Z
---

# 账号族与实体归并：一个主体多个账号，一个账号多个变体

> **Status**: draft · **Priority**: medium · **Created**: 2026-11-06

## Overview

**执行顺序**：`043 身份来源 → 038 称呼归一 → 041 指称层 → 042 账号族（本 spec）`。
依赖 041：`Claim / Sense / 三档查询` 都来自它，本 spec 只加「账号」这一层类型和词形族。

**问题**：账号 ≠ 主体，但系统现在把账号当主体。

**这不是假想** —— 你们通讯录里已经有同名多账号：

```
宁小智业务助手 ×3   (6378798185 / 6744432801 / 6745197140)
AI助理        ×2   (5409619649 / 6551919831)
邓嘉乐        ×2   (2898948281 / 6675411906)
```

再加上 bot 的变体命名：`xxxx的大肥鱼`、`xxx的ds`、`xxx的鱼` ——
同一个主体在不同群、不同时期可能挂着不同账号名。

**目标（粗）**：把「账号 → 主体」做成**带证据的弱关联**，让「谁是鱼」「这个号是谁的」
这类问题能给出候选而不是沉默。

**非目标**

| 不做 | 理由 |
|---|---|
| 跨群全局归并 | 需要全局 id + 跨群水位，先做会话/群簇级 |
| 用 LLM 自由判断归属 | 判断要在候选集内做选择题（复用 alias_resolver 的思路） |
| 自动合并账号节点 | 账号是硬事实（有 uid），只能「加边」不能「并节点」 |

## Design

### 1. 三层模型

```
(:Account {uid, nick, platform:"qq|dingtalk"})   ← 账号，全局唯一，硬事实
(:Bot     {name:"鱼", scope:"qq:123"})            ← 主体（bot 实例），群级
(:Person  {name})                                 ← 真人

(:Account)-[:INSTANCE_OF  {label, confidence, evidence}]->(:Bot)
(:Account)-[:OPERATED_BY  {label, confidence, evidence}]->(:Person)
(:Account)-[:SAME_AS      {label, confidence, evidence}]->(:Account)
```

**为什么账号和主体必须分开**：`xxxx的大肥鱼` 是账号、`鱼` 是主体；
一个主体可以有多个账号（3 个宁小智业务助手），一个账号也可能换主体（离职交接）。
直接 `Account -[:ALIAS_OF]-> Bot` 表达不了「一主体多账号」。

### 2. 候选生成（blocking）：只按词形族，不按语义

| 模式 | 产出 |
|---|---|
| 账号名含 `的大肥鱼` / `的ds` / `的鱼` | 加入 Family(鱼) 候选 |
| 账号名完全相同（不同 uid） | `SAME_AS` 候选（强） |
| 账号名前缀相同（`宁小智*`） | 同族候选（弱） |

**词形族只做候选生成，绝不直接建边** —— `ds` 既可能指 deepseek 也可能指「大神」。

### 3. 归属断言：抽取断言，不抽取结论

| 原话形态 | 产出 |
|---|---|
| `X 账号是谁的鱼` | `Claim(OwnedBy, subject=X, object=空槽, mode=assert)` ← 主体待填 |
| `X 这个账号是鱼吧？` | `Claim(IsBot, subject=X, object=鱼, mode=question)` |
| `@X 鱼` / `对，都是我` | `Claim(IsBot, …, mode=assert)` |
| `不是那个鱼，是另一个` | `Claim(IsBot, …, mode=deny)` |

**空槽是重点**：「是谁的鱼」这句话本身不能建边，只能等后文补上主体。
`mode=question` 与 `mode=deny` 走 041 的同一套规则（question 不建边、deny 降级）。

### 4. 收敛规则

```
同一 (subject, predicate, object)：
  ≥2 条 assert 且无矛盾            → label=probable
  用户确认「对，都是我的」          → label=exact（记 operator + ts）
  出现 deny / 矛盾 assert          → label=conflict，双方降级，进人工队列
  ≥3 条 assert 但来自同一人重复    → 只算 1 条（防刷）
```

### 5. 与 041 的复用关系

| 041 提供 | 042 使用 |
|---|---|
| `Claim` + `mode` | 归属/身份断言 |
| `Sense`（scope 内义项） | `鱼` 这个行话在群内的义项 |
| 三档查询 / 弱边不传递 | 查询与传播规则 |

042 不新造机制，只加「账号」这一层类型和词形族 blocking。

## Plan

- [ ] T1 `Account / Bot / Person` 三层节点与唯一键（uid 全局唯一）
- [ ] T2 词形族 blocking（规则 + 配置）
- [ ] T3 归属断言抽取（含空槽、question、deny 三种）
- [ ] T4 收敛与冲突处理（复用 041 的 Claim 机制）
- [ ] T5 人工复核队列（复用 002 的 `alias_reject` 表结构）
- [ ] T6 查询：`strict` / `default` / `explore` 三档

## Test

- [ ] A1 同名不同 uid 的 3 个「宁小智业务助手」产出 `SAME_AS` 候选，不自动合并
- [ ] A2 `xxx这个账号是鱼吧？` 只写 `Claim(mode=question)`，不建 `INSTANCE_OF` 边
- [ ] A3 `X 账号是谁的鱼` 产出空槽断言，不建边
- [ ] A4 两条 assert → `probable`；确认后 → `exact`
- [ ] A5 deny → 边降级为 `conflict`，`strict` 查询不再命中
- [ ] A6 同一人重复说 3 次不升 probable（防刷）
- [ ] A7 跨群查询不串（群 A 的 `鱼` 不展开到群 B）

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 041 指称层 | 本 spec 的地基；Claim / Sense / 三档查询都来自它 |
| 038 称呼归一 | 姊妹应用（称呼 → 真人），同样需要 scope |
| 040 导入工具 | 分组粒度必须「会话 + 账号」，否则 blocking 没有输入 |
| 039 关系谓词 | `OPERATED_BY` 这类谓词要在 039 登记，避免重复定义 |

### 诚实边界

- 账号变体命名是**人为约定**，词形族只能生成候选，判不了对错
- `ds` 的歧义（deepseek / 大神）只能进候选
- 真人自称「我是鱼」与 bot 撞名 → 靠账号类型 + 发言内容区分
- 账号交接（同号换人）会产生历史断言矛盾 → 进 conflict，人工裁决

### 未定

- 词形族规则放配置还是代码？（配置更好改，但容易飘）
- `SAME_AS` 要不要考虑「同一人多账号」与「同一账号多主体」两种情况？
- 是否需要一个「账号 → 主体」的时间维度（交接前后）？
