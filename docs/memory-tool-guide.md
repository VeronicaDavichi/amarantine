# 记忆工具使用指南（LLM 侧）

> 这份文档是 **memory 工具的唯一真相**。MCP 提示词、用户手册、测试用例
> 都从这里取，不要再各写一份。

## 这是什么

一套给 LLM 用的长期记忆工具。四个工具，走 MCP stdio：

| 工具 | 作用 | 状态 |
|---|---|---|
| `memory_recall` | 按当前问题召回相关记忆 | ✅ 可用 |
| `memory_remember` | 记下一件值得长期记住的事 | ✅ 可用 |
| `memory_forget` | 让某条记忆失效（软删） | ✅ 可用 |
| `memory_feedback` | 告诉系统某条记忆有没有用 | ⏳ 待实现 |

## 三条铁律

### 1. `namespace` 不是工具参数

由宿主注入环境变量 `MEMORY_NAMESPACE`，服务端强制校验。
**你在工具参数里传 `namespace` 会被直接拒绝**（`isError: true`）。

理由：如果 LLM 能选 namespace，一次幻觉就能把私人记忆写进工作区，
或者反过来。隔离必须由不可被提示词影响的那一层保证。

合法 namespace：`^[a-z0-9][a-z0-9_-]{0,63}$`。

### 2. 空结果也是结果

`memory_recall` 找不到东西时返回 **HTTP 200 + `materials: []`**，
并在 `trace.empty_reason` 里说明原因：

| `empty_reason` | 含义 | 你该做什么 |
|---|---|---|
| `no_candidate` | 下面真的没有相关记忆 | 正常回答，别硬编 |
| `below_threshold` | 有候选但分数太低，被过滤了 | 同上，或换个问法 |

**服务端永远不填塞低质量结果，也不返回 404/204。**
看到空数组就是空，不要假装记得。

### 3. 不可用 ≠ 没有

服务不可用时返回 **503**（`RecallUnavailableError`），不是空数组。
这两个必须分清：

- `200 + []` → 确实没有这段记忆
- `503` → 检索挂了，不知道有没有

不要用「可能是 503」为借口编造记忆。

## memory_recall

按当前对话的语义召回。**不要**把整个历史对话喂进去，给它一个具体的查询。

```json
{
  "name": "memory_recall",
  "arguments": {
    "query": "上个月项目A延期的事"
  }
}
```

### 时间会自己解析

`query` 里的中文时间词会被解析成日期区间并参与过滤：

| 说法 | 解析结果 |
|---|---|
| 今天 / 昨天 / 前天 | 单日 |
| 本周 / 上周 / 本月 / 上个月 / 上个月底 | 区间 |
| 今年 / 去年 | 区间 |
| `2026-09` / `2026年9月` | 区间 |

解析结果在 `trace.date_range` 里，可以用来自查：

```json
{"start": "2026-09-01", "end": "2026-09-30", "confidence": 1.0, "matched": "上个月"}
```

### 返回结构

```json
{
  "materials": [
    {
      "event_id": "evt_0e0baab0b22e999e",
      "summary": "张三决定项目A延期到六月",
      "score": 0.844,
      "date_range": {"start": "2026-09-01", "end": "2026-09-30"},
      "types": ["decision", "work"],
      "pinned": true
    }
  ],
  "degraded": false,
  "partial": false,
  "trace": {
    "channels": {"vector": 1, "bm25": 0, "graph": 2},
    "top_k_decision": {"k": 1, "reason": "clamp_min", "overflow": false},
    "empty_reason": null
  }
}
```

### `degraded` 和 `partial` 是两件事

| 字段 | 含义 | 你要做什么 |
|---|---|---|
| `degraded: true` | 某个检索通道挂了（通常是图库），结果**不完整** | 可以正常用，但知道可能漏 |
| `partial: true` | 整体超时预算耗尽，只返回了部分 | 结果**可信但可能少**，必要时再查一次 |

两个都 `false` 才是完整结果。不要因为 `degraded` 就拒绝回答。

### top-K 是算出来的，不是你传的

不接受 `top_k` 参数。返回几条由六个步骤决定：
硬保留（pinned/unresolved，允许超员）→ 绝对分数过滤 → 相对衰减
→ 断崖检测 → token 预算 → clamp 到 `[3, 30]`。

`trace.top_k_decision.reason` 会告诉你为什么是这几条：

| `reason` | 含义 |
|---|---|
| `clamp_max` | 候选太多，截到上限 |
| `clamp_min` | 候选太少，但**不补低分**，给到下限就停 |
| `drop_ratio` | 相对衰减淘汰了尾部 |
| `gap` | 检测到断崖，断崖以下全丢 |
| `budget` | token 超了预算 |
| `overflow_forced` | pinned/unresolved 太多，超出上限也全保留 |

