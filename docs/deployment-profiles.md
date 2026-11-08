# 部署档位建议：高配（GPU）与低配（2–3 GB 开发本）

> 数据来源标注：**实测** = 本机（16 GB / RTX 4060 Ti 16G）当场量的；
> **估算** = 按模型大小与算子特性推的，没有硬件可验，落地前要自己量一次。

## 0. 结论先行

| | A · 高配（GPU 一体机） | B · 低配单机（2–3 GB，无 GPU） | C · 混合（低配跑存储，模型在高配） |
|---|---|---|---|
| embedding | 本地 bge-m3 / GPU fp16 | 本地 ONNX int8 小模型 | **远端**（指向高配 8082） |
| rerank | 本地 GPU | 关 | 关（或远端） |
| Weaviate | 跑 | 跑 | 跑 |
| Neo4j | 跑 | **不跑**（图降级） | **不跑** |
| 常驻内存 | **≈ 5.7 GB**（实测） | ≈ 1.3–1.7 GB（估算） | ≈ 0.5–0.7 GB（估算） |
| 召回延迟 | **96 ms**（实测） | 预计 1–3 s（估算） | 同 A + 局域网往返 |
| 灌库 | 本机 | **别在本机灌** | 在高配机灌 |

一句话：**2–3 GB 的机器跑不下 A 档**——光 embedding 一个进程就要 2.3 GB。
要么把模型挪出去（C），要么换 ONNX int8 并砍掉 Neo4j（B）。

## 1. A 档：高配（当前这台，全部实测）

| 组件 | 形态 | RSS | 端口 |
|---|---|---|---|
| Weaviate 1.28.4 | docker | 60 MB | 8081 / 50061 |
| Neo4j 5.26 | docker | 802 MB | 7474 / 7687 |
| embedding | `harness/embed_server.py`（bge-m3，torch 2.15+cu132，fp16） | **2.32 GB** | 8082 |
| rerank | `harness/rerank_server.py`（bge-reranker-v2-m3，fp16） | **2.28 GB** | 8083 |
| admin | `memory.entrypoints.admin_http` | 229 MB | 8765 |
| 合计 | | **≈ 5.7 GB**（WSL 侧 5.4 GB used） | |

显存：4.7 GB / 16 GB（两个模型都在 GPU 上，fp16）。

**实测延迟**

| 操作 | 耗时 |
|---|---|
| embedding 32 条 | **43 ms** |
| rerank 50 条候选 | **67 ms**（冷）/ 12–16 ms（热） |
| recall top5（向量+图+rerank） | **96 ms** |
| 灌库 | ~25 卡/分钟（瓶颈是 LLM 网关，不是本机） |

**磁盘**：两个模型 4.4 GB + neo4j 518 MB + weaviate 9.4 MB（606 张卡）+ sqlite 444 KB。
按 606 卡 ≈ 9 MB 向量数据推，10 万卡大约 1.5 GB —— Weaviate 不是磁盘瓶颈。

**启动**：`bash harness/up_local.sh`（容器 + 两个模型服务 + admin）。

## 2. B 档：低配单机（2–3 GB，无 GPU）

### 2.1 为什么不能照搬 A 档

`torch` 运行时的固定开销（CUDA 库、算子）就有 500–600 MB，加上权重：

| 项目 | 内存 |
|---|---|
| bge-m3 fp16 进程 | 2.32 GB（实测） |
| bge-reranker-v2-m3 进程 | 2.28 GB（实测） |
| Neo4j | 802 MB（实测） |

**两个模型服务就 4.6 GB，已超 2–3 GB 总内存**，还没算 WSL / 系统 / Weaviate。

### 2.2 三刀砍下去

**第一刀：embedding 换 ONNX int8**

