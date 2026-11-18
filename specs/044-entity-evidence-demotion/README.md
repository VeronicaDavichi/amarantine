---
status: planned
created: 2026-11-07
priority: high
tags:
- entity
- evidence
- labeler
created_at: 2026-11-07T13:26:40.752953500Z
updated_at: 2026-11-07T13:26:40.752953500Z
---

# 实体证据与降级：先假设是 topic，按证据否决

> **Status**: planned · **Priority**: high · **Created**: 2026-11-07

## Overview

**要解决的问题**：拉丁词形该不该是 topic，现在靠**写入时猜**（静态停用词表），
而单个词条是不是「被谈论的主题」，单张卡根本看不出来。

**改成按证据判**：

```
① 写入：词形够就建 topic（先假设是）
② 累积：每见一次记一行证据（哪张卡、什么位置、有没有命名后缀、谁说的）
③ 定期：曝光量够 且 正向信号为 0 → 标 rejected，停止再建，软删已有边
④ 复活：之后出现命名上下文，或被查询命中 → 自动恢复
```

**实测依据**（037 上线后用 500 张真实 QQ 摘要跑一遍，新通道会新增 13 种 topic）：

| 词 | 出现卡数 | 位置 | 命名后缀 | 判断 |
|---|---|---|---|---|
| `Veronica` / `Davichi` | 27 / 27 | **全部在「X 说：」发言位** | 0 | 应否决（根因是导出语料丢了名册名） |
| `AI` / `GPU` | 2 / 1 | 陈述句中间，纯提及 | 0 | 应否决（通用词） |
| `Fm7` / `Cm7` | 1 / 1 | 音乐和弦 | 0 | 写入时按形态拒 |
| `IMF` / `LGBT` / `T80UD` / `NAS` / `AGI` / `ScalingLaw` | 各 1 | 提及 | 0 | **证据不足，不动** |

最后一行是核心约束：**证据不足 ≠ 证据相反**。只出现一次的词不能因为少就杀，
否则退化成「冷门词一律拒」，比静态停用词表更糟。

**为什么不在写入时一次判死**：单张卡看不到这个词在别的卡里怎么出现。
`IMF` 在这张卡里是提及，但它可能在十张卡里都是主题。只有跨卡累积才能分清
「顺嘴一提」和「反复讨论」。

## Design

### 证据表（SQLite）

```sql
CREATE TABLE entity_evidence (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- topic / person / ...
    mentions    INTEGER NOT NULL DEFAULT 0,     -- 出现次数
    cards       INTEGER NOT NULL DEFAULT 0,     -- 出现于多少张不同的卡
    suffix_hits INTEGER NOT NULL DEFAULT 0,     -- 与命名后缀共现（X接口/X系统）
    head_hits   INTEGER NOT NULL DEFAULT 0,     -- 出现于「X 说：」发言位
    queried     INTEGER NOT NULL DEFAULT 0,     -- 被召回查询命中
    status      TEXT NOT NULL DEFAULT 'active', -- active / rejected
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    reason      TEXT,
    PRIMARY KEY (namespace, name)
);
```

**为什么放 SQLite 而不是图属性**：图是派生层，`rebuild_graph` 一跑统计就没了；
热路径查 SQLite 也比查 Neo4j 便宜。抑制状态必须活在图的**外面**。

### 判定规则

```
rejected 当且仅当：
    cards >= DEMOTE_MIN_CARDS                    （曝光够，默认 5）
AND suffix_hits = 0                              （从未出现在命名上下文）
AND queried = 0                                  （从未被人问过）
AND head_hits >= cards * DEMOTE_HEAD_RATIO       （默认 0.8：基本只当发言人）

复活 当且仅当：
    suffix_hits > 0  或  queried > 0
```

`DEMOTE_MIN_CARDS` 是防误杀的核心旋钮：调到 0 就等于「冷门词一律杀」。

### 写入侧（与 037 的分工）

- 形态噪声**根本不进证据表**：媒体后缀、hex/UUID、音乐和弦在抽取时就拒
- 其余按词形建 topic，同时 upsert 一行证据（`mentions +1`，首次则建行）
- 建节点前查一次「是否 rejected」，命中即跳过

### 写图侧也必须过滤（关键）

`graph_outbox._split_entities(card)` 重放时用的是**卡上已存的 entities**，
不经过抽取器。抑制表只加在抽取层挡不住重放 —— 必须在写图那侧再过滤一次，
否则删了又长回来。

