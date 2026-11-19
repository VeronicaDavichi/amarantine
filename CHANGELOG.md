# 变更记录

本项目用 [LeanSpec](specs/) 记录设计决策，本文件只记**对使用者可见**的变化。
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。

## [Unreleased]

### 新增
- `docker-compose.yml` 增加 `admin` 服务（后端镜像 `back_end/Dockerfile`），
  一条 `docker compose up -d` 起全套依赖 + 后端
- `GET /health` 汇总四项依赖状态（`degraded` / `deps` / `down`），5 秒缓存
- 备份 / 还原：`harness/backup_state.py`（SQLite 在线备份 + 卡片 JSONL 含向量 + 图 JSONL）、
  `harness/restore_state.py`（按 namespace 还原并校验条数）
- SQLite schema 版本与迁移：`memory/services/schema.py`（`PRAGMA user_version`，
  有序幂等迁移，拒绝降级）
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

## [0.1.0] - 待发布

发布前置条件见 [spec 049](specs/049-open-source-release/README.md)：
README 重写、CI 首跑通过、`v0.1.0` 打 tag。

许可证：MIT（见 [LICENSE](LICENSE)）。
