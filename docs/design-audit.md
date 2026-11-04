# 设计审计 · 配置与 ADR 承诺落地情况

> 生成日期：2026-11-04 · 审计对象：`config.py` 全部字段 × `src/` 消费点、ADR 分期与待定项
> 前置文档：`docs/acceptance-audit.md`（验收层 34 条，已全部清零）

## 一句话结论

**ADR 分期内没有遗漏，但配置层有 22 个「配了但没生效」的问题**（已修 4 个，剩 18 个有明确理由）。

其中最危险的一类不是「没实现」，而是**声明的值和实际行为不一致** ——
`AGENT_KEY_HEADER` 写着 `X-Agent-Key` 而实现是 `Authorization: Bearer`，
`GRAPH_TIMEOUT_MS` 写着 200 而代码里超时是另一个常量。
这种「你以为调了，其实没调」比「压根没这个配置」危险得多。

> 口径说明：「22 个」是**本次审计那一刻的存量 + 本次修掉的**。
> 下一节「七起」是跨两轮审计的模式统计，其中前 4 起（A10 / A16 / A27 / A4）
> 在验收审计阶段就修了，**不在**上面 22 的统计里。

## 这份审计与验收审计的区别

| | 验收审计 | 设计审计（本文） |
|---|---|---|
| 问的问题 | 「ADR 写的验收标准能不能过」 | 「ADR 写的配置与承诺，代码里真的兑现了吗」 |
| 手段 | 读测试断言 | 字段 × 消费点交叉扫描 |
| 抓到的典型问题 | 功能没实现 | **配置有默认值，消费它的逻辑不存在** |
| 结果 | 34 条全绿 | 22 个问题 → 已修 4，剩 18 有理由 |

两者互补：验收审计保证「说到的做到」，设计审计保证「能调的真的能调」。

## 方法

一条命令扫全部字段：

```bash
./harness/dev-linux.sh python ../harness/audit_config_usage.py
```

逻辑：读 `config.py` 的顶层字段 → 在 `src/`（排除 `config.py`）里找**代码**中的独立标识符
→ 输出零引用项。

两个细节很重要：

- **注释里出现不算用了**。`TIME_CONF_MIN` 就是活例子：`time_resolver.py` 的注释提到它，
  但代码从不读。检测器专门有 `_strip_comments` 和一条元测试守着。
- **检测器本身要有元测试**。造一个必然无人用的字段名，确认它会报 ——
  否则检测器写错了也是全绿，那才是最大的假实现。

## 结论速览

| 类别 | 数量 | 处理 |
|---|---|---|
| 已修复（行为不一致） | **4** | spec 014（3 个）+ 015（1 个） |
| P2 预留（ADR 明确排后） | 6 | 合理，等 P2 |
| 外部服务适配 | 3 | 当前只支持本地 TEI |
| 未接线的真参数 | 9 | 有价值，待排期 |
| **合计剩零引用** | **18** | 全部在白名单里带理由 |

## 七起「假实现」的完整记录

这是本次审计最有价值的部分。同一个模式反复出现七次。

前 4 起在**验收审计**阶段就修了（`docs/acceptance-audit.md`），
后 3 起 + 文末那个附例是本次设计审计新发现的。

### 1. A10 —— 五类硬信号只实现了三类

`forget_policy.classify()` 只判 `pinned` / `permanent_retention` / `unresolved`。
`GRAPH_HUB_DEGREE` 与 `CONFIRMED_MIN` 在 config 有默认值，判定逻辑里零引用，
`ForgetCandidate` 连字段都没有。

**已修**：spec 008，`test_hard_signals.py` 18 条。

### 2. A16 —— 整个 chunk 层不存在

`CHUNK_KEEP_PER_EVENT = 2` 有默认值，`src/` 零引用。
不是「去重逻辑写错」，是**一条卡只存一个向量，没有 chunk 这个概念**。

**已修**：spec 010，真 e2e 验证 4 块落库 / `chunk_parents=2`。

### 3. A27 —— 五个 `RERANK_*` 全部零引用

`RERANK_MODEL`、`RERANK_BASE_URL`、`RERANK_CANDIDATES`、`RERANK_TIMEOUT_MS`、
`RERANK_ENABLED` 全都有默认值，全都没人读。

**已修**：spec 013。真服务上线后又暴露两个 bug（超时卡边界、rerank 分尺度不同），
见 `specs/013-rerank-degrade/README.md` 的「真服务补充验证」。

### 4. A4 —— `ARCHIVE_WINDOW_DAYS` 改了没有任何影响

`ArchiveRequest.window_days` **硬编码 180**，配置项零消费。
这一条最隐蔽：功能看起来在（归档报告能出），但「换窗口值 → 冷热集变化」这个
验收标准**永远不可能通过**。

**已修**：spec 007，改成默认 `None` 回落 config。

### 5. `RERANK_CANDIDATES` —— 我自己引入的漂移

