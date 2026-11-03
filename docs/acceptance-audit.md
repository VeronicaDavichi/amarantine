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
| 🟡 | 功能在，但断言不完整（缺边界/缺遍历/缺交叉） —— 本次已清零 | 补测试即可 |
| 🔴 | 依赖的服务/校验/脚本**不存在** | 先做功能，不是写测试 |
| ⚪ | 明确属 P2，P0 不做 | 无需动作 |

## 全表

| A | 判定点（缩写） | 状态 | 证据 / 缺口 |
|---|---|---|---|
| A1 | recall result 过 JSON Schema，缺字段即失败 | ✅ | `test_acceptance_gaps.py::test_a1_materials_carry_contract_fields`（字段完备 + 可 JSON 序列化） |
| A2 | 时间区间与查询区间**有交集**（30 条夹具，含 `date_end=null`） | ✅ | `test_acceptance_gaps.py::test_a2_query_date_range_is_applied_to_search`（区间真的传到检索层） |
| A3 | kill Neo4j → 正常返回 + `degraded=true` + **≤1200ms** | ✅ | `test_recall.py::test_graph_failure_degrades_but_returns_results` + `test_acceptance_gaps.py::test_a3_graph_failure_degrades_within_budget`（≤1200ms） |
| A4 | `ARCHIVE_WINDOW_DAYS=90` 与 `=365` 的冷热集划分不同 | 🔴 | `test_admin_http.py` 测了 archive 不搬，但没有「换窗口值→划分变化」的用例；`ARCHIVE_WINDOW_DAYS` 只在 config 与 archive 里读到 |
| A5 | 每条 material 的**每个** `raw_quote_ids` 都 200 且 `text` 非空 | ✅ | `test_acceptance_gaps.py::test_a5_every_material_quote_is_resolvable`（遍历每个 turn 的 quote 逐个溯源） |
| A6 | 同实体两版本事实返回 **2 条**、各带时间戳、不合并 | 🔴 | 无对应用例；`test_recall.py` 无冲突场景 |
| A7 | 两次 `remember` 相同内容 → 库内 1 条；管理侧同 `dedupe_key` 重放同样幂等 | ✅ | `test_event_id.py::test_same_input_same_id`、`test_remember.py::test_second_write_is_idempotent`、`test_raw_quote.py::test_ingest_dedupe_returns_same_job` |
| A8 | 清空 Neo4j 后从事件卡重建，`Event` 覆盖 100%，`weaviate_id` 双向可解析 | 🔴 | **无重建脚本**（`harness/` 里没有 rebuild），也无测试 |
| A9 | 扫描前后 EventCard 与 `raw_quote` 条数**与内容哈希**完全一致 | ✅ | `test_acceptance_gaps.py::test_a9_sweep_never_mutates_facts`（扫描前后事实指纹逐位比对） |
| A10 | 5 组夹具逐一命中**五类**硬信号，均不入 `SAFE_TO_DELETE` | 🔴 | `classify()` 只实现 3 类（`pinned` / `permanent_retention` / `unresolved`）；**「图结构位置」与 `confirmed_by_use` 连 `ForgetCandidate` 字段都没有**。`GRAPH_HUB_DEGREE` / `CONFIRMED_MIN` 在 config 有默认值，但 `forget_policy` 零引用 |
| A11 | 创建 ≤ `FORGET_GRACE_DAYS` 且 `recall_count=0` → 不入任何删除桶 | ✅ | `test_forget_policy.py::test_new_card_never_enters_delete_bucket`、`test_grace_expires_after_threshold` |
| A12 | `dry_run` 抽检 100 条误删率 <5% 才可开自动；`dry_run=false` → **400** | ✅ | `test_forget_policy.py::test_sweep_refuses_non_dry_run` + `test_acceptance_gaps.py::test_a12_sweep_exposes_per_item_evidence_for_audit`（逐条 event_id + reason 可供抽检） |
| A13 | 仅进候选池、未进 top-K 的片段 `recall_count` 不加 | ✅ | `test_recall_count.py::test_skips_when_score_below_threshold`、`test_record_many_counts_only_passing` |
| A14 | 清空向量后重建，20 条固定 query 的 top-K `event_id` 覆盖 100% | 🔴 | **无重建脚本**，无测试 |
| A15 | consolidation 不污染事实源 | ⚪ | `consolidation` 在源码里**只有 config 注释（P2）**，无实现 |
| A16 | `chunk` 数 > `CHUNK_KEEP_PER_EVENT` 时只保留 rerank 前 N | 🔴 | `CHUNK_KEEP_PER_EVENT` **只在 config 定义、从未被使用**；无 chunk 层 |
| A17 | 响应含 `trace.top_k_decision`，`k` 落在 [`MIN`,`MAX`] 或 `overflow` | ✅ | `test_dynamic_k.py`（23 条，含 `test_clamp_max` / `test_clamp_min_is_reported_but_does_not_pad` / `test_trace_is_json_friendly`） |
| A18 | 40 条 `pinned` → `k > MAX` 且 `overflow=true`，pinned 全在结果里 | ✅ | `test_dynamic_k.py::test_forced_overflow_is_reported`、`test_recall.py::test_pinned_survives_truncation`（逻辑覆盖，未造 40 条夹具） |
| A19 | `score < RECALL_COUNT_MIN_SCORE` 的 material 不计数 | ✅ | `test_recall_count.py::test_skips_when_score_below_threshold` |
| A20 | 无相关记忆 → 200 + `materials=[]` + `empty_reason` 非空 | ✅ | `test_recall.py::test_no_candidate_is_explicit`、`test_below_threshold_is_explicit` |
| A21 | LLM 输出词表外标签 → 写入失败或转候选池，`types` 不含未审核标签 | ✅ | `test_acceptance_gaps.py::test_a21_llm_cannot_emit_types_outside_vocab`（白名单过滤）+ `test_a21_llm_returns_only_illegal_types_falls_back` |
| A22 | 双跑一致直接落库；不一致进 `LABEL_REVIEW` 且 `types` 暂不写 | ✅ | `test_dual_run.py::test_agreement_does_not_enqueue`、`test_disagreement_enqueues`、`test_written_card_uses_rule_types` |
| A23 | 词表变更后 `vocab_version` 递增，可按版本回溯 | 🔴 | `vocab_version` 在 `src/` 里**零命中**，接口 `GET /label/vocab` 不存在 |
| A24 | `LABEL_REVIEW` 与 `NEED_REVIEW` 互不串流 | ✅ | `test_admin_http.py::test_label_review_two_queues_are_separate`（两个方向都断言）+ `test_review_invalid_bucket_rejected` |
| A25 | 换 `EMBEDDING_MODEL` 后向旧索引写入 → **错误**，索引未被污染 | 🔴 | 无模型版本校验；`EMBEDDING_MODEL` 只用于请求体 |
| A26 | embedding 不可用 → 写入 **503**，不产生无向量卡 | ✅ | `test_remember.py::test_embedding_failure_propagates`、`test_recall.py::test_embedding_failure_is_503` |
| A27 | kill rerank → 200 + `rerank_skipped=true` + `rerank_ms=0` + 材料非空 | 🔴 | **rerank 服务根本不存在**（`rerank` 只在 config 与注释里出现），用 RRF 合并代替 |
| A28 | 3 年前高相关 vs 昨天低相关 → 前者 `final` 更高或至少入选 | ✅ | `test_scoring.py` 权重与半衰期 + `test_acceptance_gaps.py::test_a28_recency_does_not_dominate_relevance`（3 年前高相关压过昨天低相关） |
| A29 | `feedback` 报 `used_material_ids` → `confirmed_by_use` +1；重复计入 `deduped` | ✅ | `test_feedback.py::test_feedback_accepts_returned_ids`、`test_duplicate_in_same_request_counted_once`、`test_absolute_value_not_increment` |
| A30 | Jaccard=0.85 落库；=0.75 进 `LABEL_REVIEW` 且 `types_pending=true` | ✅ | `test_dual_run.py::test_jaccard_threshold_is_configurable`、`test_agreement_does_not_enqueue` |
| A31 | `pinned=true` + `pinned_source=user_directive`，且 `GET /admin/review` 不出现 | ✅ | `test_admin_http.py::test_review_need_review_bucket`（NEED_REVIEW 只有 evt_imp）+ `test_review_protected_bucket`（evt_pin 在 PROTECTED） |
| A32 | `memory_forget` 后 `recall` 检索不到，但 `GET /raw/{id}` 仍 200 | ✅ | `test_raw_quote.py::test_get_raw_survives_soft_delete` |
| A33 | 24h 内第 21 次「记住」不再 pin，降级 `explicit_important` + `directive_abuse` | ✅ | `test_guards.py::test_21st_remember_is_downgraded`、`test_within_limit_still_pins` |
| A34 | namespace=A 检索不到 B；入参带 `namespace` 被拒 | ✅ | `test_mcp_stdio.py::test_namespace_argument_rejected`、`test_missing_namespace_exits_nonzero`、`test_invalid_namespace_blocks_start` |

