---
status: in-progress
created: 2026-11-12
priority: high
tags:
- ingest
- context
- coreference
created_at: 2026-11-12T01:53:57.142013608Z
updated_at: 2026-11-12T02:33:10.859929340Z
transitions:
- status: in-progress
  at: 2026-11-12T02:33:10.859929340Z
---
# 会话内指代回溯与卡归并

> **Status**: planned · **Priority**: high · **Created**: 2026-11-12

## Overview

**问题**：建卡时 LLM 只看当前这一片（30 条）消息。片首的
「那个多少钱」「他说的那个」没有任何上文，只能瞎猜，产出的卡含糊且互相不知道对方存在。

**两条规则（用户定的）**：

| 模式 | 规则 |
|---|---|
| **灌输**（批量） | 每读 30 条建卡；指代不明就回溯上一轮 30 条，最多 120 条；**回溯内容只用于补充单一事件，不能为它建卡**；若结论是建新事件，必须回退到原始 30 条范围 |
| **日常**（1–2 条） | 取同会话往上 30 条，不够再往上，最多 120 条；判断并进旧卡或建新卡 |

**顺带解决**：让 LLM 输出「这条事件来自本片第几条消息」——建卡范围天然受限，
且时间精确（不再回落导入日）。

## Requirements

- **REQ-1** **WHEN** 建卡 **THEN** SHALL 带上同会话之前最多 30 条消息作为**只读上文**，
  并明确告知模型「不要为上文建卡」。
- **REQ-2** **WHEN** 模型表示上文不足 **THEN** SHALL 回溯到最多 120 条（同会话、按时间倒序）后重试一次。
- **REQ-3** **WHEN** 模型输出事件 **THEN** SHALL 带 `source_indices`（本片消息序号）；
  建卡**只允许**落在本片范围内，越界索引丢弃并计数。
- **REQ-4** **WHEN** 模型判定与已有卡是同一事件 **THEN** SHALL 输出 `merge_into`（目标 event_id）；
  系统建**新卡**并用 `resolves` 指向旧卡，**旧卡不动**。
- **REQ-5** **WHEN** 写卡 **THEN** SHALL 记录 `session_id`（群/私聊），供日常模式回溯。
- **REQ-6** **WHEN** 日常 1–2 条消息进入 **THEN** SHALL 走 `/admin/ingest`（原文落 `raw_quote`），
  享受与灌输模式相同的回溯能力。

## 不变行为

- `event_id` 仍由内容哈希（summary/entities/date）决定；归并不改旧卡，所以旧 id 稳定
- 没有会话上文时，行为与改动前一致（只处理本片）
- `raw_quote` 的结构与去重口径不变

## Design

### 1. 上下文从哪来

`raw_quote` 已经有 `session_id` + `ts` + `text`：

```sql
SELECT text, ts FROM raw_quote
WHERE namespace = ? AND session_id = ?
ORDER BY ts DESC LIMIT 30      -- 不够时再取 120
```

**只取当前批最早一条之前**的消息（不能把未来消息当上文）。

### 2. LLM 输出契约

```json
[
  {"summary": "…", "source_indices": [3, 4], "merge_into": "evt_xxx"},
  {"summary": "…", "source_indices": [7]}
]
```

- `source_indices` 是**本片内**的 1-based 序号；越界一律丢弃
- `merge_into` 可为空；给出时必须是上文里出现过的 event_id
- 额外字段 `needs_more_context: true` 表示要拉 120 条重试

### 3. 时间怎么定

`date_start` = 该事件所有 `source_indices` 对应消息里**最早**的 ts。

这取代了现在的 4-gram 猜测（那套保留作降级：模型没给索引时仍可用）。

### 4. 归并的表示

```
旧卡 evt_old（不动）
新卡 evt_new { resolves: [evt_old] }
```

查询时把 `resolves` 链展开，用户看到的是合并后的叙述；历史可追溯，
重灌也幂等（旧卡 id 不变）。

### 5. 提示词（关键约束）

```
【上文，仅供理解指代，不要为它建卡】
1. 王洁：那个键帽多少钱
2. 任泽安：你说的是哪个

【待切分，只处理这些】
1. 王洁：蓝色那个
2. 任泽安：89
```

明确「越界 = 建卡失败」，比事后校验更省事（但服务端仍要校验）。

## Plan

- [x] T1 `EventCard.session_id` + Weaviate 属性 + 写入路径
- [x] T2 `llm_split.split_with_sources()`：带上文、输出 source_indices 与 merge_into
- [x] T3 `run_ingest`：取上文 → 建卡（按索引定时间）→ 归并写 `resolves`
- [x] T4 越界索引与 `needs_more_context` 的 120 条重试
- [x] T5 日常模式：确认 1–2 条走 ingest 的链路
- [x] T6 测试与文档