- 权重 ~570 MB（`BAAI/bge-m3` 的 `onnx/model_quantized.onnx`）
- `onnxruntime` 运行时约 30–50 MB，没有 CUDA 库开销
- 进程 RSS 预计 **0.8–1.0 GB**（估算）
- 代价：CPU 上慢一个量级，32 条约 1.5–3 s（估算）
- **现状**：仓库里的 `harness/embed_server.py` 是 torch 版（GPU 用），
  ONNX 版没有保留 —— 要回退得重写一份（接口不变，约 100 行：tokenizer + int8 模型 + CLS 池化 + L2 归一化）

**第二刀：砍 Neo4j**

图是**派生层、可全量重建**（ADR 的 D1）。不跑 Neo4j 时：

- 写事件只告警、不回滚 —— 卡照样进 Weaviate，召回靠向量 + BM25
- 省下 ~800 MB
- ⚠️ **没有「关掉图通道」的配置**：`recall.py` 里图通道是无条件提交的。
  但 Neo4j 不起时，连接被拒是**毫秒级失败**，不会拖 200ms，代价只是 trace 里多一条
  `graph` 错误 + 每次召回一行 WARNING。所以 `GRAPH_TIMEOUT_MS` **保持默认**即可，
  不要设 0（它的语义是「Cypher 查询超时」，0 可能被解释成不限制）

**第三刀：关 rerank**

`RERANK_ENABLED=false` 是设计好的降级路径（A27），排序退化成 RRF，不影响可用性。

砍完合计：Weaviate ~100 MB + admin ~230 MB + embedding ~0.9 GB ≈ **1.3 GB**，
留给 WSL/系统的还有 1–1.7 GB —— 2–3 GB 能跑，但**别再在本机开 Docker Desktop**（它自己就要 1–2 GB）。
WSL 里只装 `docker` 引擎跑 Weaviate 一个容器，或者干脆把 Weaviate 也换成远端。

### 2.3 别在本机灌历史数据

灌 12 个群（1993 轮）在本机要跑 LLM 切分 + 逐条 embedding。
低配机做这件事会持续吃满 CPU 十几分钟到几十分钟，体验很差。
建议：**在 A 档机器灌完，把 Weaviate 的 volume 目录整块拷过来**
（`/var/lib/docker/volumes/amarantine_weaviate_data`，606 张卡只有 9.4 MB），
低配机只负责「查」。SQLite（`back_end/data/memory.db`）里的 `raw_quote` / `ingest_job`
也一起拷，原文才查得到。

### 2.4 低配专项调参

```dotenv
# ---- 模型 ----
EMBEDDING_BASE_URL=http://127.0.0.1:8082   # ONNX 服务
EMBEDDING_DIM=1024                          # 用 int8 版 bge-m3，维度不变、可复用向量
RERANK_ENABLED=false

# ---- 砍掉图 ----
# 不启 neo4j 容器即可；**不要把 GRAPH_TIMEOUT_MS 设 0**（那是 Cypher 超时，不是开关）。
# 连接被拒是毫秒级失败，只会在 trace 里留一条 graph 错误。

# ---- 放宽超时：CPU 推理慢，卡太紧会把通道全标成 partial ----
VECTOR_TIMEOUT_MS=2000      # 默认 800
RECALL_BUDGET_MS=3000       # 默认 1500
CONTEXT_TOKEN_BUDGET=2000   # 默认 3000，省 token 也省内存

# ---- 召回规模 ----
RECALL_TOP_K_MAX=15         # 默认 30，低配别一次拉太多
```

Weaviate 加内存上限（compose 里）：

```yaml
  weaviate:
    environment:
      GOMEMLIMIT: "512MiB"
    deploy:
      resources:
        limits:
          memory: 1G
```

### 2.5 B 档的底线预期

- 单条召回 **1–3 s**（CPU embedding 是主要成本），比 A 档慢一个量级
- 图相关能力（「谁和谁是什么关系」）**基本不可用**，只剩向量/BM25
- 首次查询会触发模型加载，冷启动再等 5–10 s

## 3. C 档：混合部署（最省内存，推荐给 2–3 GB 本子）

