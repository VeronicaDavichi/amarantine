---
status: in-progress
created: 2026-11-06
priority: medium
tags:
- ingest
- tooling
- draft
created_at: 2026-11-06T08:37:41.289140704Z
updated_at: 2026-11-09T03:45:35.565228409Z
transitions:
- status: planned
  at: 2026-11-09T03:45:35.494506305Z
- status: in-progress
  at: 2026-11-09T03:45:35.565228409Z
---
# 历史聊天记录导入工具（草稿）

> **Status**: draft · **Priority**: medium · **Created**: 2026-11-06

## Overview

**问题**：现在是「一次性临时脚本 + 手工拼批次」，每灌一个群就要重写一遍，
踩过的坑散落在对话里，下次还会再踩。

已实际踩过的坑（全部有记录）：

| # | 坑 | 后果 |
|---|---|---|
| 1 | `contentType=102`（链接分享）只取标题，丢了 `source_url` | GitLab 地址一个字都没进库 |
| 2 | 无重叠窗口（15 条/轮） | 指代类卡片读不懂（70/793 张） |
| 3 | 批次太大（53 轮/批） | 撞 `MAX_EVENTS=40`，尾部事件静默丢失 32 条（036） |
| 4 | 重复灌同一段 | `quote_id` 非内容哈希 → 原文重复；卡片措辞一变就是新卡 |
| 5 | 敏感信息（身份证号/明文凭据）无统一闸门 | 靠临时脚本逐个判断，容易漏 |
| 6 | `date_start` 一律写成入库日 | 时间过滤失效（已收编，见下「事件时间」） |
| 7 | 摘要里的「就是」被当等价声明 | 脏 `ALIAS_OF` 边（038） |

**目标（粗）**：把上面这些固化成工具的一等公民，而不是操作者的记忆。

## Design

草案，**未定**。目前只有方向：

```
数据源适配器（DingTalk / 文件 / 其他）
   ↓
规范化（contentType → text，含 link 的 source_url）
   ↓
隐私闸门（证件号 / 手机 / 账号口令 / 2FA）→ 拦下的单独报告
   ↓
切片器（窗口大小 + 重叠 stride）← 与 039 的关系抽取共用
   ↓
提交（dedupe_key 幂等 + 断点续跑 + 进度持久化）
   ↓
校验（条数守恒 / 抽检 / 召回自测）
```

待定的关键问题：

- 窗口到底按「条数」还是「token」？
- 重叠产卡的重复怎么处理（锚点去重 / 不重叠 / 允许重复）？
- 增量导入用什么做水位（时间戳？mid？）
- 关系抽取（039）内置进工具，还是工具只管灌、抽取走服务端？

### 事件时间：`date_start` 必须来自源消息，不许默认今天

> **收编记录**：这一条原在 README「已知缺陷」里，计划单独立项；改为**并入本 spec**——
> 批量导入是唯一能修它的入口，单独做个工具去补时间反而绕远。

**现象**：036 重灌后 229 张卡的 `date_start` **全是入库日 `2026-11-06`**，与真实发生时间无关。

**根因**：`IngestRequest` 没有事件时间入参，写入路径回落到「今天」；切分出的每条事件也不携带时间。

**契约**

| 环节 | 要求 |
|---|---|
| 归一化 | 源消息的 `msgTime` / `createTime` → `event_time`（保留原时区，落库存 `date`） |
| 切片 | 一条消息切出的多张卡**继承该消息时间**；跨消息的卡取 `[min, max]`，`date_start = min` |
| 缺时间 | 显式写 `null` + 报告行，**禁止填充今天** |
| 相对时间（「昨天」） | 以批次基准日换算，卡上标 `time_source = inferred` |
| 幂等 | `dedupe_key` 不含时间 —— 改时间只更新属性，不产新卡 |

**迁移**：现有 229 张卡**不可反推**（原文没存消息时间）→ 报告里逐条标「不可修复」，靠重灌覆盖。

**不变行为**：`date_start` / `date_end` 类型仍是 `date`；`event_id` 生成规则不动。

