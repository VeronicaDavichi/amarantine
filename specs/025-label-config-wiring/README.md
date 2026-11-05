---
status: complete
created: 2026-11-05
priority: medium
tags:
- config
- labeler
- admin
- backend
created_at: 2026-11-05T09:20:52.565401512Z
updated_at: 2026-11-05T09:31:23.611046482Z
completed_at: 2026-11-05T09:31:23.611046482Z
transitions:
- status: in-progress
  at: 2026-11-05T09:20:53.336559163Z
- status: complete
  at: 2026-11-05T09:31:23.611046482Z
---
# 025 剩余三项零引用配置：两项接线，一项记录为「有意不接」

## Overview

零引用清单还剩 13 项。其中 6 项是 P2 预留（`CONSOLIDATE_*` / `CLUSTER_*` /
`COLD_QUERY_P95_MS`）、3 项是外部适配（`EMBEDDING_API_KEY` /
`EMBEDDING_PROVIDER` / `WEAVIATE_GRPC_PORT`）、1 项属 C.B3
（`ENTITY_ALIAS_SIM`）。

真正该处理的是这三项：

| 配置 | 查明的现状 | 结论 |
|---|---|---|
| `LABEL_REVIEW_BATCH` | `list_pending(limit=50)` 有默认值，端点也写死 `Query(default=50)` | **接线** |
| `LABEL_LLM_TEMP` | `llm.chat()` **根本不支持 temperature** 参数 | **接线**（要加参数） |
| `TOKENIZER` | `_estimate_tokens()` 是自创启发式，**docstring 明确写了「P0 不引入 tokenizer」** | **不接线，记录决策** |

第三项是这次最重要的发现：**它不是遗漏，是一个已经写在代码里的产品决策**，
但 ADR 里还挂着 `TOKENIZER=cl100k_base`，看起来像「配了没接」。

## Design

### D1 `LABEL_REVIEW_BATCH`：端点的默认值来源

`/admin/label-review` 端点是 `Query(default=50, ge=1, le=500)`，和
`list_pending(limit=50)` 两处硬编码。

改法：Query 的默认值改成 `None`，拿不到时回落到配置：

```python
limit: int | None = Query(default=None, ge=1, le=500)
...
effective = limit if limit is not None else cfg.LABEL_REVIEW_BATCH
```

**只改标签复核这一个端点**。其余 4 处 `Query(default=50)` 是不同用途
（events 列表、alias-reject 队列等），不该被 `LABEL_REVIEW_BATCH` 影响。

`list_pending` 的默认参数保持 50 —— 它是服务内部 API，调用方都显式传值。

### D2 `LABEL_LLM_TEMP`：给 `chat()` 加 temperature

现在 `chat()` 的 payload 里**没有 temperature 字段**，所以用的是服务端默认
（OpenAI 系通常 1.0）。ADR C.B10 说打标要防漂移、`LABEL_DUAL_RUN` 要双跑
校验一致率 —— 温度 1.0 会让同一句话两次打标结果不同，**一致率天然偏低**。

这是真实的功能缺陷，不只是配置没接。

改法：

```python
def chat(..., temperature: float | None = None) -> str:
    ...
    if temperature is not None:
        payload["temperature"] = temperature
```

`None` 时不传（保持现有行为，不改变其他调用方）。
`llm_labeler` 传 `temperature=settings.LABEL_LLM_TEMP`。

**注意 Anthropic 和 OpenAI 两条分支都要加** —— 只加一条就是半个修复。

### D3 `TOKENIZER`：不接线，把决策写清楚

`recall._estimate_tokens()` 的 docstring 原文：

> 粗略 token 估算：中文约 1 字 1 token，英文 4 字符 1 token。
> P0 不引入 tokenizer（会拖进 transformers 依赖），
> 估得偏保守即可——预算的作用是防止爆窗，不是精确计费。

这是**有意决策**，而且理由成立：
- `CONTEXT_TOKEN_BUDGET` 的用途是**截断**，不是计费
- 启发式偏保守（不会低估），对「防爆窗」是安全方向
- tiktoken 需要额外依赖 + 首次下载词表（离线环境会炸）

**所以不接线，但要消除「看起来像漏了」的误解**：
1. ADR 里把 `TOKENIZER` 标成「**有意不接**」并说明理由
2. 白名单理由从「分词器写死」改成准确描述
3. 保留配置项（将来换精确计数时用），不删

### D4 验收标准（沿用 spec 024 定下的规则）

每一项都必须有「改配置 → 行为变」的测试：

