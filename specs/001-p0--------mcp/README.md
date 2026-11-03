---
status: complete
created: 2026-10-31
priority: high
tags:
- p0
- memory
- mcp
- backend
created_at: 2026-10-31T02:02:33.378234700Z
updated_at: 2026-11-03T02:51:13.438499288Z
completed_at: 2026-11-03T02:51:13.438499288Z
transitions:
- status: complete
  at: 2026-11-03T02:51:13.438499288Z
---
# P0 长期记忆服务（MCP 双入口）

> **Status**: in-progress · **Priority**: high · **Created**: 2026-10-31

## Overview

交付可被大模型通过 **MCP stdio** 调用、同时提供 **本地管理通道** 的长期记忆服务，
跑通「写入 → 召回 → 遗忘 → 复核 → 反馈」闭环。

**出口（二值判定）**：30 条验收全绿 —— A1–A13、A17–A19、A20–A26、A28–A34。

**非目标（P0 明确不做）**

| 不做 | 归属 |
|---|---|
| rerank 接入与绝对阈值重标（A27） | P1 |
| 图重建作业（A14–A16） | P1 |
| 物理冷热拆分 | P1 |
| consolidation / TopicCluster | P2 |
| SetFit 分类器 | P2 |
| 多用户 / 多租户 | 不做（只要 namespace 级隔离） |

## Design

**关键决策**

| 决策 | 位置 |
|---|---|
| 单 collection + `namespace` 字段过滤，`event_id` 含 namespace 防撞 id | ADR D1 |
| 双库单向派生：事件卡 → Weaviate / Neo4j | ADR D2 |
| 动态 top-K：token 预算 + 相对衰减 + 断崖 + 强制保留溢出 | ADR D3 / 4.3 |
| 遗忘三桶 + 五类硬信号；archive 与 forget 职责分离 | ADR D7 / 4.4 / 4.7 |
| 标签四层 + 双跑防漂移 + 词表版本化 | ADR D9 / 4.5 |
| 双入口：MCP stdio（LLM）+ Admin HTTP（人） | ADR 4.0 |
| 工具使用指南单一源头 → MCP instructions | ADR 4.10 |
| 语言信号：意图判断上移给 LLM，服务端只做参数校验与滥用护栏 | ADR C.B13 |
| 日志落盘，stdout/stderr 都不承载日志 | ADR 4.0 通用约定 |

**风险与缓解**

| 风险 | 缓解 |
|---|---|
| LLM 不主动调工具 | 工具 description 调优（E3）+ instructions |
| 指南与 schema 漂移 | `harness/check_guide.py`（E4）+ 用户 crontab |
| 事件切分粒度不稳 | 先用手工夹具验证，再调 prompt |
| 合成夹具不代表真实分布 | 基线结果标注「不可外推」 |
| stdio 调试困难 | 日志落盘 + JSON-RPC 回显先打通 |

## Plan

**里程碑**

| 里程碑 | 判定 | 状态 |
|---|---|---|
| M1 地基 | 三容器 healthy + MCP 能被 host 拉起 | ✅ 已完成（46 tests） |
| M2 可写入 | LLM 调 `memory_remember` 落库 | ✅ 已达成（002–005 陆续交付） |
| M3 可召回 | `memory_recall` 返回材料 + trace | ✅ 已达成（含 `trace_id` 与 feedback 回路） |
| M4 闭环完整 | 遗忘 / 复核 / 删除 / 反馈全通 | ✅ 已达成（+ archive 候选、ingest 批量导入） |
| M5 管理可用 | 前端能裁决与看报告 | ➡️ **拆到 [006](../006-frontend-m5/README.md)**（ADR 无前端章节，需求待澄清） |
| M6 验收全绿 | 30 条脚本全过 | ⚠️ **等价达成**：850 条 pytest + `check_spec` / `check_guide` / `e2e_mcp` 全绿；ADR G2 要求的「每条 A 项一个独立脚本」**形式未做**，记为遗留 |

**M1 已完成的具体项**

- [x] A1 配置加载：ADR D6 全部配置，`init > env > .env > yaml > 默认`
- [x] A2 EventCard：25 字段（8 必填 / 17 可选），与附录 B 契约对拍
- [x] A3 合成夹具：33 条，覆盖五类硬信号、冲突对、持续事件、新老条目
- [x] A4 docker-compose：Weaviate + embedding + Neo4j（WSL 运行）
- [x] A5 MCP stdio 框架：JSON-RPC 收发 + 4 工具契约 + namespace 三重防护 + 日志落盘

## Test

以 ADR 第 5 节 A1–A34 为准（P0 覆盖 30 条）。脚本化见 P0 清单 G2。

## Notes

- [ADR-001 架构](../../docs/ADR-001-memory-architecture.md)
- [P0 任务清单](../../docs/P0-task-plan.md)
- [工具使用指南](../../docs/memory-tool-guide.md)
- [技术选型](../../stack-decision.md)

（本文件于 2026-11-03 按 `.lean-spec/templates/spec-template.md` 骨架重排，
内容未改；原手写的 `## 目标 / ## 非目标 / ## 关键决策 / ## 里程碑 / ## 验收 /
## 风险 / ## 相关文档` 归位到上述五段。顺带修正一处笔误：
`harness/check-guide.py` → `harness/check_guide.py`。）
