# ADR 34 条验收 · 覆盖审计

> 生成日期：2026-11-03 · 审计对象：`docs/ADR-001-memory-architecture.md` 第 5 节 A1–A34

## 一句话结论

34 条里 **14 条已覆盖、11 条部分覆盖、8 条是功能缺口、1 条属 P2**。

**最重要的发现**：`001` 里我把 M6 记成「30 条脚本没写」，实际上
**8 条缺口里有 7 条是服务或校验压根没实现**（不是没写测试）。
补测试解决不了它们 —— 得先做功能。

## 分类标准

| 标记 | 含义 | 谁来补 |
|---|---|---|
| ✅ | 功能在，且有断言对得上 | 无需动作 |
| 🟡 | 功能在，但断言不完整（缺边界/缺遍历/缺交叉） | 补测试即可 |
| 🔴 | 依赖的服务/校验/脚本**不存在** | 先做功能，不是写测试 |
| ⚪ | 明确属 P2，P0 不做 | 无需动作 |

## 全表

| A | 判定点（缩写） | 状态 | 证据 / 缺口 |
|---|---|---|---|
| A1 | recall result 过 JSON Schema，缺字段即失败 | 🟡 | `test_event_card.py::test_required_parity` 对拍的是 EventCard schema，不是 tool result；`test_recall.py::test_returns_materials_with_trace` 只验字段存在 |
| A2 | 时间区间与查询区间**有交集**（30 条夹具，含 `date_end=null`） | 🟡 | `test_time_resolver.py`（14 条）+ `test_recall.py::test_time_hint_parsed_from_query` 测了**解析**，没测**交集** |
| A3 | kill Neo4j → 正常返回 + `degraded=true` + **≤1200ms** | 🟡 | `test_recall.py::test_graph_failure_degrades_but_returns_results` ✅；**耗时断言缺失** |
| A4 | `ARCHIVE_WINDOW_DAYS=90` 与 `=365` 的冷热集划分不同 | 🔴 | `test_admin_http.py` 测了 archive 不搬，但没有「换窗口值→划分变化」的用例；`ARCHIVE_WINDOW_DAYS` 只在 config 与 archive 里读到 |
| A5 | 每条 material 的**每个** `raw_quote_ids` 都 200 且 `text` 非空 | 🟡 | `test_raw_quote.py::test_get_raw_returns_text_and_event_ids` 测单条；缺遍历 |
| A6 | 同实体两版本事实返回 **2 条**、各带时间戳、不合并 | 🔴 | 无对应用例；`test_recall.py` 无冲突场景 |
| A7 | 两次 `remember` 相同内容 → 库内 1 条；管理侧同 `dedupe_key` 重放同样幂等 | ✅ | `test_event_id.py::test_same_input_same_id`、`test_remember.py::test_second_write_is_idempotent`、`test_raw_quote.py::test_ingest_dedupe_returns_same_job` |
| A8 | 清空 Neo4j 后从事件卡重建，`Event` 覆盖 100%，`weaviate_id` 双向可解析 | 🔴 | **无重建脚本**（`harness/` 里没有 rebuild），也无测试 |
| A9 | 扫描前后 EventCard 与 `raw_quote` 条数**与内容哈希**完全一致 | 🟡 | `test_forget.py` 测了软删幂等与不物理删；**缺哈希对比** |
| A10 | 5 组夹具逐一命中**五类**硬信号，均不入 `SAFE_TO_DELETE` | 🟡 | `test_forget_policy.py` 只覆盖 `pinned` / `unresolved` / `permanent_retention` 三类 |
| A11 | 创建 ≤ `FORGET_GRACE_DAYS` 且 `recall_count=0` → 不入任何删除桶 | ✅ | `test_forget_policy.py::test_new_card_never_enters_delete_bucket`、`test_grace_expires_after_threshold` |
| A12 | `dry_run` 抽检 100 条误删率 <5% 才可开自动；`dry_run=false` → **400** | 🟡 | `test_forget_policy.py::test_sweep_refuses_non_dry_run` ✅ 400；**误删率计算与抽检缺失** |
| A13 | 仅进候选池、未进 top-K 的片段 `recall_count` 不加 | ✅ | `test_recall_count.py::test_skips_when_score_below_threshold`、`test_record_many_counts_only_passing` |
| A14 | 清空向量后重建，20 条固定 query 的 top-K `event_id` 覆盖 100% | 🔴 | **无重建脚本**，无测试 |
| A15 | consolidation 不污染事实源 | ⚪ | `consolidation` 在源码里**只有 config 注释（P2）**，无实现 |
| A16 | `chunk` 数 > `CHUNK_KEEP_PER_EVENT` 时只保留 rerank 前 N | 🔴 | `CHUNK_KEEP_PER_EVENT` **只在 config 定义、从未被使用**；无 chunk 层 |
| A17 | 响应含 `trace.top_k_decision`，`k` 落在 [`MIN`,`MAX`] 或 `overflow` | ✅ | `test_dynamic_k.py`（23 条，含 `test_clamp_max` / `test_clamp_min_is_reported_but_does_not_pad` / `test_trace_is_json_friendly`） |
| A18 | 40 条 `pinned` → `k > MAX` 且 `overflow=true`，pinned 全在结果里 | ✅ | `test_dynamic_k.py::test_forced_overflow_is_reported`、`test_recall.py::test_pinned_survives_truncation`（逻辑覆盖，未造 40 条夹具） |
| A19 | `score < RECALL_COUNT_MIN_SCORE` 的 material 不计数 | ✅ | `test_recall_count.py::test_skips_when_score_below_threshold` |
| A20 | 无相关记忆 → 200 + `materials=[]` + `empty_reason` 非空 | ✅ | `test_recall.py::test_no_candidate_is_explicit`、`test_below_threshold_is_explicit` |
| A21 | LLM 输出词表外标签 → 写入失败或转候选池，`types` 不含未审核标签 | 🟡 | `test_dual_run.py::test_disagreement_enqueues` 覆盖「不一致」；**缺「词表外」专门用例** |
| A22 | 双跑一致直接落库；不一致进 `LABEL_REVIEW` 且 `types` 暂不写 | ✅ | `test_dual_run.py::test_agreement_does_not_enqueue`、`test_disagreement_enqueues`、`test_written_card_uses_rule_types` |
| A23 | 词表变更后 `vocab_version` 递增，可按版本回溯 | 🔴 | `vocab_version` 在 `src/` 里**零命中**，接口 `GET /label/vocab` 不存在 |
| A24 | `LABEL_REVIEW` 与 `NEED_REVIEW` 互不串流 | 🟡 | `test_label_review.py::test_namespace_isolation` 只隔离了 namespace；**缺「标签不改遗忘桶、遗忘不改标签」的交叉断言** |
| A25 | 换 `EMBEDDING_MODEL` 后向旧索引写入 → **错误**，索引未被污染 | 🔴 | 无模型版本校验；`EMBEDDING_MODEL` 只用于请求体 |
| A26 | embedding 不可用 → 写入 **503**，不产生无向量卡 | ✅ | `test_remember.py::test_embedding_failure_propagates`、`test_recall.py::test_embedding_failure_is_503` |
| A27 | kill rerank → 200 + `rerank_skipped=true` + `rerank_ms=0` + 材料非空 | 🔴 | **rerank 服务根本不存在**（`rerank` 只在 config 与注释里出现），用 RRF 合并代替 |
| A28 | 3 年前高相关 vs 昨天低相关 → 前者 `final` 更高或至少入选 | 🟡 | `test_scoring.py` 测了权重与半衰期；**缺这条具体对比场景** |
| A29 | `feedback` 报 `used_material_ids` → `confirmed_by_use` +1；重复计入 `deduped` | ✅ | `test_feedback.py::test_feedback_accepts_returned_ids`、`test_duplicate_in_same_request_counted_once`、`test_absolute_value_not_increment` |
| A30 | Jaccard=0.85 落库；=0.75 进 `LABEL_REVIEW` 且 `types_pending=true` | ✅ | `test_dual_run.py::test_jaccard_threshold_is_configurable`、`test_agreement_does_not_enqueue` |
| A31 | `pinned=true` + `pinned_source=user_directive`，且 `GET /admin/review` 不出现 | 🟡 | `test_remember.py::test_pinned_is_recorded_after_success` ✅；**缺「review 队列不出现」** |
| A32 | `memory_forget` 后 `recall` 检索不到，但 `GET /raw/{id}` 仍 200 | ✅ | `test_raw_quote.py::test_get_raw_survives_soft_delete` |
| A33 | 24h 内第 21 次「记住」不再 pin，降级 `explicit_important` + `directive_abuse` | ✅ | `test_guards.py::test_21st_remember_is_downgraded`、`test_within_limit_still_pins` |
| A34 | namespace=A 检索不到 B；入参带 `namespace` 被拒 | ✅ | `test_mcp_stdio.py::test_namespace_argument_rejected`、`test_missing_namespace_exits_nonzero`、`test_invalid_namespace_blocks_start` |