| 配置 | 怎么证明 |
|---|---|
| `LABEL_REVIEW_BATCH` | 不传 limit 请求端点，返回条数受配置控制 |
| `LABEL_LLM_TEMP` | 配 0.2 → 发出去的 payload 里 `temperature == 0.2` |

`TOKENIZER` 不需要这种测试（它本来就不生效，且已文档说明）。

## Plan

- [x] T1 `LABEL_REVIEW_BATCH`：端点 default 改 None + 回落配置
- [x] T2 `llm.chat()` 加 `temperature` 参数（两个 provider 分支）
- [x] T3 `llm_labeler` 传 `settings.LABEL_LLM_TEMP`
- [x] T4 `TOKENIZER`：ADR 标注「有意不接」+ 白名单理由改准
- [x] T5 测试：`LABEL_REVIEW_BATCH` 控制返回条数
- [x] T6 测试：`LABEL_LLM_TEMP` 出现在 payload 里
- [x] T7 白名单移除两项（`LABEL_REVIEW_BATCH` / `LABEL_LLM_TEMP`）

## Test

- [x] D1 `LABEL_REVIEW_BATCH=10` + 库里 30 条待复核 → 端点返回 10 条
- [x] D2 显式传 `?limit=3` → 返回 3 条（显式参数优先于配置）
- [x] D3 `LABEL_LLM_TEMP=0.2` → 打标请求的 payload 含 `temperature: 0.2`
- [x] D4 没传 temperature 时 payload 里**没有** temperature 字段（不改变其他调用方）
- [x] D5 Anthropic 分支同样带 temperature（两个分支都要测）
- [x] D6 现有 1080 条测试全绿

## Notes

### 为什么 `TOKENIZER` 不接线反而是对的

「把零引用清零」是个诱人的目标，但代价可能是引入 transformers 依赖、
让离线部署变脆、给一个只用来截断的预算加精确计费级的复杂度。

代码里已经有人想清楚了，并写在了 docstring 里。**这个决策值得保留**，
该做的是让文档和它一致，而不是让代码去迎合一个没跟上实现的 ADR 条目。

这是一个反例：**不是所有零引用都该接线**。有些是「有意不接」，
需要的是文档修正，不是代码改动。

### 与 spec 024 的关系

024 定的规则是「每个接线都要有『改配置→行为变』的测试」。
这里 D1/D2 照做，D3 明确豁免 —— 因为它不接线，写测试反而会固化
一个没人想要的依赖。

### 剩下的 10 项为什么不碰

- P2 预留 6 项：`CONSOLIDATE_*` 3 个 + `CLUSTER_*` 2 个 + `COLD_QUERY_P95_MS`，
  都属于 D8 consolidation，是**没实现的功能**，接线等于实现整个 P2
- 外部适配 3 项：`EMBEDDING_API_KEY` / `EMBEDDING_PROVIDER` /
  `WEAVIATE_GRPC_PORT`，当前只用本地无鉴权 TEI + REST，接了也没有分支走
- `ENTITY_ALIAS_SIM`：属 C.B3 三级消歧，spec 016 已经记了它是**新子系统**

这些留在白名单里是对的，只需要理由准确。

## 实现记录

### 三项的处置不同，第三项是这次最重要的发现

| 配置 | 查明的实际情况 | 处置 |
|---|---|---|
| `LABEL_REVIEW_BATCH` | 端点写死 `Query(default=50)` | 接线 |
| `LABEL_LLM_TEMP` | `llm.chat()` **根本不支持 temperature 参数** | 接线（要加参数） |
| `TOKENIZER` | `_estimate_tokens()` 的 docstring 已写明「P0 不引入 tokenizer」 | **不接线，记录决策** |

前两项是「配置没接」，第三项是「**有意不接**」。

### `TOKENIZER`：一个不该被接线的零引用

`recall._estimate_tokens()` 的 docstring 原文：

> 粗略 token 估算：中文约 1 字 1 token，英文 4 字符 1 token。
> P0 不引入 tokenizer（会拖进 transformers 依赖），
> 估得偏保守即可——预算的作用是防止爆窗，不是精确计费。

理由成立：
- `CONTEXT_TOKEN_BUDGET` 的用途是**截断**，不是计费
- 启发式偏保守（不会低估），对「防爆窗」是安全方向
- tiktoken 需要额外依赖 + 首次下载词表（**离线环境会炸**）

所以处置是**让文档追上代码**，而不是让代码迎合 ADR：

1. ADR 里标成「有意不接」并写明理由
2. 白名单理由从「分词器写死」改成准确描述
3. 保留配置项（将来换精确计数时用），不删

**这是一个反例：不是所有零引用都该接线。** 有些是「有意不接」，
需要的是文档修正。把零引用清零当目标，会引入没人想要的依赖。