低配机只跑「存储 + admin」，**模型全在高配机**：

```
低配本（2–3 GB）                          高配机（GPU）
  admin_http  :8765  ──HTTP──>  embed  :8082
  Weaviate    :8081  ──HTTP──>  （可选）rerank :8083
  Neo4j 不跑
```

`.env` 只改这几行（**IP 和 key 都是现成配置项，不用改代码**）：

```dotenv
# 指向高配机；服务器地址用局域网 IP
EMBEDDING_BASE_URL=http://<高配机局域网IP>:8082
EMBEDDING_API_KEY=<和高配机一致>
RERANK_BASE_URL=http://<高配机局域网IP>:8083
RERANK_API_KEY=<同上>
RERANK_ENABLED=false   # 想用就改成 true
# Neo4j 不启；GRAPH_TIMEOUT_MS 保持默认（连接被拒是毫秒级失败）
```

高配机侧同样只改 `.env`（服务启动时自己读，不用命令行参数）：

```dotenv
# 监听地址 = BASE_URL 的 host:port，所以填 0.0.0.0 才能被局域网访问
# （0.0.0.0 作为**客户端**地址在 Linux 上也能连通，本机自调不用改）
EMBEDDING_BASE_URL=http://0.0.0.0:8082
EMBEDDING_API_KEY=<随便一串，低配机要填同一个>
EMBEDDING_PRELOAD=1

RERANK_BASE_URL=http://0.0.0.0:8083
RERANK_API_KEY=<同上>
RERANK_PRELOAD=1
```

**关键点**：`EMBEDDING_API_KEY` / `RERANK_API_KEY` 是**同一个变量被两端读** ——
服务端拿它校验，客户端拿它发 `Authorization: Bearer`。所以只配一处，
不会出现「一边配了另一边忘了」。实测：配了 key 后直连不带 header 是 401，
带 key 是 200，而召回（走客户端）照常成功。

同理，`*_BASE_URL` 也是同一个变量：服务端拿它决定**绑哪**，客户端拿它决定**调哪**。
单机时两边都是 `http://127.0.0.1:8082`；对外提供服务时改成 `0.0.0.0`。

低配机常驻只需 **0.5–0.7 GB**（Weaviate + admin + WSL），2 GB 机器也能跑。

**代价与前提**

- 高配机必须常开、在同一局域网，且防火墙放行 8082（这跟之前 LLM 网关那件事是同一类问题）
- 断网就完全没有召回能力 —— 比 B 档（至少本地能算）更依赖网络
- 模型加载在远端，低配机冷启动很快

## 4. 切换档位时最容易踩的坑

| 坑 | 后果 | 规避 |
|---|---|---|
| 换 embedding 模型但没改 `EMBEDDING_DIM` | 写入直接报维度不符 | int8 版 bge-m3 仍是 **1024**，可平滑换；换 bge-small（512）**必须重灌** |
| 换维度后复用旧向量 | 召回全乱、且不报错 | 换维度 = 清空 Weaviate + 重灌，没有捷径 |
| 只拷 Weaviate 不拷 SQLite | 卡里引用的 `raw_quote_ids` 查不到原文 | 两个一起拷 |
| 低配仍开 Docker Desktop | 内存被吃掉 1–2 GB，模型被 OOM | 用 WSL 内的 docker 引擎，只跑必要容器 |
| 把 `GRAPH_TIMEOUT_MS` 设 0 当作「关图」 | 语义是 Cypher 超时，0 可能变「不限制」 | 保持默认 200；不启 Neo4j 就是最快的失败方式 |

## 5. 选型建议（一句话版）

- **有 GPU、16 GB 以上** → A 档，全套本地，96 ms 召回
- **2–3 GB 开发本、能连到 A 档机器** → C 档（最舒服）
- **2–3 GB、必须离线自足** → B 档（ONNX int8 + 无图无 rerank），并在 A 档机器上灌完数据再拷过去
