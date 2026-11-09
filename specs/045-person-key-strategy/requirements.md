# 045 需求：一个账号只有一个 Person

> 父 spec：[README.md](README.md) · 设计：[design.md](design.md) · 任务：[tasks.md](tasks.md)

## 1. 背景

`Person` 节点的身份键是 `key_for(name)` —— **由显示名派生**。
这在「同名即同人」的时代够用，但 043 引入 `Account(uid)` 之后暴露了两个问题：

| 场景 | 现状 | 应该 |
|---|---|---|
| 同一个人改名 | 变成两个 `Person`，旧卡片挂旧的 | 一个 `Person`，旧名是曾用名 |
| 同名不同人（3 个「智小安业务助手」） | `key_for` 相同 → 合并成一个 `Person` | 每个 `Account` 一个人 |

**注意**：第二个问题在 043 里只做到「`Account` 不合并」，`Person` 仍会合并。
本 spec 一并收口。

## 2. 当前行为（WHEN / THEN）

- **WHEN** 同一 `uid` 的昵称从 `A` 改成 `B` **THEN** 图上出现 `Person(A)` 与 `Person(B)` 两个节点，
  只共享一个 `Account`
- **WHEN** 两个不同 `uid` 的昵称相同 **THEN** 它们共用同一个 `Person`（`key_for` 相同）
- **WHEN** 用旧名查询 **THEN** 只能靠 `ALIAS_OF` 边召回，没有别名边就查不到

## 3. 预期行为（WHEN / THEN SHALL）

- **REQ-1** **WHEN** 写入时已知 `uid` **THEN** `Person` 的身份键 SHALL 用
  `uid:<uid>`，而不是名字派生键。
- **REQ-2** **WHEN** 同一 `uid` 改名 **THEN** SHALL 只保留一个 `Person`；
  新名字写 `name`，旧名字进 `previous_names`（数组），**不得**新建节点。
- **REQ-3** **WHEN** 两个 `uid` 的昵称相同 **THEN** SHALL 是两个 `Person`（不再合并）。
- **REQ-4** **WHEN** 查询用旧名 **THEN** SHALL 仍能召回该人名下的事件
  （走 `previous_names` 匹配或 `ALIAS_OF`，二者至少要有一条路）。
- **REQ-5** **WHEN** 图上已有按名字键建的 `Person` **THEN** SHALL 提供一次性迁移：
  把同一 `uid` 的多个名字节点合并成一个 `uid:` 节点，`INVOLVES` / `ALIAS_OF` / `MENTIONS`
  边全部迁移，不丢历史。

## 4. 不变行为（回归防护）

- **WHEN** 写入时**没有** `uid`（单条 `memory_remember`）**THEN** SHALL CONTINUE TO
  用 `key_for(name)`，行为与现在完全一致
- **WHEN** 事件卡的 `event_id` **THEN** SHALL CONTINUE TO 不受 `Person` 键影响
  （`event_id` 依赖 `entity_names` 文本，不依赖图键）
- **WHEN** 别名边 `(Person)-[:ALIAS_OF]->(Alias)` **THEN** SHALL CONTINUE TO 语义不变
  （别名仍可一对多）
- **WHEN** 名册准入（035/043）**THEN** SHALL CONTINUE TO 决定「谁是 `Person`」，
  本 spec 只改「同一个人的键怎么算」
- **WHEN** `check_rebuild.py` 做双向解析 **THEN** SHALL CONTINUE TO 通过

## 5. 非功能需求

| 维度 | 要求 |
|---|---|
| 迁移可回滚 | 先 dry-run 打印要合并的节点对，`--apply` 才改；改前 `--backup` 导出受影响子图 |
| 迁移幂等 | 重复跑不重复合并、不产生新边 |
| 查询兼容 | `graph_search` 按 `name` 匹配，键变更**不影响**查询（`name` 属性保留） |
| 可观测 | 迁移输出「合并了几组、迁了几条边、留了几条孤边」 |

## 6. 非目标

| 不做 | 理由 |
|---|---|
| 跨平台账号合并（钉钉 uid ≠ QQ uid） | 需要全局身份体系，属 042 的后续 |
| 把 `Person` 与 `Entity` 合并成一种节点 | 图模型的既有分层，改动面太大 |
| 按 uid 重算历史 `event_id` | `event_id` 是内容哈希，不该因图键变化而变 |
| 实时改名监听 | 本 spec 只做「写入时按 uid 建键」+ 一次性迁移 |
