---
status: complete
created: 2026-11-03
priority: high
tags:
- alias
- admin
- entity-edit
- tooling
- backend
created_at: 2026-11-03T01:11:04.568533131Z
updated_at: 2026-11-03T01:27:42.054272331Z
completed_at: 2026-11-03T01:27:42.054272331Z
transitions:
- status: complete
  at: 2026-11-03T01:27:42.054272331Z
---
# 管理面三个闭环：拒绝可修、纠错可查、spec 可校验

> **Status**: in-progress · **Priority**: high · **Created**: 2026-11-03

## Overview

002 让「被拒的别名声明」变成了**看得见**的（`alias_reject` 表 + 两个查询接口）。
但看见之后还有两步没走通：

| 缺口 | 现状 | 后果 |
|---|---|---|
| **拒绝可修** | `POST /admin/alias-rejects/resolve` 只写 `resolution` 字段 | 人裁决「判据错了」，图上的边**还是没建** —— 还得人肉去 Neo4j 补 |
| **纠错可查** | 有 `:EntityEdit` 节点、4 个写接口（rename/merge/split/rebind） | **没有任何读接口**。文档说「纠错是修正，不是抹除痕迹」，但痕迹查不到 |
| **spec 可校验** | `leanspec validate` 报 `not yet migrated`；MCP `validate_spec` 对 enum 报类型错 | spec 骨架只能靠人眼，下一次又会手搓出不合模板的文件 |

三个缺口是同一个病：**写进去了，但拿不回来 / 验证不了。**

**目标**：`resolution="accepted"` 后图上真的有边；`:EntityEdit` 能查；
spec 骨架有脚本守门。出口是 A1–A9 全绿。

**非目标**

| 不做 | 理由 |
|---|---|
| 补建边时重写事件卡 / 重算 `event_id` | 补边只需 `AliasClaim`，与事件卡内容无关（见 Design） |
| 让 `accepted` 的边参与 `event_id` 哈希 | 声明本来就不参与，改了幂等就废 |
| 给 `:EntityEdit` 加撤销/回滚 | 只做**读**。回滚是另一个量级的问题 |
| 替换 `leanspec` 官方校验 | 只补「骨架 + 必修章节 + 行数」三项本地约定，不做通用校验器 |
| 管理前端 | 独立工程，归 M5 |

## Design

### A · 拒绝可修：accepted 必须绕过判据

**这是本节最关键的一点。** `accepted` 的语义是「人工否决了判据」，所以补建边
时**绝不能再走一遍 `_partition_alias_claims`** —— 否则同一道判据会把边再拦一次，
正是 002 修的 bug 形态（判据说不行、人说不、结果还是不行，且无声）。

`append_alias_edges` 加一个开关：

```
append_alias_edges(..., force: bool = False) -> AliasWriteResult
    force=False  走 _partition_alias_claims（默认，正常写入链路）
    force=True   直接 [_alias_item(c) for c in claims]，不判、不跳
```

`_alias_item()` 已存在（带 `key`），所以改动只有三行。`skipped` 在 force 下恒为空。

补建流程（顺序很重要）：

```
resolve(..., resolution="accepted")
  → 构造 AliasClaim(alias, canonical, label="exact", source="user_declared")
  → append_alias_edges(force=True)
      written > 0  → resolution="accepted"          响应 repaired=true
      written == 0 → resolution="accept_pending"    响应 repaired=false + 告警
  → resolution="dismissed"/"deferred" 一律不碰图
```

**先补图、后落库**，所以「失败」这个状态本身是可查的。
`list_pending` 的条件随之从 `resolved_at IS NULL`
放宽成 `resolved_at IS NULL OR resolution = 'accept_pending'` ——
补建失败的行仍然留在待办里，可以重试（`MERGE` 幂等）。

`label="exact"` 因为是人拍的板；`source="user_declared"` 因为人说的话就是用户声明。

### C · 纠错可查：GET /admin/entities/edits

`:EntityEdit` 的现有结构（写入侧已实现）：

```
(:EntityEdit {
  namespace, op, target, args, reason, operator, created_at
})
op ∈ {rename, merge, split, rebind}
args 是 **JSON 字符串**（Neo4j 属性不收嵌套 Map）
```

新端点只读，按 `namespace` 隔离，可选过滤 `op` / `target`，按 `created_at` 倒序：

```
GET /admin/entities/edits?namespace=&op=&target=&limit=50
  → { items: [{op, target, args: <解析后的对象>, reason, operator, created_at}], total }
```

`args` 出参**解析成对象**而不是回吐 JSON 字符串 —— 查历史的人要看懂，不该自己 `json.loads`。
解析失败时原样返回字符串，并附 `args_parse_error: true`（不静默吞）。

### D · spec 可校验：harness/check_spec.py

`leanspec` 的两条校验路径在本机都不可用（见 002 的 Notes），但骨架约定是明确的：
`.lean-spec/templates/spec-template.md` 定了五段，`.lean-spec/config.json` 定了行数阈值。

脚本只查这四项，够用且不会过度约束：

1. 每个 `specs/NNN-*/README.md` 有 frontmatter，且含 `status` / `created` / `priority`
2. `status` ∈ {draft, planned, in-progress, complete, archived}
3. 正文含五个二级标题：`Overview` / `Design` / `Plan` / `Test` / `Notes`
4. 行数 ≤ `config.json` 的 `validation.maxLines`（400）

输出 `OK: spec 骨架一致`，退出码 0/1，风格对齐 `harness/check_guide.py`。

**它校验自己这个 spec** —— 003 建完第一件事就是跑它。

## Plan

