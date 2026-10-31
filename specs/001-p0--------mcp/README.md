---
status: in-progress
created: 2026-10-31
priority: high
tags:
- p0
- memory
- mcp
- backend
created_at: 2026-10-31T02:02:33.378234700Z
updated_at: 2026-10-31T02:02:33.378234700Z
---

# P0 长期记忆服务（MCP 双入口）

## 目标

交付可被大模型通过 **MCP stdio** 调用、同时提供 **本地管理通道** 的长期记忆服务，跑通「写入 → 召回 → 遗忘 → 复核 → 反馈」闭环。

**出口（二值判定）**：30 条验收全绿 —— A1–A13、A17–A19、A20–A26、A28–A34。

## 非目标（P0 明确不做）

| 不做 | 归属 |
|---|---|
| rerank 接入与绝对阈值重标（A27） | P1 |
| 图重建作业（A14–A16） | P1 |
| 物理冷热拆分 | P1 |
| consolidation / TopicCluster | P2 |
| SetFit 分类器 | P2 |
| 多用户 / 多租户 | 不做（只要 namespace 级隔离） |

## 关键决策

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
| 日志落盘，stdout 只跑协议 | ADR 4.0 通用约定 |

## 里程碑

| 里程碑 | 判定 | 状态 |
|---|---|---|
| M1 地基 | 三容器 healthy + MCP 能被 host 拉起 | ✅ 已完成（46 tests） |
| M2 可写入 | LLM 调 `memory_remember` 落库 | ⏳ |
| M3 可召回 | `memory_recall` 返回材料 + trace | ⏳ |
| M4 闭环完整 | 遗忘 / 复核 / 删除 / 反馈全通 | ⏳ |
| M5 管理可用 | 前端能裁决与看报告 | ⏳ |
| M6 验收全绿 | 30 条脚本全过 | ⏳ |

## 已完成（M1）

- A1 配置加载：ADR D6 全部配置，`init > env > .env > yaml > 默认`
- A2 EventCard：25 字段（8 必填 / 17 可选），与附录 B 契约对拍
- A3 合成夹具：33 条，覆盖五类硬信号、冲突对、持续事件、新老条目
- A4 docker-compose：Weaviate + embedding + Neo4j（WSL 运行）
- A5 MCP stdio 框架：JSON-RPC 收发 + 4 工具契约 + namespace 三重防护 + 日志落盘

## 验收

以 ADR 第 5 节 A1–A34 为准（P0 覆盖 30 条）。脚本化见 P0 清单 G2。

## 风险

| 风险 | 缓解 |
|---|---|
| LLM 不主动调工具 | 工具 description 调优（E3）+ instructions |
| 指南与 schema 漂移 | `harness/check-guide.py`（E4）+ 用户 crontab |
| 事件切分粒度不稳 | 先用手工夹具验证，再调 prompt |
| 合成夹具不代表真实分布 | 基线结果标注「不可外推」 |
| stdio 调试困难 | 日志落盘 + JSON-RPC 回显先打通 |

## 相关文档

- [ADR-001 架构](../../docs/ADR-001-memory-architecture.md)
- [P0 任务清单](../../docs/P0-task-plan.md)
- [工具使用指南](../../docs/memory-tool-guide.md)
- [技术选型](../../stack-decision.md)
