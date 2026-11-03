---
status: complete
created: 2026-11-03
priority: high
tags:
- acceptance
- label
parent: '001'
created_at: 2026-11-03T03:30:01.158417969Z
updated_at: 2026-11-03T05:12:05.592377897Z
completed_at: 2026-11-03T05:12:05.592377897Z
transitions:
- status: in-progress
  at: 2026-11-03T05:04:44.079947444Z
- status: complete
  at: 2026-11-03T05:12:05.592377897Z
---
# A23：词表版本化

## Overview

ADR A23 原文：

> 词表变更后 `vocab_version` 递增，历史卡保留旧版本号，`GET /label/vocab` 可按版本回溯

**现状：零实现。**

- `vocab_version` 在 `src/` 里零命中
- `GET /label/vocab` 接口不存在
- 词表是代码里的常量（`DEFAULT_TYPE_RULES` / 环境变量 `TYPES_VOCAB`）

问题在于：**今天打的 `types`，半年后没人知道是按哪版词表打的**。
一旦词表改过，历史卡的标签就失去了可解释性 —— 而 ADR 的整个标签体系
是建立在「可回溯」上的。

## 非目标

- 不做词表在线编辑（本 spec 只解决「版本可追溯」，不解决「在线改词表」）
- 不改现有打标逻辑（`label_by_rules` / `label_event` 的入参不动）
- 不做迁移（历史卡没有版本号的，标为 `0` = 版本化之前）

## Design

### 版本号从哪来

三条路，推荐第三条：

1. 人工维护一个 `VOCAB_VERSION` 常量 —— 容易忘
2. 对词表内容做哈希 —— 稳定但不可读（`a3f9…`）
3. **内容哈希的前 8 位 + 自增序号** —— 可读且能判断「变没变」

推荐：`vocab_version = f"v{n}-{sha256(词表)[:8]}"`。
`n` 来自一个持久化的计数器（SQLite 表，沿用现有 service 建表模式）。

### 卡上怎么存

写入时把当时的 `vocab_version` 写进卡的属性（Weaviate schema 加一个字符串字段）。

历史卡没有这个字段 → 读出时补 `"v0-legacy"`，不假装它是某个具体版本。

### 接口怎么设计

    GET /label/vocab                    → 当前版本 + 完整词表
    GET /label/vocab?version=v3-a1b2c3d4 → 该版本的词表（从版本表读）

版本表只需存 `version` + `vocab_json` + `created_at`，不需要复杂查询。

## Plan

- [x] T1 `services/vocab_version.py`：计算版本号 + 持久化版本表
- [x] T2 Weaviate schema 加 `vocab_version` 字段（走 `_add_missing_properties` 增量添加）
- [x] T3 写入路径：打标时取当前版本，随卡写入
- [x] T4 `GET /label/vocab`（含 `?version=` 回溯）
- [x] T5 历史卡读出时补 `"v0-legacy"`
- [x] T6 测试：版本号稳定、变更后递增、回溯能取到旧词表

## Test

- [x] D1 同样词表算两次 → 版本号相同
- [x] D2 词表加一个类型 → 版本号变化且序号 +1
- [x] D3 写入的卡带 `vocab_version`，且等于当时的值
- [x] D4 `GET /label/vocab?version=<旧>` 返回旧词表原文
- [x] D5 不带版本号的历史卡读出为 `v0-legacy`（不报错、不伪造）
- [x] D6 不存在的版本 → 404（不返回空词表假装成功）

## Notes

### 为什么这条比 chunk/rerank 简单却排在前

它**不改检索行为**，只增加可追溯性。风险低、收益明确（审计和排障都要用）。
适合在动 chunk 之前先把「元数据可追溯」这条地基打好。

### 与 001 的接口清单关系

新增 `GET /label/vocab` 会让路由数从 26 变 27。
**实测 `check_guide.py` 不校验路由数量**（它校验的是配置项 73 / 工具 4 / 文档变量 37），
所以没有期望值需要同步 —— 但 `user-manual.md` 的接口清单仍应补上这一条。

### 实现记录

**1 · 版本号格式选了 `v{n}-{hash8}`，两个信息都要。**
`v{n}` 是自增序号（能一眼看出「改过几次」），`{hash8}` 是内容指纹
（能一眼看出「是不是同内容」）。只有序号的话，「改回旧词表」会被当成新版本；
只有 hash 的话，不可读。

**2 · 同内容不登记第二版。**
`current()` 先扫已有版本的指纹后缀，命中就复用。单测
`test_switching_back_reuses_registered_version` 钉住这条：
改词表 → 改回来，版本列表必须仍只有两项。

**3 · 词表原文必须存下来，不能只存 hash。**
只存 hash 的话，「回溯某个版本」只能回答「有这版」，说不出「这版长什么样」——
那和没有差不多。所以 `vocab_registry` 表带 `vocab_json` 列。

**4 · 一个向后兼容的坑：`insert_event` 多了一个参数。**
`vocab_version` 加到 `insert_event` 签名后，5 个测试文件的 FakeStore 全部
`TypeError`（43 条测试挂掉）。修法是把 FakeStore 的签名同步补上
`vocab_version=None`，而不是在 `remember` 里 try/except 吞掉 TypeError ——
后者会让「调用方传错参数」也静默通过。

**5 · 历史卡补 `v0-legacy` 放在 `_to_hit` 这个统一出口。**
不放在 `list_all` / `get_by_event_id` 各写一遍 —— 那是两处会漂移的重复逻辑。
`LEGACY_VOCAB_VERSION` 常量在 store 里本地定义（不 import `vocab_version`，
避免 service 层互相依赖）。

## 验收证据

- 单测：`pytest tests/test_vocab_version.py -q` → **13 passed**
- 全量：`pytest tests -q --tb=line` → **910 passed**（897 + 13）
- 守门：`check_guide.py` exit 0（配置 73 / 工具 4 / 文档变量 37）；`check_spec.py` exit 0
- 版本：同词表两次同版本；加类型 → `v2-*`；改回 → 复用旧版本不登记新的
- 回溯：`GET /label/vocab?version=<旧>` 返回旧词表原文；不存在 → 404
- 遗留：历史卡读出 `v0-legacy`，已有版本号不被覆盖
