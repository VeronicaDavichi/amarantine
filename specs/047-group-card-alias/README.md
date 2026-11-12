---
status: planned
created: 2026-11-12
priority: high
tags:
- identity
- alias
- scope
- import
created_at: 2026-11-12T07:32:05.147448496Z
updated_at: 2026-11-12T07:32:05.147448496Z
---

# 群名片与按 scope 的别名

> **Status**: planned · **Priority**: high · **Created**: 2026-11-12

## Overview

**问题**：群名片是「按群生效的显示名」，但它既没进模型，导入时也丢了。

实例（大肥鱼乐园 `485490399`）：

| 层 | 值 | 谁看得到 |
|---|---|---|
| uid | `u_oZvYzRujHn5KleWUi8o--Q` | 系统 |
| 账号昵称 | 潮间带 | 好友 / 资料页 |
| **群名片** | **往汐（北瑀家的鱼）** | **这个群里所有人** |

而且同一张名片被两个号共用：

```
3883585327 → 往汐（北瑀家的鱼）
2154245492 → 往汐（北瑀家的鱼）复活版
```

**导入现状**：1999 条消息里 1040 条 `senderName=undefined` —— 别人的群名片在导入时
根本没解析出来。所以图里永远叫「潮间带」或者干脆没名字。

**目标**：把「显示名」统一成**带 scope 的别名**，一个人在不同群可以有不同名字，
查询按 scope 选名，同名多账号标 ambiguous。

## Requirements

- **REQ-1** **WHEN** 导入某群消息 **THEN** SHALL 用该群的成员表把
  `uid → 群名片` 映射出来；消息的发送人 SHALL 用群名片，而不是账号昵称。
- **REQ-2** **WHEN** 消息没有 sender 信息 **THEN** SHALL 用 `senderUid` 反查群名片；
  仍拿不到才回落到昵称 / QQ 号，并计数。
- **REQ-3** **WHEN** 写图 **THEN** SHALL 写 `(:Account)-[:DISPLAYED_AS {scope}]->(:Alias {name})`，
  `scope` 形如 `group:485490399` 或 `global`。
- **REQ-4** **WHEN** 同一 uid 在不同群有不同名片 **THEN** SHALL 允许多条 `DISPLAYED_AS` 共存，
  **不得**互相覆盖。
- **REQ-5** **WHEN** 同一个名字在同一 scope 指向多个 uid **THEN** SHALL 标记 `ambiguous`，
  查询返回全部主人，不替用户挑。
- **REQ-6** **WHEN** 查询带 scope **THEN** SHALL 优先用该 scope 的群名片；
  没有则回落到昵称；再没有回落到 uid。

## 不变行为

- `Person` 的键仍是 `uid:<uid>`（045），群名片只影响显示与别名，不改身份
- `event_id` 仍由内容哈希决定，**不包含**显示名
- 没有成员表时（如已有 DingTalk 语料）行为不变：按 sender 里的字面入库
- `ALIAS_OF` 的既有语义（一对多、带 label）不变

## Design

### 1. 数据模型

```
(:Account {namespace, uid})
     ├─[:DISPLAYED_AS {scope:"global", source:"nick"}]→(:Alias {name:"潮间带"})
     └─[:DISPLAYED_AS {scope:"group:485490399", source:"group_card"}]→(:Alias {name:"往汐（北瑀家的鱼）"})
(:Account)-[:OPERATED_BY]->(:Person {key:"uid:..."})     ← 043/045 已有
```

**为什么复用 `(:Alias)`**：041 已经用它表示「名字」，`ALIAS_OF` 已经支持一对多。
群名片只是多一个**来源**和**scope**，不需要新节点类型。

### 2. 名字的优先级（按查询 scope）

| 场景 | 取哪个 | 身份锚 |
|---|---|---|
| 群 `485490399` 内 | 群名片 | uid |
| 跨群 / 全局 | 昵称 | uid |
| 私聊 | 备注 > 昵称 | uid |

