# 变更记录

本项目用 [LeanSpec](specs/) 记录设计决策，本文件只记**对使用者可见**的变化。
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。

## [0.1.0] - 2026-11-20

### 新增
- `docker-compose.yml` 增加 `admin` 服务（后端镜像 `back_end/Dockerfile`），
  一条 `docker compose up -d` 起全套依赖 + 后端
- `GET /health` 汇总四项依赖状态（`degraded` / `deps` / `down`），5 秒缓存
- 备份 / 还原：`harness/backup_state.py`（SQLite 在线备份 + 卡片 JSONL 含向量 + 图 JSONL）、
  `harness/restore_state.py`（按 namespace 还原并校验条数）
- SQLite schema 版本与迁移：`memory/services/schema.py`（`PRAGMA user_version`，
  有序幂等迁移，拒绝降级）
- `memory_whoami` MCP 工具 + `GET /admin/whoami`：回答「我是谁」与「我与别人的关系」
- 「我」参与关系层：账号昵称 / 群名片 / 第一人称代词统一归到 `acct_<self_uid>`
- `harness/rebind_self_edges.py`（已有边重指）、`harness/scan_self_mentions.py`（看多少卡提到我）
- 文档：`docs/quickstart.md`（冷启动）、`docs/degradation.md`（依赖挂掉会怎样）、
  `docs/config-reference.md`（86 项配置，自动生成）
- 协作文件：`CONTRIBUTING.md`、`SECURITY.md`、`.github/workflows/ci.yml`
- 实体抑制：`entity_suppress` 表 + `GET /admin/entities/suppress`
  （人名碎片不再被建成实体；写图侧也过滤，重放不复活）
- emoji 昵称派生别名：`harness/emoji_alias_scan.py`（`便携式🦆🍗 → 便携式`，
  候选须在语料出现 ≥3 次才采纳）

### 变更
- **营销内容不再参与关系抽取**（营销只用于「有没有关注的子项」）
- 关系层只保留陈述通道；行为通道停用（数据凑不齐，映射表保留）
- 空名字/连续串切片不再当人名（`新机子瓦一兔子` 这类谐音不拆）

### 修复
- 别名扩展召回从未生效（预算被主召回耗尽）→ 改「从总预算预留」
- 会话内 `merge_into` 从未生效（提示词没把已有卡给模型）→ 注入 + 白名单
- 越界 `source_indices` 静默丢卡 → 计数 + 不建卡

### 安全 / 隐私
- 工作区脱敏（真实姓名 / 内网 IP / QQ 号），映射表外置且不入库
- `data/`、`corpus/`、`backups/`、`docs/labeling-data.js`、`.env`、
  `harness/.privacy-map.json` 全部 gitignored

---

## 发布前检查（全部通过）

| 项 | 状态 | 证据 |
|---|---|---|
| G1 可安装 | ✅ | 镜像 1.15GB 构建成功；`compose up -d admin` → healthy；`/health` 四项 ok |
| G2 数据安全 | ✅ | 备份含向量；A3 还原一致；A4 库 v0→v2 幂等迁移 |
| G3 可运维 | ✅ | 配置 94 项；降级五类；`LOG_FORMAT=json` 实测合法 |
| G4 合规 | ✅ | LICENSE MIT；依赖 59 包无 GPL；A5 历史干净；脱敏 0 残留 |
| G5 可验证 | ✅ | CI 两 job 全绿；A7 `verify_all --static` 全绿 |
| A1 | ✅ | 容器全链路：写入 1 卡 → 召回命中 → purge 干净 |
| A2 | ✅ | `check_doc_cmds.py --run`：44 个脚本存在且可启动 |
| A3–A7 | ✅ | 见 [发布检查清单](docs/release-checklist.md) |

**已知限制（不隐藏）**

- 关系层 precision 0.95 / recall 0.67；行为通道停用（数据不足）
- 召回 hit@5 = 0.50
- 单机单租户；`namespace` 不是鉴权边界；单 `ADMIN_TOKEN`；默认无 TLS
- 中文群聊优先，英文语料未验证

许可证：MIT（见 [LICENSE](LICENSE)）。
