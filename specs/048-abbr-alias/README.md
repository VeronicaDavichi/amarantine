---
status: in-progress
created: 2026-11-13
priority: high
tags:
- alias
- abbr
- recall
- nbnhhsh
created_at: 2026-11-13T04:10:36.945358509Z
updated_at: 2026-11-13T04:17:12.590244091Z
transitions:
- status: in-progress
  at: 2026-11-13T04:17:12.590244091Z
---
# 拼音首字母缩写与别名展开

> **Status**: planned · **Priority**: high · **Created**: 2026-11-13

## Overview

**现象**：中文社群大量用**拼音首字母缩写**指代，避开全名（避审核、避搜索、圈内黑话）：

```
lysk = 恋与深空      nn = 暖暖      mz = 母猪（氪金母猪）
dz   = 叠纸/地址     wxnn = 无限暖暖  synn = 闪耀暖暖
```

实测群 `876081394`（Lo 群）：

```
[11-04 15:01] 之前nn老爱出黑丝啥的，lysk不是还出项圈周边
[11-09 14:11] 最大的问题不是在lysk上吗
[11-09 19:13] 我抵制lysk
```

**问题**：记忆里 `lysk` 和 `恋与深空` **是断开的**（raw 各 3 条 / 1 条，卡片各 1 张 / 5 张）。
搜「恋与深空」召回不到 `lysk` 那张卡。

