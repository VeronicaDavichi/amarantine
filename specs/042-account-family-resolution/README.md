---
status: in-progress
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
updated_at: 2026-11-09T06:44:40.746275338Z
transitions:
- status: in-progress
  at: 2026-11-09T06:44:40.746275338Z
---
# 账号族与实体归并：一个主体多个账号，一个账号多个变体

> **Status**: draft · **Priority**: medium · **Created**: 2026-11-06

## Overview

**执行顺序**：`043 身份来源 → 038 称呼归一 → 041 指称层 → 042 账号族（本 spec）`。
依赖 041：`Claim / Sense / 三档查询` 都来自它，本 spec 只加「账号」这一层类型和词形族。

**问题**：账号 ≠ 主体，但系统现在把账号当主体。

**这不是假想** —— 你们通讯录里已经有同名多账号：

```
智小安业务助手 ×3   (6378798185 / 6744432801 / 6745197140)
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
一个主体可以有多个账号（3 个智小安业务助手），一个账号也可能换主体（离职交接）。
直接 `Account -[:ALIAS_OF]-> Bot` 表达不了「一主体多账号」。

### 2. 候选生成（blocking）：只按词形族，不按语义

| 模式 | 产出 |
|---|---|
| 账号名含 `的大肥鱼` / `的ds` / `的鱼` | 加入 Family(鱼) 候选 |
| 账号名完全相同（不同 uid） | `SAME_AS` 候选（强） |
| 账号名前缀相同（`智小安*`） | 同族候选（弱） |

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

- [x] T1 `Account / Bot / Person` 三层节点与唯一键（uid 全局唯一）
- [x] T2 词形族 blocking（规则 + 配置）
- [x] T3 归属断言抽取（含空槽、question、deny 三种）
- [x] T4 收敛与冲突处理（复用 041 的 Claim 机制）
- [ ] T5 人工复核队列（复用 002 的 `alias_reject` 表结构）
- [ ] T6 查询：`strict` / `default` / `explore` 三档

## Test

- [x] A1 同名不同 uid 的 3 个「智小安业务助手」产出 `SAME_AS` 候选，不自动合并
- [x] A2 `xxx这个账号是鱼吧？` 只写 `Claim(mode=question)`，不建 `INSTANCE_OF` 边
- [x] A3 `X 账号是谁的鱼` 产出空槽断言，不建边
- [x] A4 两条 assert → `probable`；确认后 → `exact`
- [x] A5 deny → 边降级为 `conflict`，`strict` 查询不再命中
- [x] A6 同一人重复说 3 次不升 probable（防刷）
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

### 已定的三个未定项（2026-11-09）

| 未定 | 决定 |
|---|---|
| 词形族规则放配置还是代码 | **代码**（`FAMILY_SUFFIXES` + `FAMILY_ALIASES` 常量）。只有 5 条规则，配置化反而容易漂：改配置的人不知道会少哪些候选 |
| `SAME_AS` 的语义 | **只表示「同名不同 uid」候选**，不表示同人。同人用多条 `OPERATED_BY` 表达；「同一账号多主体」不进 v1 |
| 时间维度（交接前后） | **v1 不做**。账号交接会产生矛盾断言 → 走 041 的 `conflict`，人工裁决 |

### 2026-11-09 · 实现记录（T1–T4）

**已交付**

| 文件 | 内容 |
|---|---|
| `src/memory/services/account_family.py` | 词形族 blocking、归属断言抽取（question/空槽/deny）、`SAME_AS` / `INSTANCE_OF` 写入 Cypher |
| `harness/build_account_families.py` | 从图读账号 → 候选 → 写边（dry-run / `--apply`，幂等） |
| `tests/test_account_family.py` | 10 条 |

**真库验证**（合成账号，`verify042`，验完已清理）

```
账号 5 个 → 候选族 3 个：
  [probable] same_nick  智小安业务助手  成员 2
  [uncertain] suffix    鱼            成员 2
  [uncertain] prefix    智小安         成员 2

写入 SAME_AS 1 条（probable） / INSTANCE_OF 2 条（uncertain）+ 1 个 Bot 节点
二次运行：边数仍 3（幂等）
```

**真实数据现状**：20 个账号里**没有**同名或 `的<主体>` 变体，候选族 0 个 ——
机制已通，输入还不够。等语料里出现 bot 变体命名才会有产出。

**实现中的取舍**

- `INSTANCE_OF` 的 Bot 节点按 `(namespace, name)` 合并 —— 主体是群级概念，跨群同名也先视为同一主体
- 词形族只**生成候选边**，不合并账号节点（账号有 uid，是硬事实）
- `prefix` 档只出候选、**不写边**：`智小安业务助手` 与 `智小安助理` 前缀相同不等于同主体

**未做**：T5 人工复核队列（复用 `alias_reject` 表）、T6 三档查询接口、A7 跨群隔离实测。