## 统计

| 状态 | 条数 | 编号 |
|---|---|---|
| ✅ 已覆盖 | **14** | A7 A11 A13 A17 A18 A19 A20 A22 A26 A29 A30 A32 A33 A34 |
| 🟡 部分 | **11** | A1 A2 A3 A5 A9 A10 A12 A21 A24 A28 A31 |
| 🔴 功能缺口 | **8** | A4 A6 A8 A14 A16 A23 A25 A27 |
| ⚪ P2 | **1** | A15 |

## 🔴 那 8 条分别缺什么

| A | 缺的东西 | 性质 |
|---|---|---|
| A4 | 窗口值变化 → 冷热集变化的验证 | 功能在（`ARCHIVE_WINDOW_DAYS` 被读到），缺测试 |
| A6 | 冲突不合并的产物 | **功能**：召回层没有「同实体两版本」的处理 |
| A8 | 图重建脚本 + 覆盖率校验 | **工具**：`harness/` 无 rebuild |
| A14 | 派生层重建脚本 | **工具**：同上 |
| A16 | chunk 层 + `CHUNK_KEEP_PER_EVENT` 的实际使用 | **功能**：config 有默认值，零引用 |
| A23 | `vocab_version` + `GET /label/vocab` | **功能**：零实现 |
| A25 | embedding 模型版本校验 | **功能**：零实现 |
| A27 | rerank 服务与降级路径 | **功能**：服务不存在，用 RRF 代替 |

**A25 和 A27 最值得注意**：它们不是「没做」，是「设计里说了但实现走了另一条路」
（RRF 代替 rerank）。这类差异如果不写下来，下次读 ADR 的人会以为 rerank 在跑。

## 建议的下一步（按性价比排序）

1. **补 11 条 🟡**（纯加断言，不动功能）—— 一轮可完成
2. **A4 / A8 / A14** —— 补工具与测试，不动线上逻辑 —— 一轮可完成
3. **A6 / A16 / A23 / A25 / A27** —— 要么实现功能，要么**改 ADR 承认现状**。
   这五条需要单独决策：做功能，还是把 ADR 的承诺改成「P2 / 改用 RRF」。
