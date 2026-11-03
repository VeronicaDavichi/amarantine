---
status: complete
created: 2026-11-03
priority: high
tags:
- acceptance
- tooling
parent: '001'
created_at: 2026-11-03T03:29:05.502050173Z
updated_at: 2026-11-03T05:47:42.849241167Z
completed_at: 2026-11-03T05:47:42.849241167Z
transitions:
- status: in-progress
  at: 2026-11-03T05:42:31.781421918Z
- status: complete
  at: 2026-11-03T05:47:42.849241167Z
---
# 验收工具补齐：A4 / A8 / A14

## Overview

审计（`docs/acceptance-audit.md`）标了 9 条 🔴，其中 3 条缺的是**工具或测试**，
不是线上逻辑：

| A | ADR 原文要求 |
|---|---|
| A4 | `ARCHIVE_WINDOW_DAYS=90` 与 `=365` 下，同一批事件的冷集/热集划分不同且符合 D1 定义 |
| A8 | 清空 Neo4j 后从事件卡重建，`Event` 节点覆盖 Weaviate 全部 `event_id`（100%），`weaviate_id` 双向可解析 |
| A14 | 清空全部向量片段后从事件卡重新 embed，20 条固定 query 的 top-K `event_id` 覆盖率 100% |

共同点：**数据是可重建的，但没有一条命令能证明「真的能重建」**。
派生层与图都只写不重建 —— 一旦 Neo4j 数据损坏，没有任何恢复手段。

## 现状证据

- `ARCHIVE_WINDOW_DAYS` 在 `config.py` 有定义、`/admin/archive` 读到它，
  但没有「换值 → 划分变化」的测试
- `harness/` 下只有 `check_spec.py` / `check_guide.py` / `e2e_*.py` / `dev-*.sh`，
  **没有任何 rebuild 脚本**
- 图与向量都只经写入路径产生，没有「从事实层反向重建」的入口

## 非目标

- 不改线上写入路径
- 不引入新的编排框架（脚本沿用 `harness/dev-linux.sh` + `python -m`）
- 不做增量重建（先做全量，增量等痛点出现）

## Design

### 为什么 A8/A14 是脚本而不是测试

重建是**运维动作**，不是请求路径。做成 pytest 意味着每次跑测试都要清库，
在共享的 Weaviate / Neo4j 上是破坏性的，还会污染其他验证。

所以分两层：
- **脚本**：`harness/rebuild_graph.py`、`harness/rebuild_vectors.py`，
  幂等、默认 `--dry-run` 只报告不写
- **测试**：pytest 只测**纯函数**（覆盖率计算、双向解析、窗口分桶），不碰真库

### 为什么默认 dry-run

重建要清空目标库。默认不写意味着「误执行」的代价是零。
真要写必须显式 `--yes`，且脚本会先打印将被删除的条数。

### A4 用参数化测试

同一个夹具跑两次（`window=90` / `window=365`），断言两次的冷集**不全等**。
只断言「不全等」而不写死具体集合，避免测试和实现细节绑死。

## Plan

- [x] T1 `harness/rebuild_graph.py`：读 Weaviate 全部卡 → 逐条写图 → 报告覆盖率
- [x] T2 `harness/rebuild_vectors.py`：读全部卡 → 重新 embed → upsert → 报告覆盖率
- [x] T3 `harness/check_rebuild.py`：双向解析校验（`weaviate_id` ↔ `event_id`）
- [x] T4 `tests/test_archive_window.py`：A4 参数化断言
- [x] T5 `docs/user-manual.md` 加「重建」一节（含风险提示）

## Test

- [x] D1 正向覆盖 100%（节点数 **不等于** 卡数：软删的卡仍有图节点，实测 47 卡 / 59 节点）
- [x] D2 双向：正向覆盖 100% + 图内自洽 100%（口径见实现记录）
- [x] D3 `window=90` 与 `window=365` 的冷集不全等
- [x] D4 `--dry-run` 前后目标库条数不变
- [x] D5 重建脚本在空库上不报错（幂等起点）

## Notes

### 为什么这三条能一批做

它们的共同前提是「事实层（事件卡）是唯一真相源」，而这一点在 004/005 已经成立。
剩下的只是**验证重建能力**，不动写入语义，所以风险最低。

### 与其它 spec 的关系

A10 需要「图结构位置」作为硬信号（`GRAPH_HUB_DEGREE`），依赖 T1 建立的重建能力 ——
否则无法验证「重建后 degree 仍然正确」。所以本 spec 应先于 008。

（实际情况：008 先做完了，因为它的实现不依赖重建能力；
本 spec 的 `check_rebuild.py` 现在补上了「重建后 degree 仍正确」的验证手段。）

### 实现记录

**1 · A4 不是「测试缺」，是配置项根本没接线。**
spec 原本把 A4 归为「功能在、断言缺」。实施时发现
`ArchiveRequest.window_days` **硬编码 180**，`ARCHIVE_WINDOW_DAYS` 在
`src/` 里零消费 —— 改配置对行为没有任何影响。

这和 A10 / A16 / A27 是同一个模式：**配置项在、逻辑不在**。
修法是把默认值改成 `None`，端点里回落到 `cfg.ARCHIVE_WINDOW_DAYS`。
请求参数仍然优先，覆盖能力没丢。

所以 A4 的实际改动是 **bugfix + 测试**，不是纯补测试。

**2 · D1 的原始断言写错了（实测推翻）。**
spec 里写「重建后 `Event` 节点数 == Weaviate 卡数（100%）」。
实测是 **47 张卡 / 59 个节点** —— 因为**软删的卡在 Weaviate 里被过滤掉，
但图节点仍在**。硬要求数量相等会永远失败。

正确口径：
- **正向覆盖**（卡 → 图）：未删除的卡 100% 都在图里
- **反向自洽**（图 → 卡）：每个节点的 `weaviate_id` == `deterministic_uuid(ns, event_id)`
- 「图有、未删除卡没有」的数量单独报出来，**不判失败**（软删正常）

**3 · 反向校验不能依赖 Weaviate 的未删除视图。**
第一版 `check_rebuild.py` 用「图的 weaviate_id 能否在 `list_all` 里找到」
做反向校验，跑出 79.7% 假失败 —— 那 12 个就是软删的卡。
改成图内自洽后 100%。

**这条经验值得记**：派生层校验要拿「派生层自己的身份规则」做基准，
不要拿「另一个系统的当前可见视图」做基准 —— 两个系统的可见性语义不同。

**4 · `update_vector` 用 PATCH 不用 PUT。**
A14 只需要替换向量。PUT 是整对象替换，漏传 properties 会把卡的内容清空。
PATCH 只改指定字段。

**5 · 三个脚本默认 dry-run。**
重建是破坏性运维动作。默认只报告，加 `--apply` 才写 ——
和 `forget`/`archive` 的 `dry_run=true` 强制是同一套谨慎逻辑。

## 验收证据

- 单测：`pytest tests/test_archive_window.py -q` → **6 passed**
- 全量：`pytest tests -q --tb=line` → **921 passed**（915 + 6）
- `rebuild_graph.py --namespace work` → DRY-RUN 47 条，示例 id 正确
- `rebuild_vectors.py --namespace work --limit 2` → **成功 2/2（100%）**，
  模型 `BAAI/bge-m3`，A25 校验通过
- `check_rebuild.py --namespace work` → 正向 **100%**、反向自洽 **100%**、
  12 个软删卡节点（正常）→ `OK 双向可解析`，exit 0
- 空库：三个脚本都在 `cards` 为空时打印「空库重建是幂等的」并 exit 0
