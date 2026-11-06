---
status: complete
created: 2026-11-06
priority: high
tags:
- graph
- testing
- harness
- observability
created_at: 2026-11-06T03:35:39.502248061Z
updated_at: 2026-11-06T03:47:46.775550380Z
completed_at: 2026-11-06T03:47:46.775550380Z
transitions:
- status: complete
  at: 2026-11-06T03:47:46.775550380Z
---
# 030 健康报告的两个假警报，和 hub 保护的端到端验证

## Overview

`check_data_health.py` 报了两条警告，查下来**都不是它说的那回事**：

### 警报 1：「实体名为空: 1」

那张卡是 `周会上讨论了明年的预算分配方案`。实测：

```
extract_entities("周会上讨论了明年的预算分配方案")  →  []
```

**这是正常的**：`extract_entities` 抽的是人名/地名/机构名/项目名，
「周会」是会议、「预算」是议题，都不是实体。这句话**本来就该没有实体**。

问题是健康报告只数了个数，**没有区分「本该为空」和「该有却没有」**。
前者是正常数据，后者才是提取链路出了问题 —— 报告把两者混成同一个数字，
于是每次跑都会看到一条不该存在的警告。

### 警报 2：「没有任何节点达到 GRAPH_HUB_DEGREE=8 —— hub 保护从未生效」

实测现状：`work` 只有 2 个 Entity（项目A、项目B，度数各 1），
Event 最高度数 5。

**但这条警报的措辞把两件事混在一起**：

- 「从未生效」可能意味着**逻辑有 bug**（度数永远算成 0）
- 也可能意味着**数据量不够**（度数真的只有 5）

现有测试证明了策略层是对的（`forget_policy` 收到 `graph_degree=8` 会保护），
也证明了 `event_degrees` 在 fake driver 下返回正确 map ——
**但没有一条测过「真实图上算出的度数」**。

fake driver 注入测的是「解析逻辑」，不是「Cypher 写得对不对」。
`event_degrees` 里那句 Cypher 如果漏了某种边、或者 `count(r)` 数错了方向，
fake driver 那几条测试会**全部通过**。

## Design

### D1 空实体：用 `extract_entities` 复算，区分「正常」和「异常」

```
alone  = 卡里没有实体，且 extract_entities(summary) 也为空   → 正常（无可提取实体）
lost   = 卡里没有实体，但 extract_entities(summary) 有        → 异常（写入时丢了）
```

只对 `lost` 报警。`alone` 单独报个数，措辞明确写「本来就无可提取实体」。

**代价**：对每张卡跑一次 `extract_entities`，有性能开销。但这是**离线诊断脚本**，
可以接受；而且数据量大时这个信号（写入丢实体）恰恰最需要被看见。

### D2 hub 保护：报告「差多少」，不是「从没生效」

```
最大度数 5，阈值 8 —— 差 3
```

并且算一个**可达性提示**：当前 namespace 有 N 个 Event、最大度数 D，
如果 D 长期远低于阈值，说明这个硬信号在该数据规模下**结构上不可达**，
而不是「坏了」。

措辞要能让人一眼分清：

- 差得不多（比如 5 vs 8）→ 「快到了，再长长就能触发」
- 差得很远（比如 5 vs 30）→ 「这个阈值在当前规模下不可达，属预期」

### D3 端到端验证：在临时 namespace 里造真图

**这是本 spec 的核心**。三步必须真实：

1. 在 Neo4j 里造一个临时 namespace（`hubtest_<随机>`），
   写入一个 `:Event` 节点 + 8 个邻居 + 8 条边
2. 调**真实的** `event_degrees(ids, ns, settings=...)`（不注入 driver），
   断言返回的度数 **= 8**
3. 把它喂给 `forget_scan`（或 `decide_forget`），断言 bucket = PROTECTED、
   reason = `graph_hub`

第 1 步和第 3 步之间不能有 fake —— **测的就是「Cypher 写得对不对」**。

### D4 清理必须放在 `finally`，且验证清干净

027 的教训：造测试数据的脚本如果不在 `finally` 里清理，
中断一次就留下一堆垃圾 namespace（那次清了 32 个）。

所以 fixture 必须：

- `try/finally` 里 `DETACH DELETE`
- 清理后再查一次，断言 0 残留
- 名字带随机后缀，避免并行跑冲突

### D5 测试要能在 Neo4j 不可用时 skip，不能 fail

