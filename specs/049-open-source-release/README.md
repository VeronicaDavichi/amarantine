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
- 交付侧：一键部署、备份恢复、版本号、合规材料（详见 G1–G5；LICENSE 已有 MIT）
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
- [x] 冷启动文档 `docs/quickstart.md` + `harness/check_doc_cmds.py`（文档里的命令机械核对）
- [x] A2 可复现性：文档引用 43 个脚本**全部存在且 `--help` 可启动**（`--run` 实测）

### G2 数据安全（不丢、可迁）

- [x] 备份脚本：`harness/backup_state.py`（SQLite 在线备份 + 卡片 JSONL + 图 JSONL + manifest）
- [x] 恢复脚本 `harness/restore_state.py`（SQLite 按 namespace + 卡片带向量 + 图 MERGE）
- [x] schema 版本迁移：`memory/services/schema.py`（PRAGMA user_version + 有序幂等迁移）
- [x] 文档写明三处一起备份（脚本 docstring + 本节实测数字）

### G3 可运维

- [x] 配置清单 `docs/config-reference.md`（`harness/gen_config_doc.py` 生成，86 项分组 + 默认值 + 源码注释）
- [x] 结构化日志：`LOG_FORMAT=json`（一行一对象，业务字段可直接 jq；默认 text 不变）
- [x] 降级行为 `docs/degradation.md`（五类依赖逐一写「谁挂 → 什么坏 → 怎么恢复」）

### G4 可协作（开源合规）

- [x] 第三方依赖许可证清单 `docs/third-party-licenses.md`
      - nbnhhsh（Apache-2.0）已在 README 标注
      - bge-m3 / bge-reranker-v2-m3 的许可证与再分发条件需确认
- [x] README 重写完成（问题/30秒/实测数字/设计/文档索引/限制；许可节已写 MIT）
- [x] `CONTRIBUTING.md`（环境 / 提交前检查 / spec 规矩 / 隐私红线 / PR 要求）
- [x] `SECURITY.md`（私报渠道 + 安全模型 + 密钥 + 已知弱默认 + 已知限制）
- [x] 隐私说明：`data/`、`corpus/`、`backups/`、`docs/labeling-data.js` 均已在 `.gitignore`

### G5 可验证（可持续）

- [x] GitHub Actions `.github/workflows/ci.yml`（test + privacy 两个 job）
- [~] `CHANGELOG.md` 已写；`v0.1.0` tag 待发布时打
- [ ] 发布前最后一遍：`bash harness/verify_all.sh --static` 全绿

## Plan

- [ ] P1 G1 可安装：compose 全套 + `/health` 汇总 + 首启自建表 + `.env.example` + 冷启动文档
- [ ] P2 G2 数据安全：备份 / 恢复 / schema 版本迁移 / 三处状态一起备份的说明
- [x] 结构化日志：`LOG_FORMAT=json`（一行一对象，业务字段可直接 jq；默认 text 不变）
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
本 spec 不假设；G4 的**第三方依赖**许可清单仍需逐项核对（见 docs/third-party-licenses.md）。

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

### 2026-11-19 · G1/G2 验收记录（浓缩）

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



### 2026-11-19 · G1：admin 进 compose（构建未验证）+ CHANGELOG

**交付**

- `back_end/Dockerfile`：`python:3.12-slim` + `uv sync --frozen --no-dev`，
  非 root（uid 10001），`HEALTHCHECK` 用 `/health?deps=0`（只探进程）
- `docker-compose.yml` 增加 `admin` 服务：
  - `env_file: ./back_end/.env`，但**用 `environment:` 覆盖四个依赖地址** ——
    `.env` 里写的是 `127.0.0.1`（宿主视角），容器里那指向容器自己
  - `./back_end/data:/app/data` 落卷，否则容器重建记忆就没了
  - `depends_on` 用 `service_healthy`；rerank 有 profile，故意不设强依赖
- `docker compose config --quiet` 通过，`--services` 列出 `admin`

**为什么没验证镜像构建（诚实记录）**

本机 Docker Hub 不可达且没配镜像加速：

```
failed to resolve source metadata for docker.io/library/python:3.12-slim:
  dial tcp 199.59.148.222:443: i/o timeout
```

