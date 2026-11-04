---
status: complete
created: 2026-11-04
priority: high
tags:
- config
- recall
- time
- backend
created_at: 2026-11-04T03:07:34.291269406Z
updated_at: 2026-11-04T03:15:36.334070403Z
completed_at: 2026-11-04T03:15:36.334070403Z
transitions:
- status: in-progress
  at: 2026-11-04T03:07:52.034549338Z
- status: complete
  at: 2026-11-04T03:15:36.334070403Z
---
# 015 时间置信度下限：`TIME_CONF_MIN` 真正生效

## Overview

`TIME_CONF_MIN = 0.5` 在 `config.py` 有默认值，`src/` 里**零引用**（014 审计发现）。
ADR **C.B9** 对它的语义写得很明确：

> 规则区间 `R`、LLM 区间 `L` …… `confidence < TIME_CONF_MIN(0.5)`
> → **不设 date filter**（退回全库），标 `time_unresolved=true`

### 现状：用一个粗糙的门代替了它

`recall.py` 现在写的是：

```python
if date_range.confidence > 0 and date_range.end is not None:
    date_from = ...
```

`> 0` 等于「只要解析出个东西就当确定时间用」。而 `time_resolver` 的
`DateRange.confidence` 是**分级**的：精确日期高、模糊相对时间低。
于是「前几天」这种低置信解析会把召回**锁死在错误的日期窗口里**，
比不设过滤器更糟 —— 不设过滤器至少还能靠向量/BM25 召回。

这是「配了没生效」里危害最直接的一个：不是少了个功能，是**召回率被悄悄削掉**。

### 为什么独立做

与 `ENTITY_ALIAS_SIM` 一起发现，但工作量差一个量级：
那个要新建实体名 embedding 索引（见 spec 016），这个只是把已有的
`confidence` 字段接到判定上。不捆绑。

## 非目标

- 不改 `time_resolver` 的解析逻辑与分级（它的 confidence 取值已有测试）
- 不实现 C.B9 的「规则区间 ∩ LLM 区间」合并（现在没有 LLM 时间解析这条路径，
  只有规则层；等有 LLM 区间时再做）
- 不动 `date_to`（当前没有上界逻辑）

## Design

### D1 判定点的位置

只有一处：`recall.py` 步骤 1 计算 `date_from` 的地方。
改成：

```python
date_from = None
time_unresolved = False
if date_range.end is not None:
    if date_range.confidence >= settings.TIME_CONF_MIN:
        date_from = f"{date_range.start.isoformat()}T00:00:00Z"
    else:
        time_unresolved = True
```

注意 `date_range.end is None` 时不算 `time_unresolved` —— 那是「持续中/单日」
的正常形态（`resolve_from_query` 对「最近」这类给 `end=None`），不是解析失败。

### D2 `time_unresolved` 要进 trace

ADR 原文要求「标 `time_unresolved=true`」。它属于**可解释性**：
排查「为什么召回不到」时，要知道是「没解析出时间」还是「解析出来了但我不信」。

trace 里加 `time_unresolved: bool` 与 `time_confidence: float`。

### D3 阈值 0 = 关闭该闸门

沿用项目既有约定（A10 的硬信号、A25 的守卫都这样）：
`TIME_CONF_MIN <= 0` 表示「不设置信度下限」，即退回 `confidence > 0` 的老行为。
这样配置成 0 是一个明确的「关掉」动作，而不是「全部不通过」。

必须写测试钉住，否则 `0` 会被误读成「什么都不算」。

## Plan

- [x] T1 确认 `DateRange.confidence` 各分支的取值分布（读 time_resolver 测试）
- [x] T2 `recall.py`：用 `TIME_CONF_MIN` 替换 `> 0`，加 `time_unresolved`
- [x] T3 trace 里带 `time_unresolved` / `time_confidence`
- [x] T4 `TIME_CONF_MIN <= 0` 时退回老行为
- [x] T5 `.env.example` 补该项说明

## Test

- [x] D1 高置信（0.9）→ 有 `date_from`，`time_unresolved=false`
- [x] D2 低置信（0.3）→ 无 `date_from`，`time_unresolved=true`，且**召回仍有材料**
- [x] D3 `end=None`（「最近」）→ `time_unresolved=false`，不设 `date_from`
- [x] D4 `TIME_CONF_MIN=0` → 退回老行为（0.3 也设 `date_from`）
- [x] D5 现有 979 条测试全绿

## Notes

### 为什么「低置信就不设过滤器」比「按低置信设过滤器」好

直觉是「有点线索总比没有好」。但时间过滤器是**硬 AND**：
一旦把窗口设窄了，窗口外的正确材料就**永远回不来**（后面所有通道都在窗口内）。