CI 里可能没有 Neo4j。用 `pytest.importorskip` 或显式探测：

- 连不上 → `pytest.skip("Neo4j 不可用")`
- 连上 → 必须真跑，不能默默 skip（否则又变成「未验证分支」）

**风险**：如果 CI 永远连不上，这个测试就永远 skip —— 等于没测。
所以要在 spec 里写明：**本地必须有 Neo4j 才能算验收通过**，
并且跑真实测试时要看得到 "1 passed" 而不是 "1 skipped"。

## Plan

- [x] T1 `check_data_health.py`：空实体区分「正常 / 丢失」
- [x] T2 `check_data_health.py`：hub 报告「差多少」+ 可达性措辞
- [x] T3 新增 `tests/test_hub_end_to_end.py`：临时 namespace 造真图
- [x] T4 断言 `event_degrees` 真实返回度数 = 造出来的度数
- [x] T5 断言端到端 `forget_scan` 会保护该事件
- [x] T6 fixture 在 `finally` 里清理并验证零残留
- [x] T7 跑健康报告确认两条假警报消失
- [x] T8 全量测试

## Test

- [x] D1 「周会/预算」卡被归为「正常空实体」而非警告
- [x] D2 造一张实体本该为空但被清掉的卡 → 报为「丢失」（反例测试）
- [x] D3 合成图度数 8 → `event_degrees` 返回 8
- [x] D4 合成图度数 7 → 不保护（边界）
- [x] D5 端到端 → `forget_scan` 把度数 8 的事件判为 PROTECTED / graph_hub
- [x] D6 测试结束后临时 namespace 零残留
- [x] D7 健康报告不再报「实体名为空」警告
- [x] D8 健康报告说清「最大度数 5，阈值 8，差 3」

## Notes

### 为什么这条值得单独一个 spec

它不改任何业务逻辑 —— 两个警报查下来都是**报告不准**，不是功能坏了。

但「报告不准」本身有代价：**假警报会训练人忽略报告**。
当 `check_data_health.py` 每次跑都报「实体名为空: 1」「hub 从未生效」，
而这两条实际上是正常的，那真正的异常出现时也没人会停下来看。

这和 026/028 是同一个模式的第三次出现：
**一个数字被写下来，但没人验证它说的是不是那个意思。**

- 026：`ENTITY_ALIAS_SIM=0.92` 声称能分开实体，实测不可分
- 028：`COLD_QUERY_P95_MS=800` 从未被测过
- 030：两条健康警告，一条是正常数据，一条是「测了策略没测 Cypher」

### hub 阈值 8 要不要调低

**不调**。理由：

`graph_degree` 是 **Event 的度数**（它连了 Time + Action + Entity + Person）。
典型事件是 4–6 条边。8 意味着「这个事件内容特别丰富、连接特别多」，
删了会破坏图结构 —— 所以该保护。这是**正确的设计意图**。

调低到 4 会让大部分事件都被保护，遗忘机制失效；调到 5 也同理。
**阈值高是特性不是 bug**：硬信号的职责是「宁可几乎不触发，也不能误伤」。

真正要改的是**报告措辞**，让它说「差 3，快到了」而不是「从未生效」。

### 关于「未验证分支」的正确理解

代码有分支不等于分支被验证。这里的现状是：

| 层 | 有没有测 | 测了什么 |
|---|---|---|
| `forget_policy` 拿到 degree=8 | ✅ | 会保护，reason 正确 |
| `event_degrees` 解析 driver 返回 | ✅ | fake driver，测解析 |
| **`event_degrees` 的 Cypher 在真图上** | ❌ | **本 spec 补这个** |
| **端到端：造图 → 算度 → 保护** | ❌ | **本 spec 补这个** |

前三行是「零件合格」，第四行是「装起来能跑」。零件全合格但装不起来的东西，
项目里已经有过一次了（026 的向量阈值：单看每个 pair 都有理，合起来不可分）。

## 实现记录

### 两条警报的真相

| 原报告 | 真相 | 性质 |
|---|---|---|
| `实体名为空: 1` | 「周会上讨论了明年的预算分配方案」——`extract_entities` 实跑返回 `[]` | **正常数据**，报告不该报 |
| `没有任何节点达到 GRAPH_HUB_DEGREE=8 —— hub 保护从未生效` | 逻辑正确，最大度数 5 只是数据不够 | **测了策略没测 Cypher** |