### 扫描

复用 033 的扫描族形态（`harness/meta_test_*.py`）：定期跑，输出
「本次降级 N 个 / 复活 M 个」报告，`--apply` 才落库。

## Plan

- [ ] **T1 证据表**：建表 + `upsert` / `query` 接口 + 老库自动建表
- [ ] **T2 写入侧接线**：抽到拉丁 topic 时记一行证据
- [ ] **T3 抑制过滤（抽取侧）**：建节点前查 rejected
- [ ] **T4 抑制过滤（写图侧）**：`graph_outbox` 写前过滤，堵住重放复活
- [ ] **T5 判定扫描**：`harness/entity_evidence_scan.py`，`--dry-run` 默认
- [ ] **T6 复活路径**：命名后缀共现或查询命中 → 置回 active
- [ ] **T7 管理接口**：`GET /admin/entities/evidence`（可查、可看 reason）
- [ ] **T8 测试与文档**：阈值边界、误杀防护、重放不复活、user-manual 补一节

## Test

| 验收 | 手段 |
|---|---|
| A1 曝光 5 张、零后缀、全发言位 → 判 rejected | 单测（固定数据） |
| A2 只出现 1 次 → **不动**（证据不足不杀） | 单测 |
| A3 rejected 后重新写入同一句 → 图里不新增该节点 | 真库 |
| A4 `pending_task` 重放 → 也不新增（写图侧过滤生效） | 单测 |
| A5 出现 `X接口` → 自动复活为 active | 单测 |
| A6 扫描输出「降级 N / 复活 M」，`--dry-run` 不改库 | 脚本 |

## Notes

### 非目标

| 不做 | 理由 |
|---|---|
| 用 LLM 判词义 | 规则层够用；LLM 已在摘要热路径，不该再加一次 |
| 图节点物理删除 | 沿用项目 tombstone 风格（`split_into` / `orphan_since`） |
| 跨 namespace 共享抑制 | 按 namespace 隔离：`AI` 在闲聊是噪声，在工作群可能是主题 |
| 自动清理证据表 | 一个词一行，体积可忽略，先留着 |

### 关联

- **037**：本 spec 消费它的词形通道；037 负责「建」，本 spec 负责「判」
- **033**：扫描器的工程形态直接复用
- **005 / 021**：软删与审计口径保持一致
- **043**：人名不该走 topic 通道，由 043 的账号准入解决，本 spec 兜底

### 风险

| 风险 | 缓解 |
|---|---|
| 阈值误杀冷门真词 | `DEMOTE_MIN_CARDS` 默认 5；被查询过即免疫 |
| 热路径多一次 SQLite 查 | 进程内缓存 rejected 集合，写路径只读缓存 |
| 抑制状态与图不同步 | 扫描只改 SQLite，图的边顺带软删，两者都可重跑 |
| 与 043 冲突（人名被当 topic 再判） | 043 落地后人名走 Person，本 spec 只兜剩余的 |
### 2026-11-18 · 可行性实测：降级会误杀真人，得先拆「人名碎片」

在 qq-505456846（4383 卡 / Entity 575 / Person 278）上量了一遍：

| 事实 | 数值 |
|---|---|
| `Entity.kind=topic` | 454 |
| 其中**是某账号显示名的一部分** | **141**（`Veronica ⊂ Veronica Davichi`、`瓦一 ⊂ 新机子瓦一兔子`、`员8 ⊂ 传讯员8号`） |
| 不匹配任何人名的 topic | 313 |
| 后者里曝光 ≥5（本 spec 的候选） | **11** |

11 个「候选」长这样：

```
Valhalla 18 / LYLCC 16 / Metalheart 11 / SuJiuEr9 11 / Golden_Fox 7
RVC 6 / DSH 6 / 排球少年 5 / 群里多次 5 / Skylark 5 / Fuga 5
```

**其中 9 个是群友昵称，2 个是真主题**（`FGO`、`排球少年`），
`群里多次` 是句子碎片。也就是说：

> 按本 spec 的规则（`cards>=5` + 无命名后缀 + `queried=0` + 发言位占比高）
> 跑下去，会把 `Valhalla`/`LYLCC` 这些**真人**判成 rejected 并软删边。

根因不在「判据松」，而在**上游把带空格/emoji 的显示名切成了多个 Entity**：
`Veronica Davichi` 被切成 `Veronica` + `Davichi`，`新机子瓦一兔子` 切成 `瓦一` 等。