- [x] T1 graph_outbox：`append_alias_edges(force=)` 开关
- [x] T2 alias_reject：`resolve()` 认得 `accept_pending` 可重试；`list_pending`/`count_pending`/`stats` 口径一致
- [x] T3 alias_apply：`repair_accepted_edge()`（可单测的补建入口，`force=True`）
- [x] T4 admin_http：`/admin/alias-rejects/resolve` 接线，响应加 `repaired`；`AliasRejectEntry` 暴露 `resolution`
- [x] T5 entity_edit：`list_edits()` 查询函数（含 args 解析）
- [x] T6 admin_http：`GET /admin/entities/edits`
- [x] T7 harness/check_spec.py
- [x] T8 测试：A 组 7 条 + C 组 4 条 + 接口 5 条（新增 16 条，全量 789 → 805）
- [x] T9 文档：user-manual 补「裁决后自动补边」与「痕迹可查」+ changelog v0.16
- [x] T10 用 check_spec.py 校验 001/002/003；001 缺五段骨架，已按模板重排（内容未改）
- [x] T11 **真库活体才暴露的 bug**：`APPEND_ALIAS_CYPHER` 用 `CREATE` 建 `ALIAS_OF`，
      重复补建会产生平行边 → 改成 `MERGE (p)-[r:ALIAS_OF]->(a)`
      （`ON CREATE` 记首次时间与来源事件，`ON MATCH` 只更新 label/confidence/source）

## Test

- [x] A1 `append_alias_edges(force=True)` 对 `糖包就是这种人` 这类被拒 claim 也能建出边
- [x] A2 `force=False` 行为不变（既有测试全绿，证明开关是纯增量）
- [x] A3 `accepted` + 图可用 → `repaired=true`、`resolution=accepted`、图上 `ALIAS_OF` 边存在
- [x] A4 `accepted` + 图不可用 → `repaired=false`、`resolution=accept_pending`、该行仍在待办
- [x] A5 `accept_pending` 重试成功 → 转成 `accepted`，行从待办消失
- [x] A6 `dismissed` / `deferred` → 图上零新增边（monkeypatch 监测调用）
- [x] A7 `GET /admin/entities/edits` 能列出 rename/merge/split/rebind 四类，`args` 是对象
- [x] A8 `op` / `target` 过滤生效；`namespace` 隔离（别的 namespace 查不到）
- [x] A9 `harness/check_spec.py` 对三个 spec 输出 `OK: spec 骨架一致`，退出码 0

**真库活体（15/15 绿，namespace `closure68753` / `closure68807`）**：
被拒行落表 → `accepted` 补出 `(这种人)-[:ALIAS_OF {label:exact}]->(糖包)` →
重复补建边不增 → 改名后 `GET /admin/entities/edits` 查到 `op=rename`、
`args={'to': '豆包2', 'count': 1}`（对象而非 JSON 字符串）→ `op` 过滤与
namespace 隔离均生效。

## Notes

### 为什么补建边不需要重写事件卡

002 的 Notes 里我写过「补建要重走写入链路（`event_id` 是内容哈希），归 P3」——
**这句是错的**，本 spec 已纠正。

`append_alias_edges(event_id, namespace, claims, ...)` 里的 `event_id` 只作为
边的 `source_event_id` 属性写进图，用于追溯「这条边是哪条记忆带来的」。
它不参与任何校验，也不需要事件卡在场。所以补建边就是：

```
AliasClaim(alias, canonical, label, source)  →  append_alias_edges(force=True)
```

跟内容哈希、跟重写、跟 Weaviate 全都无关。

### 为什么 `accept_pending` 是一个真正的状态而不是日志

如果补建失败只打一行 warning、`resolution` 照写 `accepted`，
那这一行就从待办里消失了 —— **人以为修好了，其实没有**。
这正是 002 要消灭的「静默」。所以失败必须是一个**能被下次查询看见的状态**。

同理，`args` 解析失败不吞：附 `args_parse_error` 让调用方知道看到了残缺数据。

### 第三个 bug：`ALIAS_OF` 原来是 `CREATE`

这个 bug **单测抓不到，只有真库活体能暴露** —— 值得单独记一笔。

`APPEND_ALIAS_CYPHER` 建关系写的是 `CREATE (p)-[:ALIAS_OF {...}]->(a)`。
正常写入链路上，同一句话重放会被 `event_id` 的幂等短路挡在门外，
所以第二次建边从来不会发生，`CREATE` 和 `MERGE` 看不出差别。

但补建路径天然会重复调用：补建失败可重试、人可能连点两次。
活体验证里那一步「重复补建幂等（边不增）」直接红了 —— 图上变成 **2 条**平行边。
后果不只是脏数据：`RESOLVE_ALIAS_CYPHER` 按边返回，同一条别名会被算两遍。

修法是关系改 `MERGE`。语义上这也更对：**一对 (canonical, alias) 只该有一条
`ALIAS_OF` 边** —— 这条边表达的是「这两个名字指同一个东西」，与「由哪条记忆
带来」无关。所以 `created_at` / `source_event_id` 用 `ON CREATE` 保留首次，
而 `label` / `confidence` / `source` 用 `ON MATCH` 允许被人工裁决升级
（`uncertain` → `exact`）。

顺带修了两个测试：
`_merge_clauses()` 的 `MERGE\s*\([^)]*\)` 只能匹配节点模式，遇到关系 MERGE 会
截成 `MERGE (p)`，导致「MERGE 必须按 key」这条断言误报 —— 正则补上关系形态，
并让断言跳过关系型 MERGE（它合并的是边，不涉及节点身份）。
