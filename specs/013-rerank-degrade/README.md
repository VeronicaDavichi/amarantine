---
status: planned
created: 2026-11-03
priority: high
tags:
- acceptance
- recall
parent: '001'
created_at: 2026-11-03T03:30:01.374281242Z
updated_at: 2026-11-03T03:30:01.374281242Z
---

# A27：rerank 服务与降级路径

## Overview

ADR A27 原文：

> kill rerank 服务，`POST /admin/recall` 仍返回 200，`trace.rerank_skipped=true`、`trace.rerank_ms=0`，材料集非空

**现状：rerank 服务不存在。**

- `RERANK_MODEL = "BAAI/bge-reranker-v2-m3"` 在 `config.py` 有默认值
- `rerank` 在 `src/` 里只出现在两处**注释**里
- 实际实现用 **RRF 合并**两路（向量 + BM25）代替了 rerank

也就是说：ADR 设计的「召回 → rerank → 取 top-K」链路，
实现里是「召回 → RRF → 取 top-K」。**没有 rerank 这一环。**

## 这条的关键分歧

不是「忘了做」，是「**实现选了另一条路**」。两种处理：

- **A（本 spec）**：补上 rerank，让实现回到 ADR 的链路
- **B**：改 ADR，承认 P0 用 RRF，rerank 归 P2

用户已选 A。但 spec 里必须写清 B 的代价，否则以后的人不知道这是个选择。

**选 B 的理由也很强**：RRF 更便宜、无额外服务依赖、已经够用。
选 A 的代价是多一个服务依赖 + 每次召回多一次网络往返。

## 非目标

- 不改 RRF（它继续负责融合两路召回）
- 不做多模型 rerank 切换
- 不做 rerank 结果缓存

## Design

### rerank 放在哪一步

    BM25 ─┐
          ├─ RRF 融合 → 候选集(N) → rerank → top-K
    向量 ─┘

必须在 RRF **之后**、截断 **之前**。理由：
- rerank 是精排，输入应该是融合后的候选（而不是单路的原始分）
- 截断后再 rerank 没有意义（已经丢了候选）

### 降级语义

`rerank` 调用失败（超时 / 服务挂 / 返回格式错）：

- **不重试**（ADR 要求 `rerank_ms=0`，重试会破坏这条断言）
- `trace.rerank_skipped = true`
- `trace.rerank_ms = 0`
- 材料集用 RRF 分继续排 —— **结果非空**
- 整体仍是 200，不降级成 5xx

### 服务从哪来

`amarantine-embedding` (8082) 是否提供 rerank 接口需要**先核实**。
若没有，有两条路：
1. 在同容器加一个 rerank 端点（改 Docker 镜像，用户要自己 pull）
2. 用 `sentence-transformers` 在进程内加载（吃内存，WSL 只有 15 GiB）

**推荐先核实 1**。核实不通过再评估 —— 不要让 spec 假设服务已存在。

## Plan

- [ ] T1 核实 embedding 服务是否有 `/rerank` 端点（curl 探测）
- [ ] T2 `services/rerank.py`：调用 + 超时 + 异常归一
- [ ] T3 `recall` 接线：RRF 之后、截断之前
- [ ] T4 trace 加 `rerank_skipped` / `rerank_ms`
- [ ] T5 降级：失败时不重试、不 5xx、继续用 RRF 分
- [ ] T6 测试：正常 rerank / 服务挂降级 / trace 字段正确

## Test

- [ ] D1 rerank 正常 → `rerank_skipped=false`，`rerank_ms > 0`
- [ ] D2 rerank 失败 → 200，`rerank_skipped=true`，`rerank_ms == 0`，材料非空
- [ ] D3 失败时 trace 里 `top_k_decision` 仍存在（不能因为降级丢了可解释性）
- [ ] D4 rerank 超时不阻塞超过配置上限（防拖垮召回）
- [ ] D5 关掉 rerank（`RERANK_ENABLED=false`）时行为与「服务挂」一致

## Notes

### 这条一旦做，A16 才能做

A16 要求「保留 rerank 最高分的 N 个」。没有 rerank 就没有 per-chunk 分数来源。
所以 013 必须先于 010。

### 如果核实发现服务没有 rerank 端点

那 T1 就会失败。此时应当**回来改这个 spec**，而不是硬造一个进程内 rerank ——
在 15 GiB 的 WSL 里再加载一个 cross-encoder 模型，代价可能超过收益。
