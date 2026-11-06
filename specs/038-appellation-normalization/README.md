---
status: planned
created: 2026-11-06
priority: high
tags:
- bugfix
- entity
- alias
depends_on:
- '043'
created_at: 2026-11-06T08:08:06.427690904Z
updated_at: 2026-11-06T08:08:06.427690904Z
---

# 称呼没归一到真人：王泳和王老师是两个节点，别名边还被摘要句式污染

> **Status**: planned · **Priority**: high · **Created**: 2026-11-06

## 目标

让「王老师 = 王泳」「刘老师 = 刘佳」这类**称呼声明**变成图上的 `ALIAS_OF` 边，
使「前端开发是谁」这类组合查询能命中；同时拦住摘要在叙述语气里产生的脏别名边。

## 执行顺序

```
043 身份来源 → 038 称呼归一（本 spec） → 041 指称层 → 042 账号族
```

- **前置**：043 先落地「身份来自账号数据」，本 spec 的边以它产出的 `uid → Person` 为锚
- **给 041 留口**：边先写成**带 scope 字段**的形状，不要写死成全局等价 ——
  041 会把「称呼」泛化成「指称（scope 内义项）」，届时不用返工

## 本 spec 的文档结构

| 文件 | 内容 |
|---|---|
| [requirements.md](requirements.md) | 缺陷现象、当前/预期/不变行为（EARS）、非目标 |
| [design.md](design.md) | 根因（模式表 + 抽取位置 + 合并方向）、修复方案、风险 |
| [tasks.md](tasks.md) | T1–T7 任务，每条带验证方法 |

## 现状（一句话）

灌完四人小群后：`王泳` 88 条事件、`王老师` 8 条事件，**两个节点之间没有边**；
而全库唯一一条 `ALIAS_OF` 是脏的（`接口 → 任泽安说明`，label=uncertain）。

## 关联

- **035**（roster-name-extraction）：解决「名册里的名字被 jieba 拆坏」，本 spec 解决「称呼落到真人」
- **037**（latin-acronym-entities）：同属「抽取通道缺失」，那条是英文缩写，这条是称呼
- **002 / 003**：别名拒绝留痕与 `accepted` 补边机制已存在，本 spec 要复用而不是重造