## Plan

- [x] T1 盘点现有脚本与踩坑，写成「输入契约」
- [ ] T2 定窗口与重叠策略（需 039 的实验结果）
- [x] T3 隐私闸门（规则可配 + 命中报告）
- [x] T4 幂等与断点（job 表 + 水位）
- [x] T5 校验与报告（条数守恒、抽检、召回自测）
- [x] T6 收编现有临时脚本，删掉一次性代码
- [x] T7 事件时间贯通（归一化 `msgTime` → 切片继承 → 落库 `date_start`/`date_end`）
- [ ] T8 缺时间显式 `null` + 报告行；`time_source` 标记 `exact`/`inferred`

## Test

- [x] A1 同一份语料跑两次，结果幂等（条数、卡片数不变）
- [ ] A2 中途 kill 后重跑，从断点继续，不重复产卡
- [x] A3 含身份证号/口令的样本被拦下并出现在报告里
- [x] A4 链接分享的 URL 能进库并可检索（以 GitLab 地址为用例）
- [ ] A5 灌完自动跑「5 个已知问题的召回自测」，输出命中率
- [x] A6 带源消息时间的样本：`date_start` 等于源消息日，不等于导入日
- [ ] A7 无时间样本：`date_start` 为 `null` 且出现在报告，**不是今天**

## Notes

### 与现有 spec 的关系

| spec | 关系 |
|---|---|
| 036 切分截断 | 工具的窗口大小必须避开 40 上限，否则重现丢数据 |
| 038 称呼归一 | 工具灌完，图上称呼节点是否合并由 038 决定 |
| 039 关系谓词 | 窗口策略两边共用；关系抽取是否内置待定 |
| 041 指称层 | 时间不影响指称，但 `time_source` 会影响「这条什么时候说的」类查询 |

### 收编记录

**2026-11-06 · 并入「`date_start` 取入库日」缺陷**

- 来源：根 README「已知缺陷」第 1 条（原计划单独立项）
- 理由：该缺陷只在批量导入路径上产生、也只能在导入路径上修；040 是导入工具的
  唯一归属，另立 spec 会让「灌数据」的知识再分散一处
- 顺带收编 036 进展记录里的遗留项「229 张卡 `date_start` 全是 `2026-11-06`」
- 影响：040 从「纯 tooling」变成「tooling + 一个数据正确性缺陷」，优先级
  **建议从 medium 提到 high**（frontmatter 待 CLI 可用时用 `update` 改，不手改）

### 进展记录

**2026-11-07 · 首次全量灌库（QQ 真实语料）**

- 环境：Weaviate 1.28.4 + Neo4j 5.26（docker）+ 自建 bge-m3 GPU 服务（替代 TEI 容器）
  + admin HTTP；LLM 走本机网关 `deepseek-v4-flash`
- 结果：**12 个群 / 606 张卡 / 0 失败 / `dropped_events` 全 0**；
  图侧 Event 611 · Person 49 · Entity 107 · relations 3659
- 召回抽检：「欧盟和联合国 哪个更强」→ 0.90 / 0.895 / 0.89 命中相关卡
- **踩到的新坑（已修进工具）**：整会话一次提交时，506 轮的大群让 LLM 调用超时，
  降级成**一张 17k 字的原文卡**（`_looks_like_echo` 只判「条数/长度」，
  判不出「模型超时返回原文」）。`seed_qq.py` 因此加了 `--window N`：
  按 N 条切窗提交，每窗独立 `dedupe_key`。补灌后 0 张超长卡。
- 仍未解决：`date_start` 依旧是入库日（本轮 606 张全是 `2026-11-07`）——
  即本 spec 的「事件时间」一节，尚未实现

**2026-11-07 · mcp-qq 接入实测（管道通到 ingest 边界）**

