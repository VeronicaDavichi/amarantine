# 安全策略

## 报告漏洞

**不要开公开 issue。** 用 GitHub 的 *Report a vulnerability*
（Security → Advisories → Report a vulnerability）私下报，或邮件联系仓库维护者。

请附：影响面（哪条路径）、复现方式、是否需要特殊配置。
我们会在确认后修复并在 Advisory 里致谢（除非你要求匿名）。

## 安全模型（必读）

这不是一个多用户服务，**默认信任本机**：

| 维度 | 现状 |
|---|---|
| 部署形态 | 单机、单租户；隔离粒度是 `namespace`（**不是**鉴权边界） |
| 管理面鉴权 | 一个 `ADMIN_TOKEN`（Bearer），**没有角色分级** |
| 传输 | 默认 `127.0.0.1` HTTP；**不内置 TLS** |
| 存储 | SQLite + Weaviate + Neo4j，均未加密 |

**因此**：不要把管理端口直接暴露到公网。要远程访问请放在
反向代理 + TLS + 认证之后，或用 SSH 隧道。

## 敏感数据

这套服务**天然会处理隐私数据**（聊天记录、人名、账号）。

- 仓库**不内置任何语料**：`data/`、`corpus/`、`backups/`、`docs/labeling-data.js`
  与 `.env` 都在 `.gitignore` 里
- 备份文件含真实内容 —— `backups/` 已 gitignore，**,但导出的备份请自行加密保管**
- 提交前跑 `python3 harness/scrub_privacy.py`（真实映射放
  `harness/.privacy-map.json`，不入库）

## 密钥

| 密钥 | 存放 |
|---|---|
| `ADMIN_TOKEN` | `back_end/.env`（gitignored） |
| LLM / embedding API key | 同上 |
| Neo4j 口令 | `docker-compose.yml` 里的默认值**仅供本地**，部署时改 |

**已知弱默认**：`docker-compose.yml` 里 `NEO4J_AUTH=neo4j/password`。
本地开发可接受；对外提供服务必须改，并在 `.env` 里同步 `NEO4J_PASSWORD`。

## 外部依赖带来的风险

| 依赖 | 风险 | 说明 |
|---|---|---|
| LLM 网关 | **聊天内容会发到该网关** | 自建/可信网关；不要用不明来源的代理 |
| `nbnhhsh` 公开 API | 只发**缩写字符串**（如 `lysk`），不发上下文 | 默认关闭（`ABBR_NBNHHHSH_ENABLED=false`） |
| embedding / rerank | 本地服务，不出网 | 用镜像自带的模型 |
| MCP 客户端 | 由客户端决定把什么写进记忆 | 记忆里可能出现你不希望留存的内容，用 `memory_forget` 删 |

## 已知限制（不是漏洞，但要知道）

- 关系层/实体抽取会**猜错**（precision 0.95 / recall 0.67）；不要把抽取结果当事实
- `namespace` 相同即互相可见：多用户共用一台实例时，必须各自用不同 namespace
- 没有速率限制：`/admin/recall` 被高频调用会打满 CPU
