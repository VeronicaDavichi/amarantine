---
status: complete
created: 2026-11-19
priority: high
tags:
- privacy
- purge
- admin
created_at: 2026-11-19T06:07:17.166179328Z
updated_at: 2026-11-19T06:13:01.917410125Z
completed_at: 2026-11-19T06:13:01.917410125Z
transitions:
- status: complete
  at: 2026-11-19T06:13:01.917410125Z
---
# 050 命名空间清理入口（把导入过的聊天记录彻底删掉）

## Overview

**问题**：导入的聊天记录散在三处（SQLite 原文 / Weaviate 卡片 / Neo4j 图），
现在想「删掉某个 namespace 的全部数据」只能手写三种语句。实测已经手写过三次
（清理验证数据、重灌前清空、修规则后重建）。

用户 2026-11-19 提问后确认这不止是运维便利，还是**隐私要求**：
- 导错了群、导多了、或者不想留某段对话，必须能一键清干净
- 「删了」必须是**三处都删**，只删一处会留下对不上的残片

**现状（实测）**：

| 存储 | 该 namespace 的量（qq-505456846） |
|---|---|
| SQLite `raw_quote` | 9415 行 |
| Weaviate `MemoryEvent` | 4383 张 |
| Neo4j 节点/边 | 69628 / 78521（全部 namespace） |

另有一类**不在本项目管辖**的残留：qq-mcp 的 `.qq-mcp/plain/`（解密副本，
实测 **3.3 GB**）。它由外部工具生成，本项目只能提示用户清理，不能代删。

## Design

### 入口

| 形态 | 用途 |
|---|---|
| `GET /admin/namespaces` | 列出所有 namespace 与各自条数（先看清要删什么） |
| `POST /admin/namespaces/purge` | 清理指定 namespace；**默认 dry-run** |
| `harness/purge_namespace.py` | 无 HTTP 时用（容器内/脚本场景） |

### 安全约束（关键）

1. **必须显式确认**：请求体要带 `confirm`，值必须**等于** `"purge <namespace>"`。
   写错一个字就 400 —— 防止把 `--namespace` 打错时误删别的域。
2. **默认 dry-run**：不带 `apply=true` 只返回「将删多少」，不改数据。
3. **只删一个 namespace**：代码里不接受空字符串 / `*` / 通配；
   `njzq-dingtalk` 这类别的域绝不能被顺带清掉。
4. **删序固定**：图 → 卡片 → SQLite。
   先把派生层拆掉，再删事实源；反过来会出现「卡没了但图上还挂着节点」。
5. **返回逐项计数**：`{graph_nodes, graph_edges, cards, raw_quote, ingest_job, ...}`，
   删完再数一遍（`verify` 段），不一致要报出来。

### 不删什么

- `.env`、配置文件、`config/` 下的词典
- 其它 namespace 的任何数据
- qq-mcp 的 `.qq-mcp/plain/`（外部工具，只在输出里提示一句）

## Plan

- [x] T1 `memory/services/purge.py`（list / count / purge，dry-run 默认，删后复核）
- [x] T2 管理接口 `GET /admin/namespaces`、`POST /admin/namespaces/purge`
- [x] T3 CLI `harness/purge_namespace.py`（`--list` / 预演 / `--apply`）
- [x] T4 测试 9 条：字面校验 / 确认语 / 预演免确认 / 残留必报
- [x] T5 文档：user-manual 补一节（含 qq-mcp `plain/` 清理与三道闸说明）
- [x] T6 用后即删：`harness/clean_qq_plain.py` + 导出脚本默认清理（`--keep-plain` 保留）
- [x] T7 MCP 工具名按能力解析：`harness/mcp_client.mjs`（换实现只改配置）
- [x] T8 「我是谁」：`SELF_QQ` → uid → canonical + `GET /admin/whoami` + `mark_self.py`

## Test

| 编号 | 验收 | 手段 |
|---|---|---|
| A1 | dry-run 返回条数但数据不变 | 真库：`verify046b` 前后计数一致 |
| A2 | 确认语错误 → 400，且不删任何东西 | 接口测试 |
| A3 | apply 后三处都清空，其它 namespace 不变 | 真库：`verify046b` 清零、`qq-…` 计数不变 |
| A4 | 删后复核不一致要报错 | 单测（mock 计数） |

## Notes

- 关联：040（导入）、005（原文层）、021（清脏数据，当时也是手写脚本）
- **不做**：按时间范围/按群部分删除（那是另一个粒度，先做「整个 namespace」）
- 风险：误删。缓解 = 确认语 + dry-run + 只允许字面 namespace（无通配）

