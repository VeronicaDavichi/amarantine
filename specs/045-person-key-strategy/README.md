---
status: planned
created: 2026-11-09
priority: high
tags:
- graph
- identity
- migration
depends_on:
- '043'
created_at: 2026-11-09T03:00:47.386926156Z
updated_at: 2026-11-09T03:00:47.386926156Z
---

# Person 的键：从「名字派生」改为「账号锚定」

> **Status**: planned · **Priority**: high · **Created**: 2026-11-09

## 目标

让**同一个账号改名字**之后，图上仍然只有**一个** `Person` 节点；
旧名字作为「曾用名」保留，旧卡片不丢、旧名仍可召回。

## 本 spec 的文档结构

| 文件 | 内容 |
|---|---|
| [requirements.md](requirements.md) | 5 条 REQ + 不变行为 + 非目标 |
| [design.md](design.md) | 三种键策略对比、迁移脚本、别名路径的兼容点 |
| [tasks.md](tasks.md) | T1–T7 任务 + A1–A6 验收映射 |

## 现状（一句话）

`Person` 的键是 `key_for(name)`（名字派生）。043 落地后 `Account` 有了 `uid` 锚，
但**改名仍会新建一个 `Person`**：

```
改名前：Account(uid=X) --OPERATED_BY--> Person{key:key_for("王泳"), name:"王泳"}
改名后：Account(uid=X) --OPERATED_BY--> Person{key:key_for("王泳"), name:"王泳"}
                                    \--> Person{key:key_for("王泳2"), name:"王泳2"}
```

这是 [043](../043-identity-source-priority/) A4 的已知偏差，本 spec 负责收口。

## 关联

- **043**（identity-source-priority）：提供 `Account.uid` 与 `nicks` 历史，是本 spec 的前置
- **038**（appellation-normalization）：`ALIAS_OF` 以名字匹配主人，键策略变更时必须同步兼容
- **041**（reference-layer）：指称层将来也要按 scope 区分同名，键策略要能承载