## 统计

| 状态 | 条数 | 编号 |
|---|---|---|
| ✅ 已覆盖 | **24** | A1 A2 A3 A5 A7 A9 A11 A12 A13 A17 A18 A19 A20 A21 A22 A24 A26 A28 A29 A30 A31 A32 A33 A34 |
| 🟡 部分 | **0** | （10 条已补，见下方「2026-11-03 补齐」） |
| 🔴 功能缺口 | **9** | A4 A6 A8 A10 A14 A16 A23 A25 A27 |
| ⚪ P2 | **1** | A15 |

> **勘误**：初版把 A10 记为 🟡。复核 `classify()` 后发现五类硬信号只实现了
> 三类，「图结构位置」（`GRAPH_HUB_DEGREE`）与「被引用次数」
> （`CONFIRMED_MIN`）在 config 里有默认值、判定逻辑里零引用 ——
> 这是功能缺口，不是测试缺口。**先照抄了测试文件的存在，没核实实现**。

### 2026-11-03 补齐记录

初版 11 条 🟡 的实际处理：

| 处理 | 条数 | 编号 | 说明 |
|---|---|---|---|
| 补测试 | 10 | A1 A2 A3 A5 A9 A12 A21 A24 A28 A31 | 落在 `tests/test_acceptance_gaps.py`（10 条全绿）；其中 **A24/A31 复核后发现原本就有测试**，是初版看漏 |
| 降级 🔴 | 1 | A10 | `classify()` 只实现 3 类硬信号 |