### 2026-11-19 · 实现与真库验证

**交付**

| 形态 | 用法 |
|---|---|
| CLI | `python3 harness/purge_namespace.py --list` / `--namespace X`（预演）/ `--apply` |
| HTTP | `GET /admin/namespaces`、`POST /admin/namespaces/purge`（`apply=false` 默认） |

**真库验证（verify046b）**

```
预演：cards 3 / graph_nodes 10 / raw_quote 3 / ingest_job 3 / embedding_index_meta 1
真删：cards 3 / graph_nodes 10 / raw_quote 3 / ingest_job 3 / embedding_index_meta 1
复核：干净 ✓
其它域不受影响：qq-505456846 仍 4383 张、work 仍 1 张
确认语写错（purge verify046c）：✗ 拒绝，未删任何东西
```

**过程中修的两个真问题**

1. **预演被确认语挡住** —— 原本 `_require_confirm` 在 dry-run 之前就校验，
   结果「看看会删多少」都做不到。改成**只有 `apply=True` 才校验**。
2. **相对路径解析错** —— `.env` 里是 `sqlite:///./data/memory.db`，
   按进程 CWD 解析会跑到 `harness/data/`，实测 `unable to open database file`。
   改成相对 `back_end/` 解析。

**顺带记录（不属于本 spec 管辖但用户在问）**：
`~/.qq-mcp/plain/` 是外部工具的解密副本，实测 **3.3 GB**（同一库多份、旧的不清）。
已在 user-manual 写清「原库还在，可直接 `rm -rf ~/.qq-mcp/plain`；`qqkey` 要留」。

### 2026-11-19 · T6 用后即删（用户要求：用过后就删掉）

**问题**：qq-mcp 每次读库都在 `~/.qq-mcp/plain/` 留**明文**副本，
累积实测 **3.3 GB / 197 个文件**（同一库多份、旧的不清）。靠人记得清 = 迟早忘。

**做法**

| 组件 | 行为 |
|---|---|
| `harness/clean_qq_plain.py` | 独立清理：默认预演，`--apply` 真删，`--older-than` 只删旧的 |
| `export_qq_incremental.py` | 导出结束**自动清理**；`--keep-plain` 保留 |

**三道保护**

1. 只删 `.plain.db` / `-wal` / `-shm` —— 不碰 `qqkey`（删了要重新 frida 抓）、不碰别的文件
2. **原库目录不在就拒绝删**（原库没了，副本就是唯一数据源）
3. 默认预演；删完打印释放多少 MB

**踩到的路径坑**：qq-mcp 是 Windows 侧 node 进程，`Path.home()` 在 WSL 里是
`/home/veronica`，而实际目录在 `/mnt/c/Users/EDY/.qq-mcp/plain` ——
第一次跑「将删 0 个文件」就是这个原因。现在按
`QQ_MCP_PLAIN_DIR` → `~/.qq-mcp/plain` → `/mnt/c/Users/*/.qq-mcp/plain` 顺序找。

**测试**：6 条（只选 plain 副本 / 原库缺失拒删 / older-than / apply 计数 / 预演不删 / 目录不存在）。

### 2026-11-19 · T7 工具名自动解析 + T8「我是谁」

**T7 换 MCP 实现不用改代码**

`harness/mcp_client.mjs`：能力 → 工具名别名表。脚本调用
`callCap("list_contacts", …)`，名字由别名表匹配；不匹配时报错**并列出实际可用工具**。

实测 `node harness/export_qq.mjs --list` 打印：

```
可用工具(18): list_qq_databases, capture_offline_key, verify_offline_key,
decrypt_qq_database, query_chat_messages, …, list_contacts, list_group_members, get_db_schema
```

**T8 我是谁**

配置 `SELF_QQ`（登录用的 QQ 号）→ 从权威成员表反查 uid（spec 043 的账号锚定）
→ 得出：

| 字段 | 实测值（qq 505456846） |
|---|---|
| uid | `u_7zrOQx_TiNIGvZcyFZipCA`（唯一） |
| 账号昵称 | **Veronica Davichi** |
| 群名片 | 19–20 个（`海嗣`、`苏苏大可爱`、整活名…） |
| 出现在 | 64 个群的成员表里 |

**踩到的坑**：第一版 `preferred_name` 从**群名片**里挑，挑成了「海嗣」
（同分取较短）。正确做法是 **canonical 取账号昵称**，群名片只当别名。
`mark_self.py` 现在会无条件把本人 Person 的 name 设为 canonical，
并写 `is_self=true`（Account 与 Person 各一份）。

接口：`GET /admin/whoami?namespace=…`（已验证返回上述字段）。
