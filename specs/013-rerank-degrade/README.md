---
status: complete
created: 2026-11-03
priority: high
tags:
- acceptance
- recall
parent: '001'
created_at: 2026-11-03T03:30:01.374281242Z
updated_at: 2026-11-03T06:39:12.062428966Z
completed_at: 2026-11-03T06:39:12.062428966Z
transitions:
- status: in-progress
  at: 2026-11-03T06:34:51.166389625Z
- status: complete
  at: 2026-11-03T06:39:12.062428966Z
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

- [x] T1 核实：端点**存在**（TEI cpu-1.8），但只加载 bge-m3 → 424「not a re-ranker」
- [x] T2 `services/rerank.py`：调用 + 超时 + 异常归一
- [x] T3 `recall` 接线：RRF 之后、截断之前
- [x] T4 trace 加 `rerank_skipped` / `rerank_ms`
- [x] T5 降级：失败时不重试、不 5xx、继续用 RRF 分
- [x] T6 测试：正常 rerank / 服务挂降级 / trace 字段正确

## Test

- [x] D1 rerank 正常 → `rerank_skipped=false`，`rerank_ms > 0`
- [x] D2 rerank 失败 → 200，`rerank_skipped=true`，`rerank_ms == 0`，材料非空
- [x] D3 失败时 trace 里 `top_k_decision` 仍存在（不能因为降级丢了可解释性）
- [x] D4 rerank 超时不阻塞超过配置上限（防拖垮召回）
- [x] D5 关掉 rerank（`RERANK_ENABLED=false`）时行为与「服务挂」一致

## Notes

### 这条一旦做，A16 才能做

A16 要求「保留 rerank 最高分的 N 个」。没有 rerank 就没有 per-chunk 分数来源。
所以 013 必须先于 010。

### 如果核实发现服务没有 rerank 端点

那 T1 就会失败。此时应当**回来改这个 spec**，而不是硬造一个进程内 rerank ——
在 15 GiB 的 WSL 里再加载一个 cross-encoder 模型，代价可能超过收益。

### T1 探测结果（实测）

**端点存在，但模型不可用。** 这是一个比「有 / 没有」更细的结论：

```
镜像    : infiniflow/text-embeddings-inference:cpu-1.8
启动参数 : --model-id /models/bge-m3 --port 80 ...
POST /rerank  {"query":"a","documents":["b"]}  → 422 missing field `texts`
POST /rerank  {"query":"a","texts":["b"]}      → 424 "model is not a re-ranker model"
POST /rerank  加 model="BAAI/bge-reranker-v2-m3" → 424 同上
```

结论：

- `/rerank` 路由**存在**（422 而不是 404 证明它被注册了）
- 参数是 `query` + `texts`（不是 `documents`），`model` 可省
- **TEI 实例只会用它 `--model-id` 加载的那个模型做 rerank**，bge-m3 不是 reranker → 424

所以要真跑起来，需要**第二个 TEI 容器**加载 `BAAI/bge-reranker-v2-m3`
（`RERANK_BASE_URL` 已经预留成 `http://127.0.0.1:8083`，说明设计时就是这么想的）。

这是基础设施变更（用户要自己 pull 镜像 + 下模型），不在本 spec 的代码范围内。

### 因此本 spec 交付的是「代码侧 + 降级路径」

A27 的 ADR 原文核心断言是「**kill 掉服务**，召回仍 200」——
它验的从来就是降级，不是「rerank 调通」。所以：

- 降级路径：**用真异常测**（ConnectError / ReadTimeout / 424），已验
- rerank 正常路径：用注入的 fake client 验（请求形状、分数映射、重排效果）
- **真服务 e2e 未做** —— 需要第二个容器。这是诚实的缺口，不是「已实现」。

### 一个实现上的意外：`decide_k` 会自己排序

第一版接线是「rerank 后把 `scored` 按新顺序排一下」，测试挂了 —— 顺序没变。

原因：`decide_k` 的 docstring 明说「**顺序不限，本函数自己排**」，
而且 `Candidate` 是 `frozen` dataclass。所以改 list 顺序完全无效，
必须 `replace(c, score=rerank_score)` 换掉分数。

**代价（已写进代码注释）**：阈值 `min_score` / `drop_ratio` / `gap_ratio`
之后是在 **rerank 分的尺度**上判断，不再是 RRF 分。
两套分数不能混用于同一个排序，只能整套换掉 —— 这是 rerank 生效的必然结果。

## 验收证据

- 单测：`pytest tests/test_rerank.py -q` → **12 passed**
- 全量：`pytest tests -q --tb=line` → **933 passed**（921 + 12）
- 降级（真异常）：`ConnectError` / `ReadTimeout` / `RuntimeError` / 424
  → 全部 `skipped=true`、`rerank_ms == 0`、材料集非空
- trace：降级时 `top_k_decision` 仍在（不丢可解释性）
- 关闭：`RERANK_ENABLED=false` 与「服务挂」的 materials 顺序完全一致
- 请求形状：URL 以 `/rerank` 结尾，body 是 `query` + `texts` + `model`
- **缺口**：真 TEI reranker 服务未起（需第二个容器），e2e 未验