所以 Dockerfile 改成**基础镜像可覆盖**：

```bash
docker build --build-arg BASE=<镜像站>/library/python:3.12-slim \
             --build-arg UV_IMAGE=<镜像站>/astral-sh/uv:latest .
```

**A1（干净环境 `compose up` 起全套）在本机无法验证** —— 需要一台能拉
Docker Hub（或配置了镜像站）的机器。这条留作发布前的人工验收项。

**CHANGELOG.md** 已写：`Unreleased` 段按「新增 / 变更 / 修复 / 安全」分类，
条目都来自真实 spec 与提交；`0.1.0` 段留占位，发布时把 Unreleased 内容
挪过去并打 tag。

### 2026-11-19 · README 重写

从 196 行的「开发中 WIP」公告改成面向使用者的说明（165 行）：

| 旧版问题 | 新版 |
|---|---|
| 开头一大段 WIP 警告 + 过时数字（1262 测试 / 44 spec / 2 项未开工） | 开门见山「这是什么」+ 架构图 |
| 缺少「解决什么问题」 | 五行表格：问题 → 做法 |
| 快速开始混着 WSL/Windows 细节 | 两行 `compose up` + `/health`，细节跳 `quickstart.md` |
| 特性列表像功能清单 | **实测数字表**（1613 passed / hit@5 0.50 / P95 299ms / precision 0.95 / recall 0.67） |
| 已知限制里混着已修好的项 | 分成「取舍（不是 bug）」与「待办」 |
| 文档散落 | 统一索引表（quickstart / degradation / config / manual / specs） |

**刻意保留的两句实话**：

- 「召回 hit@5 只有一半，这是最真实的短板」
- 「实测数字是某次快照，不是承诺」

**英文段**只保留最小可用版（这是什么 / 快速开始 / 数字 / 文档 / 许可），
不逐段翻译 —— 维护两份等长说明书必然走样。

### 2026-11-19 · 更正：LICENSE 一直是 MIT（我判断错了）

**事实**：仓库根目录 `LICENSE` 从 `1c21a8d` 起就是 MIT
（Copyright 2026 VeronicaDavichi），不是「待选定」。

**我的错**：前几轮把这条写成「LICENSE 待你拍板」，还建议改用 Apache-2.0 ——
**没有先去看仓库里有没有**。用户指出后才查证。

**教训**：清单里凡是带「（需确认）」的条目，必须**当场查证**再写状态；
写「待定」比写「已确认」省事，但会让用户以为要去决策一件早就定好的事。

已修正：README / CHANGELOG 的许可节、本条 checklist。

**MIT 的合规义务**（比 Apache-2.0 简单）：

- 保留版权声明与许可全文（本仓库 `LICENSE` 已在根目录，符合）
- 分发二进制时同样要带上许可文本
- **没有**专利授权条款、没有商标条款 —— 这是 MIT 与 Apache-2.0 的实质差别

### 2026-11-20 · G3 结构化日志（JSON 行）

`LOG_FORMAT=json` → 日志一行一对象；默认 `text`（人读）**行为不变**。

实测（`LOG_FILE=/tmp/json-test.log LOG_FORMAT=json` 起 admin）：

```
{"ts": "2026-11-20T00:38:48+00:00", "level": "INFO", "logger": "memory.admin",
 "msg": "admin 启动 127.0.0.1:8765"}
{"ts": "...", "level": "INFO", "logger": "httpx",
 "msg": "HTTP Request: GET http://127.0.0.1:8081/... 200 OK"}
合法 JSON 行 4 / 非法 0
```

设计要点：

- `ts` 用 **UTC ISO**（与代码里其他时间戳同口径）；要本地时间在采集端换
- 业务字段通过 `logger.info("…", extra={"event_id": …})` 自动带上，
  不用把上下文拼进 msg
- `LogRecord` 自带字段（filename/lineno/process/thread…）**不**当业务字段输出
- 不可序列化的 extra 退化成 `repr`，不让日志本身抛异常
- `ensure_ascii=False`：中文不转成 `\uXXXX`（否则 grep 中文会失效）

**G3 至此完成**：配置清单 ✅ + 降级文档 ✅ + 结构化日志 ✅。