## memory_remember

记一件事。**不是记流水账** —— 只记未来会再用的信息。

```json
{
  "name": "memory_remember",
  "arguments": {
    "summary": "张三决定项目A延期到六月",
    "date_hint": "上个月",
    "pinned": false
  }
}
```

### 什么时候该记

| 该记 | 不该记 |
|---|---|
| 决策和它的理由 | 一次性的闲聊 |
| 人的偏好、约束 | 从上下文能立刻推出来的 |
| 还没解决的问题 | 已经完成的操作日志 |
| 明确的期限、承诺 | 猜的、不确定的 |

### 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `summary` | 是 | 一句话，含**谁 + 做了什么 + 什么时候** |
| `date_hint` | 否 | 中文时间词，不填默认今天 |
| `pinned` | 否 | 用户明确说「记住这个」时传 `true` |
| `important` | 否 | 用户强调重要性时传 `true` |
| `emotion_intensity` | 否 | 0–1，情绪强度 |

> `pinned=true` 会让这条记忆进入**永久保护**，遗忘扫描永远不碰它。
> 只在用户真的这么要求时才用，不要自己判断「这个很重要」就 pin。

### 幂等

同样的内容重复写入**不会产生重复卡**：`event_id` 由
`namespace + 日期 + 类型 + 实体 + 归一化摘要` 哈希得到。重复写入返回
`created: false` 和同一个 `event_id`。

## memory_forget

让一条记忆失效。

```json
{
  "name": "memory_forget",
  "arguments": {
    "event_id": "evt_0e0baab0b22e999e",
    "reason": "user_forget"
  }
}
```

`reason` 只能取 `user_forget`（用户要求忘掉）或 `outdated`（过时了）。

### 它到底做了什么

**软删，不是删除。** 具体说：

- 检索立刻查不到（`memory_recall` 不再返回）
- 对象本身**还在**，只是打了 `is_deleted = true`
- 原始对话记录**一动不动**

这是架构决定（N4）：事件卡与原文永不物理删除。所以你「忘掉」的东西
在审计通道里还能查到 —— 这是特性，不是 bug。

### 什么时候该用

| 场景 | 用吗 |
|---|---|
| 用户说「忘了吧」「删掉那条」 | ✅ 用，`user_forget` |
| 记忆过时了（比如换了公司） | ✅ 用，`outdated` |
| 记忆写错了 | ❌ **不要忘**，重写一条正确的 |
| 记太多了想清理 | ❌ 不要，交给遗忘扫描 |

## memory_feedback（待实现）

目前调用会返回 `isError: true`。等功能上线后再用。

## 常见错误对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `isError: true` + namespace 相关 | 你在参数里传了 namespace | 去掉，让宿主注入 |
| `200 + materials: []` | 确实没有这段记忆 | 正常回答 |
| `503` | 检索服务挂了 | 明确说「查不到」，不要编 |
| `degraded: true` | 图通道超时（常见但无害） | 照常用结果 |
| `partial: true` | 超了 1500ms 预算 | 结果可信，可能不全 |
| `created: false` | 内容重复，命中了幂等 | 正常，没写第二份 |
| `forgotten: true` 但还记得 | 那是你的上下文，不是记忆 | 记忆层已经删了 |

## 配置项（运维参考）

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MEMORY_NAMESPACE` | **必填** | 记忆域，不填拒绝启动 |
| `RECALL_TOP_K_MIN` | 3 | 返回条数下限（**不补低分凑数**） |
| `RECALL_TOP_K_MAX` | 30 | 返回条数上限 |
| `RECALL_COUNT_MIN_SCORE` | 0.5 | 计入「有效召回」的分数门槛 |
| `RECALL_DROP_RATIO` | 0.6 | 相对衰减：低于最高分 60% 的丢弃 |
| `RECALL_GAP_RATIO` | 0.15 | 断崖比例 |
| `CONTEXT_TOKEN_BUDGET` | 3000 | 单次召回 token 预算 |
| `RECALL_BUDGET_MS` | 1500 | 整体超时预算 |
| `FORGET_IDLE_DAYS` | 180 | 多久没碰算「闲置」 |
| `FORGET_MIN_RECALLS` | 1 | 召回少于几次算「没用过」 |
| `FORGET_GRACE_DAYS` | 90 | 新卡保护期 |
| `PROTECT_IMPORTANCE_REVIEW` | 0.7 | importance 超过它进人工复核 |

## 更新记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-10-31 | 首版：四个工具、三条铁律、时间解析、返回结构、错误对照 |
