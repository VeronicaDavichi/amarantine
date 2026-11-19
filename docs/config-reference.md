# 配置项参考

由 `harness/gen_config_doc.py` 从 `back_end/config.py` 自动生成，共 **88** 项。

> **注释为空的项表示源码里没写说明** —— 不是这里漏了。
> 修改任何一项前先读对应 spec；标「不要改」的项改动会破坏数据一致性。

## LLM 网关与超时（5 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `None` |  |
| `LLM_BASE_URL` | `'http://198.51.100.58:3000'` |  |
| `LLM_MODEL` | `'deepseek-v4-flash'` |  |
| `LLM_PROVIDER` | `'anthropic'` | : `anthropic`（Messages API）或 `openai`（chat/completions）。；: 本机接的是 hongwang 网关，走 Anthropic 协议。 |
| `LLM_TIMEOUT_MS` | `60000` |  |

## 向量模型（5 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `EMBEDDING_API_KEY` | `None` |  |
| `EMBEDDING_BASE_URL` | `'http://127.0.0.1:8082'` |  |
| `EMBEDDING_DIM` | `1024` |  |
| `EMBEDDING_MODEL` | `'BAAI/bge-m3'` |  |
| `EMBEDDING_PROVIDER` | `'local'` | ---------- 模型供给 ---------- |

## 重排（6 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `RERANK_API_KEY` | `None` | : rerank 服务的 Bearer key。为空时不发 header（本地服务不需要）。；: 混合部署（模型在高配机）时填上，避免任何能连到 8083 的进程都能白嫖算力。 |
| `RERANK_BASE_URL` | `'http://127.0.0.1:8083'` |  |
| `RERANK_CANDIDATES` | `50` |  |
| `RERANK_ENABLED` | `False` |  |
| `RERANK_MODEL` | `'BAAI/bge-reranker-v2-m3'` |  |
| `RERANK_TIMEOUT_MS` | `800` | : CPU 上 bge-reranker-v2-m3 实测热身后 0.22-0.26s、冷启动更久。；: 原来 300ms 卡在边界上，真服务一开就几乎必然 ReadTimeout（等于白开）。；: 800ms 留足余量，且仍在 RECALL_BUDGET_MS=1500 之内。 |

## 向量库（2 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `WEAVIATE_GRPC_PORT` | `50061` | : gRPC 端口与 REST 不同（本机为避开 Dify 占用改成 50061） |
| `WEAVIATE_URL` | `'http://127.0.0.1:8081'` |  |

## 图库（3 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `NEO4J_PASSWORD` | `'password'` |  |
| `NEO4J_URI` | `'bolt://127.0.0.1:7687'` |  |
| `NEO4J_USER` | `'neo4j'` |  |

## 召回（9 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `RECALL_BUDGET_MS` | `1500` | : 整体预算：超了就降级返回 partial，是**运行时保护**不是性能目标。 |
| `RECALL_CANDIDATES` | `50` | : 每路召回的候选条数（ADR：候选每路 top 50，再交给动态 K 截断） |
| `RECALL_COUNT_MIN_SCORE` | `0.5` |  |
| `RECALL_DROP_RATIO` | `0.6` |  |
| `RECALL_EMPTY_THRESHOLD` | `0.35` |  |
| `RECALL_GAP_RATIO` | `0.15` |  |
| `RECALL_TOP_K_MAX` | `30` |  |
| `RECALL_TOP_K_MIN` | `3` |  |
| `RECALL_VECTOR_MIN_SIM` | `0.5` | : 向量通道的绝对余弦闸门。；: **必须在融合前过滤**：RRF 只保留排名，`normalize_relevance` 又把 top-1；: 归一成 1.0，于是无关查询也会拿到接近满分。绝对相似度是唯一还能区分；: 「真相关」和「矮子里拔将军」的信号。；: bge-m3 中文短句实测：相关 0.6–0.8，无关 0.35–0.50，故取 0.5。 |

## 缩写与别名扩展（3 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ABBR_EXPAND_BUDGET_MS` | `800` | : 别名扩展（二次召回）自己的预算。**不能吃主召回的剩余**：；: 实测主召回常把 RECALL_BUDGET_MS 用完，剩余为负 → 扩展永不执行。 |
| `ABBR_EXPAND_ENABLED` | `True` | : 召回时是否用 SAME_SENSE 边做双向扩展（spec 048 T5）。；: 只查本地图，不联网；默认开。 |
| `ABBR_NBNHHHSH_ENABLED` | `False` | : 规则层总开关。关掉 = 完全回退到「只有 ③ LLM」的旧行为。；: 是否允许调用 nbnhhsh 公开 API 猜缩写（spec 048）。；:；: **默认关** —— 它依赖外网，公司网/VPN 断开时会拖慢导入。；: 关掉后只用 `config/abbr_local.json` 的本地词典，功能仍在、只是覆盖面小。 |

