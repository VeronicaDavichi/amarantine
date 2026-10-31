# P0 任务清单（v2.0）

> 依据 [`ADR-001-memory-architecture.md`](./ADR-001-memory-architecture.md) v0.11 拆分。**只覆盖 P0**，P1/P2 不在此。
> 规模标记为相对工作量（S/M/L），**不是工期承诺**。

## 1. 目标与出口

**目标**：跑通「MCP 工具（LLM 侧）+ 管理通道（人侧）」双入口闭环。

**出口条件（二值判定）**：30 条验收全绿。

| 组 | 验收项 | 条数 |
|---|---|---|
| 契约与数据 | A1–A7、A13 | 8 |
| 图与遗忘 | A8–A12 | 5 |
| 动态 K | A17–A19 | 3 |
| 空结果与打标 | A20–A26 | 7 |
| 时效、指令与隔离 | A28–A34 | 7 |
| **合计** | | **30** |

## 2. 不做清单（防蔓延）

| 不做 | 原因 | 归属 |
|---|---|---|
| rerank 接入与绝对阈值重标 | 无 reranker 服务，先跑通纯向量 | P1（A27） |
| 图重建作业 | P0 只需 outbox 幂等 | P1（A14–A16） |
| 物理冷热拆分 | 数据量未知，P0 只做逻辑分层 | P1 |
| consolidation / TopicCluster | 无数据，触发条件不成立 | P2 |
| SetFit 分类器 | 数据不足，P0 用 LLM + 规则 | P2 |
| 多用户 / 多租户 | 只要 namespace 级隔离 | 不做 |
| embedding/rerank 服务正式契约 | 内部服务，格式由框架决定 | 不做 |

## 3. 依赖前置（开工前必须就绪）

| 依赖 | 验收方式 |
|---|---|
| docker 环境 | `docker compose up` 三容器 healthy |
| Weaviate | `GET /v1/.well-known/ready` → 200 |
| embedding-svc（bge-m3） | `POST /v1/embeddings` 返回 **1024 维** |
| Neo4j | `RETURN 1` 成功 |
| 对话模型 API | 一次 `messages` 调用 200 |
| **MCP host** | Claude Desktop 或 DSH 能拉起 stdio server |
| **Node.js 20+** | `node -v` 满足，供前端构建 |

## 4. 任务分解（40 任务 / 7 阶段）

### 阶段 A：地基（5 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| A1 | 项目骨架 + 配置加载 | 全部 D6 配置可读可覆盖（env > yaml > 默认） | — | M |
| A2 | EventCard 模型 + 校验器 | **25 字段**模型（含 `namespace`），附录 B 落地 | A1 | M |
| A3 | 合成夹具生成器 | ≥30 条，覆盖 `date_end=null`、冲突对、5 类硬信号、不可逆 | A2/A6/A10/A11 | M |
| A4 | docker-compose | weaviate + embedding-svc + neo4j | 前置就绪 | S |
| A5 | **MCP server 框架** | stdio JSON-RPC 收发 + tool 注册表 + `MEMORY_NAMESPACE` 读取 | — | L |

### 阶段 B：写入链路（9 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| B1 | **`memory_remember` 工具** | 参数解析 + 幂等（`event_id`） | A7 | M |
| B2 | 事件切分 | `summary` + 日期区间 + `resolves` 闭环检测 | A6 | L |
| B3 | **参数校验与护栏** | `pinned`/`important`/`emotion_intensity` 类型校验；24h 滥用降级；`llm_inferred` 禁令 | A31/A33 | M |
| B4 | 打标 ①② 层 | 规则层 + LLM 结构化输出 + 候选集约束 | A21 | L |
| B5 | 双跑 + LABEL_REVIEW | Jaccard + 入队 + 词表版本化 | A22/A23/A24/A30 | M |
| B6 | importance 评分 | 4 档 rubrics + `importance_reason`（C.B8） | — | S |
| B7 | `event_id` 生成 | canonical hash **含 namespace** | — | S |
| B8 | Weaviate 写入 | embed → 写入；**强制注入 namespace filter**；混模型拒绝；挂 → 503 | A25/A26/A34 | M |
| B9 | 图 outbox | 幂等写 Event 节点（带 namespace）；失败只告警 | A8 | M |

### 阶段 C：召回链路（7 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| C1 | `time_resolver` | 规则 + LLM 合并（C.B9） | A2 | M |
| C2 | 四通道并行扇出 | 超时矩阵 + `degraded`/`partial`；**全通道 namespace 过滤** | A3/A34 | L |
| C3 | RRF 融合 | k=60 **等权**，通道缺失直接移除 | — | S |
| C4 | `final_score` | 时效公式（C.B1），`W_TIME_BASE=0.7` | A28 | M |
| C5 | 动态 K | 绝对过滤 → 相对衰减 → 断崖 → 预算 → 夹取 | A17/A18/A19 | L |
| C6 | 空结果显式化 | `materials=[]` + `empty_reason` | A20 | S |
| C7 | **`memory_recall` 工具** | tool result 封装 + `recall_count` 回写口径 | A1/A13 | M |

