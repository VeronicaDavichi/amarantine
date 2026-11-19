---
status: planned
created: 2026-11-19
priority: high
tags:
- release
- oss
- packaging
created_at: 2026-11-19T02:48:57.237087765Z
updated_at: 2026-11-19T02:48:57.237087765Z
---

# 049 开源发布准备（Open-source release readiness）

## Overview

目标：把 amarantine 从「能自己跑」推进到**可以公开开源发布的第一版**。

当前状态（2026-11-19）：

- 功能侧：48 个 spec 全部 complete；测试 **1603 passed**
- 交付侧：**没有**一键部署、没有备份恢复、没有版本号、没有 LICENSE 之外的合规材料
- 运维侧：86 个配置项无分组说明；无 CI；健康检查分散在 4 个服务各自探活

用户已决定：**开源发布**。发布前仍有若干工程细节由用户亲自处理，
本 spec 负责把「发布需要哪些门」列清楚并逐项验收。

## Design

### 发布门（Release gates）

### G1 可安装（别人能跑起来）

- [x] `docker-compose.yml` 已有四依赖（admin 由 `harness/up_local.sh` 起，见 G1 待办）
- [x] `GET /health` 汇总四依赖（`degraded`/`deps`/`down`，5s 缓存、3s 超时）
- [ ] 首次启动自动建表/建 collection，无需手工脚本
- [x] `.env.example` 与 `Settings` **完全对齐**（86/86，`check_env_coverage.py` 绿）
- [x] 冷启动文档 `docs/quickstart.md`（compose → 后端 → 写入 → 召回 → 接 MCP）

### G2 数据安全（不丢、可迁）

- [x] 备份脚本：`harness/backup_state.py`（SQLite 在线备份 + 卡片 JSONL + 图 JSONL + manifest）
- [x] 恢复脚本 `harness/restore_state.py`（SQLite 按 namespace + 卡片带向量 + 图 MERGE）
- [ ] 版本迁移：SQLite schema 版本号 + 迁移脚本（现有库能升上来）
- [x] 文档写明三处一起备份（脚本 docstring + 本节实测数字）

### G3 可运维

- [x] 配置清单 `docs/config-reference.md`（`harness/gen_config_doc.py` 生成，86 项分组 + 默认值 + 源码注释）
- [ ] 结构化日志（JSON 行）或至少统一前缀，便于 grep 与采集
- [x] 降级行为 `docs/degradation.md`（五类依赖逐一写「谁挂 → 什么坏 → 怎么恢复」）

### G4 可协作（开源合规）

- [ ] LICENSE 确认（当前仓库已有？待核）+ 第三方依赖许可证清单
      - nbnhhsh（Apache-2.0）已在 README 标注
      - bge-m3 / bge-reranker-v2-m3 的许可证与再分发条件需确认
- [ ] README 重写：这是什么 / 解决什么问题 / 30 秒快速开始 / 架构图 / 已知限制
- [ ] CONTRIBUTING.md（开发环境、测试、spec 流程）
- [ ] SECURITY.md（如何报漏洞；明确「自托管、单租户」的安全边界）
- [x] 隐私说明：`data/`、`corpus/`、`backups/`、`docs/labeling-data.js` 均已在 `.gitignore`

### G5 可验证（可持续）

- [x] GitHub Actions `.github/workflows/ci.yml`（test + privacy 两个 job）
- [ ] CHANGELOG.md + `v0.1.0` tag
- [ ] 发布前最后一遍：`bash harness/verify_all.sh --static` 全绿

## Plan

- [ ] P1 G1 可安装：compose 全套 + `/health` 汇总 + 首启自建表 + `.env.example` + 冷启动文档
- [ ] P2 G2 数据安全：备份 / 恢复 / schema 版本迁移 / 三处状态一起备份的说明
- [ ] P3 G3 可运维：86 项配置分组说明 + 结构化日志 + 三种降级路径写清
- [ ] P4 G4 合规：LICENSE 与依赖许可确认 + README 重写 + CONTRIBUTING + SECURITY + 隐私说明
- [ ] P5 G5 可验证：CI（pytest/check_spec/check_guide）+ CHANGELOG + `v0.1.0` tag

## Test

发布前的验收（都能二值判定）：