## 时间解析（2 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `TIMEZONE` | `'Asia/Shanghai'` | ---------- 时间与排序 ---------- |
| `TIME_CONF_MIN` | `0.5` |  |

## 实体与消歧（5 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ENTITY_ALIAS_SIM` | `0.92` |  |
| `ENTITY_HONORIFICS` | `'总,经理,老师,哥,姐,先生,女士,同学,工'` | : 称呼后缀：剥掉后相等 -> 判同一实体。逗号分隔；空字符串禁用。；: 剥离后长度必须 >= 2，否则「王总 -> 王」会被误判（单字名无法判定）。 |
| `ENTITY_RULE_ENABLED` | `True` |  |
| `ENTITY_SUFFIXES` | `'AI,助手,公司,集团,科技,股份,有限公司,市,省,县,区,镇,部,处,科,院,局'` | : 后缀白名单：短名是长名前缀、且多出的部分在此表内 -> 判同一实体。；: 逗号分隔；**显式设为空字符串表示禁用这条规则**（不是用默认值）。；: 注意「组」故意不在默认值里 —— 项目A/项目A组 是不同实体（实测 0.8801）。 |
| `ENTITY_SYNONYMS` | `'周会=每周例会,例会=每周例会,月会=每月例会,腾讯=Tencent,阿里=Alibaba'` | : 同义词表：`a=b,c=d` 格式，双向生效。空字符串禁用。；: 这是**承认规则能力边界**的做法 —— 周会/每周例会 字面不包含、；: 余弦仅 0.4806，任何规则都判不出来，只能靠词典。 |

## 管理接口（3 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_HOST` | `'127.0.0.1'` |  |
| `ADMIN_PORT` | `8765` |  |
| `ADMIN_TOKEN` | `None` |  |

## SQLite（1 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `DB_BUSY_TIMEOUT_MS` | `5000` |  |

## 记忆域与保留（1 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `MEMORY_NAMESPACE` | `Field(default=None, description='MCP 侧由 MCP host 通过环境变量注入；Admin 侧不使用')` | ---------- 身份与隔离 ---------- |

## 分块（4 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `CHUNK_ENABLED` | `False` | ---------- chunk 层（A16，默认关）----------；: 关掉时写入/检索路径完全不变（连 chunk_store 都不会被 import）。；: 打开后长 summary 按 CHUNK_SIZE 滑窗切块、各存一个向量，；: 检索命中 chunk 再聚合回卡 —— 一条卡最多保留 CHUNK_KEEP_PER_EVENT 个块。 |
| `CHUNK_KEEP_PER_EVENT` | `2` |  |
| `CHUNK_OVERLAP` | `32` | : 相邻块重叠字符数，防止关键信息正好被切断。 |
| `CHUNK_SIZE` | `256` | : 每块字符数。中文按字符切，256 字约 256 token，远小于 bge-m3 的 8192 上限。 |

## 上下文（1 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `CONTEXT_TOKEN_BUDGET` | `3000` |  |