### 阶段 D：管理通道（8 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| D1 | **Admin HTTP 骨架** | FastAPI + Bearer token + 绑定 `127.0.0.1` | — | M |
| D2 | `/admin/ingest` | 历史数据批量导入 + `dedupe_key` 幂等 | A7 | M |
| D3 | `/admin/archive` | 逻辑分层，`dry_run` 默认 true | A4 | S |
| D4 | `/admin/label/*` | 队列 / 裁决 / 词表回溯 | A23 | M |
| D5 | `/admin/review` + resolve | 两桶列表 + 裁决（keep→PROTECTED） | A12 | M |
| D6 | `/admin/forget/sweep` | 三桶分流 + 强制 `dry_run`；`false` 返 400 | A9/A10/A11/A12 | L |
| D7 | `/admin/event` PATCH/DELETE | 白名单 `pinned`/`retention`；软删 | A32 | S |
| D8 | `/admin/raw/{quote_id}` | 原文读取（软删后仍可读） | A5 | S |

### 阶段 E：MCP 工具与指南（4 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| E1 | `memory_forget` | 软删 + `raw_quote_preserved` | A32 | S |
| E2 | `memory_feedback` | `(trace_id,event_id)` 幂等 | A29 | S |
| E3 | **工具使用指南** | `docs/memory-tool-guide.md`（单一源头）+ MCP `instructions` 生成 | — | M |
| E4 | **一致性校验脚本** | `harness/check-guide.py`：比对 inputSchema 与指南，不一致即非零退出 | — | S |

### 阶段 F：管理前端（4 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| F1 | React + MUI + Zustand 骨架 | Vite 工程 + 主题 + 路由 + 请求层 | — | M |
| F2 | 复核队列页 | 列表 + 裁决按钮（keep/delete） | A12 | M |
| F3 | 遗忘报告页 | dry_run 三桶数量 + 候选列表 | A12 | S |
| F4 | 事件卡浏览 + pin | 搜索 + 详情 + 置 pin | A31 辅助 | M |

### 阶段 G：验收收口（3 任务）

| ID | 任务 | 产出 | 验收 | 规模 |
|---|---|---|---|---|
| G1 | 契约测试 | MCP tool result + Admin API × 正常/边界/异常 | A1 | M |
| G2 | 30 条 A 项脚本 | 每条一个可重复执行的断言 | 全部 | L |
| G3 | 基线记录 | 环境/数据/结果写入测试文档，含「不可外推」声明 | — | S |

## 5. 关键路径

```
A（地基：含 MCP 框架）→ B（写入）→ C（召回）→ G（验收）
                        ↓
                   D（管理通道）→ F（前端）
                   E（其余工具）
```

- **B → C 硬串行**：召回需要写入的数据
- A2/A3/A5 必须先于 B（模型、夹具、MCP 框架）
- D 依赖 B（要有卡才有删除/复核对象）
- F 依赖 D4/D5/D6（要接口才有页面）
- E 可与 D 并行

## 6. 里程碑

| 里程碑 | 判定 | 可验项 |
|---|---|---|
| M1 地基就绪 | 三容器 healthy + MCP server 能被 host 拉起 | A1 |
| M2 可写入 | LLM 调 `memory_remember` 落库，卡可查 | A6/A7/A21/A25/A26 |
| M3 可召回 | `memory_recall` 返回材料 + trace | A1/A2/A3/A5/A20/A28/A34 |
| M4 闭环完整 | 遗忘/复核/删除/反馈全通 | A9–A12/A29/A32 |
| M5 管理可用 | 前端能裁决与看报告 | A12/A31 |
| M6 验收全绿 | 30 条脚本全过 | 全部 |

## 7. 风险

| 风险 | 触发点 | 缓解 |
|---|---|---|
| LLM 不主动调工具 | E3 | 工具 description 反复调优；在 system prompt 里提示可用工具 |
| LLM 乱填 `pinned` | B3 | 滥用护栏 + 服务端只认参数不认文本 |
| 事件切分粒度不稳 | B2 | 先用手工夹具验证，再调 prompt |
| 图通道拖慢召回 | C2 | Neo4j 200ms 硬超时 |
| MCP stdio 调试困难 | A5 | 先写 JSON-RPC 回显 server 打通链路 |
| 指南与工具定义漂移 | E4 | `check-guide.py` 定时校验，不一致即非零退出 |
| 合成夹具不代表真实分布 | A3 | 标注「基线不可外推」 |

## 8. 与 ADR 的对应

| 本清单 | ADR |
|---|---|
| MCP 工具 | 4.3、4.9 |
| 管理接口 | 4.2、4.4–4.8 |
| 任务「验收」列 | 第 5 节 A1–A34 |
| 配置 | D6 配置表 |
| 判定方法 | 附录 C B1–B13 |

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-31 | 首版：30 任务 / 5 阶段 / 29 条验收映射 |
| v1.1 | 2026-10-31 | 按 stack-decision 回写同步：加入 namespace 隔离（A34），29→30 条 |
| v2.0 | 2026-10-31 | **架构改为 MCP 双入口**：任务 30→**39**、阶段 5→7；新增 A5（MCP 框架）、D（管理通道 8 项）、E（其余工具 3 项）、F（前端 4 项）；B1 改为 `memory_remember`、C7 改为 `memory_recall`；新增风险「LLM 不主动调工具」 |
| v2.1 | 2026-10-31 | 新增 E3 工具使用指南（MCP `instructions` 单一源头）与 E4 一致性校验脚本，任务 39→**40**；新增风险「指南与工具定义漂移」 |
