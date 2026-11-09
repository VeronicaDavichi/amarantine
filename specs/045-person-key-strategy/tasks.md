# 045 任务：uid 键 + 一次性迁移

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 复现**
  - 造一个「改名」场景：同 uid 两个昵称先后写入，断言图上出现 2 个 `Person`
  - 引用：REQ-1、REQ-2（反证）
  - 验证：脚本输出 `persons=2 account=1`，退出码 0

- [ ] **T2 `resolve_person_key()`**
  - 新增 `memory/services/person_key.py`：uid 优先 → 查表复用 → `key_for(name)` 兜底
  - 引用：REQ-1、REQ-3
  - 验证：单测四分支（有 uid / 查到 uid 键 / 查到名字键 / 都没有）

- [ ] **T3 写入路径接入**
  - `write_event_node` / `APPEND_ALIAS_CYPHER` / `ensure_identities` 改用 `resolve_person_key`
  - 引用：REQ-1、REQ-2
  - 验证：改名后仍只有 1 个 `Person`，`previous_names` 有旧名

- [ ] **T4 同名不同人不合并**
  - 两个 uid 用同一昵称 → 两个 `Person`
  - 引用：REQ-3
  - 验证：真库 `count(Person)=2`，`count(Account)=2`

- [ ] **T5 迁移脚本**
  - `harness/migrate_person_keys.py`：dry-run / `--apply` / `--backup`
  - 引用：REQ-5
  - 验证：迁移前后 `INVOLVES` 边数守恒；重复跑幂等

- [ ] **T6 查询兼容**
  - `graph_search` 的人名匹配加上 `previous_names`
  - 引用：REQ-4
  - 验证：用旧名查询能召回，与用新名等价

- [ ] **T7 测试与文档**
  - 单测 + 真库脚本；`docs/user-manual.md` 补「一个人多个名字」的键说明
  - 引用：全部 REQ
  - 验证：全量 pytest 绿；`check_guide` OK

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 改名后 `Person` 数仍为 1 | REQ-2 | 真库 Cypher |
| A2 旧名进 `previous_names` | REQ-2 | 真库读属性 |
| A3 同名不同 uid → 2 个 `Person` | REQ-3 | 真库 Cypher |
| A4 旧名查询能召回 | REQ-4 | `search_by_entities` |
| A5 迁移前后 `INVOLVES` 边数守恒 | REQ-5 | 迁移脚本报告 + Cypher |
| A6 无 uid 场景行为不变 | 不变行为 | 复用 035/043 用例 |

## 完成定义（DoD）

- [ ] A1–A6 全绿；全量 `pytest` 无回归（1324 基线）
- [ ] `check_spec.py` / `check_guide.py` 均 PASS
- [ ] 迁移脚本在真库跑过一遍，报告存档（合并组数 / 边数 / 孤儿数）
- [ ] `docs/user-manual.md` 更新

## 进展记录

### 2026-11-09 · spec 建立

来源：[043](../043-identity-source-priority/) 的 A4 偏差 —— `Account.nicks`
已经保留历史昵称，但 `Person` 仍按名字建键，改名会多一个节点。

**当时为什么没在 043 里做**：`Person.key` 由名字派生是全局约定
（`INVOLVES`、`ALIAS_OF`、图重建、`check_rebuild` 都依赖它）；
改键策略等于重做实体身份，必须单开 —— 混进 043 会把「身份来源」和「实体键」
两件事绑死，出问题也分不清哪层错。
