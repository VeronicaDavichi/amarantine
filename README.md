# amarantine

> [!WARNING]
> **开发中 · 未完成（WIP）**
>
> 这个项目还在施工：接口、数据模型、spec 都可能变，**不保证对外稳定，不要用于生产**。
> 当前状态：1262 个测试全绿、44 个 spec；已实现 036（切分不丢）/ 037（英文实体），
> 还剩 **2 项有 spec 未开工 + 4 项草稿**（`date_start` 缺陷已并入 [040](specs/040-chat-import-tool/)，见[已知限制](#已知限制)）。

> 给大模型用的长期记忆：**MCP 工具（LLM 侧）+ 本地管理通道（人侧）**。
> 事件卡片进向量库与图库双写，支持召回、遗忘、复核、反馈的完整闭环。

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

一个自托管的长期记忆服务。大模型通过 MCP（stdio JSON-RPC）写入与召回记忆；
人通过本地 HTTP 管理通道做复核、纠错、遗忘和归档。

```
┌──────────────────────────────────────────┐
│ MCP Host（Claude Desktop / DSH / ...）    │
│   └─ 拉起子进程 env: MEMORY_NAMESPACE=xxx │
│        └─ memory-mcp  (stdio JSON-RPC)    │
└──────────────────────────────────────────┘
              ↓ 共享 service 层
┌──────────────────────────────────────────┐
│ FastAPI — 127.0.0.1:8765 管理通道          │
└──────────────────────────────────────────┘
              ↓
   Weaviate（事实源）· Neo4j（派生图）· SQLite（原文/计数）
```

### 核心设计

| 决策 | 内容 |
|---|---|
| **事实源与派生层分离** | 事件卡写进 Weaviate 即为事实；Neo4j 图是可丢弃、可全量重建的索引 |
| **写序固定** | 先写向量（成功才算写成功），再异步更新图；图失败只告警，不回滚 |
| **namespace 隔离** | 只来自环境变量，入参携带一律拒绝；向量与图同源派生 |
| **原话与摘要分层** | 原文进 SQLite（可溯源），摘要进向量（可语义召回） |
| **遗忘不物理删** | 事实层只软删（`is_deleted` + `deleted_at`），图节点才会 `DETACH DELETE` |
| **弱关联可分层** | 关系带 `label` / `confidence` / 证据，查询分 strict / default / explore 三档 |

### 特性

- **双入口**：MCP 工具给 LLM，Admin HTTP 给人，共用 service 层
- **批量导入**：`POST /admin/ingest` 把历史对话切分成事件卡（LLM 切分 + 幂等 `dedupe_key`）
- **动态召回预算**：token 预算 + 相对衰减 + 断崖 + 强制保留溢出
- **别名与称呼**：规则层 + LLM 复核，拒绝也留痕（`alias_reject`）可复查
- **可观测**：召回带 `trace_id`，反馈能回流；健康检查覆盖数据一致性

### 快速开始

前置：WSL2 + Docker（中间件）、Python 3.12。

```bash
# 1. 中间件（WSL 内执行）
docker compose up -d          # weaviate / embedding / reranker / neo4j

# 2. 后端
cd back_end
uv sync --python 3.12
cp .env.example .env          # 填 LLM_API_KEY / ADMIN_TOKEN
uv run pytest -q

# 3. 起管理通道
uv run python -m memory.entrypoints.admin_http   # http://127.0.0.1:8765

# 4. MCP 冒烟
MEMORY_NAMESPACE=agent-a uv run memory-mcp
```

### 目录

```
back_end/         Python 3.12 · FastAPI · MCP stdio · SQLite
harness/          夹具生成、开发脚本、一致性校验、导入工具
specs/            LeanSpec 规格（43 个，含需求/设计/任务/验收）
docs/             ADR、P0 任务、用户手册、工具指南、审计记录
docker-compose.yml
```

### 文档

| 文档 | 内容 |
|---|---|
| [ADR-001 架构](docs/ADR-001-memory-architecture.md) | 决策 D1–D10、契约 4.0–4.10、验收 A1–A34 |
| [用户手册](docs/user-manual.md) | 安装、配置、操作、FAQ、限制 |
| [工具使用指南](docs/memory-tool-guide.md) | 给 LLM 的调用说明（MCP instructions 单一源头） |
| [验收审计](docs/acceptance-audit.md) | 34 条验收标准逐条核对 |
| [部署档位](docs/deployment-profiles.md) | 高配（GPU）/ 低配（2–3 GB）/ 混合，含实测内存与延迟 |
| [规格目录](specs/) | 每个 spec 有需求、设计、任务、验收 |

### 质量

```bash
cd back_end && uv run pytest -q        # 1545 passed
python3 ../harness/check_spec.py       # spec 骨架一致（48 个）
python3 ../harness/check_guide.py      # 文档与代码一致

# 一键全量验收（静态 + 真库；会先唤醒容器）
bash harness/verify_all.sh
bash harness/verify_all.sh --static   # 只跑静态检查，不需要容器
```

`verify_all.sh` 依次跑：单测 → spec 骨架 → 文档一致性 → `wake.sh` →
`verify_046.py`（指代回溯/卡归并）→ `verify_047.py`（群名片/头衔/whois）。

### 外部依赖

| 依赖 | 用途 | 是否必需 |
|---|---|---|
| LLM（OpenAI 兼容网关） | 事件切分、标签、摘要 | **必需**（不可用时降级，质量下降） |
| [nbnhhsh](https://github.com/itorr/nbnhhsh)（`lab.magiconch.com`，Apache-2.0） | 把拼音首字母缩写猜成全称（`lysk → 恋与深空`） | **可选，默认关** |

**nbnhhsh 的使用方式**

- 开关：`ABBR_NBNHHHSH_ENABLED=false`（默认）。
  开启后，解析缩写时会先查本地缓存，未命中才 `POST /api/nbnhhsh/guess`
  （只发缩写字符串，如 `{"text":"lysk"}`，不含聊天内容）。
- 关掉后仍可用 `back_end/config/abbr_local.json` 的本地词典（`nn=暖暖` 这类
  nbnhhsh 未收录的词），功能不消失、只是长尾覆盖变少。
- **它没有公开的整包词库**：仓库里只有油猴脚本（7 个文件），API 也只有 `guess`
  这一个端点。因此本项目把每次查询结果缓存到 `back_end/data/abbr_cache.db`，
  再配合本地词典逐步积累，而不是「扒一份完整词库」。
- 引用该项目的名字与链接仅作来源标注；其许可证为 Apache-2.0。

### 已知限制

**按 spec 记录的未完成项**

| 项 | spec | 状态 / 影响 |
|---|---|---|
| 实体证据降级（拉丁 topic 误当实体） | [044](specs/044-entity-evidence-demotion/) | `planned`，**故意放一放**：当前语料量不足 |
| 群名片的「备注」标记 | [047](specs/047-group-card-alias/) | 数据源不可得：`list_contacts` 不返回 `remark` |

**已实现（供对照）**

| 项 | spec |
|---|---|
| 切分超上限按行二分，不再静默丢尾部 | [036](specs/036-split-truncation-data-loss/) |
| 英文词形（ESB / Kafka / PyCharm）成为实体 | [037](specs/037-latin-acronym-entities/) |
| 称呼归一到真人（`王老师` → 实名节点） | [038](specs/038-appellation-normalization/) |
| 关系谓词：陈述 + 行为双通道，precision 20/20 | [039](specs/039-relation-predicates/) |
| 历史聊天导入（QQ / 钉钉），营销闸门 + 指代回溯 | [040](specs/040-chat-import-tool/) |
| 指称层：字面 → 义项（LLM 抽 gloss，8/8 正确） | [041](specs/041-reference-layer/) |
| 身份来源分层：uid > 备注 > 昵称 > 姓名 | [043](specs/043-identity-source-priority/) |
| Person 键从名字派生改为账号锚定 | [045](specs/045-person-key-strategy/) |
| 会话内指代回溯 + 卡归并（`resolves`） | [046](specs/046-context-backtracking-merge/) |
| 群名片按 scope 别名 + 群头衔 `TITLED` | [047](specs/047-group-card-alias/) |
| 拼音首字母缩写（`lysk → 恋与深空`） | [048](specs/048-abbr-alias/) |
| 实体消歧候选池（「看过但没敢合」也留痕，可人工裁决） | [016](specs/016-entity-disambiguation/) |

**已知缺陷 / 边界**

- 单机自托管，无多租户；隔离粒度是 `namespace`
- 依赖外部 LLM 做切分与标签复核：不可用时降级（产出原文卡），质量下降
- 中文人名抽取依赖权威名册；无名册时退化到分词，会误判品牌名
- 关系层：precision 0.95 / recall 0.67（2026-11-18 实测）；**行为通道已停止投入**（数据凑不齐，只有陈述通道产出边）
- **清空 namespace 会连 `Account`/`Alias` 一起删**，名字标记与头衔要重写：
  `harness/apply_display_names.py` / `harness/apply_titles.py`
- QQ 语料只导了 12 / 106 个群；**数据导入已暂停**（2026-11-18 用户决定：磁盘吃紧）。
  恢复前先确认磁盘：`df -h /mnt/c`（C 盘曾经只剩 6G，清 `Temp/wsl-crashes` 回收 12G）

### License

[MIT](LICENSE)

---

## English

> [!WARNING]
> **Work in progress** — APIs and data models are still changing. Tests are green
> (1262 passing), but this is **not production-ready** and carries several known gaps.

**amarantine** is a self-hosted long-term memory service for LLMs.

- **MCP tools** for the model side (stdio JSON-RPC), **HTTP admin channel** for humans
- **Dual store**: event cards in Weaviate (source of truth) + Neo4j graph (derived, rebuildable)
- **Batch ingest** of historical conversations with idempotent dedupe keys
- **Forget without deleting**: fact layer is soft-deleted only
- **Weak relations**: edges carry `label` / `confidence` / evidence, queries have three strictness tiers

```bash
docker compose up -d
cd back_end && uv sync --python 3.12 && cp .env.example .env && uv run pytest -q
uv run python -m memory.entrypoints.admin_http   # http://127.0.0.1:8765
```

Docs are in Chinese under `docs/`. MIT licensed.