**初版审计的教训**：按「测试文件里有没有相关名字」推测，既没读断言也没核对实现。
所以出现双向错误——把已有的（A24/A31）记成缺，把没实现的（A10）记成有。
**审计必须读断言，不能读文件名。**

## 🔴 那 9 条分别缺什么

| A | 缺的东西 | 性质 |
|---|---|---|
| A4 | 窗口值变化 → 冷热集变化的验证 | 功能在（`ARCHIVE_WINDOW_DAYS` 被读到），缺测试 |
| A6 | 冲突不合并的产物 | **功能**：召回层没有「同实体两版本」的处理 |
| A8 | 图重建脚本 + 覆盖率校验 | **工具**：`harness/` 无 rebuild |
| A10 | 两类硬信号（图结构位置、`confirmed_by_use`）的判定 | **功能**：config 有 `GRAPH_HUB_DEGREE` / `CONFIRMED_MIN`，`forget_policy` 零引用，`ForgetCandidate` 连字段都没有 |
| A14 | 派生层重建脚本 | **工具**：同上 |
| A16 | chunk 层 + `CHUNK_KEEP_PER_EVENT` 的实际使用 | **功能**：config 有默认值，零引用 |
| A23 | `vocab_version` + `GET /label/vocab` | **功能**：零实现 |
| A25 | embedding 模型版本校验 | **功能**：零实现 |
| A27 | rerank 服务与降级路径 | **功能**：服务不存在，用 RRF 代替 |

**A10 / A25 / A27 有个共同模式**：config 里躺着默认值，判定或服务不存在。
配置项在、逻辑不在 —— 这是最容易骗过审查的一种「假实现」。

**A25 和 A27 最值得注意**：它们不是「没做」，是「设计里说了但实现走了另一条路」
（RRF 代替 rerank）。这类差异如果不写下来，下次读 ADR 的人会以为 rerank 在跑。

## 建议的下一步（按性价比排序）

1. ~~补 10 条 🟡~~ —— **已完成**（`test_acceptance_gaps.py`，10 passed）
2. **A4 / A8 / A14** —— 补工具与测试，不动线上逻辑 —— 待做
3. **A6 / A10 / A16 / A23 / A25 / A27** —— 要么实现功能，要么**改 ADR 承认现状**。
   这六条需要单独决策：做功能，还是把 ADR 的承诺改成「P2 / 改用 RRF」。
   用户已决定：**走实现功能的路线**。
