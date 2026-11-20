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
| **A2** | 文档命令存在且可启动 | `harness/check_doc_cmds.py --run` → 44 个脚本全部 ok |
| **A6** | **CI 在 GitHub 实际跑通** | run `44653037541`：`test` 40s（1643 passed / 13 skipped）+ `privacy` 4s，两 job 全绿 |
| **A1** | **镜像构建 + 容器全链路** | 镜像 1.15GB 构建成功；`compose up -d admin` → `healthy`；`/health` 四项 ok、`degraded=false`；写入 1 卡 → 召回命中 `tr_6f693c9cd357` → purge 干净（`a1-smoke`） |
| **A7** | 静态全绿 | `bash harness/verify_all.sh --static` → 1656 passed / 50 spec / 文档一致 / 命令存在 |

### A1 的一个限定（必须说清）

A1 的构建用的是**镜像站**（`docker.m.daocloud.io`），不是 Docker Hub 直连：
本机 `registry-1.docker.io` 不可达（`dial tcp 199.59.148.222:443: i/o timeout`）。

因此准确表述是：**「`Dockerfile` + `docker-compose.yml` 在换基础镜像源后可构建、
可启动、全链路可用」已实测**；「Docker Hub 直连路径」未在本机跑过
（差异仅为基础镜像来源，与代码无关）。

`docker-compose.yml` 的 `BASE` 就是这个开关，默认 `python:3.12-slim`：

```bash
DOCKER_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim docker compose up -d
```

## 未验证（需要你在一台干净机器上做）

| 编号 | 验收 | 为什么本机做不到 |
|---|---|---|
| **A2（人工部分）** | 照 `docs/quickstart.md` 从 clone 走到第一次 recall | 本机已有数据与 .env，不是干净状态 |
| **A1（Docker Hub 直连）** | 不配 `DOCKER_BASE_IMAGE` 直接 `docker compose up -d` | 本机拉不到 Docker Hub；且没有可上传镜像的 registry 账户 |

## A1 的复现命令（已验证，经镜像站）

```bash
git clone <repo> amarantine && cd amarantine
DOCKER_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  docker compose up -d                    # 含 admin，首次 3–5 分钟
docker compose ps                         # 五个服务 healthy/running
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
# 期望：status=ok，degraded=false，deps 四项 ok:true
```

## 已知限制（发布时不要隐藏）

- 关系层 precision 0.95 / recall 0.67；**行为通道停用**（数据不足）
- 召回 hit@5 = 0.50（最难的那批是纯闲聊卡）
- 单机单租户；`namespace` 不是鉴权边界；单 `ADMIN_TOKEN`；默认无 TLS
- 中文群聊优先，英文语料未验证
- 仓库不含任何语料；`data/`、`corpus/`、`backups/` 全部 gitignored
