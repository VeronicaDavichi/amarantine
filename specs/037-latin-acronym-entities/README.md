---
status: complete
created: 2026-11-06
priority: high
tags:
- bugfix
- labeler
- entity
depends_on:
- '036'
created_at: 2026-11-06T07:26:10.842850606Z
updated_at: 2026-11-09T00:52:05.836271403Z
completed_at: 2026-11-09T00:52:05.836271403Z
transitions:
- status: complete
  at: 2026-11-09T00:52:05.836271403Z
---
# 英文缩写没有实体通道：ESB / API / Kafka 永远进不了图

> **Status**: planned · **Priority**: high · **Created**: 2026-11-06

## 目标

让 `extract_entities()` 能认出「像专有名词的拉丁缩写」（ESB / Kafka / CIF / PyCharm），
把它们变成 `Entity(kind="topic")` 节点；同时用停用词把 `API` / `SQL` / `JSON`
这类通用词挡在外面。

## 本 spec 的文档结构

| 文件 | 内容 |
|---|---|
| [requirements.md](requirements.md) | 缺陷现象、当前/预期/不变行为（EARS）、停用词口径、非目标 |
| [design.md](design.md) | 根因定位、第三条抽取通道、判据与去重、风险 |
| [tasks.md](tasks.md) | T1–T6 任务，每条带验证方法 |

## 现状（一句话）

图上 11 个 `Entity` 节点、**0 个纯英文**；`extract_entities("ESB接口返回字段的数据字典")`
返回空列表，整句话一个实体都抽不出来。

## 关联

- **035**（roster-name-extraction）：中文人名靠名册绕过 jieba，本 spec 是同一类
  「规则层缺一条通道」的问题
- **036**（split-truncation-data-loss）：ESB 卡片先要补回库，才能验证本 spec 的真库节点
- **033**（conflict-scan-health）：英文实体不参与前缀/编辑距离震荡扫描，需确认不误报
