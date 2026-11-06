---
status: planned
created: 2026-11-06
priority: high
tags:
- bugfix
- ingest
- llm
- data-loss
created_at: 2026-11-06T07:25:53.877961401Z
updated_at: 2026-11-06T07:25:53.877961401Z
---

# 切分超限静默截断：批次尾部事件永久丢失

> **Status**: planned · **Priority**: high · **Created**: 2026-11-06

## 目标

让 `POST /admin/ingest` 的批次切分**不再丢事件**：LLM 切出多少条，就落多少条；
万一到保护上限仍然超限，也必须留下 `dropped_events` 与 WARNING，不许静默。

## 本 spec 的文档结构

| 文件 | 内容 |
|---|---|
| [requirements.md](requirements.md) | 缺陷现象、当前/预期/不变行为（EARS）、非功能需求、非目标 |
| [design.md](design.md) | 根因定位、递归二分方案、观测字段、取舍与风险 |
| [tasks.md](tasks.md) | T1–T7 任务，每条带验证方法 |

## 现状（一句话）

补灌私聊语料时，10-29 丢 3 条、10-30 丢 25–29 条，丢的全是**批次尾部**；
其中一条正是「ESB 查询手机号需求」，直接导致记忆库回答不了「ESB 谁维护」。

## 关联

- **034**（seed-dingtalk-corpus）：首次记录 `MAX_EVENTS=40` 截断，但没算过损失量
- **037**（latin-acronym-entities）：英文缩写要进图，依赖本 spec 先把 ESB 卡片补回来