两条都不是功能坏了，是**报告不准**。但报告不准有代价：假警报会训练人忽略
报告。当它每次跑都报这两条、而两条其实都正常，真正的异常出现时也没人看。

这是 026/028 同一模式的第三次：**一个数字被写下来，没人验证它说的是不是那个意思。**

### hub 的端到端验证：7 条真图测试

在临时 namespace 里造真图，走**真实 Cypher**（不注入 driver）：

| 测试 | 证明什么 |
|---|---|
| `counts_real_edges` | 造 8 条边 → 算出 8。**核心**：验 Cypher 本身 |
| `counts_both_directions` | 8 出边 + 3 入边 = 11，没写成 `(e)-->(n)` |
| `is_namespace_scoped` | 别的 namespace 的节点查不到 |
| `missing_id_absent` | 查不到的 id 不出现在字典里（调用方按 0） |
| `hub_protection_end_to_end` | 造图 → 算度 → `classify` 判 PROTECTED / graph_hub |
| `boundary_seven_not_protected` | 7 条边不保护，边界二值可判 |
| `cleanup_leaves_nothing` | 清理后零残留 |

跑完确认图里 `hubtest` 残留 0、namespace 只有 `work`、节点 15 个。

**结论：hub 保护的 Cypher 和整条链路都是对的。** 没触发纯粹是数据量。

### meta-test 挂了三次，三次都是我的测试写错

**第一次**：`test_event_degrees_is_namespace_scoped` 用**同名 event_id**
在两个 namespace 造节点，然后断言查到的度数 == 8。

问题：不加过滤时 Cypher 返回**两行同一个 event_id**，字典推导式后行覆盖前行，
结果取决于 Neo4j 遍历顺序 —— 测试时过时不过。注入「去掉 namespace 过滤」
时它照样通过。

改法：换成**不同 event_id**，查一个只存在于别的 namespace 的 id，
过滤生效时它根本不该出现在字典里。判定变成干净的「有 / 没有」。

**第二次**：注入后测试还是过 —— 因为 `.pyc` 缓存。改文件太快时 `.py` 的
mtime 可能和旧 `.pyc` 记录的一致，Python 用了旧字节码，**等于注入了但没生效**。
meta-test 自己加上 `_purge_pycache()` 和「注入后读回验证」才可靠。

**第三次**：`assert old not in injected` 失败 —— 因为
`MATCH (e:Event {namespace: $namespace, event_id: eid})` 这行在
`graph_search.py` 里出现 **2 次**（L408 `_NEIGHBOR_CYPHER`、L509
`_DEGREE_CYPHER`），`replace(..., 1)` 只换第一处，等于没改到度数用的那条。

改法：锚点用**整块 Cypher**（含 `UNWIND` 和 `RETURN`），保证唯一。

**这三条叠起来是个提醒**：验证工具本身也会写错，而且错得比被测代码更隐蔽 ——
被测代码错了测试会红，验证工具错了测试会**绿**。

### 健康报告现在这么说话

```
实体名为空: 1
  其中 1 张本来就无可提取实体（正常）

hub 保护：最大度数 5，阈值 8，差 3
  → 快到了，数据再长一点就能触发
```

判断依据：

- **空实体分两类**：用同一条 `extract_entities` 复算（写入和查询共用它是设计意图）
  - `alone`：复算也为空 → 正常，只报数
  - `lost`：复算有但卡里没有 → 写入链路丢了，才报警
- **hub 报差值**，并按 gap 给不同措辞
  - `gap <= 3` → 「快到了」
  - `gap > 3` → 「当前规模下结构上不可达，属预期」+ 指明端到端已验证

### hub 阈值 8 没调，因为它是特性不是 bug

`graph_degree` 是 **Event 的度数**（连了 Time / Action / Entity / Person）。
典型事件 4–6 条边。8 意味着「这个事件内容特别丰富、连接特别多」，
删了会破坏图结构 —— 该保护。

调低到 4 会让大部分事件都被保护，遗忘机制失效。
**硬信号的职责是「宁可几乎不触发，也不能误伤」。**

### 一个副产物：确认 027 的清理没反弹

跑完测试查图：`namespace` 只有 `work` 一个，共 15 个节点，
`hubtest` 残留 0。说明 027 清净的状态保持住了，而且 030 的临时 namespace
fixture 确实做到了「用完即删」。
