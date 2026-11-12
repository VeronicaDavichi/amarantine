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
- **REQ-6** **WHEN** 查询带 scope **THEN** SHALL 取该 scope 的名字标记；
  没有则回落 `global`；全都没有才用 uid 占位并标 `name_missing`。

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

### 2. 名字标记集合（不是优先级）

**昵称、群名片、备注都是同一个人身上的「名字标记」，彼此没有高下之分。**
身份只有一个（uid），名字可以有任意多个；差别只在**这个标记在哪个范围生效**。

```
人的名字标记 = {(text, source, scope, since)}

source ∈ {nick, group_card, remark, real_name}
scope  ∈ {global, group:<gid>, friend:<uid>}
```

| 标记 | source | 默认 scope | 谁看得到 |
|---|---|---|---|
| 账号昵称 | `nick` | `global` | 好友 / 资料页 |
| 群名片 | `group_card` | `group:<gid>` | 该群所有人 |
| 好友备注 | `remark` | `friend:<uid>` | 只有备注的人自己 |
| 通讯录真名 | `real_name` | `global` | 平台字段，可能为空 |

**同名不同来源不合并成一条边的理由**：`往汐` 既出现在群名片里，也可能被人叫成备注 ——
它们指向同一个 uid，但**证据不同**；合并会丢掉「在哪个范围、谁这么叫」。

### 3. 取名规则（按 scope 匹配，不是按等级）

查询发生在某个 scope 里，就取该 scope 的标记：

1. 精确 scope 有标记 → 用它们；同一 scope 多个时按 `card > remark > nick` 兜底（仅排序，不代表高低）
2. 该 scope 没有 → 回落到 `global`
3. 全都没有 → 用 `uid` 的短形式占位（`u_oZvYzRu…`），并在结果里标 `name_missing=true`

**关键**：抽取器（名册）要收**全部标记**，不只收当前 scope 的那一个 ——
否则群里有人用昵称叫、有人用名片叫，总有一半对不上。

### 2.5 名片五分类与整活判定（实测驱动）

群 `1019369531`（500 人）同名组 10 个，形态完全不同 —— **不分类就关联会把 21 人并成 1 人**：

| 名片 | 号数 | 本质 | 处理 |
|---|---|---|---|
| `合并转发存在广告,禁言15分钟` | 23 | 管理员处罚标记 | `moderation`：**不是名字** |
| `文本存在广告,禁言15分钟` | 13 | 同上 | 同上 |
| `刘芮西宝宝` | 21 | 集体整活（昵称各不相同） | `impersonation`：**不关联** |
| `　`（全角空格） | 3 | 整活人（全空白） | `blank_ws`：**不是名字** |
| `……` / `.` | 2 | 无意义名 | `blank_punct`：回落昵称 |
| `往汐…复活版` | 1 | 真·小号 | `alt`：同主人候选 |
| `秋` / `咸鱼` / `黑崎一护` | 各 2 | 真实同名 | `ambiguous`：留痕不关联 |

判定顺序（`name_marker.classify_card`）：

```
含 禁言/广告/管理禁止/违规/警告 → moderation
去空白+控制字符后为空         → blank_ws（整活人）
去括号后只剩标点              → blank_punct
含 小号/复活版/二号机/转生…    → alt
其余                          → normal
```

**分桶关联规则**（`analyze_group`，阈值为 3）：

| 组规模 | 条件 | 结论 |
|---|---|---|
| ≥3 | — | `impersonation`（整活），**不建任何关联边** |
| =2 | 恰好一个带小号后缀 | `same_owner` → 可建 `SAME_OWNER` |
| =2 | 都无后缀 | `ambiguous`（同名但无证据），只留痕 |

**阈值依据**：实测整活组 13/21/23 个号，真实同名多为 2 个号 —— 定 3 既不会误伤「秋×2」，
又能拦住大组。

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
- [ ] A6 昵称/群名片/备注三个标记**各自查询**都能命中同一 uid
- [ ] A7 无成员表时回落行为与改动前一致
- [ ] A8 全量 pytest 无回归

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

### 2026-11-12 · 术语修正

用户指出：**昵称、群名片、备注都是「人名标记」，不是有高下之分的来源**。
所以设计里不再写「优先级」，改成「标记集合 + 按 scope 匹配」：

- 身份只有一个（uid），标记可以任意多个
- 差别只在**生效范围**（global / group / friend）
- 抽取用名册要收**全部标记**，否则群里换一种叫法就对不上

### 2026-11-12 · 名片刻画落地

**已交付**：`src/memory/services/name_marker.py`（`classify_card` / `analyze_group` /
`strip_parens` / `strip_alt_suffix`）+ `tests/test_name_marker.py`（10 条）。

**用真实群数据验证**（`1019369531`，500 人）：

```
moderation  26 个号（23+13+2 的处罚名片）→ 全部排除
impersonation 「刘芮西宝宝」21 个号 → 整活桶
blank_ws    「　」3 个、「　　　　　」2 个 → 排除
ambiguous   「秋」「咸鱼」「黑崎一护」各 2 个 → 留痕
same_owner  「往汐」+「往汐…复活版」→ 可建 SAME_OWNER
```

**踩到的坑**：括号必须**先剥再删标点** —— 否则 `往汐（北瑀家的鱼）复活版` 会变成
`往汐北瑀家的鱼`（括号被当标点删掉，括号内文字粘进名字）。

**下一步**：T2 导入用名片（sender 解析链）、T3 写 `DISPLAYED_AS`。