### 2026-11-20 · A2 护栏：文档里的命令必须真实存在

早期 quickstart 写过 `POST /admin/remember`（接口不存在），用户照抄会失败。
为让这类问题**机械暴露**，新增 `harness/check_doc_cmds.py`：

| 模式 | 做什么 | 用途 |
|---|---|---|
| 默认 | 扫 `README/CONTRIBUTING/SECURITY/CHANGELOG/docs/*.md` 里的 `harness/*.py\|mjs\|sh`，检查文件存在 | CI（秒级） |
| `--run` | 逐个跑 `--help`（部分脚本用只读参数） | 发布前人工验收 |

**第一次跑就抓到 4 个问题**

1. `docs/ADR-001` 与 `docs/P0-task-plan.md` 写的是 `harness/check-guide.py`，
   真实文件名是 `check_guide.py`（下划线）—— 已修
2. `harness/embed_server.py` / `rerank_server.py` 在模块顶层 `import torch`，
   没装 torch 时 `--help` 直接 `ModuleNotFoundError` —— 已改成 `--help` 提前退出
   （文档让人先看用法，缺可选依赖不该像装错了）
3. 修的过程中还踩到 `NameError: sys` —— 早退代码用了 `sys` 但没导入，已补
4. `harness/export_qq_members.mjs` 没有 `--help`，缺参数时报栈 —— 已补用法

**现在的结果**

```
OK: 文档引用的 43 个 harness 脚本全部存在
OK: 文档引用的 43 个 harness 脚本全部可启动
```

已接进 CI（`test` job 新增一步）与 `verify_all.sh --static`（第 3b 步）。

**A2 的边界（诚实）**：这只证明「命令存在且能启动」。
真正的「照文档从零走一遍」仍需在干净环境人工做（与 A1 一起）。

### 2026-11-20 · 发布检查清单 + CHANGELOG 收口

**`docs/release-checklist.md`**：把 A1–A7 逐条写成「已验证 / 未验证」，
已验证的带**可复现命令 → 结果**，未验证的写清「为什么本机做不到」。

**CHANGELOG**：`Unreleased` 收进 `[0.1.0] - 待发布（等 A1）`，
末尾加「发布前检查」表。tag 仍未打 —— 按流程放在 A1 通过后。

**又抓到一次隐私泄漏（值得记）**

```
harness/scrub_privacy.py → 将替换 15 处，涉及 6 个文件：'505456846' × 15
  back_end/tests/test_050_whoami.py / docs/user-manual.md /
  harness/merge_self_persons.py / harness/rebind_self_edges.py /
  harness/scan_self_mentions.py / specs/050-namespace-purge/README.md
```

来源：**我自己**这几轮写文档与测试时，把真实的 QQ 号与 namespace
（`505456846` / `qq-505456846`）当示例抄了进去。
`--apply` 已清干净（替换成 `10000000001`），测试仍 1656 passed。

> 这条正好证明 CI 的 `privacy` job 有存在价值：靠人自觉，
> 每隔几轮就会漏一次。写完就扫一遍是最低成本的护栏。

**A1 仍是唯一硬阻塞**：本机 Docker Hub 不可达，镜像从未构建过。
清单里给了「配镜像站」的替代命令。

### 2026-11-19/20 · 其余门的验收（浓缩，细节见 docs/release-checklist.md）

| 门 | 关键证据 |
|---|---|
| G1 | `/health` 四依赖 ok + `degraded=false`；`.env.example` 94/94 对齐 |
| G2 | 备份含向量；A3 演练（verify046b 3 卡/10 节点/24 边）还原一致；A4 真实库 v0→v2 幂等 |
| G3 | 配置清单 94 项；降级文档五类依赖；`LOG_FORMAT=json` 实测 4 行合法 JSON |
| G4 | LICENSE MIT；依赖许可 59 包无 GPL；CONTRIBUTING/SECURITY/README；A5 历史干净 |
| G5 | CI 两 job；CHANGELOG；A7 `verify_all --static` 全绿（1656 passed / 50 spec） |
| A2 | `check_doc_cmds.py --run`：44 个脚本存在且可启动 |
