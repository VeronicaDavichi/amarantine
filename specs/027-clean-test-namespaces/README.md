---
status: complete
created: 2026-11-06
priority: high
tags:
- data
- cleanup
- harness
created_at: 2026-11-06T02:15:41.436706098Z
updated_at: 2026-11-06T02:18:39.377677942Z
completed_at: 2026-11-06T02:18:39.377677942Z
transitions:
- status: complete
  at: 2026-11-06T02:18:39.377677942Z
---
# 027 清理残留测试 namespace

## Overview

跑完 026 顺手盘点数据发现：**测试残留已经超过真实数据**。

| 存储 | 总量 | `work`（真实） | 垃圾 | 垃圾占比 |
|---|---|---|---|---|
| Neo4j 节点 | 173 | 15 | **158** | **91%** |
| Weaviate `MemoryEvent` | ~200 | 56 | **~144** | **~72%** |
| Weaviate namespace 数 | 30 | 1 | **29** | — |
| SQLite `alias_reject` | 8 | 0 | **8** | 100% |
| SQLite `surname_decl` | 2 | 0 | **2** | 100% |

### 具体是哪 29 个 namespace

```
aliasent  aliasent2  aliasllm  aliasv2  aliasverify  action_verify
backfillcheck  chunke2e  chunke2e58377  closure68753  closure68807
editdemo  event0d8fcf  extract_verify  graph_verify  graphroute
intentrecovery  intentrecovery607690  intentrecovery607998  jieba_verify
markerfix611095  markerfix611130  markerfix611147  markerfix611279
raw73c6a7  renfix  rejectfix67449  rejectfix67470  resolve_verify
scan61c22a  ui440d33  verify
```

三类来源：
- **手工验证脚本**：`aliasverify`、`backfillcheck`、`aliasedit` 系
- **测试框架生成**（带随机数后缀）：`markerfix611095`、`rejectfix67449`、`closure68807`
- **早期 e2e**：`chunke2e`、`intentrecovery`

### 为什么必须清

1. **污染后续测量**：026 的「真实数据验证」在 66 对实体名里跑，
   其中 37 对来自垃圾 namespace —— 结论虽然没被推翻，但样本是脏的
2. **影响 DO/DOUBLE 判断**：`check_data_health.py` 会把垃圾算进总量
3. **将来 028 测延迟**：冷查询要扫全库，垃圾会摊薄结果

## Design

### D1 默认 dry-run，`--apply` 才真删

清的是**真实数据库**，删错不可逆。

```
python ../harness/purge_namespaces.py            # 只报告
python ../harness/purge_namespaces.py --apply    # 真删
```

dry-run 必须输出每条要删的记录（存储 / namespace / 条数），
让人能在 `--apply` 前逐行确认。

### D2 保护列表写死在代码里，不从配置读

保护 `work`。**`personal` 默认也保护** —— 它在
`embedding_index_meta` 里有 1 条记录，虽然看起来也是残留
（没有任何实体/事件/向量），但名字不像测试生成的。

宁可少删一个空的，不要删掉一个可能有用途的。

`--include-protected` 才能删保护列表里的（额外一道闸）。

### D3 删除顺序与原子性

三处存储没有跨库事务（ADR 已记录这个已知限制）。顺序：

1. **Neo4j**：`MATCH (n) WHERE n.namespace = $ns DETACH DELETE n`
2. **Weaviate**：按 `namespace` 属性批量删
3. **SQLite**：按 `namespace` 删 `alias_reject` / `surname_decl` /
   `embedding_index_meta` / `recall_trace` / `recall_feedback` /
   `directive_usage`

**先图后向量**：图和向量都可从事件卡重建（`rebuild_graph.py` /
`rebuild_vectors.py`），删错能救；SQLite 里那几张是唯一数据，放最后。

单步失败不中断 —— 清理是幂等操作，重跑即可。但每步要单独记日志，
失败的要**最后汇总报错并以退出码 1 结束**（否则「没清干净」会被当成成功）。