## 其他（38 项）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AGENT_KEY_HEADER` | `'Authorization'` | : 管理接口读 token 的 header 名。；: **默认 `Authorization`**：实现一直是 Bearer，ADR v0.10 写的 `X-Agent-Key`；: 从未实现过 —— 配置的默认值不该描述一个不存在的协议（2026-11-04 审计）。；: 值等于 `Authorization`（不区分大小写）时按 `Bearer <token>` 解析；；: 其它名字则整个 header 值就是 token。 |
| `ARCHIVE_WINDOW_DAYS` | `180` | ---------- 召回：条数与阈值 ---------- |
| `CLUSTER_MERGE_SIM` | `0.85` |  |
| `CLUSTER_MIN_SIZE` | `15` |  |
| `COLD_QUERY_P95_MS` | `3000` | : 冷查询**SLA 护栏**：单次召回超过它就打 WARNING。`<=0` 关闭告警。；:；: 2026-11-06 实测 P95 = 671 / 878 / 739ms（三轮，见 spec 028/029），；: 离 3000 还有 4 倍余量 —— 所以它只在**严重性能退化**时才响。；:；: 原来的 800 是 ADR 当初定的**优化目标**，实测未达成（瓶颈已定位在；: rerank 约 314ms/次 + 每次 recall 重建 3 个 httpx.Client 重载 CA 证书；: 约 100ms）。800 保留在 ADR C.B12 作为优化方向，不再作为告警阈值 ——；: 否则每次查询都告警，日志被淹，真正的退化反而看不见。 |
| `CONFIRMED_MIN` | `2` |  |
| `CONSOLIDATE_DUP_RATIO` | `0.4` |  |
| `CONSOLIDATE_LATENCY_MS` | `300` |  |
| `CONSOLIDATE_MIN_EVENTS` | `5000` | ---------- consolidation（P2） ---------- |
| `DATABASE_URL` | `'sqlite:///./data/memory.db'` | ---------- 存储 ---------- |
| `EMOTION_BOOST` | `0.15` |  |
| `EXPLICIT_IMPORTANT_FLOOR` | `0.85` |  |
| `FORGET_GRACE_DAYS` | `90` |  |
| `FORGET_IDLE_DAYS` | `180` |  |
| `FORGET_MIN_RECALLS` | `1` |  |
| `FORGET_SWEEP_DAYS` | `30` | ---------- 遗忘 ---------- |
| `GRAPH_HUB_DEGREE` | `8` |  |
| `GRAPH_TIMEOUT_MS` | `200` | ---------- 召回：超时 ----------；: 图通道超时。 |
| `GUIDE_CHECK_CRON` | `'0 9 * * 1'` | ---------- 运维 ---------- |
| `ISLAND_MAX_NEIGHBORS` | `0` | : 孤岛判定（spec 022）：事件级邻域 <= 此值视为孤立子图。；:；: **0 = 关闭（默认）**，与 `GRAPH_HUB_DEGREE` 的 `0=关` 惯例一致。；: 关闭的理由不是「没做完」，是「冷启动阶段数据少，孤立是常态而非异常」——；: 建议等 namespace 有 50+ 条真实记忆后再设为 2。 |
| `ISLAND_MIN_EVENTS` | `50` | : 孤岛判定的数据量门槛：namespace 事件数低于此值时不判孤岛。；: 没有它，只有 3 条记忆时会把全部判成孤岛并一起加速淘汰。 |
| `LABEL_DUAL_RUN` | `True` |  |
| `LABEL_DUAL_RUN_JACCARD` | `0.8` |  |
| `LABEL_LLM_TEMP` | `0.2` |  |
| `LABEL_REVIEW_BATCH` | `50` |  |
| `LOG_FILE` | `'logs/memory.log'` |  |
| `LOG_LEVEL` | `'INFO'` |  |
| `PROTECT_IMPORTANCE_REVIEW` | `0.7` |  |
| `QQ_MCP_PLAIN_DIR` | `Field(default='')` | qq-mcp 解密副本目录（`plain/`）。留空时按；`~/.qq-mcp/plain` → `/mnt/c/Users/*/.qq-mcp/plain` 顺序自动找。 |
| `QQ_NT_MSG_DB` | `Field(default='')` | ---------- 外部工具路径（QQ 语料相关，可选）----------；；这些原本是「跑脚本时临时 export 的环境变量」，用户要求统一放到 .env 里配。；全部可留空：留空时相关脚本自己找默认位置，或明确报错说「没配」。；；NT QQ 的消息库（`nt_msg.db` 的完整路径）。导出/清理脚本用它定位原库。 |
| `RECENCY_HALF_LIFE_DAYS` | `180` |  |
| `SQLITE_WAL` | `True` |  |
| `TOKENIZER` | `'cl100k_base'` |  |
| `TYPES_VOCAB` | `Field(default_factory=list)` | ---------- 标签 ---------- |
| `USER_DIRECTIVE_DAILY_MAX` | `20` | ---------- 语言信号（C.B13 v0.11 形态） ---------- |
| `VECTOR_TIMEOUT_MS` | `800` | : 向量通道超时。**不是** `COLD_QUERY_P95_MS` 的镜像 ——；: 它对齐的是 ADR C.B12 里那个 800ms 的**优化目标**，不是 3s 的 SLA。；: 超时值是「一次 HTTP 等多久算异常」，SLA 是「整次查询多久算不可接受」，；: 两者不该相等：把这里也改成 3000 会让单通道卡 3 秒，整次查询直接贴 SLA。 |
| `W_IMP_BASE` | `0.8` |  |
| `W_TIME_BASE` | `0.7` |  |