## Test

- [x] A1 片首「那个多少钱」带 30 条上文后，卡里能看出指代对象
- [x] A2 模型给 `needs_more_context` 时，重试带 120 条
- [ ] A3 越界 `source_indices` 被丢弃并计数，不建卡
- [x] A4 时间 = 源消息最早 ts（不再是导入日）
- [ ] A5 `merge_into` → 新卡 `resolves` 指旧卡，旧卡 summary 不变
- [x] A6 无上文时行为与改动前一致
- [x] A7 全量 pytest 无回归

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 036 切分 | 本 spec 替换「按 4-gram 猜时间」，但保留其作为降级 |
| 040 导入工具 | 上下文来自它落的 `raw_quote`；窗口参数归它管 |
| 041 指称层 | 本 spec 做「会话内指代」，041 做「术语义项」，两者互补 |
| 039 关系谓词 | `resolves` 是事件级归并，039 是实体级关系，不冲突 |

### 诚实边界

- 回溯只解决**同会话**指代；跨群指代（「上次那个群说的」）不做
- 模型可能仍写错指代对象，`unresolved` 留待后续补（本 spec 不改该字段）
- 120 条是硬上限，超出仍看不懂就按「指代未解决」建卡（信息不丢，只是含糊）

### 2026-11-12 · T1–T5 落地

**已交付**

| 文件 | 内容 |
|---|---|
| `event_card.py` | 新增 `session_id`（不进 `event_id` 哈希） |
| `weaviate_store.py` | `session_id` 属性 |
| `llm_split.py` | `split_with_sources()`：上文只读 + `source_indices` + `merge_into` + `needs_more_context` |
| `raw_quote.py` | `RawQuoteStore.recent_for_session()`；`run_ingest` 分两条路径（有 session 走带上下文切分，无 session 保留 036 二分） |
| `admin_http.py` | `session_id` 透传给 `run_ingest` |

**真库验证**（`verify046`，6 条 / 2 片）

```
[1] 上文 0 条 | 本片 3 条 → 3 卡
[2] 上文 3 条 | 本片 3 条 → 3 卡      ← 第二片确实带了上文
卡片：6 张，session_id=verify046 全部写入
时间：date_start=2026-05-01（真实日期），source=msg_ts，不是导入日
```

**为什么保留老路径**：不传 `session_id` 的调用方（测试/批量脚本）仍需要
036 的「二分不丢事件」保证；两条路径共用同一套建卡循环（`SourcedEvent` 统一形状）。

**踩到的两个坑**

1. `recent_for_session` 插入失败（patch 静默未命中）→ job 卡在 `running`。
   教训：**上下文查询必须在 try 内**，否则后台任务异常退出、job 永远 running。
2. 带上下文的短文本会被「≤10 字符跳过 LLM」短路，导致 `needs_more_context` 丢失 ——
   该短路只对**单条**短消息生效，多片/日常单条都已覆盖。

**未验**：A3（越界索引已单测，真库未构造）、A5（`merge_into` 靠模型判定，
真库未稳定触发，单测覆盖）。

### 2026-11-12 · 重灌验证（10 群 + 大肥鱼乐园）

**修复后的真实数据**（107 片 / 3025 条原文）

| 指标 | 值 |
|---|---|
| 卡片 | **1659** |
| `time_source` | `msg_ts` **1656** / `relative` 3 |
| `date_start` 落在导入日 | **0 张**（修复前 46%） |
| 摘要率 | 大肥鱼 91% · 键帽群 92% · 病友群 95% · 搬💩中心 99% |
| 丢事件 | 0 |

**这一轮修掉的三处**（都是真库暴露的）

1. **`chat()` 返回 JSON 字符串，`_parse_sourced` 没解析** → 每次静默降级成
   逐条原文卡（实测 1900/1956 张是原文）。修后摘要率 82–100%。
2. **超时 60s 太短**：30 条上文 + 30 条本片 ≈ 60 条消息，实测单次 20–80s。
   改为 180s；失败时**截短上文（30→10）重试一次**，再失败才降级。
3. **提示词写「不润色」** → 模型照抄原文。改成「概括，不要照抄；纯图片写成
   『某人在群里发了图片』」。

**A1/A4/A6 已通过**：第二片起日志稳定出现「上文 30 条」；时间来自
`source_indices` 指向消息的最早 ts；无 session 的调用仍走 036 老路径。

**仍未验**：A3（越界索引，单测覆盖）、A5（`merge_into` 真库未稳定触发）。
