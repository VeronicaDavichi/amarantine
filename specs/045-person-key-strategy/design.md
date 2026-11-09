# 045 设计：uid 优先的键 + 可迁移的合并

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 任务：[tasks.md](tasks.md)

## 1. 根因

| # | 位置 | 问题 |
|---|---|---|
| 1 | `back_end/src/memory/core/entity_key.py:44` | `key_for(name)` 是唯一键来源 —— 名字变了键就变 |
| 2 | `graph_outbox.py:103` | `MERGE (p {namespace, key})` 用上面那个键，改名即新节点 |
| 3 | `graph_outbox.py:137` | 别名建边同样按 `key_for(canonical)`，与真实节点可能对不上 |
| 4 | 无 | 写入路径没有「这个人在本 namespace 的 uid 是什么」的查询 |

## 2. 三种方案

| 方案 | 做法 | 结论 |
|---|---|---|
| **A uid 优先** | 有 uid 用 `uid:<uid>`，无 uid 回退 `key_for(name)` | ✅ 采用：改动最小，可迁移 |
| B 改名时合并 | 保留 name 键，改名时把旧节点的边迁到新节点 | ❌ 每次改名都要合并，且旧名信息会丢 |
| C 独立身份节点 | 新增 `(:PersonIdentity {uid})`，`Person` 只做显示名 | ❌ 多一层，查询与别名都要改，收益不明显 |

**为什么 A 能同时解决「改名」和「同名」**：键不再由名字决定 ——
同一个 `uid` 无论叫什么都落到同一个节点；两个 `uid` 即使同名也是两个节点。

## 3. 写入路径的改造点

```
resolve_person_key(namespace, name, uid=None) -> str
   1. 若 uid 已知          → f"uid:{uid}"
   2. 否则查图上是否有该 name 的节点：
        - 有且键是 uid:xxx  → 复用该键（避免名字键重建一份）
        - 有且键是名字键     → 复用（迁移前的历史节点）
   3. 都没有              → key_for(name)（无 uid 场景的兜底）
```

**为什么第 2 步必须存在**：别名/事件写入时常常只拿到名字（`ALIAS_OF` 只有
canonical 名字），不知道 uid。如果不先查已有节点，就会用 `key_for(name)`
再建一个，**迁移后立刻又分裂**。

调用点：

| 位置 | 现状 | 改法 |
|---|---|---|
| `write_event_node` 的 persons | `key_for(e.name)` | `resolve_person_key(ns, name, uid)`，uid 从 `card` 新增字段或 ingest 上下文取 |
| `APPEND_ALIAS_CYPHER` 的 `p` | `key_for(claim.canonical)` | 同上（只给 name → 走第 2 步查表） |
| `ensure_identities`（043） | `key_for(display_name)` | 直接用 `uid:<uid>`，并把旧名写进 `previous_names` |

## 4. 数据模型

```
(:Person {
   key: "uid:53660559",     // 身份（uid 优先）
   name: "王泳",             // 当前显示名
   previous_names: ["王泳2"],// 曾用名（数组，改名时追加）
   first_seen, last_seen
 })
```

- `name` 仍是查询入口（`graph_search` 按 `p.name IN $person_names`），键变更不影响查询
- `previous_names` 参与查询：`WHERE p.name IN $names OR any(n IN $names WHERE n IN p.previous_names)`
- `ALIAS_OF` 语义不变

## 5. 迁移脚本（REQ-5）

`harness/migrate_person_keys.py`：

```
1. 读 Account(uid, nicks[])  ← 043 已经写好
2. 对每个 uid：
     a. 收集图上 name ∈ nicks 的所有 Person 节点
     b. 目标节点 = 已存在的 uid:<uid>，没有就新建一个
     c. 把每个旧节点的边迁到目标（INVOLVES / MENTIONS 用 CREATE 重建后 DELETE 旧边）
     d. 旧节点的 name 追加进目标 previous_names
     e. DELETE 旧节点
3. 打印报告：合并组数 / 迁移边数 / 剩余孤儿
```

**边迁移用「读出来再 CREATE」而不是 APOC**：社区版无 APOC，且边上有 `created_at` 等属性，
直接 `CREATE` 能逐条保留。

**回滚**：`--backup out.json` 先把受影响的节点与边导出成 JSON，出问题可按文件重建。

## 6. 兼容与风险

| 风险 | 缓解 |
|---|---|
| 别名路径先建了名字键节点，迁移后又分裂 | `resolve_person_key` 第 2 步查表复用 |
| 迁移期间并发写入 | 迁移要求先停 admin 服务（脚本检查端口并提示） |
| `previous_names` 查询变慢 | 只对 `Person` 节点、且加 `WHERE` 前置过滤；数据量 < 10k 无感 |
| 无 uid 的历史节点不迁移 | 保留 name 键，行为不变（REQ 的不变行为） |