**所以调整实施顺序**（用户 2026-11-18 讨论后）：

| 任务 | 现在做 | 理由 |
|---|---|---|
| T1 证据表 | ✅ 可做 | 只记不改，先有数据 |
| T5 扫描（dry-run） | ✅ 可做 | 先出报告，不落库 |
| T7 管理接口 | ✅ 可做 | 让它可查，人才能判断 |
| T3/T4 抑制过滤 | ⏸ 暂停 | 会把真人杀成 rejected |
| T6 复活路径 | ⏸ 暂停 | 没有抑制就不需要复活 |

**前置问题**：把「人名碎片」并回 `Person`（141 个 topic 属于这一类），
这是 047/043 的延伸，收益远大于降级 —— 降级只是让噪声闭嘴，
并回 Person 才能让「谁说了什么」查得准。
### 2026-11-18 · 用户洞见：连续串里不要切词（谐音/拼词）

**原话**：「有些你没法理解的短句名字，要不然就别拆了。`新机子瓦一兔子`
这个明显是日语的（真相只有一个）的谐音。」

`新机子瓦一兔子` = 「真実は一つ」（shinjitsu wa hitotsu，真相只有一个）。
`瓦一` 只是音节切片，不是另一个叫法。同理：

| 名字 | 被误切的碎片 | 真相 |
|---|---|---|
| 新机子瓦一兔子 | 瓦一 | 日语谐音，整串是一个名字 |
| 传讯员8号 | 员8 | 机器账号，整串是名字 |
| 扑街少年MIKA | MIKA | 整串是一个名字 |
| 办法总比问题多♔ | 问题多 | 整句是短语 |

**规则**：碎片两侧必须有一侧是**真正的分隔符**（空格/标点/括号/emoji），
才允许当成另一个叫法。

实现在 `memory.services.name_marker.has_name_boundary()`（可测试），
`harness/make_labeling_data.py --fragments` 用它筛候选。

**效果**：碎片候选从 64 个降到 **17 个**，剩下的都是真有分隔的
（`ALICE + OLIVIA`、`小特（DeepSeek）`、`Heat？`）。

**已知边界**：短语碎片（`问题多 ⊂ 办法总比问题多♔`）有分隔符，这条规则会放行；
它属于「整句是短语」而不是「连续串」，只能靠人工复核页拦。
### 2026-11-18 · 改成自动化（用户：不该我主动合并）

**用户原话**：「我们要做的应该是自动化的处理这些问题，所以不应该你主动的合并。」

所以撤掉了人工复核页（`docs/labeling.html` 的「人名碎片」组已移除），
改成一条可复现的判定链：

| 情形（frag ⊂ alias，且 alias 只属于一个账号） | 动作 | 依据 |
|---|---|---|
| 多出的部分是**纯装饰**（emoji/标点/数字） | **自动合并**到该账号的 `Person` | 去掉装饰就是完整名字 |
| 多出的部分是**实义词** | **自动抑制**（不建 Entity） | 碎片是分词产物，不是独立实体 |

脚本：`harness/auto_entity_repair.py`（默认 dry-run，`--apply` 才落库改图）。

**已落地（qq-505456846）**

```
人名碎片 17 个 → 自动合并 3（NotFound-404- / Heat？ / Ester☆）
               自动抑制 14（ALICE / OLIVIA / DeepSeek / 问题多 / NFC …）
结果：Entity 575 → 558，Person 278 → 281
```

**抑制表**（`entity_suppress`，SQLite）

```sql
namespace, name, reason, decided_by, status, detail, created_at, updated_at
```

- 与 016 的 `alias_reject` 分开：那张表是「别名裁决」，这张是「实体准入」
- 按 namespace 隔离（`AI` 在闲聊是噪声、在工作群可能是主题）
- 进程内缓存，`add()` 后失效

**写图侧过滤（T4）**：`graph_outbox` 写 `MENTIONS` 前按抑制表剔除 ——
重放用的是卡上已存的 entities，只挡抽取侧挡不住复活。

**与原文设计的差异（诚实记录）**：原 T1 要一张带 `mentions/cards/suffix_hits/
head_hits/queried` 的证据表，用来**按证据判**是否降级。现在先只做了
「名字碎片」这一类确定情形的抑制；证据计数与扫描（T2/T5）没做 ——
因为实测暴露的主要问题是**分词把人名切碎**，不是「通用词曝光过多」。
泛化到「通用词降级」需要 `head_hits` 信号，那要先做位置统计，另开。
