---
status: planned
created: 2026-11-03
priority: high
tags:
- acceptance
- tooling
parent: '001'
created_at: 2026-11-03T03:29:05.502050173Z
updated_at: 2026-11-03T03:29:05.502050173Z
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

- [ ] T1 `harness/rebuild_graph.py`：读 Weaviate 全部卡 → 逐条写图 → 报告覆盖率
- [ ] T2 `harness/rebuild_vectors.py`：读全部卡 → 重新 embed → upsert → 报告覆盖率
- [ ] T3 `harness/check_rebuild.py`：双向解析校验（`weaviate_id` ↔ `event_id`）
- [ ] T4 `tests/test_archive_window.py`：A4 参数化断言
- [ ] T5 `docs/user-manual.md` 加「重建」一节（含风险提示）

## Test

- [ ] D1 重建后 `Event` 节点数 == Weaviate 卡数（100%）
- [ ] D2 任意 `weaviate_id` 能查到 `event_id`，反之亦然
- [ ] D3 `window=90` 与 `window=365` 的冷集不全等
- [ ] D4 `--dry-run` 前后目标库条数不变
- [ ] D5 重建脚本在空库上不报错（幂等起点）

## Notes

### 为什么这三条能一批做

它们的共同前提是「事实层（事件卡）是唯一真相源」，而这一点在 004/005 已经成立。
剩下的只是**验证重建能力**，不动写入语义，所以风险最低。

### 与其它 spec 的关系

A10 需要「图结构位置」作为硬信号（`GRAPH_HUB_DEGREE`），依赖 T1 建立的重建能力 ——
否则无法验证「重建后 degree 仍然正确」。所以本 spec 应先于 008。