### 3. 导入流程改动

```
导出阶段（一次性）
  list_group_members(groupQq) → 落 data/corpus/qq-identities/<group>.json
  { "485490399": [{"uid":"u_...","qq":3883585327,"nickname":"潮间带","card":"往汐（北瑀家的鱼）"}] }

导入阶段
  读成员表 → uid → card 映射
  逐条消息：sender = card(uid) ?? senderName ?? senderUid ?? QQ号
  名册（roster）用 card，让抽取器把人名当**完整字符串**（不会被 jieba 切开）
  计数：卡片命中率 / 回落率写进报告
```

### 4. 图写入

- `ensure_identities` 扩展：除了 Account/Person，再写 `DISPLAYED_AS` 边（带 scope）
- 消息里的名字 → 先按 `uid → card` 解析出 uid；解析不到再按名字走 043 的名册
- **不新建 Person**：名片只是别名，身份仍由 uid 定

### 5. 查询语义

`/admin/senses` 之外新增 `GET /admin/whois?name=&scope=`：

```json
{"name":"往汐（北瑀家的鱼）","scope":"group:485490399","ambiguous":true,
 "accounts":[{"uid":"u_oZv...","nickname":"潮间带"},{"uid":"u_Z0c...","nickname":"琴酒拜松年"}]}
```

- `ambiguous=true` 时**必须列出全部**，不能只返回第一个
- 不带 scope 时只查 `global`

### 6. 冲突处理

| 冲突 | 处理 |
|---|---|
| 同 uid 多群不同名片 | 正常，多条边 |
| 同名片多 uid（往汐两号） | `ambiguous`，查询全返回 |
| 名片被改名 | 新旧都留（`Alias` 一对多），旧的加 `since/until` |
| 名片为空 | 不写 `DISPLAYED_AS`，回落昵称 |

## Plan

- [ ] T1 导出：`list_group_members` → 每群一份 `uid → card` JSON（含 nickname/qq）
- [ ] T2 导入：sender 解析链（card → senderName → uid → QQ号），报告命中率
- [ ] T3 图：`DISPLAYED_AS` 写入（scope / source / 时间），幂等
- [ ] T4 查询：`GET /admin/whois?name=&scope=`，`ambiguous` 全量返回
- [ ] T5 名册：导入时把群名片当人名送进抽取器（避免 jieba 切开）
- [ ] T6 测试与文档

## Test

- [ ] A1 群 `485490399` 内查「往汐（北瑀家的鱼）」→ 返回 2 个 Account，`ambiguous=true`
- [ ] A2 全局查「潮间带」→ 命中同一 uid
- [ ] A3 导入时 `senderName` 缺失 → 用 uid 反查名片成功（命中率写进报告）
- [ ] A4 同 uid 在两个群不同名片 → 两条 `DISPLAYED_AS` 边共存
- [ ] A5 跨 scope 不串：群 A 的名片在群 B 查不到
- [ ] A6 无成员表时回落行为与改动前一致
- [ ] A7 全量 pytest 无回归

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 041 指称层 | 复用 `(:Alias)` 与 scope 语义；041 管术语义项，本 spec 管**人的显示名** |
| 043 身份来源 | 本 spec 是它的**第 4 个来源**：群名片（scope 内） |
| 045 Person 键 | 身份仍按 uid 定键，名片不改身份 |
| 046 指代回溯 | 回溯上下文里出现的名字，同样按本 spec 的 scope 解析 |

### 诚实边界

- 群名片由群主/本人随时可改，`DISPLAYED_AS` 会累积多条 —— 需要 `since/until` 才能按时间还原
- 名片里的括号内容（`（北瑀家的鱼）`）是**人写的备注式后缀**，是否拆掉待定（先整体保留）
- 非 QQ 数据源（钉钉）没有群名片概念，本 spec 只对 QQ 生效
