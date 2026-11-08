---
phase: design
---

# 036 设计：按行递归二分 + 可观测的兜底

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 任务：[tasks.md](tasks.md)

## 1. 根因

| # | 位置 | 问题 |
|---|---|---|
| 1 | `back_end/src/memory/services/llm_split.py:55` | `MAX_EVENTS = 40` 只是个常量，没有配套的「超限怎么办」 |
| 2 | `back_end/src/memory/services/llm_split.py:173` | `return parts[:MAX_EVENTS]` —— 直接砍尾，无日志无计数 |
| 3 | `back_end/src/memory/services/raw_quote.py:476-478` | 整批 turn 用 `\n` 拼成一段交给 `split_events`，**一次 LLM 调用处理整批**，没有分片编排 |
| 4 | `IngestJob` / `IngestOutcome` | 没有 `dropped_events` / `split_rounds` 字段，截断在可观测性上隐形 |

关键事实：`parts` 按对话时序排列，所以「截断」等于「砍掉最近发生的事」。

## 2. 修复方案：按行递归二分

`joined` 是 `"\n".join(turns)` 的结果 —— **行边界就是 turn 边界**，可以直接二分。

```
MAX_SPLIT_ROUNDS = 3        # 最多 8 片

split_all(text, settings, round_no=0):
    parts = split_events(text, settings)          # 原函数不变，仍返回 <= 40 条
    if len(parts) <= MAX_EVENTS or round_no >= MAX_SPLIT_ROUNDS:
        return parts, (round_no + 1)
    lines = text.splitlines()
    if len(lines) <= 1:                            # 单行长文本，无法二分 → 兜底
        return parts, (round_no + 1)
    mid = len(lines) // 2
    left,  r1 = split_all("\n".join(lines[:mid]), settings, round_no + 1)
    right, r2 = split_all("\n".join(lines[mid:]), settings, round_no + 1)
    return left + right, max(r1, r2)
```

**要点**

- `split_events` 的契约不动（仍 ≤40），分片在调用方编排 —— 现有单测全部继续有效
- 只有「第一批就撞上限」才会继续二分；≤40 条的批次走原路径，零额外调用
- 两个半批各自独立调用 LLM，上下文在二分点断开 —— 这是**为了不丢**付的代价，
  且只影响超限批次
- `round_no >= MAX_SPLIT_ROUNDS` 仍超限：保留当前片，`dropped = 原始 - 保留`，
  打 WARNING。此时至少「知道丢了」，不再是静默

### 2.1 实现时的偏离（2026-11-07）

伪代码里 `parts = split_events(text, ...)` 有个**致命前提问题**：`split_events`
返回前已经砍到 40 条了，`split_all` 拿不到「原始切出多少条」，
上面那句 `dropped = 原始 - 保留` **永远算成 0** —— A3 根本没法过。

落地时改成：

| 改动 | 说明 |
|---|---|
| 新增内部 `_split_once()` | 原 `split_events` 的函数体，**不截断**，LLM 给多少返回多少 |
| `split_events()` 变成两行 | `_split_once(...)[:MAX_EVENTS]`，对外契约不变（现有 27 条单测全过） |
| `split_all()` 用 `_split_once` | 因此能看见 65 > 40，二分才有触发条件，`dropped` 才是真数 |
| 返回值 `SplitResult(parts, rounds, dropped)` | 原设计是 `(parts, rounds)` 二元组；`dropped` 要一路传到 job，加一个字段 |

另外两处实现细节：

- **二分条件是 `> MAX_EVENTS` 而不是 `>=`**：刚好 40 条一条没丢，为它重切会破坏
  A2（≤40 条结果不变），也白付一次 LLM 调用
- **BFS 队列而不是递归**：两半的先后顺序与递归一致，但不用把 `rounds` 沿栈回传；
  `rounds` 取（最大深度）

## 3. 观测与数据模型

`ingest_job` 加两列（沿用项目 `CREATE TABLE IF NOT EXISTS` + `ALTER` 的迁移风格）：

| 列 | 类型 | 含义 |
|---|---|---|
| `dropped_events` | INTEGER DEFAULT 0 | 到保护上限后仍被丢弃的条数 |
| `split_rounds` | INTEGER DEFAULT 1 | 本批切分调用轮次（1 = 没分片） |

`IngestOutcome` 同步加这两个字段，`GET /admin/ingest/{job_id}` 的响应带上它们。

日志（截断时）：

```
logger.warning("切分超限截断 | job=%s kept=%d dropped=%d rounds=%d",
               job_id, kept, dropped, rounds)
```

## 4. 备选方案与取舍

| 方案 | 结论 | 理由 |
|---|---|---|
| 把 `MAX_EVENTS` 调到 200 | ❌ | 只把炸弹埋更深；200 之上仍会丢，且单次调用 token 上限没变 |
| 在 `/admin/ingest` 入口按轮次分批 | ❌ | 改变 `dedupe_key` 粒度与 job 语义，破坏幂等约定 |
| 截断后递归重跑剩余部分 | ✅ **采用** | 保证不丢；对调用方透明；短批次零影响 |
| 直接去掉上限（信任 LLM） | ❌ | 失去「防输出失控」的保护；`max_tokens` 截断会产出半截 JSON |

## 5. 测试策略

三层，缺一不可：

| 层 | 覆盖 | 手段 |
|---|---|---|
| 单测 | 分片正确性、兜底、降级不变 | 注入假 LLM（返回 N 条），断言 `dropped_events` / 调用次数 |
| 回归 | ≤40 条路径逐字节一致 | 同一夹具跑旧逻辑与新逻辑，对比 summaries |
| 真库 | 端到端补回 ESB 卡 | `--suffix` 重灌 10-30，查 Weaviate summary / Neo4j Event |

注意：真库验证**不要**用 `dedupe_key` 重放已有窗口（会返回旧 job），必须换后缀。

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 二分点切断上下文 | 两片摘要风格略有差异 | 仅超限批次；内容本身是独立事件，可接受 |
| 2 次 LLM 调用成本 | 长批次成本翻倍 | 只在撞上限时发生；53 轮 ≈ +1 次 |
| 递归写错导致重复落库 | 卡片重复 | 单测断言「片间 quote 不重叠」+ 总条数守恒 |
| `ALTER TABLE` 加到老库 | 迁移失败影响启动 | `PRAGMA table_info` 检查后再加（项目既有做法） |
