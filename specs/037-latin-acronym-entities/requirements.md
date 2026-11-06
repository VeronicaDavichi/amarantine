---
phase: requirements
---

# 037 需求：拉丁缩写要成为实体，但不能是噪声

> 父 spec：[README.md](README.md) · 设计：[design.md](design.md) · 任务：[tasks.md](tasks.md)

## 1. 缺陷现象

`extract_entities()` 只有两类通道：中文词性（`nr`/`ns`/`nt`）与「中文前缀 + 编号」
（`项目A`）。**独立的拉丁缩写没有任何通道接住**，于是系统名/工具名从不成为图节点。

实测（服务端运行时 `.venv-linux` + jieba）：

```
任泽昂让王婕有空看一下群里ESB查询手机号的需求  → ['任泽昂', '王婕']
ESB接口返回字段的数据字典                    → []
王婕用esb查不到同联系信息                    → ['王婕']
API网关限制了模型大小                       → []
项目A 的系统日志                            → ['项目A']    ← 只有「项目+编号」能过
```

图上的现状：11 个 `Entity` 节点，**0 个纯英文**。

## 2. 当前行为（WHEN / THEN）

- **WHEN** 文本出现独立英文缩写 **THEN** 被丢弃（jieba 标 `eng`，无分支处理）。
- **WHEN** 文本是「英文缩写 + 中文后缀」 **THEN** 只有中文部分可能被收，缩写丢失。
- **WHEN** 缩写出现在 `项目A` 这类位置 **THEN** 靠 `_CODED_PATTERN` 侥幸保留。

## 3. 预期行为（WHEN / THEN SHALL）

- **REQ-1** **WHEN** 文本含「像专有名词的拉丁缩写」 **THEN** `extract_entities`
  SHALL 产出 `Entity(kind="topic")`。
- **REQ-2** 判据口径：长度 2–10；全大写，或含 ≥2 个连续大写，或字母+数字混合；
  且不在停用词表。
- **REQ-3** **WHEN** 缩写与 `_CODED_PATTERN` 命中重叠 **THEN** SHALL 只保留一条
  （`项目A` 不得被拆出 `A`）。
- **REQ-4** **WHEN** 同一份实现被查询侧调用（`memory_recall` 抽 query 实体）
  **THEN** SHALL 与写入侧同口径 —— 共用 `extract_entities`，不另写一套。

## 4. 不变行为（回归防护）

- **WHEN** 抽取中文人名 / 地名 / 机构 **THEN** SHALL CONTINUE TO 与当前一致
  （035 的名册最长匹配优先不变）。
- **WHEN** 文本含 `项目A` / `系统2` **THEN** SHALL CONTINUE TO 为 `topic`，
  kind 与形态不变。
- **WHEN** 缩写是通用技术词（`API` / `SQL` / `JSON` / `HTTP` / `ID` / `OK` …）
  **THEN** SHALL CONTINUE TO 不入图（停用词拦截）。
- **WHEN** jieba 不可用 **THEN** SHALL CONTINUE TO 走降级路径且可用
  （本通道是纯正则，不依赖 jieba）。
- **WHEN** 名册注册 **THEN** SHALL CONTINUE TO 保持最长匹配优先。

## 5. 非功能需求

| 维度 | 要求 |
|---|---|
| 延迟 | 纯正则，单次 `extract_entities` 增量 < 1ms，不引入 LLM 调用 |
| 噪声 | 停用词表首版 ≥ 30 项；上线后按实际噪音增补 |
| 一致性 | 写入与查询共用同一实现（REQ-4），不做两套判据 |
| 可维护 | 停用词表是模块级 `frozenset`，改一处即生效 |

## 6. 非目标

| 不做 | 理由 |
|---|---|
| 用 LLM 抽实体 | 写入是热路径（已有两次 LLM），成本与延迟都不划算 |
| 收纯小写英文单词 | `and`/`for`/`the`/任意英文词会大量涌入，噪声不可控 |
| 合并中英混写（`反洗钱ESB`） | 涉及分词边界，属于另一类问题 |
| 停用词配置化 | 首版写死够用，等真有运维需求再说 |
| 改 `_CODED_PATTERN` / 名册逻辑 | 现有行为已覆盖，动它只会引入回归 |
