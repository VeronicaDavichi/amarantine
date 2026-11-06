---
phase: design
---

# 038 设计：三种称呼句式 + 摘要护栏 + 脏边清理

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 任务：[tasks.md](tasks.md)

## 1. 根因

| # | 位置 | 问题 |
|---|---|---|
| 1 | `back_end/src/memory/services/labeler.py:235-241` | `_ALIAS_PATTERN` 只认 `也叫/也就是/又称/亦称/又叫/就是/即`，**没有称呼句式** |
| 2 | `back_end/src/memory/services/labeler.py:1474` | 别名抽取跑在 **`summary`（卡片摘要）** 上，而摘要是第三人称叙述：「任泽昂说明就是这个接口」 |
| 3 | `back_end/src/memory/services/labeler.py:1280-1313` | `_merge_alias_names` 只把两侧名字并为 `Entity(kind="topic")`，**不建边、不指向 Person** |
| 4 | `graph_outbox` | 建边只针对抽到的 claim，称呼节点（王老师）仍由 jieba 独立成 Person，无人合并 |

**最关键的认知**：`就是` 在用户原话里是等价断言（「糖包就是豆包」），
在摘要里却是叙述谓语（「任泽昂说明就是这个接口」）。同一个模式，两种语义 —— 这是脏边的来源。

## 2. 修复方案

### 2.1 补三种称呼句式（REQ-1）

```
_APPELLATION_PATTERN = [
    r"称呼\s*([\u4e00-\u9fa5]{2,4})\s*为\s*([\u4e00-\u9fa5]{2,6})",   # 称呼王勇为王老师
    r"(?:叫|称)\s*([\u4e00-\u9fa5]{2,4})\s*为?\s*([\u4e00-\u9fa5]{2,6})",  # 叫他王老师
    r"([\u4e00-\u9fa5]{2,4})\s*[（(]\s*([\u4e00-\u9fa5]{2,6})\s*[）)]",    # 王勇（王老师）
]
```

抽出的 claim：`canonical=王勇`（真人）、`alias=王老师`（称呼）。
方向必须固定：**canonical 是真名，alias 是称呼** —— 和 002 的 `X -> Y` 语义一致，
查询时从 alias 展开到 canonical。

### 2.2 摘要上的护栏（REQ-3 / REQ-4）

在 `extract_alias_claims` 的调用点区分来源：

```
原话（remember 的 input / ingest 的 turn）  → 现有模式，置信度按 002 的规则算
摘要（LLM 切分产物）                        → 额外要求：
                                               1. 两侧都 `_is_clean_name`
                                               2. 右侧长度 ≤ 6 且不含动词后缀
                                               3. 只要有一侧是「说明/表示/提到」开头 → 直接拒
```

被拒的走 `alias_reject.record_safe(...)`，reason 用新值 `summary_prose`，
进 002 已有的待办列表，人能看见、能复议。

### 2.3 建边与展开（REQ-2）

- 两侧都是 `Person`（或名册成员）→ `MERGE (alias)-[:ALIAS_OF {label:'exact', source_event_id}]->(canonical)`
- **不搬 `INVOLVES` 边**：王老师名下的 8 条事件留在原地，查询侧做展开：
  ```
  MATCH (p:Person {name:$q})-[:ALIAS_OF*0..1]-(alias:Person)
  MATCH (alias)-[:INVOLVES]-(e:Event)
  ```
- 展开深度固定 1 跳，不做闭包 —— 防止「A→B→C」把无关的人连起来

### 2.4 脏边清理（REQ-5）

新增管理动作（脚本即可，不必开新端点）：

```
MATCH (a)-[r:ALIAS_OF]->(b) WHERE r.label='uncertain' AND r.confidence < $th
DETACH DELETE r
```

先 dry-run 打印，人工确认后再删。清理记录进 spec 的进展段，不写库。

## 3. 备选方案与取舍

| 方案 | 结论 | 理由 |
|---|---|---|
| 用 LLM 判断称呼归属 | ❌（本次） | 每次写入多一次调用；已有关键证据是显式声明，规则够用 |
| 把称呼节点直接重命名为真人 | ❌ | 抹掉证据源；且同一称呼可能指不同人（035 的「王老师」就指过两人） |
| 查询侧展开 + 保留原边 | ✅ 采用 | 可逆、不破坏原始记录 |
| 只在摘要上禁用别名抽取 | ⚠️ 部分采用 | 摘要里也有真声明（「称呼王勇为王老师」），全禁会漏；改成加护栏 |

## 4. 测试策略

| 层 | 覆盖 | 手段 |
|---|---|---|
| 单测 | 三种句式各 1 正例 + 2 反例；摘要护栏拦住「说明就是」 | 直接调 `extract_alias_claims_verbose` |
| 图测试 | 别名边方向、label、source_event_id；展开查询命中 | 假 Neo4j 驱动 + 真库各一次 |
| 回归 | 002/003/035 的既有用例 | 全量 pytest |
| 真库 | 王勇 → 王老师 边存在；「王勇 前端」能召回 2 张卡 | 真库脚本 |

## 5. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 称呼句式误抽（「叫他不要动」） | 造出脏边 | 右侧必须 `_is_clean_name`；「不要/别/先」进停用 |
| 同称呼指多人 | 边连错人 | 只接受**同句内**声明；跨句推断不做 |
| 展开查询变慢 | 召回多一跳 | 只对 `Person` 做展开，且 `*0..1` |
| 清理误删正常边 | 证据丢失 | 只删 `label='uncertain'` 且 conf 低于阈值，且先 dry-run |