而低置信解析的错误率不低（「前几天」可能指 3 天也可能指 10 天）。
所以正确取舍是：**宁可全库搜（还能靠语义命中），也不锁死在一个猜的窗口里。**

### `confidence` 的分级是拍脑袋的吗

不是。`time_resolver` 对「2026-11-04」这类精确日期给高置信，
对「上个月」「前几天」这类相对表达给低置信。差别在于**可复现性**：
精确日期无论今天几号都解析成同一天，相对表达依赖 `today`。

C.B9 的「为什么规则优先」讲的是同一件事：可复现 > 覆盖广。

### 这个 spec 不碰 LLM 时间解析

C.B9 的冲突合并（`R` ∩ `L`）假设有两条路径。现在只有规则层一条，
LLM 只在 `date_hint` 参数里被动接收。等真的加了 LLM 时间解析，
再把「取交集 / 无交集降置信」补上 —— 那时候 `TIME_CONF_MIN` 会用在合并后的
`confidence` 上，与本 spec 不冲突。

## 实现记录

### 核心发现：`DateRange.confidence` 其实是布尔的

接上 `TIME_CONF_MIN` 之前先读了 `time_resolver`，发现它的置信度**只有两个值**：

```python
HIGH_CONFIDENCE = 1.0
FALLBACK_CONFIDENCE = 0.0
```

所有「能解析」的表达都给 1.0，**包括「上个月」「去年底」这类窗口宽度靠猜的相对表达**。
所以：

- 旧代码 `confidence > 0` 与 `confidence >= 0.5` **完全等价**
- C.B9 的「低置信不设过滤器」在当前实现下**永远不会触发**

必须诚实说清楚：**本 spec 接上配置后，默认行为没有任何变化。** 它的价值是：

1. 语义正确 —— 配置不再描述一个不存在的闸门
2. 未来安全 —— 一旦 `time_resolver` 开始给模糊表达中间置信度，闸门自动生效
3. `time_unresolved` / `time_confidence` 进 trace，可解释性是真提升

`test_real_resolver_confidence_is_binary` 把现状钉住了：将来有人加中间置信度，
这条测试会挂，提醒他回来看这里。

### 真正该做的下一步（不在本 spec 范围）

`time_resolver` 应该**给模糊表达低置信**：

| 表达 | 现在 | 应该 |
|---|---|---|
| `2026-11-04` | 1.0 | 1.0（可复现） |
| `昨天` / `本周` | 1.0 | 1.0（边界确定） |
| `上个月` / `去年底` | 1.0 | **0.6 左右**（窗口靠 today 推算） |
| `上个月底` | 1.0 | **0.6**（代码里给的是 `end-9天 ~ end`，9 天是拍的） |
| `最近` / `前几天` | 0.0 | 0.0（解析不出） |

判据应该是**可复现性**：与 `today` 无关的给高置信，依赖 `today` 且窗口宽度是猜的给低置信。
这是产品判断，值得单独评审，所以没在本 spec 里顺手改。

### 四个 trace 构造点，一开始只改了一个

`recall.py` 有 4 处返回 `RecallResult`（正常、`no_candidate`、`below_threshold`、
以及主路径）。第一版我只在 `gate_trace` 旁边加了一处，于是 6 个测试全挂 `KeyError`。

教训：**改 trace 字段时要数一下构造点有几个**，不能看到一处就以为只有一处。

### 白名单防腐测试抓住了自己

`test_whitelist_entries_are_still_unused`（014 加的）在跑全量时挂了：

```
FAILED tests/test_config_usage.py::test_whitelist_entries_are_still_unused
```

因为 `TIME_CONF_MIN` 现在**有消费点了**，但它还在白名单里。

这正是那个测试存在的意义 —— 白名单必须随接线进度收缩，否则会腐烂成
「历史上曾经没用过的字段集合」。已从白名单移除，并把元测试改成反向断言。

## 验收证据

- `pytest tests/test_time_confidence.py -q` → **7 passed**
- 全量：`pytest tests -q --tb=line` → **986 passed**（979 + 7）
- `audit_config_usage.py`：零引用 **18**，`TIME_CONF_MIN` 已不在其中
- `test_config_usage.py` → 9 passed（白名单已收缩）
- `.env.example` 补了该项与「为什么 <=0 是关闭」的说明

### 测试覆盖的边界

- D1 0.9 ≥ 0.5 → 设过滤器
- D2 0.3 < 0.5 → 不设，且**材料仍在**（这是取舍的全部意义）
- D3 `end=None` → 不算 unresolved（单日/持续中的正常形态）
- D4 阈值 0 → 关闭闸门，不是「全部不通过」
- D5 阈值 > 1 → 连 1.0 都过不去（确认比较方向没写反）
