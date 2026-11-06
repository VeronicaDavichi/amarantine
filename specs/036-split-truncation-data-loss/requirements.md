---
phase: requirements
---

# 036 需求：切分不得丢事件

> 父 spec：[README.md](README.md) · 设计：[design.md](design.md) · 任务：[tasks.md](tasks.md)

## 1. 缺陷现象

`split_events()` 在 LLM 切分结果超过 `MAX_EVENTS`（40）时直接截断。批次尾部的事件
永久消失：原文层（`raw_quote`）还在，卡片与图节点没有，且调用方拿不到任何信号。

实测（2026-11-06 补灌私聊语料，namespace `njzq-dingtalk`）：

| 批次 | 有效轮次 | LLM 切出 | 实际入库 | 丢失 |
|---|---|---|---|---|
| 10-29 | 39 | 43 | 40 | 3 |
| 10-30 | 53 | 65~69 | 40 | 25~29 |
| 10-31 / 11-02 / 11-03 | 17 / 7 / 8 | 18 / 7 / 8 | 同 | 0 |

被丢的内容包括「任泽昂让王婕有空看一下群里ESB查询手机号的需求」。

## 2. 当前行为（WHEN / THEN）

- **WHEN** 一批 turn 拼接后 LLM 切出 N 条，且 `N > MAX_EVENTS`
  **THEN** 只保留前 40 条，其余丢弃。
- **WHEN** 发生截断 **THEN** 无 WARNING、无 job 字段、无返回值信号。

## 3. 预期行为（WHEN / THEN SHALL）

- **REQ-1** **WHEN** `N > MAX_EVENTS` **THEN** 系统 SHALL 通过分片再切分把全部 N 条落库，
  `dropped_events = 0`。
- **REQ-2** **WHEN** 分片递归到保护上限仍超限 **THEN** 系统 SHALL 保留可落库部分，
  并在 job 上记 `dropped_events > 0`，同时打 WARNING 日志。
- **REQ-3** **WHEN** 任一批次发生截断 **THEN** `GET /admin/ingest/{job_id}`
  SHALL 能看到 `dropped_events`（值可为 0）。
- **REQ-4** **WHEN** 批次被分片 **THEN** job SHALL 记录 `split_rounds`（分片次数），
  便于区分「一次切完」与「切了三次」。

## 4. 不变行为（回归防护）

- **WHEN** 切分结果 ≤ 40 条 **THEN** SHALL CONTINUE TO 与当前逐字节一致
  （同一夹具产出同一组 summary）。
- **WHEN** LLM 调用失败 / 输出不合法 **THEN** SHALL CONTINUE TO 降级成 `[整段文本]`，
  job 不 failed。
- **WHEN** 同一 `dedupe_key` 重复提交 **THEN** SHALL CONTINUE TO 返回已有 job，不重跑。
- **WHEN** 原文落库 **THEN** SHALL CONTINUE TO 先于可见（202 返回时
  `GET /raw/{quote_id}` 可查）。
- **WHEN** 单条过长 / 原文照抄 **THEN** SHALL CONTINUE TO 由 `_looks_like_echo`
  判无效并降级。

## 5. 非功能需求

| 维度 | 要求 |
|---|---|
| 成本 | 长批次最多多 1–2 次 LLM 调用（53 轮 → 2 次）；短批次 0 次 |
| 延迟 | 切分在异步 job 内，不增加 202 的响应时间 |
| 可观测 | 截断必须 WARNING + `dropped_events`；分片必须 `split_rounds` |
| 存储 | 不改 Weaviate schema；SQLite `ingest_job` 只加列（`CREATE TABLE IF NOT EXISTS` 迁移风格） |

## 6. 非目标

| 不做 | 理由 |
|---|---|
| 改 `dedupe_key` 粒度（一次提交变多个 job） | 破坏「一批一 job」约定与幂等 |
| 改 `split_events` 的对外契约（仍返回 ≤40） | 分片放在调用方，改动面最小 |
| 顺手修 failed job 幂等 | 034 遗留问题，与本 spec 无关 |
| 重跑 LLM 让摘要风格统一 | 两片独立调用，风格差异可接受（只影响超限批次） |