**候选来源**：[itorr/nbnhhsh](https://github.com/itorr/nbnhhsh) 的公开 API。

```http
POST https://lab.magiconch.com/api/nbnhhsh/guess
{"text":"lysk"}

→ {"name":"lysk","trans":["恋与深空","老姚烧烤","老用手抠"]}
```

实测它**只给候选、不给唯一答案**，必须用本地语料消歧。

## Requirements

- **REQ-1** **WHEN** 语料里出现 2~6 位独立字母 token **THEN** SHALL 调 nbnhhsh 取候选并落本地缓存，
  同一 token 不重复请求。
- **REQ-2** **WHEN** 候选有多个 **THEN** SHALL 用**本地语料共现**打分：
  候选全称在该群/该会话出现次数、与缩写的时间距离、是否命中已有实体名。
- **REQ-3** **WHEN** 最高分明显领先（≥2 倍且 ≥2 次共现）**THEN** SHALL 建
  `(:Alias "lysk")-[:SAME_SENSE {source:"nbnhhsh", confidence}]->(:Alias "恋与深空")`。
- **REQ-4** **WHEN** 无法区分 **THEN** SHALL 标 `ambiguous`，**不建边**，只记录候选。
- **REQ-5** **WHEN** API 不可达 **THEN** SHALL 用缓存/跳过，**不得阻断导入**；
  并 SHALL 提供 `ABBR_NBNHHHSH_ENABLED` 开关（**默认关**，只走本地词典）。
- **REQ-6** **WHEN** 检索 **THEN** SHALL 双向扩展：查全称附带缩写，查缩写反查全称；
  多义缩写只提示、不合并。

## 不变行为

- 不改变身份模型（047 的 `DISPLAYED_AS` 管人，本 spec 管**术语**）
- 不改变 `event_id`（缩写展开只影响实体/检索，不改卡内容）
- 不引入外部依赖到导入主链路（失败即降级）

## Design

### 1. 候选生成（离线缓存）

```
表 abbr_cache(term PK, candidates JSON, fetched_at)
  · 扫描语料 token：^[A-Za-z]{2,6}$ 且独立出现（前后非字母）
  · 只对频次 ≥2 的 token 请求 API（控成本）
```

### 2. 消歧打分

| 证据 | 权重 |
|---|---|
| 候选全称在本群出现次数 | 每次 +1 |
| 缩写与全称在同一话题窗口（±50 条）出现 | +2 |
| 候选全称命中已有 Entity/Alias 节点 | +2 |
| 候选全称含「暖暖/深空/叠纸」等域名词 | +1（可配） |

`top_score >= 2 * second_score` 且 `top_score >= 2` → 建边；否则 ambiguous。

### 3. 图与检索

```
(:Alias "lysk")-[:SAME_SENSE {source, confidence, ambiguous}]->(:Alias "恋与深空")
```

检索扩展放在召回层：query 命中别名 → 同时检索双向别名；`ambiguous=true` 时只并列展示。

### 4. 数据来源的坑（实测）

| token | nbnhhsh 返回 |
|---|---|
| lysk | 恋与深空 / 老姚烧烤 / 老用手抠 |
| mz | 母子 / 母猪 / 秒赞 / 名字 / 明侦 |
| dz | 盗贼 / 叠纸 / 地址 / 点赞 / 断杂 |
| nn | 倪妮 / 纳尼 / 嗯嗯 / 农农 / 男娜（**没有「暖暖」**） |

→ **必须允许本地补充词典**（`data/abbr_local.json`），nbnhhsh 只做候选。

## Plan

- [x] T1 `abbr.py`：nbnhhsh 客户端 + SQLite 缓存 + 失败降级
- [x] T2 语料 token 扫描与候选生成
- [x] T3 共现消歧打分 + 单测
- [x] T4 `SAME_SENSE` 写入与查询
- [x] T5 召回层双向扩展
- [x] T6 文档

## Test

- [ ] A1 `lysk` 在 Lo 群语料里被解析为「恋与深空」（高分领先）
- [ ] A2 `mz` 在同一语料里解析为「母猪」而非「母子/明侦」
- [ ] A3 `nn` 因本地词典（暖暖）才能命中；无词典时标 ambiguous
- [ ] A4 API 不可达 → 不报错、不阻断，读缓存/跳过
- [x] A5 多义缩写不建边，只记候选与 `ambiguous`
- [x] A6 全量 pytest 无回归

## Notes

### 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 041 指称层 | 复用 `Alias`/`Sense` 与 scope；本 spec 是缩写这一路信号 |
| 047 群名片 | 人名的标记 vs 术语的缩写，两者不混 |
| 036 切分 | 缩写展开在抽取/检索层，不改切分 |

### 诚实边界

- nbnhhsh 是社区词典，覆盖圈内热词，但**冷门/新词会缺**（如 `nn=暖暖`）
- 缩写歧义是本质问题，本 spec 只做「有把握才连」，宁可漏
- 外部 API 有可用性风险，缓存 + 降级是硬要求

### 2026-11-13 · T1–T4 落地（nbnhhsh 接入）

**已交付**

| 文件 | 内容 |
|---|---|
| `services/abbr.py` | nbnhhsh 客户端 + SQLite 缓存 + 本地词典 + token 清洗/扫描 + 共现打分 |
| `config/abbr_local.json` | 本地补充词典（`nn=暖暖` 等 nbnhhsh 未收录的词） |
| `harness/resolve_abbr.py` | 从 `raw_quote` 取会话原文 → 解析 → 写 `SAME_SENSE` 边 |
| `tests/test_abbr.py` | 8 条 |

**nbnhhsh 实测**（API 可达，Windows/WSL 均 200）

```
lysk → 恋与深空 / 老姚烧烤 / 老用手抠
mz   → 母子 / 母猪 / 秒赞 / 名字 / 明侦
dz   → 盗贼 / 叠纸 / 地址 / 点赞 / 断杂
nn   → 倪妮 / 纳尼 / 嗯嗯 / 农农 / 男娜    ← 没有「暖暖」
```

**Lo 群（876081394，892 条原文）解析结果**

```
可建边 5 条：
  (lysk) → (恋与深空)  0.6
  (nn)   → (暖暖)      0.2
  (gpa)  → (绩点)      0.5
  (hhhh)/(hhhhh) → 哈哈
其余 27 个 token 标 ambiguous（如 lo/pc/xx/xd），不建边
```

**踩的两个坑**

1. **文件后缀制造假 token**：`jpg` 出现 24 次，被解析成「图片」——
   因为占位符 `[图片] XXX.jpg` 里的后缀被当缩写。修法：语料先剥 URL/占位符/XML/路径/哈希。
2. **高频词碾压上下文**：`sm` 被解析成「什么」（出现 44 次）。
   修法：候选「出现过」只 +1，不再按次数累加；共现才 +2/次。
   改完后 `sm` 正确变成 ambiguous。

**未做**：T5 召回层双向扩展（查「恋与深空」自动带 `lysk`）。

### 2026-11-13 · 外网开关（用户要求）

**用户规则**：外网通的时候可用，不通就别用；让用户能配置。

**实现**

| 位置 | 内容 |
|---|---|
| `config.py` | `ABBR_NBNHHHSH_ENABLED: bool = False`（默认关） |
| `.env.example` | 同名配置 + 注释：关闭后仍用 `config/abbr_local.json` |
| `abbr.guess_abbr(enabled=...)` | `None` 读配置；`False` 完全不发请求（缓存仍可读） |
| `harness/resolve_abbr.py` | `--no-nbnhhsh` / `--enable-nbnhhsh` 临时覆盖 |

**行为**

```
开关关：不联网 → 只用本地词典 + 已有缓存 → 结果更少但确定
开关开：先查缓存 → 未命中才请求 nbnhhsh → 失败静默降级
```

**测试**：`tests/test_abbr_switch.py`（5 条）——关闭时不发请求、缓存可读、
开启时发请求、默认读配置、关网时本地词典仍能解析 `nn=暖暖`。

全量 **1502 passed**。

### 2026-11-13 · 词库调查：nbnhhsh 没有公开整包词库

用户问「原项目有没有当前词库，有的话扒下来」。调查结果：

```
仓库 itorr/nbnhhsh 只有 7 个文件：LICENSE / README / index.html /
document.css / nbnhhsh.user.js / nbnhhsh.meta.js / sponsor.png
油猴脚本里只用了一个端点：POST /api/nbnhhsh/guess
试探 /all /dict /list /words /hot /export /full 全部 404/无响应
作者其它仓库也没有词库
```

**结论**：词库只在服务端，**没有公开整包**。所以：

- 不"扒词库"，改为**查询缓存 + 本地词典**逐步积累（`data/abbr_cache.db` + `config/abbr_local.json`）
- README 增加「外部依赖」章节：标注 nbnhhsh（Apache-2.0）、可选开关、只发缩写字符串、
  以及「它没有整包词库」这一事实

### 2026-11-13 · T5 召回双向扩展（二次召回 + 合并）

**第一版是错的**：直接拿扩展串替换 query（`恋与深空` → `恋与深空 lysk`），
实测原 query 的 5 张全称卡**全部掉出结果**，只剩 1 张缩写卡 ——
语义偏移不是相加，是替换。

**第二版**（已落地）：原 query 照旧跑，扩展 query 再跑一次，按 `event_id` 合并取高分。

```python
# recall.recall 的 _finish 里
result = _merge_expanded(result)   # 只在该次查询命中别名时执行
```

**实测对比（qq-505456846，query = 恋与深空）**

```
ABBR_EXPAND_ENABLED=False → 5 条（全是全称卡）
ABBR_EXPAND_ENABLED=True  → 6 条（5 条全称 + 1 条 lysk 缩写卡）
```

**保护**

- 扩展召回预算不足 200ms 时跳过（扩展不是必需）
- 二次召回强制 `ABBR_EXPAND_ENABLED=False`，不会递归
- 失败只告警，不影响主结果
- `trace.abbr_expanded` 记录本次扩展了哪些词

**测试**：`tests/test_abbr_expand.py`（8 条，含 recall 集成与图降级）。
全量 **1510 passed**。
