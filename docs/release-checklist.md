# 发布检查清单（v0.1.0）

对照 [spec 049](../specs/049-open-source-release/README.md) 的五道门与 A1–A7。
**这张表只写「已验证 / 未验证」，不写期望。**

## 已验证（本仓库内可复现）

| 编号 | 验收 | 证据（命令 → 结果） |
|---|---|---|
| **G1** | `/health` 汇总四依赖 | `curl /health` → `degraded=false`，四依赖 ok（含毫秒） |
| **G1** | `compose` 含 admin | `docker compose config --services` → embedding / neo4j / weaviate / admin |
| **G1** | `.env.example` 与配置对齐 | `harness/check_env_coverage.py` → 94/94 完全对齐 |
| **G2** | 备份三处齐全 | `harness/backup_state.py` → memory.db + cards.jsonl（含向量）+ graph_*.jsonl + manifest |
| **G2 · A3** | 备份→清空→还原一致 | `verify046b`：3 卡 / 10 节点 / 24 边，还原后计数一致 |
| **G2 · A4** | 旧库能迁移 | 真实库 v0→v2，重跑为空（幂等），`raw_quote` 9415 行完好 |
| **G3** | 配置清单 | `harness/gen_config_doc.py` → 94 项分组文档 |
| **G3** | 降级路径写清 | `docs/degradation.md`（五类依赖逐一写表现/丢不丢/怎么恢复） |
| **G3** | 结构化日志 | `LOG_FORMAT=json` → 实测 4 行合法 JSON / 0 非法 |
| **G4** | 许可证 | `LICENSE` = MIT（`1c21a8d` 起就有） |
| **G4** | 依赖许可清单 | `docs/third-party-licenses.md`（59 包，无 GPL/AGPL 传染） |
| **G4** | 协作文档 | `CONTRIBUTING.md`、`SECURITY.md`、README 重写 |
| **G4 · A5** | 历史无真实语料 | `git log --all --name-only` 只命中两个 spec 的 README 路径；最大 blob 是 `uv.lock` |
| **G4** | 工作区脱敏 | `harness/scrub_privacy.py` → 0 处待替换 |
| **G5** | CI | `.github/workflows/ci.yml`（test + privacy 两个 job，7 步） |
| **G5** | CHANGELOG | 本文件 |
| **A2** | 文档命令存在且可启动 | `harness/check_doc_cmds.py --run` → 43 个脚本全部 ok |
| **A6** | **CI 在 GitHub 实际跑通** | run `44653037541`：`test` 40s（1643 passed / 13 skipped）+ `privacy` 4s，两 job 全绿 |
| **A7** | 静态全绿 | `bash harness/verify_all.sh --static` → 1656 passed / 50 spec / 文档一致 / 命令存在 |

## 未验证（需要你在一台干净机器上做）

| 编号 | 验收 | 为什么本机做不到 |
|---|---|---|
| **A1** | `docker compose up -d` → `/health` 四项 ok | 本机 Docker Hub 不可达（`dial tcp 199.59.148.222:443: i/o timeout`），拉不到 `python:3.12-slim`，**镜像从未构建过** |
| **A2（人工部分）** | 照 `docs/quickstart.md` 从 clone 走到第一次 recall | 本机已有数据与 .env，不是干净状态 |
| **tag** | `v0.1.0` | 按流程放在 A1 通过后 |

## A1 的具体做法

```bash
git clone <repo> amarantine && cd amarantine
docker compose up -d                      # 或先配镜像站：
#   docker build --build-arg BASE=<镜像站>/library/python:3.12-slim \
#                --build-arg UV_IMAGE=<镜像站>/astral-sh/uv:latest -t amarantine-admin:dev back_end
docker compose ps                         # 四个服务 healthy + admin running
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# 期望：status=ok，degraded=false，deps 四项 ok:true
```

通过后：

```bash
git tag -a v0.1.0 -m "v0.1.0: 首个可开源版本"
git push origin v0.1.0
```

## 已知限制（发布时不要隐藏）

- 关系层 precision 0.95 / recall 0.67；**行为通道停用**（数据不足）
- 召回 hit@5 = 0.50（最难的那批是纯闲聊卡）
- 单机单租户；`namespace` 不是鉴权边界；单 `ADMIN_TOKEN`；默认无 TLS
- 中文群聊优先，英文语料未验证
- 仓库不含任何语料；`data/`、`corpus/`、`backups/` 全部 gitignored
