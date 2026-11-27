# 依赖不可用时会怎样（降级行为）

自托管的第一版只有一个进程（`memory.entrypoints.admin_http`）+ 四个外部依赖。
**外部依赖挂掉时，行为必须可预期** —— 这份文档把「谁挂 → 什么坏 → 怎么恢复」写死，
避免排障时靠猜。

> 结论先行：**图库挂了最轻（能自动重建）**，**向量库挂了召回直接 503**，
> **LLM 挂了写入降级成原文卡（记忆变粗糙但不断）**。

## 一览

| 依赖 | 挂掉后的表现 | 是否丢数据 | 恢复方式 |
|---|---|---|---|
| **LLM 网关** | 切分降级成「一条消息一张原文卡」；别名推断、标签复核跳过；关系抽取不产出 | 不丢（原文都在） | 网关恢复后重灌该批（`--retry-failed`） |
| **embedding 服务** | `remember` 失败（写不进向量库）；`recall` 抛 `RecallUnavailableError` → HTTP 503 | 不丢（原文先落 SQLite） | 服务恢复后重跑该 job |
| **Weaviate** | 同上：写入失败、召回 503 | 卡片在 Weaviate 里，**必须备份** | 恢复 + 从备份还原（`harness/restore_state.py`） |
| **rerank 服务** | 召回**照常返回**，只是排序退化为第一阶段分数 | 不丢 | 恢复后自动恢复；`/health` 会显示 `down: ["rerank"]` |
| **Neo4j** | 写图告警但**不影响建卡**；召回缺少图通道（少一路信号） | 不丢（图是派生层） | 重启后重建图：`harness/check_rebuild.py` / 重灌 |

## 逐项细节

### LLM 网关不可用

- **写入**：切分失败 → 降级为「按消息原文建卡」。卡变多但都很短，召回质量下降。
  日志：`ingest 切分失败 …` / `LLM 请求失败`
- **标签**：`labeler` 的标签复核跳过，卡上 `review_verdict` 为空
- **别名/关系**：`resolve_one` 捕获异常返回 None（`LLM 别名解析异常`），
  关系陈述通道不产出边
- **判定依据**：这些路径都是 `except Exception` + 告警，**不让外部依赖拖垮写卡**

### embedding / Weaviate 不可用

- 这两者一个都不能少：没有向量就写不进卡片（A26 的维度校验也在这一层）
- `recall` 抛 `RecallUnavailableError`，HTTP 层映射成 **503**（不是空结果 ——
  空结果会让人以为「确实没有这段记忆」）
- 恢复后：`ingest_job` 里失败的片用 `harness/import_chat.py --retry-failed`

### rerank 不可用 / 不生效

- 召回**不失败**：`rerank_skipped=true` + `rerank_reason`，按第一阶段分数返回
- 影响的是精度不是可用性；`/health` 的 `deps.rerank` 会变红

**但「服务 healthy」不等于「精排在生效」** —— 这两种情况都曾静默持续数周：

| 现象 | 原因 | 处理 |
|---|---|---|
| `rerank_reason=http_413_batch_limit` | 服务端 `--max-client-batch-size` 小于 `RERANK_CANDIDATES` | 把服务端上限提到 ≥ 候选数（compose 里已设 64） |
| `rerank_reason=ReadTimeout` | CPU 上排不完：实测约 **120ms/条 + 130ms 固定** | 见下面的容量表 |

**CPU 上的实测容量**（bge-reranker-v2-m3，单请求）：

| 候选数 | 耗时 | 800ms 够吗 |
|---|---|---|
| 1 | 253ms | ✓ |
| 4 | 572ms | ✓ |
| 8 | 1100ms | ✗ |
| 16 | 2056ms | ✗ |
| 50 | 5659ms | ✗ |

所以 **CPU 上 `RERANK_CANDIDATES=50` + `RERANK_TIMEOUT_MS=800` 永远超时**，
精排不会生效。启动时会打一条 `配置自检:` 告警指出可行条数；`/health` 也暴露实际结果：

```json
{"rerank_enabled": true, "rerank_last_ok": false,
 "rerank_last_reason": "ReadTimeout", "rerank_consecutive_failures": 1}
```

`rerank_consecutive_failures` 一直涨 = 精排从未成功过。

### 原文索引通道不可用（spec 051）

- 原文 BM25 是**第 4 路**信号，挂了只降级：`trace.channels` 里没有 `raw_text`，
  `trace.errors.raw_text` 记原因，其余三路照常返回
- **不会 503**：它不是事实源（事实源是向量库），只是召回率的增强
- 常见原因与处理：

| 现象 | 原因 | 处理 |
|---|---|---|
| `errors.raw_text` 含 `raw_text_tokens` | 老 collection 还没补属性 | 重启一次（`ensure_collection` 自动补），或跑 `harness/reindex_raw_text.py` |
| `/health` 里 `raw_text_missing > 0` | `reindex` 没跑完或新导的卡没索引 | 重跑 `reindex_raw_text.py`（幂等） |
| 材料里 `raw_quotes` 全是 `[]` | 开关关着，或该卡原文全被闸挡掉 | 看 `trace.raw_text_budget_chars`：0 表示预算用尽 |

- **原文取不到时只给摘要**，不编造、不给空串冒充原文（`raw_truncated` 区分「截断」与「没给」）


### 向量重算期间（spec 052）

`harness/reembed_raw.py` 跑到一半时，一部分卡是「原文向量」、一部分还是
「摘要向量」。两种语义空间不可比，**排序会短暂失真**：

| 现象 | 说明 | 处理 |
|---|---|---|
| 召回结果时好时坏 | 新旧向量混合 | 属预期；跑完即恢复 |
| `failed` 持续增长 | embedding 服务不稳 / 单卡文本异常 | 查日志；可中断后 `--resume` 续跑 |
| 重算进程被杀 | WSL 空闲回收 / OOM | 同命令重跑，会自动跳过已完成的 |

**不需要停服务**：向量是覆盖写，中断不会留下坏数据。
最坏情况是一张卡停在旧向量上，重跑就会补上。

### Neo4j 不可用

- 写图在 `graph_outbox`，异常只告警（图是**派生层**，能重建）
- 召回少一路图信号；`trace.channels` 里没有 graph
- 恢复后重建：`harness/check_rebuild.py`（校验）或重灌

## 监控与自查

```bash
# 四项依赖一眼看全（5 秒缓存）
curl -s http://127.0.0.1:8765/health | python3 -m json.tool

# 只看进程活着
curl -s "http://127.0.0.1:8765/health?deps=0"
```

`status` 恒为 `ok` 是**故意的**：它是 liveness（进程活着），
依赖状态看 `degraded` / `down`。否则向量库一挂，容器会被反复重启，
而重启救不了向量库。