| 编号 | 验收 | 手段 |
|---|---|---|
| A1 | 干净环境下 `docker compose up` 后 `/health` 四项全 ok | 新容器跑一遍 |
| A2 | 冷启动按文档能完成一次 `memory_remember` → `memory_recall` | 照文档手做一遍 |
| A3 | 备份 → 清空 → 恢复后条数与恢复前一致 | 脚本比对 SQLite/Weaviate/Neo4j 计数 |
| A4 | 旧库能通过迁移脚本升级，`check_spec`/pytest 全绿 | 用一份旧备份跑 |
| A5 | `git log --all -- '*corpus*' '*data*'` 无真实语料 | 命令输出为空 |
| A6 | CI 在 push 上跑通 pytest + 两个 check 脚本 | GitHub Actions 记录 |
| A7 | `bash harness/verify_all.sh --static` 全绿 | 发布前最后一遍 |

## 用户自己处理的（不阻塞本 spec，但发布前需确认）

用户 2026-11-19：「我还有一些工程上的细节要处理」——具体内容未知，
本 spec 不假设；发布门 G4 的 LICENSE/依赖许可确认需要用户拍板。

## 风险

| 风险 | 缓解 |
|---|---|
| 仓库历史里混入真实聊天数据 | 发布前跑一遍 `git log --all -- '*corpus*' '*data*'`；必要时重写历史或新建干净仓库 |
| 依赖外部 LLM 网关才有完整功能 | README 明确「无 LLM 时降级为原文卡」；给一个本地 ollama 示例配置 |
| 单机单租户，被误认为多用户服务 | SECURITY/README 明确隔离粒度是 namespace，无鉴权分级 |
| 中文人名/群聊特性依赖过强 | 文档写明适用范围（中文群聊优先），英文语料效果未验证 |

## Notes

- 关联：040（导入工具，发布后用户自备语料）、044（抑制表，已 complete）
- 顺序建议：G1 → G2 → G3 → G4 → G5（先能跑，再能保数据，最后合规）

### 2026-11-19 · G1 部分 + G2 备份完成（实测）

**G1 `/health`**：`status` 恒 `ok`（liveness，不因依赖挂掉而让容器被重启），
另附 `degraded` / `deps`（四项各自 ok/ms/detail）/ `down`。
并行探活、总超时 3s、结果缓存 5s（探针高频调用不打疼外部服务）。

实测：

```
status=ok degraded=false
neo4j ok(tcp 0.3ms) weaviate ok(HTTP 200, 476ms)
embedding ok(476ms) rerank ok(476ms)
```

**G2 备份**：`harness/backup_state.py` 一次备齐三处，并写 `manifest.json` 记条数
（恢复时用它校验，而不是人眼看）。实测：

| 处 | 条数 |
|---|---|
| SQLite `raw_quote` | 9415 |
| Weaviate 卡片 | 4383 |
| Neo4j | 节点 68950 / 边 70991 |

产物：`memory.db`(3.3MB) + `cards.jsonl`(5.5MB) + `graph_nodes.jsonl`(12MB)
+ `graph_edges.jsonl`(22MB) + `manifest.json`。

**为什么不用原生备份**：Weaviate 要开 `backup-filesystem` 模块、Neo4j 要
`neo4j-admin dump`（通常停库）；自托管第一版导出 JSONL 更省事、可人看、可迁移，
代价只是恢复慢一点（几千条量级够用）。

**`backups/` 已加进 `.gitignore`** —— 备份里是真实聊天数据，开源仓库绝不能进。

**G2 剩余**：恢复脚本 + 还原后条数校验（A3）；schema 版本迁移（A4）。

### 2026-11-19 · A5 历史检查通过 + 工作区脱敏完成

**A5 历史检查（命令与结果）**

```
git log --all --name-only --diff-filter=A -- "*corpus*" "*data/*" "*.db" ".env" "backups/*"
→ 只命中 specs/021-…、specs/034-… 两个 README 路径（文件名里含 corpus/data），无真实数据

git rev-list --objects --all | 按 blob 大小排序
→ 最大是 back_end/uv.lock (115KB) 与 docs/user-manual.md (110KB)，没有语料/备份
```

**工作区脱敏（96 处 / 33 个文件）**

`harness/scrub_privacy.py`（dry-run 默认）用**项目已有的**映射，不造第三套假名。