### `LABEL_LLM_TEMP`：不只是配置没接，是功能缺陷

ADR C.B10 要求打标「双跑一致性校验」，`LABEL_DUAL_RUN_JACCARD=0.8`。

但 `chat()` 的 payload 里**没有 temperature 字段**，于是用服务端默认
（OpenAI 系通常 1.0）。温度 1.0 意味着同一句话两次打标结果不同 ——
**一致率天然偏低，看起来像模型不稳定**，实际是我们自己的调用有问题。

改法：

```python
def chat(..., temperature: float | None = None) -> str:
    ...
    if temperature is not None:
        payload["temperature"] = temperature
```

`None` 时不传字段 —— 保护 `alias_resolver` / `llm_split` 这些没要求
低温度的调用方，硬塞 0.2 会改变它们的输出分布。

**两个 provider 分支都加了**（Anthropic 的 system 是顶层字段，结构不同，
最容易只改 else 分支就以为完事）。测试专门覆盖了这一点。

### `LABEL_REVIEW_BATCH`：端点默认值改由配置决定

```python
limit: int | None = Query(default=None, ge=1, le=500)
effective = limit if limit is not None else cfg.LABEL_REVIEW_BATCH
```

只改 `/admin/label-review` 这一个端点。其余 4 处 `Query(default=50)` 是
不同用途（events 列表、alias-reject 队列），不该被这个配置影响。

`limit=None` 而不是「Query 默认值写成配置」—— 后者做不到（FastAPI 的
默认值必须是字面量），而且显式 `?limit=N` 必须优先于配置。

### meta-test：2/2 条测试被证明有效

沿用 spec 024 的做法，把实现改回没接线的状态：

```
LABEL_LLM_TEMP（不传温度）
  ① 现状               -> 2 passed
  ② 删掉传参           -> 2 failed   ✓ 能抓住

LABEL_REVIEW_BATCH（端点硬编码）
  ① 现状               -> 4 passed
  ② 退回 50            -> 4 failed   ✓ 能抓住

2/2 条测试被证明有效
```

### 实施中发现的一个好信号

给 `chat()` 加 `temperature` 后，`test_llm_labeler.py` 的 9 条测试全挂了 ——
因为它们的 fake `chat` 签名不接受新参数。

**但它们没有崩，只是返回了 `None`**：`llm_labeler` 的 `except Exception`
把 `TypeError` 兜住了，回退到规则层。

```
WARNING LLM 打标异常，回退规则层: fake() got an unexpected keyword argument 'temperature'
```

这正是「写入优先，打标可以降级」的设计生效的证据 —— 一个参数不匹配
不会让记忆写入失败。给 fake 加 `**kw` 后 27 passed。

用 `**kw` 而不是逐个列参数：以后 `chat` 再加参数，这些测试不会再集体崩。

### 测试

新增 13 条（`test_label_config_wiring.py`）+ 修复 1 处 fake 签名：

| 测的什么 | 为什么重要 |
|---|---|
| `temperature` 出现在 payload（3 个值参数化） | 核心证据 |
| `None` 时 payload **没有** temperature 字段 | 保护其他调用方 |
| Anthropic 分支同样带 temperature | 防止只改一个分支 |
| 打标回调传的是**配置值**不是常数 | 防「接了但接错」 |
| 改配置 → 传下去的温度跟着变 | 同上 |
| 不传 limit 时返回条数受配置控制（3 个值） | 核心证据 |
| `?limit=` 优先于配置 | 运维要看更多时的出口 |
| `total` 不受 limit 截断 | 否则前端以为只有 5 条 |

全量：**1093 passed**（1080 + 13）。

零引用：**13 → 11**。

### 剩下的 11 项为什么不碰

| 类别 | 项 | 为什么 |
|---|---|---|
| P2 预留 | `CONSOLIDATE_*` / `CLUSTER_*` / `COLD_QUERY_P95_MS`（6 个） | D8 consolidation 是**没实现的功能**，接线等于实现整个 P2 |
| 外部适配 | `EMBEDDING_API_KEY` / `EMBEDDING_PROVIDER` / `WEAVIATE_GRPC_PORT`（3 个） | 当前只用本地无鉴权 TEI + REST，接了没有分支走 |
| C.B3 | `ENTITY_ALIAS_SIM` | 三级消歧是新子系统（spec 016 已记为待做） |
| 有意不接 | `TOKENIZER` | 见上 |

这些留在白名单是对的，只是理由要准。**零引用清单的价值不在于清零，
而在于每一项都有明确的处置理由。**