### D4 为什么不用现成的 `purge_test_data.py`

那个脚本清的是 **`work` 内部**的脏卡（021 的产物），
和「清整个 namespace」是两个正交的需求。复用它会把两件事搅在一起，
将来谁要清 work 脏卡时反而要先绕过 namespace 逻辑。

新脚本 `purge_namespaces.py`，职责单一。

### D5 清理后要验证

不能只看「退出码 0」。验证三件事：

1. 非 work 的 namespace **数量归零**
2. `work` 的数据**一条没少**（前后条数对比）
3. `check_data_health.py` 结果正常

第 2 条是关键 ——「删干净了」和「删多了」在退出码上没区别。

## Plan

- [x] T1 `harness/purge_namespaces.py`：三存储 dry-run + apply
- [x] T2 保护列表（`work` / `personal`）+ `--include-protected`
- [x] T3 汇总报告 + 失败时退出码 1
- [x] T4 dry-run 实跑，人工核对清单
- [x] T5 `--apply` 实跑
- [x] T6 验证：垃圾归零 + work 条数不变 + health check 正常
- [x] T7 `GUIDE_CHECK_CRON` 白名单理由改准

## Test

- [x] D1 dry-run 不产生任何删除（前后条数相同）
- [x] D2 `--apply` 后非 work namespace 数量 = 0
- [x] D3 `work` 的条数在清理前后**完全一致**
- [x] D4 保护列表内的 namespace 不被动
- [x] D5 `--include-protected` 才会动保护列表
- [x] D6 某一步失败时退出码为 1（不是 0）
- [x] D7 重跑幂等（第二次 `--apply` 不报错、无变化）

## Notes

### `GUIDE_CHECK_CRON` 的白名单理由要改准

现在写的是「文档一致性校验靠手动跑，没有定时器」——
这个措辞暗示「功能缺失」，实际是**有意不做**。

ADR 4.10 的原话是「用户自行装 crontab」。所以准确理由应该是：
`harness/check_guide.py` 是 repo-root 脚本，定时执行由用户在自己环境配置，
程序内部不该代劳（服务端进程读不到用户 crontab，硬做需要引入调度器依赖）。

这和 `TOKENIZER` 同类：**有意不接，不是遗漏**。措辞不能含糊，
否则下次审计又会把它当「假实现」翻出来。

### 为什么 `personal` 默认保护

它在 `embedding_index_meta` 有 1 行，但**没有任何实体、事件、向量**。

判断：
- 支持「是残留」：内容全空
- 支持「有用途」：名字是语义化的，不像测试生成（测试是 `markerfix611095` 这种）

**证据不足以判定，就保护**。删错了不可逆，留着只占一行。

### 清理前后必须对比 `work` 条数

这是本 spec 最重要的验收点。

「垃圾清完了」和「把真实数据也删了」在退出码上都可能是 0。
只有**显式对比 work 的条数**能区分。

所以脚本要输出：

```
清理前: work 实体 15 / 事件 3 / Weaviate 56 / trace 54
清理后: work 实体 15 / 事件 3 / Weaviate 56 / trace 54   <- 必须一模一样
```

## 实现记录

### 清理结果

三处存储各删一遍，`work` 的数据一条没变：

```
neo4j                  17 个 namespace / 158 条
weaviate:MemoryEvent   30 个 namespace /  96 条
sqlite（5 张表）                            15 条
```

清理后 `剩余非保护 namespace: 0`，`work` 快照前后完全一致：

```
neo4j:Action   5 -> 5    neo4j:Entity  2 -> 2    neo4j:Event   3 -> 3
neo4j:Person   2 -> 2    neo4j:Time    3 -> 3
weaviate:MemoryEvent  56 -> 56
sqlite:recall_feedback 46 -> 46   sqlite:recall_trace 54 -> 54
```

重跑 `--apply` 幂等：`待清理 0 个`，`EXIT=0`。

`check_data_health.py` 现在报「测试残留: 0 (0%)」「实体名异常长: 0」。

### dry-run 抓到了三个 bug，全部在真删之前