**映射表外置**：真实映射在 `harness/.privacy-map.json`（**gitignored**），
仓库里只有 `harness/privacy-map.example.json`（占位符之间互相替换）。
第一版脚本把真名写进了映射表本身 —— 等于换个地方泄露，已改掉。

被替换的类别：真实姓名、内网 IP、真实 QQ 号。

复查：六个模式在 `git ls-files` 里**全部 0 命中**（`check_scrub.py`）。
脱敏后全量测试仍 1607 passed（改到了测试里的固定数据）。

**顺手把评测数据移出仓库**

`docs/labeling-data.js` 是 `make_labeling_data.py` 的产物，内含真实聊天摘要 →
加入 `.gitignore` 并 `git rm --cached`（工作区文件保留，页面照常能开）。

**仍待你拍板的一处**：`back_end/config/inactive_sessions.json` 里有**真实群号**。
两种做法：
1. 改成 `.example.json` 示例 + 真实文件 gitignore（加载器加 fallback）
2. 保留现状（群号本身不含内容，但仍能暴露「你在哪些群」）

我倾向 1，但它会改运行时行为，属「需确认的改动」，等你定。

### 2026-11-19 · G2 恢复（A3 通过）+ G3 配置清单 + G5 CI

**G2 恢复演练（A3）**

在 `verify046b`（3 卡 / 10 节点 / 24 边）上做「备份 → 清空 → 还原」：

```
备份：卡片 3，图节点 10 / 边 24，SQLite raw_quote 3 / ingest_job 3
清空：删 3 卡 + DETACH DELETE 图 + 删该 namespace 的 SQLite 行
还原：卡片 3，图 10/24，SQLite 3/3
校验：备份 3 / 还原后 3  →  A3 卡片条数一致 ✓
```

`restore_state.py` 的设计要点：

- **卡片带向量还原**：`backup_state.py` 现在导出 `_additional { vector }`；
  不重新 embed（换过模型就会新旧向量混用）
- **固定 UUID**（`deterministic_uuid`），重跑幂等
- **按 namespace 还原**，`--into` 可导到别的 namespace 做演练（不动真实数据）

**G3 配置清单**：`docs/config-reference.md` 由 `harness/gen_config_doc.py`
从 `config.py` 机械生成（86 项、按前缀分组、保留源码注释）。
注释为空的项**如实留空**，提示「源码里没写说明」。

**G5 CI**：`.github/workflows/ci.yml` 两个 job ——
`test`（pytest + check_spec + check_guide）与 `privacy`（敏感串扫描为 0）。

**脱敏脚本自己的泄露也修了**：第一版把真实姓名写在映射表里，等于换个地方泄露。
现在真实映射放 `harness/.privacy-map.json`（gitignored），仓库只留
`privacy-map.example.json`（占位符之间互相替换）。

### 2026-11-19 · G1 收尾两项 + G3 降级文档

**.env.example 完全对齐**：`harness/check_env_coverage.py` 从 49/86 补到 **86/86**。
补齐时只写「默认值」（从 `config.py` 机械取），不编造用途说明。
顺带标出两项历史遗留：`EMBEDDING_PRELOAD` / `RERANK_PRELOAD` **不是 Settings 字段**，
配了不生效 —— 保留但标注，不删（删了会让人以为没这回事）。

**冷启动文档** `docs/quickstart.md`：compose → `uv sync` → `.env` 最少三项 →
起服务 → `/health` 自检 → 写入（`/admin/ingest`）→ 召回（`/admin/recall`）→ 接 MCP，
最后六条「常见卡点」。

> 写这份文档时抓到自己一个错：原本写「`POST /admin/remember`」，
> 但代码里**没有这个接口** —— 写入走 MCP 的 `memory_remember` 或批量入口
> `/admin/ingest`。已改成实际存在的路径。

**降级文档** `docs/degradation.md`：LLM / embedding / Weaviate / rerank / Neo4j
五类，逐项写「挂掉后的表现 / 是否丢数据 / 恢复方式」。要点：

- 图库挂了最轻（派生层，可重建）
- 向量库或 embedding 挂了 → 召回 **503**（不是空结果，空结果会误导成「没这段记忆」）
- LLM 挂了 → 写入降级成原文卡，记忆变粗糙但不断

**G1 只剩一项**：admin 进 compose。这需要先有 `Dockerfile`（镜像化后端），
属「新增交付物」，比改文档重，留到下一轮或由你确认后再做。