014 审计时发现：010 里我为了 chunk 检索写了 `limit=100`，
而 `RERANK_CANDIDATES` 是 50。两个数**语义不同**（一个决定 CPU 成本、
一个决定召回广度），但配置项 `RERANK_CANDIDATES` 从头到尾没被读过。

**已修**：spec 014。chunk 的 100 提成命名常量 `CHUNK_SEARCH_LIMIT` 并注释说明两者无关 ——
「硬编码」变成「显式命名常量」后就不再是隐藏的漂移。

### 6. `GRAPH_TIMEOUT_MS` —— 超时写死在代码里

`GRAPH_TIMEOUT_MS = 200` 有默认值，但 `graph_search.py` 里的默认参数是
`timeout_s: float = 0.2` —— 两处独立的值，改配置**不影响**实际超时。

这一条比看起来严重：图通道是 recall 的一个并行分支，超时写死意味着
从 Neo4j 慢查询里救不回来（即使把配置调到 5000 也没用）。

**已修**：spec 014。默认参数改 `None`，函数体内读 `settings.GRAPH_TIMEOUT_MS / 1000`。
显式传参仍可覆盖（测试与特殊调用路径需要）。

### 7. `AGENT_KEY_HEADER` —— 默认值描述了一个不存在的协议

配置写 `X-Agent-Key`，实现是 `Authorization: Bearer`。ADR v0.10 也写的 `X-Agent-Key`。
所以**配置和 ADR 都指向一个从未实现的协议**。

**已修**：spec 014。默认值改为 `Authorization`，ADR D6 同步补修正说明，
`require_token` 改成自己读 `Request.headers`（FastAPI 的 `HTTPBearer` 把 header 名写死，
光改默认值配置仍是装饰品）。

### 附：`TIME_CONF_MIN` —— 第 8 个，性质不同

它只在 `time_resolver.py` 的**注释**里被提到，代码从不消费。

**已修**：spec 015。但必须诚实：修完后默认行为**没变** ——
因为 `DateRange.confidence` 只有 `0.0` / `1.0` 两个值，`> 0` 和 `>= 0.5` 等价。
真正缺的是 `time_resolver` 不做置信度分级（「上个月」这种窗口靠猜的表达也给 1.0）。

## 剩余 18 个零引用字段

### P2 预留（6）—— 合理，等 P2

| 字段 | 说明 |
|---|---|
| `CLUSTER_MERGE_SIM` | 主题簇合并阈值，D8 未实现 |
| `CLUSTER_MIN_SIZE` | 主题簇最小规模，D8 未实现 |
| `CONSOLIDATE_MIN_EVENTS` | consolidation 触发条件 |
| `CONSOLIDATE_DUP_RATIO` | consolidation 冗余判据 |
| `CONSOLIDATE_LATENCY_MS` | consolidation 预算 |
| `COLD_QUERY_P95_MS` | 冷查询目标，物理冷热拆分未做（ADR §8 待定） |

### 外部服务适配（3）—— 当前只支持本地无鉴权 TEI

`EMBEDDING_API_KEY`、`EMBEDDING_PROVIDER`、`WEAVIATE_GRPC_PORT`

换商用 embedding 服务时会卡住。不是 bug，是范围限制 —— 但现在没有文档说明这一点。

### 未接线的真参数（9）—— 有价值，待排期

| 字段 | 现状 | 影响 |
|---|---|---|
| `VECTOR_TIMEOUT_MS` | 向量通道超时未接配置（图通道已接） | 不一致：一个通道可配一个不可配 |
| `ENTITY_ALIAS_SIM` | 见 spec 016，要的是整个 C.B3 三级消歧 | 大工程 |
| `LABEL_LLM_TEMP` | LLM 打标温度写死 | 改不了打标稳定性 |
| `LABEL_REVIEW_BATCH` | 复核队列批量大小未接 | 性能调不了 |
| `FORGET_SWEEP_DAYS` | 定期扫描间隔未接，靠手动触发 | 无自动清理 |
| `GUIDE_CHECK_CRON` | 文档一致性校验靠手动跑 | 无定时校验 |
| `DB_BUSY_TIMEOUT_MS` | SQLite busy_timeout 写死 | 高并发下无法调 |
| `SQLITE_WAL` | WAL 恒定开启，没做成开关 | 无（WAL 本来就该开） |
| `TOKENIZER` | 分词器写死 `cl100k_base` | 换模型时口径不变 |

## ADR 分期内未做的

### P1（部分）

| 项 | 现状 | 为什么合理 |
|---|---|---|
| 物理冷热拆分 | `archive` 只出报告，`mode` 恒 `logical` | ADR §8 自己说「等数据量与延迟数据」 |
| 冷查询 P95 目标 | `COLD_QUERY_P95_MS` 零引用，没测过 | 依赖上一条 |
| rerank **绝对阈值重标** | 做的是**归一化**（相对），不是重标 | 取舍：实测 rerank 分只有 0.057 量级，绝对阈值无意义 |