- `mcp-qq` 这个名字在 GitHub 上是**空壳**（`shuakami/mcp-qq`，size 0、无代码）；
  真正能用的是 [`@powercess/qq-mcp`](https://github.com/powercess/qq-mcp)（v0.3.0，
  离线解 NTQQ 的 SQLCipher 库，19 个工具）
- 本机有 3 个账号的库，**只有登录中的 2751722562 能解** —— 口令按账号推导，
  不在内存里就拿不到（505456846 那个 69MB 的历史库解不开）
- 拿到的数据：12 群 / 15 好友 / 4247 条群消息 + 53 条私聊（2023-10 → 2026-11）
- 新增两个工具脚本（本 spec 的第一版实现，替代一次性临时脚本）：
  - `harness/export_qq.mjs`：库 → corpus JSON（与 `seed_dingtalk` 同构）
  - `harness/seed_qq.py`：corpus → `POST /admin/ingest`（`source=qq`，`dedupe_key` 前缀 `qq:`）
- 语料 12 个会话 / **1993 条 turn**，落在 `harness/corpus/qq/`（**已 gitignore**：
  真实聊天记录不能入库）
- 契约验证（TestClient + 假 store，不需要中间件）：`202` → `done`，
  `dropped_events=0`、`split_rounds=1`
- **卡住**：本机没有 docker（WSL 无、Windows 无 Docker Desktop）、没有 `back_end/.env`、
  没有 `models/` → Weaviate / Neo4j / embedding 都起不来，真写卡未验
- 待办：栈起来后跑 `seed_qq.py --corpus-dir harness/corpus/qq`；
  要历史账号的库得先登录那个账号再抓一次口令

### 明确不做（草稿阶段就先划掉）

- 不做通用的「任意聊天软件」适配器，先只覆盖 DingTalk 本地库
- 不做实时同步，只做历史批量导入
- 不做前端界面，命令行 + 报告即可

### 未定

- 工具放 `harness/` 还是单开 `tools/`？
- 是否需要独立的 SQLite 表记录「导入水位 + 批次清单」？
- 与 `/admin/ingest` 的关系：继续走 HTTP，还是直接调 service 层？

### 2026-11-09 · 统一入口落地（工具 + 事件时间）

**已交付**

| 文件 | 内容 |
|---|---|
| `back_end/src/memory/services/importer.py` | 规范化 / 隐私闸门 / 切窗 / 幂等键 / 事件时间映射（纯函数，可单测） |
| `harness/import_chat.py` | 统一 CLI：`--window` / `--resume` / `--identity` / `--dry-run` / 报告 |
| `back_end/src/memory/services/raw_quote.py` | `run_ingest` 用 `assign_event_dates` 给每张卡传 `date_hint` |
| `tests/test_importer.py` | 18 条 |

**已验证**

| 验收 | 结果 | 证据 |
|---|---|---|
| A1 幂等 | ✅ | 同语料开 `--resume` 二次跑：`片 0（跳过 1）` |
| A3 隐私闸门 | ✅ | 假身份证/验证码/图片三类被拦，报告里带原因 |
| A4 链接 URL | ✅ | `gitlab http://10.2.51.11:7060/wangjie/aml-scripts` 进文本 |
| A6 事件时间 | ✅ | 真库卡 `date_start=2026-10-26`（源消息日），不是入库日 |

**未完成**

| 项 | 原因 |
|---|---|
| A2 中途 kill 续跑 | 续跑逻辑已实现（按 done 的 dedupe_key 跳过），但没做「kill 后重跑」的实测 |
| A5 召回自测 | 工具只出灌库报告，没有内置 5 个已知问题的自测 |
| A7 无时间 → `null` | **卡住了**：`EventCard.date_start` 是必填 `date`，`resolve_date_hint(None)` 回落到今天。要真正写 `null` 得让字段可空，并同步 Weaviate 属性、Neo4j `AT` 边、日期过滤查询 —— 是一次跨三存储的改动，建议单开 |
| T2 重叠策略 | 窗口（30 条）已定；重叠仍等 039 的金标结果 |
| T8 `time_source` | 未实现，与 A7 同一处改动 |

**判据说明**：事件→源消息日期用「汉字 4-gram 交集 ≥2」匹配，
摘要被改写时仍能对上；对不上就是 `None`（当前会回落今天，见 A7）。