第一次 dry-run 输出的「待清理」列表里混着 `alias_reject`、`recall_trace`、
`directive_usage` —— 那是**表名**，不是 namespace。

原因：`neo4j` 的扫描结果 key 是 namespace，而 `sqlite`/`weaviate` 的
key 是**表名/collection 名**，namespace 在 value 里。写成了

```
all_ns = set(neo) | set(lite)          # 错：set(lite) 是表名
```

正确是遍历 value。这一条如果直接 `--apply`，会对着假 namespace 空删一遍
（不会误删，但会把「清理成功」报成「0 个 namespace」而看起来像没事）。

第二次 `--apply` 崩在 `snapshot()`：`sqlite3` 没设 `row_factory` 却用
`r["name"]` 取列。**好在崩在删除之前**，一条都没删。

第三次 `--apply`：Weaviate 删除全失败，两个原因叠加 ——

1. `httpx.delete()` 不接受 `json=` 参数（必须 `httpx.request("DELETE", ...)`）
2. 修掉之后仍然 500：`validate: empty match.class clause`
   —— batch delete 的 body 里 **`match.class` 是必填的**，
   不能「删掉所有 collection 里这个 namespace」，只能按 collection 逐个删

最终按 `collection × namespace` 双循环，`MemoryEvent` 删了 96 条，
`MemoryChunk` 本来就是空的。

**三个 bug 有两个是真删前抓到的（dry-run + 快照崩溃），
第三个在删除中抓到但没有造成损失**（失败只是没删，不是删错）。
这说明「默认 dry-run」和「删除前先快照」这两道闸都在真的起作用，
不是形式主义。

### 为什么 `personal` 最终没删

它在 `embedding_index_meta` 有 1 行，但没有任何实体、事件、向量。

证据：
- 支持「是残留」：内容全空
- 支持「有用途」：名字是语义化的，不像测试生成（测试是 `markerfix611095` 这种）

**证据不足以判定就保护** —— 删错不可逆，留着只占一行。
真要删加 `--include-protected`。

### `GUIDE_CHECK_CRON` 的理由改准了

原来写的是「文档一致性校验靠手动跑，没有定时器」，
这个措辞**暗示功能缺失**，下次审计还会把它当假实现翻出来。

改成：

> 有意不接（不是遗漏）：`harness/check_guide.py` 是 repo-root 脚本，
> ADR 4.10 明确由用户在自己环境装 crontab。服务端进程读不到用户 crontab，
> 硬做需要引入调度器依赖，与「日志只写 LOG_FILE」的部署约定冲突

和 `TOKENIZER` 同类：**有意不接，不是遗漏**。措辞不能含糊。

### 清理前后的意外发现

`check_data_health.py` 报了两个一直存在但从没被看见的事实：

1. **`实体名为空: 1`** —— `work` 里有 1 张实体名为空的卡
   （就是 `evt_afec73caaff061b2` 周会/预算，`entity_names=[]`）
2. **`⚠️ 没有任何节点达到 GRAPH_HUB_DEGREE=8`** —— hub 保护**从未生效过**。
   最大度数 5，而阈值是 8。

第 2 条是这次盘点最有价值的发现：019/022 那批遗忘策略的测试全绿，
但 `GRAPH_HUB_DEGREE` 这条硬信号在真实数据上**一次都没触发过**。
不是代码错，是数据规模不够 —— 但这意味着「hub 保护」在生产环境里
目前是个**未被验证的分支**。

**记录，不修**（需要造足够大的图才能验证，属于另一件事）。

### 清理后 `work` 的真实面貌

```
卡片数: 3（另有 53 张已软删）
图节点: 3 个 Event，度数 {2:1, 5:2}，平均 4.00
边类型: AT 3 / ACTION 3 / MENTIONS 2 / INVOLVES 2
有实体边的事件: 2 / 3
```

之前看到 173 个节点，其中只有 15 个是真的。**91% 是垃圾** ——
这个比例本身就说明「测试数据没人清」已经积累了多久。