### P2（全部未做）

| 项 | 现状 |
|---|---|
| D8 consolidation（主题簇 / 阶段摘要） | 5 个配置项零引用，无任何实现 |
| D9 第②层 SetFit 分类器 | 只有规则 + LLM 两层 |
| 自动淘汰开启 | `forget/sweep` 只接受 `dry_run=true`（A12 闸门） |
| A15 | consolidation 不污染事实源 —— 没它所以无从验 |

## ADR 待定项（§8，7 条）

1. P1 是否真的做物理冷热拆分
2. `types` 受控词表初版清单
3. 不可逆事件词表按个人经历校准
4. `recall_count` 的落点（Weaviate 字段 vs 独立计数表）
5. 动态 K 对下游 prompt 稳定性的影响
6. SetFit 三种 `multi_target_strategy` 哪种最优
7. BERTopic 中文参数（UMAP / HDBSCAN）调优

这些都是「需要外部信息或实测定夺」，不是欠的活。

## 工程化缺口（ADR 外）

| 项 | 现状 | 风险 |
|---|---|---|
| admin 后端没 systemd 化 | `setsid nohup` 手动起 | **WSL 一重启就没了**，且 `.env` 改动要重启 |
| `graph_outbox.py` 名不副实 | 叫 outbox 但直接写图，没有队列语义 | 阅读误导；真正的 outbox 语义在 `pending_task` 表 |
| `GET /admin/events` 截断不可见 | 响应里没有 `scanned` / `truncated` | 用户不知道结果被截断过 |

## 机制：如何防止第八次

六次假实现之后，不该再靠人眼。已落地两层：

### 1. 字段级检测（spec 014）

`tests/test_config_usage.py`，9 条测试：

| 测试 | 作用 |
|---|---|
| `test_every_config_field_has_a_consumer` | 不在白名单且无消费点 → 挂 |
| `test_whitelist_entries_are_still_unused` | **白名单防腐**：某字段后来接上了必须删 |
| `test_whitelist_entries_have_reasons` | 白名单每题必写理由 |
| `test_whitelist_entries_exist_in_config` | 拼错会静默失效 |
| `test_detector_actually_detects` | **元测试**：检测器真的会报 |
| `test_comments_do_not_count_as_consumers` | 注释提到 ≠ 用了 |

白名单防腐那条**在 015 里抓住了我自己** —— `TIME_CONF_MIN` 接上后必须从白名单删，
否则全量测试会挂。机制真的在工作，不是靠记性。

### 2. 行为级验证（spec 014）

`tests/test_config_wiring.py`，11 条测试：「改了配置值 → 行为真的变」。
字段检测只能证明「有人在代码里提到了它」，行为验证才能证明「它有影响」。

两者缺一不可：一个字段可以被引用来打日志，但对行为毫无影响。

## 下一步建议（按价值排序）

| 优先 | 项 | 理由 | 规模 |
|---|---|---|---|
| 1 | `time_resolver` 置信度分级 | 让 015 的闸门真正有意义；现在的 `confidence` 是布尔的 | 小 |
| 2 | spec 016 的**中间态**：编辑距离前置检查 | 挡住「张三」vs「张三丰」这类明显误并，成本极低 | 小 |
| 3 | `VECTOR_TIMEOUT_MS` 接线 | 补一个明显的不一致（图通道可配、向量通道不可配） | 极小 |
| 4 | admin systemd 化 | WSL 重启就没了，实际影响可用性 | 小 |
| 5 | D8 consolidation | 唯一有完整设计却完全没做的大块 | **大** |
| 6 | C.B3 完整三级消歧 | 需要新的实体名 embedding 子系统 | **大** |

物理冷热拆分建议**暂不做** —— 47 张卡谈冷热没有意义，等数据量。

## 教训

1. **有配置项 ≠ 有功能**。七次里没有一次是「写错了」，全是「压根没写」。
   看代码时如果只看到 `config.py` 有定义就认为功能存在，必然误判。

2. **默认值也是一种文档**。`AGENT_KEY_HEADER = "X-Agent-Key"` 让两个读者
   （人和后来的 AI）都以为存在 `X-Agent-Key` 协议。默认值必须是**真实行为**的描述。

3. **「修漂移」的过程本身会造出新漂移**。014 里我第一版加了
   `GRAPH_TIMEOUT_FALLBACK_S = None` 兜底，写完立刻意识到这正是要消灭的东西。

4. **注释不是消费点**。`TIME_CONF_MIN` 在注释里被提到，看起来「有文档」，
   实际上代码从不读。检测器必须剥注释。

5. **白名单需要反向测试**。只测「白名单外的必须有消费点」不够，
   还要测「白名单内的必须仍然没有」—— 否则白名单会腐烂成历史集合。
